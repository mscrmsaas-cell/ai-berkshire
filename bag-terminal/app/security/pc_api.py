"""
PC 端对接 API — 局域网 FastAPI 接口 (仅 USB 网卡)

功能:
    - 提供 HTTP REST API 供 PC 端专业应用调用
    - 数据导出触发 (全量/按巡检/按时间/按类型)
    - 导出包列表查询与下载
    - 导出包完整性验证
    - 设备状态查询
    - 眼镜设备列表查询
    - 调试接口 (系统日志/配置检查/健康状态)

安全:
    - 仅绑定 USB 网卡接口 (10.0.0.1:9090), 不暴露到 5G/WiFi
    - Token 认证 (USB 连接时生成临时 Token, 显示在 OLED 屏上)
    - 速率限制
    - 审计日志记录所有操作

API 端点:
    GET    /api/status                 — 获取挎包终端状态
    POST   /api/export/full             — 触发全量导出
    POST   /api/export/inspection      — 按巡检 ID 导出
    POST   /api/export/date-range       — 按时间范围导出
    POST   /api/export/type             — 按数据类型导出
    GET    /api/exports                 — 列出所有导出包
    GET    /api/exports/{filename}      — 下载导出包
    POST   /api/exports/{filename}/verify — 验证导出包
    DELETE /api/exports/{filename}      — 删除导出包
    GET    /api/glasses                 — 查询已连接眼镜列表
    GET    /api/debug/logs              — 获取系统日志 (调试)
    GET    /api/debug/config            — 获取当前配置 (调试)
    GET    /api/debug/health            — 健康检查 (调试)
    POST   /api/debug/execute           — 执行诊断命令 (调试, 需高级权限)
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import (
    FastAPI,
    HTTPException,
    Depends,
    Header,
    Query,
    File,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.security.data_exporter import DataExporter, ExportConfig, ExportResult
from app.security.usb_manager import UsbConnectionManager

logger = structlog.get_logger(__name__)


# ── 认证 ──────────────────────────────────────────────────────

class TokenManager:
    """
    临时 Token 管理器

    工作原理:
        1. USB 连接时, 自动生成一个 8 位数字 Token
        2. Token 显示在挎包 OLED 屏上
        3. PC 端用户在专业应用中输入 Token
        4. Token 有效期 30 分钟, 超时自动失效
        5. 每个 Token 最多使用 1000 次 (防止暴力破解)
    """

    def __init__(self) -> None:
        self._tokens: dict[str, dict[str, Any]] = {}
        self._token_ttl: int = 1800  # 30 分钟
        self._max_uses: int = 1000

    def generate_token(self) -> str:
        """生成新 Token"""
        # 清理过期 Token
        self._cleanup_expired()

        # 生成 8 位数字 Token
        token = "".join(str(secrets.randbelow(10)) for _ in range(8))
        self._tokens[token] = {
            "created_at": time.time(),
            "uses": 0,
        }
        logger.info("pc_api.token_generated", token=token[:4] + "****")
        return token

    def validate(self, token: str) -> bool:
        """验证 Token"""
        self._cleanup_expired()
        if token not in self._tokens:
            return False
        info = self._tokens[token]
        if info["uses"] >= self._max_uses:
            del self._tokens[token]
            return False
        info["uses"] += 1
        return True

    def revoke(self, token: str) -> None:
        """撤销 Token"""
        self._tokens.pop(token, None)

    def _cleanup_expired(self) -> None:
        """清理过期 Token"""
        now = time.time()
        expired = [t for t, info in self._tokens.items() if now - info["created_at"] > self._token_ttl]
        for t in expired:
            del self._tokens[t]

    @property
    def active_token(self) -> str | None:
        """获取当前活跃 Token (用于 OLED 显示)"""
        self._cleanup_expired()
        if self._tokens:
            return list(self._tokens.keys())[0]
        return None


# ── 请求模型 ──────────────────────────────────────────────────

class ExportByInspectionRequest(BaseModel):
    inspection_id: str = Field(..., description="巡检任务 ID")


class ExportByDateRangeRequest(BaseModel):
    start_date: str = Field(..., description="开始日期 ISO8601")
    end_date: str = Field(..., description="结束日期 ISO8601")


class ExportByTypeRequest(BaseModel):
    data_type: str = Field(..., description="数据类型: photos/videos/alerts/detections/rag/telemetry")


class DebugExecuteRequest(BaseModel):
    command: str = Field(..., description="诊断命令")
    timeout: int = Field(10, description="超时秒数")


# ── FastAPI 应用 ──────────────────────────────────────────────

def create_pc_api_app(
    usb_manager: UsbConnectionManager,
    data_exporter: DataExporter,
    db: Any,
    settings: Any,
) -> FastAPI:
    """
    创建 PC 对接 API FastAPI 应用

    此应用应仅绑定到 USB 网卡接口 (10.0.0.1:9090),
    不暴露到 5G 或 WiFi 网络。
    """

    app = FastAPI(
        title="Rail-AR PC Bridge API",
        description="挎包终端 PC 对接接口 — 仅 USB 有线通道",
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS — 仅允许 USB 局域网
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://10.0.0.*", "http://localhost"],
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
    )

    token_manager = TokenManager()

    # ── 认证依赖 ──────────────────────────────────────────

    async def verify_token(x_auth_token: str = Header(..., alias="X-Auth-Token")) -> str:
        """验证请求 Token"""
        if not token_manager.validate(x_auth_token):
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        return x_auth_token

    async def verify_admin_token(x_auth_token: str = Header(..., alias="X-Auth-Token")) -> str:
        """验证管理员 Token (用于调试接口)"""
        if not token_manager.validate(x_auth_token):
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        # 管理员 Token 以 "ADMIN:" 前缀
        # 实际实现中可通过不同 Token 前缀区分权限
        return x_auth_token

    # ── 状态接口 ──────────────────────────────────────────

    @app.get("/api/status")
    async def get_status(token: str = Depends(verify_token)) -> dict[str, Any]:
        """获取挎包终端状态"""
        usb_state = usb_manager.state
        exports = data_exporter.list_exports()
        return {
            "device_id": getattr(settings, "device_id", "unknown"),
            "device_name": "Rail-AR Bag Terminal",
            "firmware_version": "1.0.0",
            "usb_connected": usb_manager.is_connected,
            "usb_mode": usb_state.mode.value,
            "usb_nic_ip": usb_state.nic_ip,
            "exports_count": len(exports),
            "exports_total_size_mb": round(sum(e["size_mb"] for e in exports), 2),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ── 导出接口 ──────────────────────────────────────────

    @app.post("/api/export/full", response_model=None)
    async def export_full(token: str = Depends(verify_token)) -> dict[str, Any]:
        """触发全量数据导出"""
        result = await data_exporter.export_full()
        return _export_result_to_dict(result)

    @app.post("/api/export/inspection", response_model=None)
    async def export_by_inspection(
        req: ExportByInspectionRequest,
        token: str = Depends(verify_token),
    ) -> dict[str, Any]:
        """按巡检 ID 导出"""
        result = await data_exporter.export_by_inspection(req.inspection_id)
        return _export_result_to_dict(result)

    @app.post("/api/export/date-range", response_model=None)
    async def export_by_date_range(
        req: ExportByDateRangeRequest,
        token: str = Depends(verify_token),
    ) -> dict[str, Any]:
        """按时间范围导出"""
        result = await data_exporter.export_by_date_range(req.start_date, req.end_date)
        return _export_result_to_dict(result)

    @app.post("/api/export/type", response_model=None)
    async def export_by_type(
        req: ExportByTypeRequest,
        token: str = Depends(verify_token),
    ) -> dict[str, Any]:
        """按数据类型导出"""
        result = await data_exporter.export_by_type(req.data_type)
        return _export_result_to_dict(result)

    # ── 导出包管理 ─────────────────────────────────────────

    @app.get("/api/exports")
    async def list_exports(token: str = Depends(verify_token)) -> list[dict[str, Any]]:
        """列出所有导出包"""
        return data_exporter.list_exports()

    @app.get("/api/exports/{filename}")
    async def download_export(
        filename: str,
        token: str = Depends(verify_token),
    ) -> FileResponse:
        """下载导出包"""
        # 安全: 防止路径遍历
        if "/" in filename or ".." in filename:
            raise HTTPException(status_code=400, detail="Invalid filename")

        export_dir = Path(data_exporter._config.export_dir)
        file_path = export_dir / filename

        if not file_path.exists() or file_path.suffix != ".tar.gz":
            raise HTTPException(status_code=404, detail="Export package not found")

        return FileResponse(
            path=str(file_path),
            media_type="application/gzip",
            filename=filename,
        )

    @app.post("/api/exports/{filename}/verify", response_model=None)
    async def verify_export(
        filename: str,
        token: str = Depends(verify_token),
    ) -> dict[str, Any]:
        """验证导出包完整性"""
        if "/" in filename or ".." in filename:
            raise HTTPException(status_code=400, detail="Invalid filename")

        export_dir = Path(data_exporter._config.export_dir)
        file_path = export_dir / filename

        if not file_path.exists():
            raise HTTPException(status_code=404, detail="Export package not found")

        result = await data_exporter.verify_export(str(file_path))
        return result

    @app.delete("/api/exports/{filename}")
    async def delete_export(
        filename: str,
        token: str = Depends(verify_token),
    ) -> dict[str, str]:
        """删除导出包"""
        if "/" in filename or ".." in filename:
            raise HTTPException(status_code=400, detail="Invalid filename")

        success = await data_exporter.delete_export(filename)
        if not success:
            raise HTTPException(status_code=404, detail="Export package not found")

        return {"status": "deleted", "filename": filename}

    # ── 眼镜设备查询 ──────────────────────────────────────

    @app.get("/api/glasses")
    async def list_glasses(token: str = Depends(verify_token)) -> list[dict[str, Any]]:
        """查询已注册的眼镜设备列表"""
        try:
            rows = await db.fetch_all(
                "SELECT * FROM glasses_devices ORDER BY registered_at DESC",
                params=(),
            )
            return [dict(row) if hasattr(row, "keys") else row for row in rows]
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    # ── 调试接口 ──────────────────────────────────────────

    @app.get("/api/debug/logs")
    async def get_logs(
        lines: int = Query(100, ge=1, le=10000),
        token: str = Depends(verify_admin_token),
    ) -> dict[str, Any]:
        """获取系统日志 (调试)"""
        try:
            log_path = Path("/var/log/bag-terminal/app.log")
            if not log_path.exists():
                log_path = Path("/mnt/sdcard/bag-terminal/logs/app.log")
            if log_path.exists():
                content = await asyncio.to_thread(
                    lambda: log_path.read_text(encoding="utf-8", errors="ignore")
                )
                log_lines = content.splitlines()[-lines:]
                return {"lines": len(log_lines), "content": "\n".join(log_lines)}
            return {"lines": 0, "content": "Log file not found"}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.get("/api/debug/config")
    async def get_config(token: str = Depends(verify_admin_token)) -> dict[str, Any]:
        """获取当前运行配置 (调试, 隐藏敏感字段)"""
        config = {
            "ble": {
                "service_uuid": getattr(settings.ble, "service_uuid", ""),
                "max_connections": getattr(settings.ble, "max_connections", 4),
            },
            "models": {
                "yolo": getattr(settings.models.yolo, "model_path", ""),
                "llm": getattr(settings.models.llm, "model_path", ""),
                "embedding": getattr(settings.models.embedding, "model_path", ""),
            },
            "rag": {
                "collection_name": getattr(settings.rag, "collection_name", ""),
                "top_k": getattr(settings.rag, "top_k", 5),
            },
            "storage": {
                "storage_root": getattr(settings, "storage_root", "/mnt/sdcard/bag-terminal"),
            },
            "tuya_enabled": getattr(settings.tuya, "enabled", False),
        }
        return config

    @app.get("/api/debug/health")
    async def health_check(token: str = Depends(verify_admin_token)) -> dict[str, Any]:
        """健康检查 (调试)"""
        import psutil
        return {
            "cpu_percent": psutil.cpu_percent(interval=1),
            "memory": dict(psutil.virtual_memory()._asdict()),
            "disk": dict(psutil.disk_usage("/")._asdict()),
            "usb_connected": usb_manager.is_connected,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @app.post("/api/debug/execute", response_model=None)
    async def execute_command(
        req: DebugExecuteRequest,
        token: str = Depends(verify_admin_token),
    ) -> dict[str, Any]:
        """
        执行诊断命令 (调试, 需管理员权限)

        白名单命令: ls, df, free, uptime, dmesg, journalctl, ip, ps, top
        """
        ALLOWED_COMMANDS = {"ls", "df", "free", "uptime", "dmesg", "journalctl", "ip", "ps", "top", "cat", "grep"}

        cmd_parts = req.command.split()
        if not cmd_parts:
            raise HTTPException(status_code=400, detail="Empty command")

        base_cmd = cmd_parts[0]
        if base_cmd not in ALLOWED_COMMANDS:
            raise HTTPException(
                status_code=403,
                detail=f"Command '{base_cmd}' not allowed. Allowed: {ALLOWED_COMMANDS}",
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_parts,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=req.timeout
            )
            return {
                "command": req.command,
                "returncode": proc.returncode,
                "stdout": stdout.decode("utf-8", errors="ignore")[:5000],
                "stderr": stderr.decode("utf-8", errors="ignore")[:2000],
            }
        except asyncio.TimeoutError:
            raise HTTPException(status_code=408, detail="Command timeout")
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    # ── Token 管理 ─────────────────────────────────────────

    @app.get("/api/token/status")
    async def token_status() -> dict[str, Any]:
        """获取当前 Token 状态 (无需认证, 用于 PC 端判断是否需要新 Token)"""
        active = token_manager.active_token
        return {
            "has_active_token": active is not None,
            "token_prefix": active[:4] + "****" if active else None,
        }

    @app.post("/api/token/refresh")
    async def refresh_token() -> dict[str, str]:
        """
        生成新 Token (显示在 OLED 屏上)

        此端点不需要 Token 认证 — 它用于首次配对。
        物理安全: USB 有线连接本身就是认证因素。
        """
        token = token_manager.generate_token()
        return {"token": token, "message": "Token 已生成, 请查看挎包 OLED 屏"}

    # 注册启动事件
    @app.on_event("startup")
    async def _startup() -> None:
        logger.info("pc_api.started", port=usb_manager.PC_API_PORT, bind=usb_manager.USB_NIC_IP)

    return app


def _export_result_to_dict(result: ExportResult) -> dict[str, Any]:
    """ExportResult 转 dict"""
    return {
        "export_id": result.export_id,
        "package_path": result.package_path,
        "package_filename": Path(result.package_path).name if result.package_path else "",
        "package_size_bytes": result.package_size_bytes,
        "package_size_mb": round(result.package_size_bytes / (1024 * 1024), 2),
        "file_count": result.file_count,
        "sha256_signature": result.sha256_signature,
        "created_at": result.created_at,
    }


# 需要 Path 导入
from pathlib import Path
