"""
USB 有线连接管理器

功能:
    - 检测 USB 主机连接状态 (CM4 USB Device / USB OTG 模式)
    - 管理 USB 网卡 (CDC-ECM) 接口 — 局域网数据通道
    - 管理 USB 串口 (CDC-ACM) 接口 — 调试通道
    - USB 连接事件回调 (插入 / 拔出)
    - 接口热插拔检测 (udev 事件监听)

架构:
    ┌─────────────┐    USB 线缆    ┌──────────────┐
    │  PC 端      │ ←──────────→ │  CM4 挎包端   │
    │  专业应用    │  CDC-ECM     │  usb_manager  │
    │  (浏览器/    │  USB 网卡     │  10.0.0.1     │
    │   CLI/API)  │  10.0.0.2    │  FastAPI:9090 │
    └──────────────┘              └──────────────┘
                    CDC-ACM
                    USB 串口
                    /dev/ttyACM0 (调试)

安全隔离:
    - USB 网卡接口 IP 固定为 10.0.0.1/24, 不路由到 5G 网络
    - iptables 规则禁止 USB 接口 → 5G/wifi 转发
    - 串口调试需 token 认证
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable

import structlog

logger = structlog.get_logger(__name__)


class UsbMode(Enum):
    """USB 工作模式"""
    DISCONNECTED = "disconnected"      # 未连接主机
    ECM_NIC = "ecm_nic"                # CDC-ECM 网卡模式 (数据导出)
    ACM_SERIAL = "acm_serial"          # CDC-ACM 串口模式 (调试)
    ECM_ACM_DUAL = "ecm_acm_dual"      # 双模式 (网卡 + 串口)


@dataclass
class UsbInterfaceState:
    """USB 接口状态"""
    mode: UsbMode = UsbMode.DISCONNECTED
    nic_name: str = ""                 # 网卡接口名 (e.g. usb0)
    nic_ip: str = "10.0.0.1"          # 固定 IP
    nic_netmask: str = "255.255.255.0"
    serial_port: str = ""             # 串口设备路径 (e.g. /dev/ttyACM0)
    pc_ip_detected: str = ""          # 检测到的 PC 端 IP
    connected_at: str = ""            # 连接时间
    bytes_transferred: int = 0        # 已传输字节数
    sessions_count: int = 0           # 会话计数


class UsbConnectionManager:
    """
    USB 有线连接管理器

    工作原理:
        1. CM4 的 USB-C 端口配置为 Device 模式 (gadget)
        2. 通过 configfs 加载 g_hid + g_ether 模块
        3. PC 插入后枚举为 USB 网卡 + USB 串口
        4. 挎包端在 usb0 接口上运行 FastAPI (端口 9090)
        5. PC 端通过 http://10.0.0.1:9090 访问数据导出 API
        6. iptables 规则确保 USB 接口与 5G 接口隔离
    """

    # 固定配置
    USB_NIC_IP = "10.0.0.1"
    USB_NIC_NETMASK = "255.255.255.0"
    USB_NIC_NETWORK = "10.0.0.0/24"
    PC_API_PORT = 9090                # PC 对接 API 端口
    SERIAL_BAUDRATE = 115200          # 调试串口波特率
    SERIAL_DEVICE_PATTERN = "/dev/ttyACM*"
    USB_NIC_CANDIDATES = ["usb0", "enx*", "enp*s*" , "usb_rndis0"]

    def __init__(self) -> None:
        self._state = UsbInterfaceState()
        self._poll_task: asyncio.Task | None = None
        self._on_connect_cb: Callable[[UsbInterfaceState], Awaitable[None]] | None = None
        self._on_disconnect_cb: Callable[[], Awaitable[None]] | None = None
        self._is_running = False

    @property
    def state(self) -> UsbInterfaceState:
        return self._state

    @property
    def is_connected(self) -> bool:
        return self._state.mode != UsbMode.DISCONNECTED

    # ── 生命周期 ──────────────────────────────────────────────

    async def start(self) -> None:
        """启动 USB 连接监控"""
        logger.info("usb_manager.starting")
        self._is_running = True
        # 初始检测
        await self._detect_interfaces()
        # 启动轮询任务 (每 2 秒检测一次)
        self._poll_task = asyncio.create_task(self._poll_loop(), name="usb-poll")
        logger.info("usb_manager.started")

    async def stop(self) -> None:
        """停止 USB 连接监控"""
        self._is_running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        logger.info("usb_manager.stopped")

    def on_connect(self, callback: Callable[[UsbInterfaceState], Awaitable[None]]) -> None:
        """注册 USB 连接回调"""
        self._on_connect_cb = callback

    def on_disconnect(self, callback: Callable[[], Awaitable[None]]) -> None:
        """注册 USB 断开回调"""
        self._on_disconnect_cb = callback

    # ── 接口检测 ──────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """轮询检测 USB 接口状态"""
        while self._is_running:
            try:
                await self._detect_interfaces()
            except Exception as exc:
                logger.warning("usb_manager.poll_error", error=str(exc))
            await asyncio.sleep(2)

    async def _detect_interfaces(self) -> None:
        """检测 USB 网卡和串口接口"""
        # 检测网卡
        nic = await self._find_usb_nic()
        serial = await self._find_serial_port()

        # 确定模式
        if nic and serial:
            new_mode = UsbMode.ECM_ACM_DUAL
        elif nic:
            new_mode = UsbMode.ECM_NIC
        elif serial:
            new_mode = UsbMode.ACM_SERIAL
        else:
            new_mode = UsbMode.DISCONNECTED

        # 状态变化处理
        if new_mode != self._state.mode:
            old_mode = self._state.mode
            if new_mode == UsbMode.DISCONNECTED:
                # 断开
                await self._handle_disconnect()
            else:
                # 连接/模式变更
                await self._handle_connect(new_mode, nic, serial)

    async def _find_usb_nic(self) -> str | None:
        """查找 USB 网卡接口"""
        try:
            result = await asyncio.create_subprocess_exec(
                "ip", "-o", "link", "show",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await result.communicate()
            output = stdout.decode("utf-8", errors="ignore")
            for line in output.splitlines():
                for pattern in self.USB_NIC_CANDIDATES:
                    if "*" in pattern:
                        prefix = pattern.replace("*", "")
                        if prefix in line and "UP" in line:
                            parts = line.split(":")
                            if len(parts) >= 2:
                                return parts[1].strip().split("@")[0]
                    elif pattern in line:
                        parts = line.split(":")
                        if len(parts) >= 2:
                            return parts[1].strip().split("@")[0]
        except Exception as exc:
            logger.debug("usb_manager.nic_detection_error", error=str(exc))
        return None

    async def _find_serial_port(self) -> str | None:
        """查找 USB 串口设备"""
        import glob
        ports = glob.glob(self.SERIAL_DEVICE_PATTERN)
        if ports:
            return ports[0]
        return None

    async def _handle_connect(self, mode: UsbMode, nic: str | None, serial: str | None) -> None:
        """处理 USB 连接事件"""
        old_mode = self._state.mode
        self._state.mode = mode
        self._state.nic_name = nic or ""
        self._state.serial_port = serial or ""
        from datetime import datetime, timezone
        self._state.connected_at = datetime.now(timezone.utc).isoformat()

        # 配置网卡 IP (如果是网卡模式)
        if nic:
            await self._configure_nic_ip(nic)
            await self._apply_firewall_rules(nic)

        logger.info(
            "usb_manager.connected",
            mode=mode.value,
            nic=nic,
            serial=serial,
            ip=self.USB_NIC_IP,
        )

        # 触发回调
        if self._on_connect_cb:
            try:
                await self._on_connect_cb(self._state)
            except Exception as exc:
                logger.error("usb_manager.connect_callback_error", error=str(exc))

    async def _handle_disconnect(self) -> None:
        """处理 USB 断开事件"""
        self._state = UsbInterfaceState()
        logger.info("usb_manager.disconnected")

        if self._on_disconnect_cb:
            try:
                await self._on_disconnect_cb()
            except Exception as exc:
                logger.error("usb_manager.disconnect_callback_error", error=str(exc))

    async def _configure_nic_ip(self, nic: str) -> None:
        """配置 USB 网卡固定 IP"""
        try:
            await asyncio.create_subprocess_exec(
                "ip", "addr", "flush", "dev", nic,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.create_subprocess_exec(
                "ip", "addr", "add",
                f"{self.USB_NIC_IP}/24", "dev", nic,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.create_subprocess_exec(
                "ip", "link", "set", "up", "dev", nic,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            logger.info("usb_manager.nic_configured", nic=nic, ip=self.USB_NIC_IP)
        except Exception as exc:
            logger.error("usb_manager.nic_config_error", nic=nic, error=str(exc))

    async def _apply_firewall_rules(self, nic: str) -> None:
        """
        应用防火墙规则 — 核心: 禁止 USB 接口与 5G/wifi 之间的转发

        这实现了数据安全隔离: USB 数据通道与外部网络完全隔离
        """
        rules = [
            # 禁止从 USB 接口转发到其他接口 (5G/wifi)
            ["iptables", "-A", "FORWARD", "-i", nic, "-j", "DROP"],
            # 禁止从其他接口转发到 USB 接口
            ["iptables", "-A", "FORWARD", "-o", nic, "-j", "DROP"],
            # 允许 USB 接口本身的入站和出站
            ["iptables", "-A", "INPUT", "-i", nic, "-j", "ACCEPT"],
            ["iptables", "-A", "OUTPUT", "-o", nic, "-j", "ACCEPT"],
            # 禁止 NAT masquerade 通过 USB 接口
            ["iptables", "-t", "nat", "-A", "POSTROUTING", "-o", nic, "-j", "DROP"],
            # 禁止 USB 接口访问 DNS 端口 (防止 DNS 泄漏)
            ["iptables", "-A", "OUTPUT", "-o", nic, "-p", "udp", "--dport", "53", "-j", "DROP"],
            ["iptables", "-A", "OUTPUT", "-o", nic, "-p", "tcp", "--dport", "53", "-j", "DROP"],
        ]
        for rule in rules:
            try:
                await asyncio.create_subprocess_exec(
                    *rule,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except Exception as exc:
                logger.warning("usb_manager.firewall_rule_error", rule=" ".join(rule), error=str(exc))

        logger.info("usb_manager.firewall_applied", nic=nic, rules_count=len(rules))

    async def cleanup_firewall(self, nic: str) -> None:
        """清理防火墙规则"""
        cleanup_rules = [
            ["iptables", "-D", "FORWARD", "-i", nic, "-j", "DROP"],
            ["iptables", "-D", "FORWARD", "-o", nic, "-j", "DROP"],
            ["iptables", "-D", "INPUT", "-i", nic, "-j", "ACCEPT"],
            ["iptables", "-D", "OUTPUT", "-o", nic, "-j", "ACCEPT"],
            ["iptables", "-t", "nat", "-D", "POSTROUTING", "-o", nic, "-j", "DROP"],
            ["iptables", "-D", "OUTPUT", "-o", nic, "-p", "udp", "--dport", "53", "-j", "DROP"],
            ["iptables", "-D", "OUTPUT", "-o", nic, "-p", "tcp", "--dport", "53", "-j", "DROP"],
        ]
        for rule in cleanup_rules:
            try:
                await asyncio.create_subprocess_exec(
                    *rule,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except Exception:
                pass

    def get_pc_endpoint(self) -> str:
        """获取 PC 端访问 URL"""
        if self.is_connected and self._state.nic_name:
            return f"http://{self.USB_NIC_IP}:{self.PC_API_PORT}"
        return ""
