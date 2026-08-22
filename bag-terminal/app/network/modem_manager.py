"""5G 模块管理器 — Quectel RM500Q-GL。

通过 USB 串口与 Quectel RM500Q-GL 5G 模组通信：
- AT 命令封装（信号查询、拨号、APN 配置）
- 网络注册状态监控
- 信号强度解析
- PPP/ECM 拨号连接管理

Quectel RM500Q-GL 关键 AT 命令：
  AT+CSQ      — 信号强度
  AT+CEREG    — EPS 网络注册状态
  AT+CGDCONT  — APN 配置
  AT$QCRMCALL — PPP/ECM 拨号
  AT+QNWINFO  — 当前网络信息（运营商/制式）
  AT+QNWPREF  — 网络制式偏好（5G/4G/3G）
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# 默认串口设备路径
DEFAULT_SERIAL_PORT = "/dev/ttyUSB2"
DEFAULT_BAUDRATE = 115200


class NetworkType(Enum):
    """网络制式。"""

    UNKNOWN = "unknown"
    N5G = "5G"
    LTE = "4G_LTE"
    WCDMA = "3G"
    GSM = "2G"


class NetworkStatus(Enum):
    """网络注册状态。"""

    NOT_REGISTERED = "not_registered"
    REGISTERED_HOME = "registered_home"
    SEARCHING = "searching"
    DENIED = "denied"
    UNKNOWN = "unknown"
    REGISTERED_ROAMING = "registered_roaming"


@dataclass
class ModemSignalInfo:
    """模块信号信息。"""

    rssi: int = 0          # 接收信号强度 dBm
    rsrp: int = 0          # 参考信号功率 dBm
    rsrq: int = 0          # 参考信号质量 dB
    sinr: int = 0          # 信噪比 dB
    snr: int = 0           # 信噪比
    level: int = 0         # 信号格数 0-5
    ber: int = 0           # 误码率

    @property
    def is_usable(self) -> bool:
        """信号是否可用（RSRP > -110 dBm）。"""
        return self.rsrp > -110

    def to_dict(self) -> dict[str, Any]:
        return {
            "rssi": self.rssi,
            "rsrp": self.rsrp,
            "rsrq": self.rsrq,
            "sinr": self.sinr,
            "level": self.level,
        }


@dataclass
class ModemConfig:
    """模块配置。"""

    serial_port: str = DEFAULT_SERIAL_PORT
    baudrate: int = DEFAULT_BAUDRATE
    apn: str = "cmnet"              # APN（移动：cmnet / 联通：3gnet / 电信：ctnet）
    apn_user: str = ""
    apn_password: str = ""
    network_pref: str = "5G"        # 5G / LTE_AUTO / 4G_3G
    dial_mode: str = "ecm"          # ecm / ppp（ECM 模式更简单稳定）
    at_timeout: float = 10.0        # AT 命令超时
    retry_count: int = 3            # 命令重试次数
    retry_delay: float = 2.0


class ModemManager:
    """Quectel RM500Q-GL 5G 模组管理器。

    使用示例::

        manager = ModemManager(config)
        await manager.connect()
        signal = await manager.get_signal()
        print(f"RSRP={signal.rsrp} dBm")
        await manager.dial()
        await manager.disconnect()
    """

    def __init__(self, config: ModemConfig | None = None) -> None:
        self._config = config or ModemConfig()
        self._serial: Any = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False
        self._dialed = False

    # ── 连接管理 ──────────────────────────────────────────────────

    async def connect(self) -> bool:
        """打开串口连接。"""
        if self._connected:
            return True
        try:
            # 使用 asyncio 子进程调用 stty 配置串口
            await self._configure_serial()
            self._reader, self._writer = await asyncio.open_connection(
                self._config.serial_port,
            )
            self._connected = True
            logger.info(
                "modem_serial_connected",
                port=self._config.serial_port,
                baudrate=self._config.baudrate,
            )
            # 初始化 AT
            ok = await self._send_at("AT", "OK")
            if not ok:
                logger.warning("modem_at_init_failed")
                return False
            # 关闭回显
            await self._send_at("ATE0", "OK")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("modem_connect_error", error=str(exc))
            return False

    async def disconnect(self) -> None:
        """关闭串口连接。"""
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        self._reader = None
        self._writer = None
        self._connected = False
        logger.info("modem_serial_disconnected")

    async def _configure_serial(self) -> None:
        """使用 stty 配置串口参数。"""
        port = self._config.serial_port
        proc = await asyncio.create_subprocess_exec(
            "stty", "-F", port,
            str(self._config.baudrate),
            "raw", "-echo",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    # ── AT 命令 ───────────────────────────────────────────────────

    async def _send_at(
        self,
        command: str,
        expected: str = "OK",
        timeout: float | None = None,
    ) -> str | None:
        """发送 AT 命令并等待响应。

        :param command: AT 命令（不含换行）
        :param expected: 期望响应前缀（如 OK / ERROR）
        :param timeout: 超时秒数
        :return: 完整响应文本或 None
        """
        if not self._connected or self._reader is None or self._writer is None:
            logger.error("modem_not_connected")
            return None
        timeout = timeout or self._config.at_timeout
        full_cmd = f"{command}\r\n"
        try:
            self._writer.write(full_cmd.encode("ascii"))
            await self._writer.drain()
        except Exception as exc:  # noqa: BLE001
            logger.error("modem_write_error", command=command, error=str(exc))
            return None

        # 读取响应直到遇到 expected 或 ERROR 或超时
        response_lines: list[str] = []
        try:
            while True:
                line_bytes = await asyncio.wait_for(
                    self._reader.readline(), timeout=timeout
                )
                line = line_bytes.decode("ascii", errors="replace").strip()
                if not line:
                    continue
                response_lines.append(line)
                if expected in line:
                    break
                if "ERROR" in line:
                    logger.warning("at_command_error", command=command, line=line)
                    return None
        except asyncio.TimeoutError:
            logger.warning("at_command_timeout", command=command)
            return None

        return "\n".join(response_lines)

    async def _send_at_retry(
        self,
        command: str,
        expected: str = "OK",
        retries: int | None = None,
    ) -> str | None:
        """带重试的 AT 命令。"""
        retries = retries if retries is not None else self._config.retry_count
        for attempt in range(retries):
            result = await self._send_at(command, expected)
            if result is not None:
                return result
            if attempt < retries - 1:
                await asyncio.sleep(self._config.retry_delay)
        return None

    # ── 信号查询 ──────────────────────────────────────────────────

    async def get_signal(self) -> ModemSignalInfo:
        """查询信号强度。

        使用 AT+CSQ（基础）和 AT+QCSQ（详细 5G/LTE 信息）。
        """
        info = ModemSignalInfo()
        # 基础信号 AT+CSQ: +CSQ: <rssi>,<ber>
        resp = await self._send_at("AT+CSQ", "OK")
        if resp:
            match = re.search(r"\+CSQ:\s*(\d+),(\d+)", resp)
            if match:
                rssi_idx = int(match.group(1))
                ber = int(match.group(2))
                info.ber = ber
                # CSQ 值 0-31 → RSSI dBm 映射
                if rssi_idx == 99:
                    info.rssi = -999  # 未知
                else:
                    info.rssi = -113 + 2 * rssi_idx
                info.level = self._rssi_to_level(info.rssi)

        # 详细 5G/LTE 信号 AT+QCSQ
        resp = await self._send_at("AT+QCSQ", "OK")
        if resp:
            # +QCSQ: "LTE",<rsrp>,<rsrq>,<rssi>,<sinr>
            # +QCSQ: "NR5G",<nr_rsrp>,<nr_rsrq>,<nr_sinr>,<nr_rssi>
            lte_match = re.search(r'"LTE",\s*(-?\d+),\s*(-?\d+),\s*(-?\d+),\s*(-?\d+)', resp)
            nr5g_match = re.search(
                r'"NR5G",\s*(-?\d+),\s*(-?\d+),\s*(-?\d+),\s*(-?\d+)', resp
            )
            if nr5g_match:
                # 5G 信号优先
                info.rsrp = int(nr5g_match.group(1))
                info.rsrq = int(nr5g_match.group(2))
                info.sinr = int(nr5g_match.group(3))
                info.rssi = int(nr5g_match.group(4))
            elif lte_match:
                info.rsrp = int(lte_match.group(1))
                info.rsrq = int(lte_match.group(2))
                info.rssi = int(lte_match.group(3))
                info.sinr = int(lte_match.group(4))

            info.level = self._rsrp_to_level(info.rsrp)

        logger.debug("modem_signal", **info.to_dict())
        return info

    @staticmethod
    def _rssi_to_level(rssi: int) -> int:
        """RSSI dBm → 信号格数 0-5。"""
        if rssi >= -70:
            return 5
        if rssi >= -85:
            return 4
        if rssi >= -100:
            return 3
        if rssi >= -110:
            return 2
        if rssi > -999:
            return 1
        return 0

    @staticmethod
    def _rsrp_to_level(rsrp: int) -> int:
        """RSRP dBm → 信号格数 0-5。"""
        if rsrp >= -80:
            return 5
        if rsrp >= -90:
            return 4
        if rsrp >= -100:
            return 3
        if rsrp >= -110:
            return 2
        if rsrp > -140:
            return 1
        return 0

    # ── 网络注册状态 ──────────────────────────────────────────────

    async def get_network_status(self) -> NetworkStatus:
        """查询 EPS 网络注册状态。

        AT+CEREG? → +CEREG: <n>,<stat>[,<tac>,<ci>,<act>]
        """
        resp = await self._send_at("AT+CEREG?", "OK")
        if not resp:
            return NetworkStatus.UNKNOWN
        match = re.search(r"\+CEREG:\s*\d+,(\d+)", resp)
        if not match:
            return NetworkStatus.UNKNOWN
        stat = int(match.group(1))
        status_map = {
            0: NetworkStatus.NOT_REGISTERED,
            1: NetworkStatus.REGISTERED_HOME,
            2: NetworkStatus.SEARCHING,
            3: NetworkStatus.DENIED,
            4: NetworkStatus.UNKNOWN,
            5: NetworkStatus.REGISTERED_ROAMING,
            6: NetworkStatus.REGISTERED_HOME,  # SMS only
            7: NetworkStatus.REGISTERED_HOME,  # SMS only (CSFB)
            8: NetworkStatus.REGISTERED_HOME,  # EPS+5G
            9: NetworkStatus.REGISTERED_HOME,  # NR5G
        }
        return status_map.get(stat, NetworkStatus.UNKNOWN)

    # ── 网络信息 ──────────────────────────────────────────────────

    async def get_network_info(self) -> dict[str, Any]:
        """查询当前网络信息（运营商/制式）。"""
        resp = await self._send_at("AT+QNWINFO", "OK")
        if not resp:
            return {}
        match = re.search(r'\+QNWINFO:\s*"([^"]*)",\s*"([^"]*)",\s*(\d+),\s*(\d+)', resp)
        if match:
            return {
                "access_technology": match.group(1),
                "operator": match.group(2),
                "band": int(match.group(3)),
                "channel": int(match.group(4)),
            }
        return {}

    async def get_iccid(self) -> str:
        """获取 SIM 卡 ICCID。"""
        resp = await self._send_at("AT+QCCID", "OK")
        if not resp:
            return ""
        match = re.search(r"\+QCCID:\s*(\d+)", resp)
        return match.group(1) if match else ""

    async def get_imei(self) -> str:
        """获取模组 IMEI。"""
        resp = await self._send_at("ATI", "OK")
        if not resp:
            return ""
        match = re.search(r"IMEI:\s*(\d+)", resp)
        return match.group(1) if match else ""

    # ── APN 配置 ──────────────────────────────────────────────────

    async def configure_apn(self, apn: str, user: str = "", password: str = "") -> bool:
        """配置 APN。

        :param apn: APN 名称
        :param user: 用户名（多数不需要）
        :param password: 密码（多数不需要）
        """
        # AT+CGDCONT=1,"IP","<apn>"
        cmd = f'AT+CGDCONT=1,"IP","{apn}"'
        resp = await self._send_at_retry(cmd, "OK")
        if resp is not None:
            logger.info("apn_configured", apn=apn)
            return True
        logger.error("apn_config_failed", apn=apn)
        return False

    # ── 拨号 ──────────────────────────────────────────────────────

    async def dial(self) -> bool:
        """执行拨号连接（ECM 模式）。

        ECM 模式下，拨号后 USB 网卡自动获得 IP。
        使用 AT$QCRMCALL=1,1 启动 ECM 数据连接。
        """
        if self._dialed:
            return True
        # 确保 APN 已配置
        if self._config.apn:
            await self.configure_apn(self._config.apn, self._config.apn_user, self._config.apn_password)

        # ECM 拨号
        resp = await self._send_at_retry("AT$QCRMCALL=1,1", "OK", retries=5)
        if resp is not None:
            self._dialed = True
            logger.info("modem_dial_success", mode=self._config.dial_mode)
            # 等待接口激活
            await asyncio.sleep(3)
            return True
        logger.error("modem_dial_failed")
        return False

    async def hangup(self) -> bool:
        """断开拨号连接。"""
        if not self._dialed:
            return True
        resp = await self._send_at("AT$QCRMCALL=0,1", "OK")
        if resp is not None:
            self._dialed = False
            logger.info("modem_hangup_success")
            return True
        return False

    # ── 网络制式偏好 ──────────────────────────────────────────────

    async def set_network_preference(self, pref: str = "5G") -> bool:
        """设置网络制式偏好。"""
        mode_map = {
            "5G": '"NR5G:NR5G"',
            "LTE_AUTO": '"LTE:LTE"',
            "4G_3G": '"LTE:WCDMA"',
            "auto": '"NR5G:LTE:WCDMA"',
        }
        mode = mode_map.get(pref, mode_map["auto"])
        cmd = f"AT+QNWPREFCFG=\"mode_pref\",{mode}"
        resp = await self._send_at_retry(cmd, "OK")
        if resp is not None:
            logger.info("network_pref_set", pref=pref)
            return True
        return False

    # ── 重启模块 ──────────────────────────────────────────────────

    async def reboot_modem(self) -> bool:
        """重启 5G 模块。"""
        resp = await self._send_at("AT+CFUN=1,1", "OK", timeout=30.0)
        if resp is not None:
            logger.info("modem_rebooting")
            await asyncio.sleep(15)  # 等待重启完成
            await self.connect()
            return True
        return False

    # ── 状态查询 ──────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_dialed(self) -> bool:
        return self._dialed

    async def get_modem_info(self) -> dict[str, Any]:
        """获取模块完整信息。"""
        signal = await self.get_signal()
        status = await self.get_network_status()
        network_info = await self.get_network_info()
        iccid = await self.get_iccid()
        return {
            "signal": signal.to_dict(),
            "network_status": status.value,
            "network_info": network_info,
            "iccid": iccid,
            "dialed": self._dialed,
            "connected": self._connected,
        }
