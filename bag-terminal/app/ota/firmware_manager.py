"""眼镜固件 OTA 升级管理器。

负责 ESP32-S3 眼镜固件的空中升级完整流程：

    下载固件 → BLE NUS 分块传输 → 眼镜写入 → 分区切换 → 重启 → 验证

OTA 流程详解：
  1. 从 SaaS / MinIO 下载固件二进制文件
  2. SHA-256 校验固件完整性
  3. 通过 BLE NUS 协议向眼镜发送 OTA 通知
  4. 眼镜进入 OTA 模式，准备好接收数据
  5. 固件按 512 字节分块通过 BLE NUS 传输
  6. 每块传输后等待眼镜 ACK 确认
  7. 全部传输完成后通知眼镜切换分区
  8. 眼镜重启进入新分区
  9. 眼镜上报新版本号，验证升级成功

BLE NUS OTA 消息类型（见 message_types.py）：
  0x70 OTA_NOTIFY  — 通知眼镜开始 OTA（含固件大小/版本/SHA256）
  0x71 OTA_REQUEST — 眼镜请求下一块数据
  0x72 OTA_DATA    — 下发固件数据块
  0x73 OTA_STATUS  — 眼镜上报写入状态/完成
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import struct
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import structlog

logger = structlog.get_logger(__name__)

# BLE NUS OTA 消息类型（与 message_types.py 对齐）
MSG_OTA_NOTIFY = 0x70
MSG_OTA_REQUEST = 0x71
MSG_OTA_DATA = 0x72
MSG_OTA_STATUS = 0x73
MSG_ACK = 0x0F

# 分块大小（BLE MTU 协商后通常 247 字节，减去协议头约 512 字节可用）
CHUNK_SIZE = 512
# 每块超时等待（秒）
CHUNK_TIMEOUT = 10.0
# 最大重试
MAX_CHUNK_RETRIES = 3
# 下载临时目录
DEFAULT_DOWNLOAD_DIR = "/tmp/firmware"


class OtaState(Enum):
    """OTA 升级状态机。"""

    IDLE = "idle"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    NOTIFYING = "notifying"
    TRANSFERRING = "transferring"
    SWITCHING = "switching"
    REBOOTING = "rebooting"
    VERIFYING_VERSION = "verifying_version"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class OtaProgress:
    """OTA 升级进度。"""

    state: OtaState = OtaState.IDLE
    total_bytes: int = 0
    transferred_bytes: int = 0
    current_chunk: int = 0
    total_chunks: int = 0
    retry_count: int = 0
    error: str = ""
    started_at: float = field(default_factory=time.time)
    firmware_version: str = ""

    @property
    def progress_percent(self) -> float:
        if self.total_bytes == 0:
            return 0.0
        return round(self.transferred_bytes / self.total_bytes * 100, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "progress_percent": self.progress_percent,
            "transferred_bytes": self.transferred_bytes,
            "total_bytes": self.total_bytes,
            "current_chunk": self.current_chunk,
            "total_chunks": self.total_chunks,
            "firmware_version": self.firmware_version,
            "elapsed_seconds": round(time.time() - self.started_at, 1),
            "error": self.error,
        }


@dataclass
class OtaConfig:
    """OTA 配置。"""

    chunk_size: int = CHUNK_SIZE
    chunk_timeout: float = CHUNK_TIMEOUT
    max_chunk_retries: int = MAX_CHUNK_RETRIES
    download_dir: str = DEFAULT_DOWNLOAD_DIR
    # 分区表：ota_0(2.5MB) + ota_1(2.5MB)
    max_firmware_size: int = 2 * 1024 * 1024  # 2.5MB - OTA 分区大小
    verify_timeout: float = 120.0    # 重启后验证超时（秒）
    inter_chunk_delay: float = 0.01  # 块间延迟（秒），避免 BLE 拥塞


class FirmwareManager:
    """眼镜固件 OTA 升级管理器。

    :param ble_bridge: BLE 网桥实例，提供 send_to_device / register_handler
    :param config: OTA 配置

    使用示例::

        fm = FirmwareManager(ble_bridge, config)
        ok = await fm.start_upgrade(
            device_address="AA:BB:CC:DD:EE:FF",
            firmware_url="https://minio.../glasses_v1.2.0.bin",
            target_version="1.2.0",
            expected_sha256="abc123...",
        )
    """

    def __init__(
        self,
        ble_bridge: Any,  # BleBridge instance
        config: OtaConfig | None = None,
    ) -> None:
        self._ble = ble_bridge
        self._config = config or OtaConfig()
        self._state = OtaState.IDLE
        self._progress = OtaProgress()
        self._cancel = asyncio.Event()

        # 外部回调
        self.on_progress: Callable[[OtaProgress], None] | None = None
        self.on_state_change: Callable[[OtaState], None] | None = None

        # 眼镜 OTA 响应缓存
        self._ota_response: asyncio.Queue = asyncio.Queue()

    # ── 状态管理 ──────────────────────────────────────────────────

    @property
    def state(self) -> OtaState:
        return self._state

    @property
    def progress(self) -> OtaProgress:
        return self._progress

    def _set_state(self, state: OtaState, error: str = "") -> None:
        self._state = state
        self._progress.state = state
        if error:
            self._progress.error = error
        logger.info("ota_state_change", state=state.value, error=error)
        if self.on_state_change:
            self.on_state_change(state)
        if self.on_progress:
            self.on_progress(self._progress)

    def _update_progress(self, **kwargs: Any) -> None:
        for key, val in kwargs.items():
            setattr(self._progress, key, val)
        if self.on_progress:
            self.on_progress(self._progress)

    # ── 主流程 ──────────────────────────────────────────────────

    async def start_upgrade(
        self,
        device_address: str,
        firmware_url: str,
        target_version: str,
        expected_sha256: str | None = None,
    ) -> bool:
        """执行完整 OTA 升级流程。

        :param device_address: 眼镜 BLE 地址
        :param firmware_url: 固件下载 URL
        :param target_version: 目标版本号
        :param expected_sha256: 预期 SHA-256 校验值
        :return: 是否升级成功
        """
        self._cancel.clear()
        self._progress = OtaProgress(firmware_version=target_version)
        logger.info(
            "ota_start",
            device=device_address,
            target_version=target_version,
            url=firmware_url,
        )

        try:
            # Step 1: 下载固件
            firmware_path = await self._download_firmware(firmware_url)
            if self._cancel.is_set():
                self._set_state(OtaState.CANCELLED)
                return False

            # Step 2: 校验固件
            if not await self._verify_firmware(firmware_path, expected_sha256):
                self._set_state(OtaState.FAILED, "firmware_verification_failed")
                return False

            # Step 3: 发送 OTA 通知
            if not await self._send_ota_notify(device_address, firmware_path, target_version):
                self._set_state(OtaState.FAILED, "ota_notify_failed")
                return False

            # Step 4: 分块传输
            if not await self._transfer_firmware(device_address, firmware_path):
                self._set_state(OtaState.FAILED, "transfer_failed")
                return False

            # Step 5: 触发分区切换
            if not await self._trigger_partition_switch(device_address):
                self._set_state(OtaState.FAILED, "partition_switch_failed")
                return False

            # Step 6: 等待重启并验证
            if not await self._wait_and_verify(device_address, target_version):
                self._set_state(OtaState.FAILED, "verification_failed")
                return False

            self._set_state(OtaState.SUCCESS)
            logger.info("ota_success", device=device_address, version=target_version)
            return True

        except asyncio.CancelledError:
            self._set_state(OtaState.CANCELLED)
            return False
        except Exception as exc:  # noqa: BLE001
            self._set_state(OtaState.FAILED, str(exc))
            logger.error("ota_error", error=str(exc))
            return False
        finally:
            # 清理临时文件
            await self._cleanup_temp()

    def cancel(self) -> None:
        """取消正在进行的 OTA 升级。"""
        self._cancel.set()
        logger.info("ota_cancel_requested")

    # ── Step 1: 下载 ──────────────────────────────────────────────

    async def _download_firmware(self, url: str) -> str:
        """从 URL 下载固件到本地临时文件。"""
        self._set_state(OtaState.DOWNLOADING)

        os.makedirs(self._config.download_dir, exist_ok=True)
        filename = url.split("/")[-1].split("?")[0] or "firmware.bin"
        local_path = os.path.join(self._config.download_dir, filename)

        # 使用 curl 下载（支持 HTTPS + 重定向）
        proc = await asyncio.create_subprocess_exec(
            "curl", "-sSL", "-o", local_path, url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"firmware download failed: {url}")

        file_size = os.path.getsize(local_path)
        if file_size > self._config.max_firmware_size:
            raise RuntimeError(
                f"firmware too large: {file_size} > {self._config.max_firmware_size}"
            )

        self._update_progress(total_bytes=file_size)
        logger.info("firmware_downloaded", path=local_path, size=file_size)
        return local_path

    # ── Step 2: 校验 ──────────────────────────────────────────────

    async def _verify_firmware(
        self, firmware_path: str, expected_sha256: str | None
    ) -> bool:
        """校验固件完整性（SHA-256）。"""
        self._set_state(OtaState.VERIFYING)

        sha256 = hashlib.sha256()
        with open(firmware_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha256.update(chunk)
        actual_hash = sha256.hexdigest()

        if expected_sha256 and actual_hash != expected_sha256.lower():
            logger.error(
                "firmware_sha256_mismatch",
                expected=expected_sha256,
                actual=actual_hash,
            )
            return False

        logger.info("firmware_verified", sha256=actual_hash)
        return True

    # ── Step 3: OTA 通知 ──────────────────────────────────────────

    async def _send_ota_notify(
        self,
        device_address: str,
        firmware_path: str,
        target_version: str,
    ) -> bool:
        """通过 BLE NUS 向眼镜发送 OTA 通知。

        消息格式（OTA_NOTIFY 0x70）：
          [total_size:4B LE] [chunk_size:2B LE] [version_len:1B] [version_str] [sha256:32B]
        """
        self._set_state(OtaState.NOTIFYING)

        file_size = os.path.getsize(firmware_path)
        total_chunks = (file_size + self._config.chunk_size - 1) // self._config.chunk_size

        # 计算 SHA-256
        sha256 = hashlib.sha256()
        with open(firmware_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha256.update(chunk)
        firmware_hash = sha256.digest()

        # 构建 OTA_NOTIFY payload
        version_bytes = target_version.encode("utf-8")
        payload = (
            struct.pack("<I", file_size)           # total_size (4B LE)
            + struct.pack("<H", self._config.chunk_size)  # chunk_size (2B LE)
            + struct.pack("<B", len(version_bytes))  # version_len (1B)
            + version_bytes                          # version_str
            + firmware_hash                          # sha256 (32B)
        )

        # 注册 OTA 响应处理
        self._ble.register_handler(MSG_OTA_REQUEST, self._on_ota_request)
        self._ble.register_handler(MSG_OTA_STATUS, self._on_ota_status)

        # 发送通知
        sent = await self._ble.send_to_device(
            device_address, MSG_OTA_NOTIFY, payload
        )
        if not sent:
            return False

        # 等待眼镜确认（STATUS: ready）
        try:
            response = await asyncio.wait_for(
                self._ota_response.get(), timeout=self._config.chunk_timeout
            )
            if response.get("status") != "ready":
                logger.error("ota_notify_rejected", response=response)
                return False
        except asyncio.TimeoutError:
            logger.error("ota_notify_timeout")
            return False

        self._update_progress(
            total_bytes=file_size,
            total_chunks=total_chunks,
            current_chunk=0,
            transferred_bytes=0,
        )
        logger.info(
            "ota_notify_accepted",
            device=device_address,
            size=file_size,
            chunks=total_chunks,
        )
        return True

    # ── Step 4: 分块传输 ──────────────────────────────────────────

    async def _transfer_firmware(
        self, device_address: str, firmware_path: str
    ) -> bool:
        """通过 BLE NUS 分块传输固件。

        每块格式（OTA_DATA 0x72）：
          [chunk_index:4B LE] [data_len:2B LE] [data:N B]
        """
        self._set_state(OtaState.TRANSFERRING)

        file_size = os.path.getsize(firmware_path)
        total_chunks = self._progress.total_chunks
        chunk_size = self._config.chunk_size

        with open(firmware_path, "rb") as f:
            for chunk_idx in range(total_chunks):
                if self._cancel.is_set():
                    return False

                chunk_data = f.read(chunk_size)
                if not chunk_data:
                    break

                # 构建数据块 payload
                payload = (
                    struct.pack("<I", chunk_idx)              # chunk_index (4B LE)
                    + struct.pack("<H", len(chunk_data))      # data_len (2B LE)
                    + chunk_data                               # data
                )

                # 发送并等待 ACK（带重试）
                success = False
                for retry in range(self._config.max_chunk_retries):
                    sent = await self._ble.send_to_device(
                        device_address, MSG_OTA_DATA, payload
                    )
                    if not sent:
                        await asyncio.sleep(self._config.inter_chunk_delay)
                        continue

                    # 等待眼镜 ACK（OTA_REQUEST 下一块 或 ACK）
                    try:
                        response = await asyncio.wait_for(
                            self._ota_response.get(),
                            timeout=self._config.chunk_timeout,
                        )
                        if response.get("status") == "ok" or response.get("next_chunk") == chunk_idx + 1:
                            success = True
                            break
                        elif response.get("status") == "error":
                            logger.error(
                                "ota_chunk_write_error",
                                chunk=chunk_idx,
                                error=response.get("error"),
                            )
                            return False
                    except asyncio.TimeoutError:
                        logger.warning(
                            "ota_chunk_timeout",
                            chunk=chunk_idx,
                            retry=retry + 1,
                        )

                if not success:
                    logger.error(
                        "ota_chunk_failed_after_retries",
                        chunk=chunk_idx,
                        retries=self._config.max_chunk_retries,
                    )
                    return False

                # 更新进度
                self._update_progress(
                    current_chunk=chunk_idx + 1,
                    transferred_bytes=(chunk_idx + 1) * chunk_size,
                )

                # 块间延迟
                await asyncio.sleep(self._config.inter_chunk_delay)

                if chunk_idx % 50 == 0:
                    logger.info(
                        "ota_transfer_progress",
                        chunk=chunk_idx + 1,
                        total=total_chunks,
                        percent=self._progress.progress_percent,
                    )

        logger.info("ota_transfer_complete", total_chunks=total_chunks)
        return True

    # ── Step 5: 分区切换 ──────────────────────────────────────────

    async def _trigger_partition_switch(self, device_address: str) -> bool:
        """通知眼镜切换到新 OTA 分区并重启。

        消息：OTA_STATUS 0x73 with status="switch"
        """
        self._set_state(OtaState.SWITCHING)

        # 发送切换指令
        payload = struct.pack("<B", 1)  # switch_and_reboot
        sent = await self._ble.send_to_device(
            device_address, MSG_OTA_STATUS, payload
        )
        if not sent:
            return False

        # 等待眼镜确认切换
        try:
            response = await asyncio.wait_for(
                self._ota_response.get(), timeout=30.0
            )
            if response.get("status") != "switching":
                return False
        except asyncio.TimeoutError:
            logger.warning("partition_switch_timeout")
            # 可能眼镜已经直接重启了，继续等待验证

        self._set_state(OtaState.REBOOTING)
        logger.info("glasses_rebooting", device=device_address)

        # 等待设备重新连接（眼镜重启需要 5-15 秒）
        await asyncio.sleep(15)
        return True

    # ── Step 6: 验证 ──────────────────────────────────────────────

    async def _wait_and_verify(
        self, device_address: str, expected_version: str
    ) -> bool:
        """等待眼镜重启后验证新版本。"""
        self._set_state(OtaState.VERIFYING_VERSION)

        deadline = time.time() + self._config.verify_timeout
        while time.time() < deadline:
            if self._cancel.is_set():
                return False

            # 通过 BLE 发送握手请求，检查设备状态上报
            try:
                # 眼镜重启后会上报新版本号（MSG_TYPE=0x10 设备状态上报）
                await self._ble.send_to_device(
                    device_address, 0x00, b"",  # 握手请求
                )
                await asyncio.sleep(2)

                # 检查最近上报的版本号
                device_state = self._ble.get_device_state(device_address)
                if device_state and device_state.get("firmware_version") == expected_version:
                    logger.info(
                        "ota_version_verified",
                        device=device_address,
                        version=expected_version,
                    )
                    return True
            except Exception as exc:  # noqa: BLE001
                logger.debug("verify_retry", error=str(exc))

            await asyncio.sleep(5)

        logger.error("ota_verify_timeout", device=device_address)
        return False

    # ── BLE 回调处理 ──────────────────────────────────────────────

    async def _on_ota_request(self, device_address: str, payload: bytes) -> None:
        """处理眼镜 OTA 请求（请求下一块数据）。"""
        if len(payload) < 4:
            return
        next_chunk = struct.unpack("<I", payload[:4])[0]
        await self._ota_response.put({
            "status": "ok",
            "next_chunk": next_chunk,
            "device": device_address,
        })

    async def _on_ota_status(self, device_address: str, payload: bytes) -> None:
        """处理眼镜 OTA 状态上报。"""
        if len(payload) < 1:
            return
        status_code = payload[0]
        status_map = {0: "ready", 1: "ok", 2: "switching", 3: "error", 4: "complete"}
        status = status_map.get(status_code, "unknown")
        error_msg = ""
        if status == "error" and len(payload) > 1:
            error_msg = payload[1:].decode("utf-8", errors="replace")
        await self._ota_response.put({
            "status": status,
            "error": error_msg,
            "device": device_address,
        })

    # ── 清理 ──────────────────────────────────────────────────────

    async def _cleanup_temp(self) -> None:
        """清理下载的临时固件文件。"""
        try:
            for f in os.listdir(self._config.download_dir):
                os.remove(os.path.join(self._config.download_dir, f))
            logger.debug("ota_temp_cleaned")
        except Exception:  # noqa: BLE001
            pass
