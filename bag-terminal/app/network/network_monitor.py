"""网络监控器。

负责网络连通性检测与自动恢复：
- 周期性 ping 检测（HTTP 探测 SaaS / 公共 DNS）
- 5G 与 WiFi 之间的自动切换
- 断网后自动重连（指数退避）
- 向上层组件广播网络状态变更

切换策略：
  1. 默认使用 5G（有线 USB 拨号）
  2. 5G 信号差/断网时，尝试切换到 WiFi（wlan0）
  3. WiFi 也不可用时，尝试重拨 5G
  4. 恢复后通知 CacheManager 触发批量同步
"""
from __future__ import annotations

import asyncio
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

import structlog

from app.network.modem_manager import ModemManager, ModemConfig, ModemSignalInfo

logger = structlog.get_logger(__name__)


class NetworkType(Enum):
    """网络类型。"""

    MODEM_5G = "5g_modem"
    WIFI = "wifi"
    NONE = "none"


class NetworkState(Enum):
    """网络状态。"""

    ONLINE = "online"
    OFFLINE = "offline"
    RECONNECTING = "reconnecting"


@dataclass
class NetworkMonitorConfig:
    """网络监控配置。"""

    check_interval: float = 30.0        # 检测间隔（秒）
    reconnect_interval: float = 10.0   # 重连初始间隔
    reconnect_max_interval: float = 300.0  # 最大重连间隔
    reconnect_backoff: float = 2.0      # 指数退避因子
    # 探测目标
    probe_urls: list[str] = None        # type: ignore
    probe_timeout: float = 5.0          # HTTP 探测超时
    # 5G 信号阈值
    min_rsrp: int = -110                # 低于此值认为 5G 不可用
    # WiFi
    wifi_interface: str = "wlan0"
    modem_interface: str = "usb0"
    # 连续失败次数阈值
    max_consecutive_failures: int = 3

    def __post_init__(self) -> None:
        if self.probe_urls is None:
            self.probe_urls = [
                "http://connectivitycheck.gstatic.com/generate_204",
                "http://www.baidu.com",
            ]


# 回调类型
NetworkStatusCallback = Callable[[bool, NetworkType], None]


class NetworkMonitor:
    """网络连通性监控与自动恢复管理器。

    使用示例::

        monitor = NetworkMonitor(modem_manager, config)
        monitor.on_status_change = handle_status_change
        await monitor.start()
        # ...
        await monitor.stop()
    """

    def __init__(
        self,
        modem_manager: ModemManager | None = None,
        config: NetworkMonitorConfig | None = None,
    ) -> None:
        self._modem = modem_manager
        self._config = config or NetworkMonitorConfig()
        self._state = NetworkState.OFFLINE
        self._active_network = NetworkType.NONE
        self._running = False
        self._monitor_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._consecutive_failures = 0
        self._reconnect_delay = self._config.reconnect_interval

        # 外部回调
        self.on_status_change: NetworkStatusCallback | None = None
        self.on_reconnect_success: Callable[[], None] | None = None

    # ── 生命周期 ──────────────────────────────────────────────────

    async def start(self) -> None:
        """启动监控循环。"""
        if self._running:
            return
        self._running = True
        # 初始检测
        await self._check_and_reconnect()
        self._monitor_task = asyncio.create_task(
            self._monitor_loop(), name="network-monitor"
        )
        logger.info("network_monitor_started")

    async def stop(self) -> None:
        """停止监控。"""
        self._running = False
        for task in (self._monitor_task, self._reconnect_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        logger.info("network_monitor_stopped")

    # ── 监控循环 ──────────────────────────────────────────────────

    async def _monitor_loop(self) -> None:
        """定期检测网络连通性。"""
        while self._running:
            try:
                online = await self.check_connectivity()
                if online:
                    self._consecutive_failures = 0
                    self._reconnect_delay = self._config.reconnect_interval
                    if self._state != NetworkState.ONLINE:
                        await self._set_state(NetworkState.ONLINE)
                else:
                    self._consecutive_failures += 1
                    logger.warning(
                        "network_unreachable",
                        failures=self._consecutive_failures,
                    )
                    if (
                        self._consecutive_failures >= self._config.max_consecutive_failures
                        and self._state != NetworkState.RECONNECTING
                    ):
                        await self._set_state(NetworkState.RECONNECTING)
                        self._schedule_reconnect()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.error("monitor_loop_error", error=str(exc))
            await asyncio.sleep(self._config.check_interval)

    # ── 连通性检测 ────────────────────────────────────────────────

    async def check_connectivity(self) -> bool:
        """检测网络是否连通。

        依次尝试 HTTP 探测目标 URL，任一成功即认为在线。
        """
        for url in self._config.probe_urls:
            if await self._http_probe(url):
                return True
        # HTTP 探测失败，尝试 DNS 解析
        if await self._dns_probe():
            return True
        return False

    async def _http_probe(self, url: str) -> bool:
        """HTTP 探测（使用 curl 避免 httpx 在网络层超时复杂度）。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "curl", "-sSL", "-o", "/dev/null", "-w", "%{http_code}",
                "--connect-timeout", str(self._config.probe_timeout),
                "--max-time", str(self._config.probe_timeout + 5),
                url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            status_code = stdout.decode().strip()
            return status_code in ("200", "204", "301", "302")
        except Exception as exc:  # noqa: BLE001
            logger.debug("http_probe_failed", url=url, error=str(exc))
            return False

    async def _dns_probe(self) -> bool:
        """DNS 解析探测。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "nslookup", "www.baidu.com",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=self._config.probe_timeout
            )
            output = stdout.decode(errors="replace")
            return "Address" in output or "address" in output
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            return False

    # ── 自动重连与切换 ────────────────────────────────────────────

    async def _check_and_reconnect(self) -> bool:
        """初始检测并自动重连。"""
        # 1. 先检查 5G 模块
        if self._modem and self._modem.is_connected:
            signal = await self._modem.get_signal()
            if signal.is_usable:
                if not self._modem.is_dialed:
                    if await self._modem.dial():
                        if await self.check_connectivity():
                            self._active_network = NetworkType.MODEM_5G
                            await self._set_state(NetworkState.ONLINE)
                            return True
                    else:
                        logger.warning("modem_dial_failed_on_startup")
                else:
                    if await self.check_connectivity():
                        self._active_network = NetworkType.MODEM_5G
                        await self._set_state(NetworkState.ONLINE)
                        return True

        # 2. 尝试 WiFi
        if await self._check_wifi():
            self._active_network = NetworkType.WIFI
            await self._set_state(NetworkState.ONLINE)
            return True

        await self._set_state(NetworkState.OFFLINE)
        return False

    def _schedule_reconnect(self) -> None:
        """在后台安排重连（指数退避）。"""
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(
            self._reconnect_loop(), name="network-reconnect"
        )

    async def _reconnect_loop(self) -> None:
        """指数退避重连循环。"""
        while self._running and self._state != NetworkState.ONLINE:
            logger.info(
                "network_reconnect_attempt",
                delay=self._reconnect_delay,
                attempt=self._consecutive_failures,
            )
            await asyncio.sleep(self._reconnect_delay)

            # 策略 1: 重拨 5G
            if self._modem and self._modem.is_connected:
                signal = await self._modem.get_signal()
                if signal.is_usable:
                    if not self._modem.is_dialed:
                        await self._modem.dial()
                    if await self.check_connectivity():
                        self._active_network = NetworkType.MODEM_5G
                        await self._set_state(NetworkState.ONLINE)
                        self._reconnect_delay = self._config.reconnect_interval
                        if self.on_reconnect_success:
                            self.on_reconnect_success()
                        logger.info("network_reconnected_via_modem")
                        return

            # 策略 2: 切换到 WiFi
            if await self._check_wifi():
                if await self.check_connectivity():
                    self._active_network = NetworkType.WIFI
                    await self._set_state(NetworkState.ONLINE)
                    self._reconnect_delay = self._config.reconnect_interval
                    if self.on_reconnect_success:
                        self.on_reconnect_success()
                    logger.info("network_reconnected_via_wifi")
                    return

            # 策略 3: 重启模块
            if self._consecutive_failures >= self._config.max_consecutive_failures * 2:
                logger.warning("modem_reboot_triggered")
                if self._modem:
                    await self._modem.reboot_modem()

            # 指数退避
            self._reconnect_delay = min(
                self._reconnect_delay * self._config.reconnect_backoff,
                self._config.reconnect_max_interval,
            )

    async def _check_wifi(self) -> bool:
        """检查 WiFi 接口是否有 IP。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ip", "addr", "show", self._config.wifi_interface,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            output = stdout.decode(errors="replace")
            # 检查是否有 IP 地址分配
            return "inet " in output
        except Exception as exc:  # noqa: BLE001
            logger.debug("wifi_check_error", error=str(exc))
            return False

    async def switch_to_wifi(self) -> bool:
        """手动切换到 WiFi 网络。"""
        # 断开 5G 拨号
        if self._modem and self._modem.is_dialed:
            await self._modem.hangup()
        # 激活 WiFi
        try:
            proc = await asyncio.create_subprocess_exec(
                "dhclient", self._config.wifi_interface,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if await self._check_wifi():
                self._active_network = NetworkType.WIFI
                logger.info("switched_to_wifi")
                return True
        except Exception as exc:  # noqa: BLE001
            logger.error("wifi_switch_error", error=str(exc))
        return False

    async def switch_to_modem(self) -> bool:
        """手动切换到 5G 网络。"""
        if self._modem:
            ok = await self._modem.dial()
            if ok:
                self._active_network = NetworkType.MODEM_5G
                logger.info("switched_to_modem")
                return True
        return False

    # ── 状态管理 ──────────────────────────────────────────────────

    async def _set_state(self, state: NetworkState) -> None:
        old_state = self._state
        self._state = state
        if state != old_state:
            is_online = state == NetworkState.ONLINE
            logger.info(
                "network_state_changed",
                old=old_state.value,
                new=state.value,
                network=self._active_network.value,
            )
            if self.on_status_change:
                try:
                    self.on_status_change(is_online, self._active_network)
                except Exception as exc:  # noqa: BLE001
                    logger.error("status_callback_error", error=str(exc))

    # ── 状态查询 ──────────────────────────────────────────────────

    @property
    def state(self) -> NetworkState:
        return self._state

    @property
    def active_network(self) -> NetworkType:
        return self._active_network

    def is_online(self) -> bool:
        return self._state == NetworkState.ONLINE

    async def get_network_summary(self) -> dict[str, Any]:
        """获取网络状态摘要。"""
        summary: dict[str, Any] = {
            "state": self._state.value,
            "active_network": self._active_network.value,
            "consecutive_failures": self._consecutive_failures,
        }
        if self._modem and self._modem.is_connected:
            signal = await self._modem.get_signal()
            summary["modem_signal"] = signal.to_dict()
            summary["modem_dialed"] = self._modem.is_dialed
        summary["wifi_available"] = await self._check_wifi()
        return summary
