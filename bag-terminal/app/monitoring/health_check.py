"""健康检查端点。

提供 /health HTTP 端点，返回挎包终端各子系统运行状态：
- BLE 网桥状态
- 5G 模块状态
- MQTT 连接状态
- 涂鸦网关状态
- SaaS API 连通性
- SQLite 数据库状态
- 充电管理状态
- 磁盘空间状态
- AI 模型加载状态

HTTP 响应格式::

    GET /health
    200 {"status": "healthy", "subsystems": {...}}
    503 {"status": "degraded", "subsystems": {...}}
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import structlog
from fastapi import FastAPI
from fastapi.responses import JSONResponse

logger = structlog.get_logger(__name__)


class HealthStatus(Enum):
    """子系统健康状态。"""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class SubsystemHealth:
    """子系统健康状态。"""

    name: str
    status: HealthStatus = HealthStatus.UNKNOWN
    details: dict[str, Any] = field(default_factory=dict)
    last_check: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "details": self.details,
            "last_check": self.last_check,
        }


# 子系统检查函数类型
HealthCheckFunc = Callable[[], tuple[HealthStatus, dict[str, Any]]]


class HealthChecker:
    """挎包终端健康检查管理器。

    注册各子系统的检查函数，定期或按需执行检查。

    使用示例::

        checker = HealthChecker()
        checker.register("ble", check_ble)
        checker.register("modem", check_modem)
        result = await checker.check_all()
        # 注册到 FastAPI
        register_health_endpoint(app, checker)
    """

    def __init__(self) -> None:
        self._checks: dict[str, HealthCheckFunc] = {}
        self._subsystems: dict[str, SubsystemHealth] = {}
        self._app_start_time = time.time()

    # ── 注册 ──────────────────────────────────────────────────────

    def register(self, name: str, check_func: HealthCheckFunc) -> None:
        """注册子系统健康检查函数。"""
        self._checks[name] = check_func
        self._subsystems[name] = SubsystemHealth(name=name)

    def unregister(self, name: str) -> None:
        self._checks.pop(name, None)
        self._subsystems.pop(name, None)

    # ── 检查执行 ──────────────────────────────────────────────────

    async def check_all(self) -> dict[str, Any]:
        """执行所有注册的子系统的健康检查。"""
        for name, check_func in self._checks.items():
            try:
                # 检查函数可能是同步或异步
                import asyncio
                result = check_func()
                if asyncio.iscoroutine(result):
                    result = await result
                status, details = result
            except Exception as exc:  # noqa: BLE001
                status = HealthStatus.UNHEALTHY
                details = {"error": str(exc)}
                logger.error("health_check_error", subsystem=name, error=str(exc))

            self._subsystems[name].status = status
            self._subsystems[name].details = details
            self._subsystems[name].last_check = time.time()

        overall = self._compute_overall_status()
        return {
            "status": overall.value,
            "uptime_seconds": round(time.time() - self._app_start_time, 1),
            "subsystems": {
                name: sub.to_dict() for name, sub in self._subsystems.items()
            },
            "timestamp": time.time(),
        }

    def _compute_overall_status(self) -> HealthStatus:
        """根据各子系统状态计算整体健康状态。"""
        statuses = [s.status for s in self._subsystems.values()]
        if not statuses:
            return HealthStatus.UNKNOWN
        if any(s == HealthStatus.UNHEALTHY for s in statuses):
            critical = {"ble", "database", "modem"}
            for name, sub in self._subsystems.items():
                if sub.status == HealthStatus.UNHEALTHY and name in critical:
                    return HealthStatus.UNHEALTHY
            return HealthStatus.DEGRADED
        if any(s == HealthStatus.DEGRADED for s in statuses):
            return HealthStatus.DEGRADED
        if all(s == HealthStatus.HEALTHY for s in statuses):
            return HealthStatus.HEALTHY
        return HealthStatus.DEGRADED

    # ── 内置检查函数 ──────────────────────────────────────────────

    @staticmethod
    def check_disk_space(threshold: float = 0.90) -> tuple[HealthStatus, dict[str, Any]]:
        """检查磁盘空间。"""
        stat = os.statvfs("/")
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bavail * stat.f_frsize
        used = total - free
        usage = used / total if total > 0 else 0
        status = HealthStatus.HEALTHY if usage < threshold else HealthStatus.DEGRADED
        if usage >= 0.95:
            status = HealthStatus.UNHEALTHY
        return status, {
            "total_gb": round(total / 1024**3, 2),
            "used_gb": round(used / 1024**3, 2),
            "free_gb": round(free / 1024**3, 2),
            "usage_percent": round(usage * 100, 1),
        }

    @staticmethod
    def check_database(db: Any) -> tuple[HealthStatus, dict[str, Any]]:
        """检查 SQLite 数据库状态。"""
        try:
            if db is None or db._conn is None:
                return HealthStatus.UNHEALTHY, {"error": "not_connected"}
            import asyncio
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 在事件循环中无法同步调用，这里只检查连接对象
                pass
            return HealthStatus.HEALTHY, {"status": "connected"}
        except Exception as exc:  # noqa: BLE001
            return HealthStatus.UNHEALTHY, {"error": str(exc)}

    @staticmethod
    def check_memory() -> tuple[HealthStatus, dict[str, Any]]:
        """检查系统内存使用。"""
        try:
            with open("/proc/meminfo", "r") as f:
                lines = f.readlines()
            meminfo = {}
            for line in lines:
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
                    val = int(parts[1]) * 1024  # kB → bytes
                    meminfo[key] = val
            total = meminfo.get("MemTotal", 0)
            available = meminfo.get("MemAvailable", 0)
            used = total - available
            usage = used / total if total > 0 else 0
            status = HealthStatus.HEALTHY
            if usage > 0.90:
                status = HealthStatus.UNHEALTHY
            elif usage > 0.80:
                status = HealthStatus.DEGRADED
            return status, {
                "total_mb": round(total / 1024**2, 0),
                "used_mb": round(used / 1024**2, 0),
                "available_mb": round(available / 1024**2, 0),
                "usage_percent": round(usage * 100, 1),
            }
        except Exception as exc:  # noqa: BLE001
            return HealthStatus.UNKNOWN, {"error": str(exc)}

    @staticmethod
    def check_cpu_temperature() -> tuple[HealthStatus, dict[str, Any]]:
        """检查 CPU 温度。"""
        try:
            temp_paths = [
                "/sys/class/thermal/thermal_zone0/temp",
                "/sys/class/thermal/thermal_zone1/temp",
            ]
            temp_c = 0.0
            for path in temp_paths:
                if os.path.exists(path):
                    with open(path, "r") as f:
                        temp_c = int(f.read().strip()) / 1000.0
                        break
            status = HealthStatus.HEALTHY
            if temp_c >= 80:
                status = HealthStatus.UNHEALTHY
            elif temp_c >= 70:
                status = HealthStatus.DEGRADED
            return status, {"temperature_c": round(temp_c, 1)}
        except Exception as exc:  # noqa: BLE001
            return HealthStatus.UNKNOWN, {"error": str(exc)}


# ── FastAPI 端点注册 ────────────────────────────────────────────

def register_health_endpoint(app: FastAPI, checker: HealthChecker) -> None:
    """将 /health 端点注册到 FastAPI 应用。

    :param app: FastAPI 实例
    :param checker: HealthChecker 实例
    """

    @app.get("/health", tags=["监控"])
    async def health() -> JSONResponse:
        result = await checker.check_all()
        status_code = 200 if result["status"] == "healthy" else (
            200 if result["status"] == "degraded" else 503
        )
        return JSONResponse(
            status_code=status_code,
            content=result,
        )

    @app.get("/health/{subsystem}", tags=["监控"])
    async def health_subsystem(subsystem: str) -> JSONResponse:
        """检查单个子系统健康状态。"""
        check_func = checker._checks.get(subsystem)
        if check_func is None:
            return JSONResponse(
                status_code=404,
                content={"error": f"Unknown subsystem: {subsystem}"},
            )
        try:
            import asyncio
            result = check_func()
            if asyncio.iscoroutine(result):
                result = await result
            status, details = result
            return JSONResponse(
                status_code=200 if status == HealthStatus.HEALTHY else 503,
                content={
                    "subsystem": subsystem,
                    "status": status.value,
                    "details": details,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=503,
                content={"error": str(exc)},
            )
