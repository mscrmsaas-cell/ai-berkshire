"""
NUS GATT 客户端 — Nordic UART Service 客户端

功能:
    - 连接/断开 BLE 设备
    - 订阅 TX Characteristic (眼镜端通知, 挎包端接收数据)
    - 写入 RX Characteristic (挎包端发送, 眼镜端接收)
    - 数据回调分发 (完整帧通过 StreamParser 解析后回调)
    - 心跳收发
    - MTU 协商

NUS UUID:
    Service:    6e400001-b5a3-f393-e0a9-e50e24dcca9e
    TX (notify): 6e400003-b5a3-f393-e0a9-e50e24dcca9e
    RX (write):   6e400002-b5a3-f393-e0a9-e50e24dcca9e
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Awaitable, Callable

import structlog
from bleak import BleakClient, BleakGATTCharacteristic
from bleak.exc import BleakError

from app.ble.message_protocol import (
    AckTracker,
    MessageProtocol,
    SequenceNumberManager,
    StreamParser,
)
from app.config import BleConfig
from app.models.ble_message import BleFrame, MessageType

logger = structlog.get_logger(__name__)

# 消息回调类型: (device_address, frame) -> None
MessageCallback = Callable[["NusClient", BleFrame], Awaitable[None]]
# 断开回调类型: (device_address) -> None
DisconnectCallback = Callable[[str], Awaitable[None]]


class NusClient:
    """
    NUS GATT 客户端

    封装 Bleak 的单个 BLE 连接, 管理与一副眼镜的通信。
    """

    def __init__(
        self,
        address: str,
        config: BleConfig,
        protocol: MessageProtocol | None = None,
    ):
        """
        Args:
            address: BLE MAC 地址
            config: BLE 配置
            protocol: 消息协议实例 (None 则自动创建)
        """
        self.address = address
        self.config = config
        self.device_name: str = ""
        self.rssi: int = -100

        # 协议层
        self.protocol = protocol or MessageProtocol(
            max_payload_size=config.max_payload_size,
            mtu=config.mtu,
        )
        self._stream_parser = StreamParser(self.protocol)
        self._seq_manager = SequenceNumberManager()
        self._ack_tracker = AckTracker(
            timeout_seconds=config.ack_timeout,
            max_retries=config.ack_max_retries,
        )

        # Bleak 客户端
        self._bleak_client: BleakClient | None = None

        # 回调
        self._message_callback: MessageCallback | None = None
        self._disconnect_callback: DisconnectCallback | None = None

        # 状态
        self.is_connected: bool = False
        self.is_running: bool = False
        self.connected_at: datetime | None = None
        self.last_heartbeat: datetime | None = None

        # 统计
        self.battery_level: int = 0
        self._bytes_received: int = 0
        self._bytes_sent: int = 0
        self._frames_received: int = 0
        self._frames_sent: int = 0

        # 心跳任务
        self._heartbeat_task: asyncio.Task | None = None
        self._ack_check_task: asyncio.Task | None = None

    # -------------------------------------------------------------------
    # 回调注册
    # -------------------------------------------------------------------

    def set_message_callback(self, callback: MessageCallback) -> None:
        """注册消息回调 (收到完整帧时调用)"""
        self._message_callback = callback

    def set_disconnect_callback(self, callback: DisconnectCallback) -> None:
        """注册断开回调"""
        self._disconnect_callback = callback

    # -------------------------------------------------------------------
    # 连接管理
    # -------------------------------------------------------------------

    async def connect(self, timeout: float | None = None) -> bool:
        """
        连接 BLE 设备

        Args:
            timeout: 连接超时 (秒)

        Returns: True 连接成功
        """
        timeout = timeout or self.config.connection_timeout

        self._bleak_client = BleakClient(
            self.address,
            timeout=timeout,
            disconnected_callback=self._on_bleak_disconnect,
        )

        try:
            await self._bleak_client.connect()
            self.is_connected = True
            self.is_running = True
            self.connected_at = datetime.now(timezone.utc)

            # 协商 MTU
            try:
                negotiated_mtu = await self._bleak_client.get_mtu()
                if negotiated_mtu and negotiated_mtu > 23:
                    self.protocol.mtu = negotiated_mtu
                    self.protocol.ble_write_chunk_size = max(20, negotiated_mtu - 3)
                    logger.info(
                        "nus.mtu_negotiated",
                        address=self.address,
                        mtu=negotiated_mtu,
                    )
            except Exception:
                pass  # 某些平台不支持 MTU 查询

            # 订阅 TX 通知
            await self._bleak_client.start_notify(
                self.config.tx_char_uuid,
                self._on_tx_notification,
            )

            # 获取设备名称
            try:
                device_info = await self._bleak_client.get_service_by_uuid(
                    self.config.service_uuid
                )
                self.device_name = getattr(
                    self._bleak_client, "name", self.address
                ) or self.address
            except Exception:
                self.device_name = self.address

            # 启动心跳和 ACK 检查任务
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            self._ack_check_task = asyncio.create_task(self._ack_check_loop())

            # 发送握手请求
            await self._send_handshake()

            logger.info(
                "nus.connected",
                address=self.address,
                name=self.device_name,
                mtu=self.protocol.mtu,
            )
            return True

        except BleakError as exc:
            logger.error("nus.connect_failed", address=self.address, error=str(exc))
            self.is_connected = False
            self.is_running = False
            return False
        except Exception as exc:
            logger.error("nus.connect_error", address=self.address, error=str(exc))
            self.is_connected = False
            self.is_running = False
            return False

    async def disconnect(self) -> None:
        """主动断开连接"""
        self.is_running = False

        # 取消后台任务
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if self._ack_check_task:
            self._ack_check_task.cancel()
            try:
                await self._ack_check_task
            except asyncio.CancelledError:
                pass
            self._ack_check_task = None

        # 停止通知
        if self._bleak_client and self.is_connected:
            try:
                await self._bleak_client.stop_notify(self.config.tx_char_uuid)
                await self._bleak_client.disconnect()
            except Exception as exc:
                logger.warning("nus.disconnect_error", address=self.address, error=str(exc))

        self.is_connected = False
        self._stream_parser.reset()
        self._ack_tracker.reset()
        logger.info("nus.disconnected", address=self.address)

    def _on_bleak_disconnect(self, client: BleakClient) -> None:
        """Bleak 断开回调 (可能由对端断开或信号丢失触发)"""
        self.is_connected = False
        logger.warning("nus.peer_disconnected", address=self.address)

        # 异步通知
        if self._disconnect_callback:
            asyncio.ensure_future(self._disconnect_callback(self.address))

    # -------------------------------------------------------------------
    # 数据接收
    # -------------------------------------------------------------------

    def _on_tx_notification(
        self,
        characteristic: BleakGATTCharacteristic,
        data: bytearray,
    ) -> None:
        """
        TX 通知回调 (眼镜端发送的数据)

        数据通过 StreamParser 解析, 完整帧通过消息回调分发。
        """
        self._bytes_received += len(data)

        try:
            frames = self._stream_parser.feed(bytes(data))
        except Exception as exc:
            logger.error("nus.parse_error", address=self.address, error=str(exc))
            return

        for frame in frames:
            self._frames_received += 1
            self._handle_frame(frame)

    def _handle_frame(self, frame: BleFrame) -> None:
        """
        处理收到的完整帧

        - ACK 类型: 确认对应已发送帧
        - HEARTBEAT 类型: 更新心跳时间, 回复 ACK
        - 其他类型: 调用消息回调
        """
        # 更新最后心跳时间 (任何消息都视为活跃)
        self.last_heartbeat = datetime.now(timezone.utc)

        if frame.msg_type == MessageType.ACK:
            self._ack_tracker.acknowledge(frame.seq_num)
            return

        if frame.msg_type == MessageType.HEARTBEAT:
            # 解析心跳 payload 更新电量/信号
            self._parse_heartbeat(frame.payload)
            # 回复 ACK
            asyncio.ensure_future(self._send_ack(frame.seq_num))
            return

        if frame.msg_type == MessageType.HANDSHAKE_REQUEST:
            # 眼镜端握手请求, 回复握手确认
            asyncio.ensure_future(self._send_handshake_ack(frame.seq_num))
            return

        # 调用消息回调
        if self._message_callback:
            asyncio.ensure_future(self._message_callback(self, frame))
        else:
            logger.debug(
                "nus.no_callback",
                address=self.address,
                msg_type=frame.msg_type.name,
                seq=frame.seq_num,
            )

    def _parse_heartbeat(self, payload: bytes) -> None:
        """解析心跳 payload, 更新电量/信号"""
        try:
            import struct as _struct
            if len(payload) >= 9:
                self.battery_level = payload[8] if len(payload) > 8 else self.battery_level
        except Exception:
            pass

    # -------------------------------------------------------------------
    # 数据发送
    # -------------------------------------------------------------------

    async def send_frame(
        self,
        msg_type: MessageType,
        payload: bytes = b"",
        wait_ack: bool | None = None,
    ) -> bool:
        """
        发送一个帧

        Args:
            msg_type: 消息类型
            payload: 负载数据
            wait_ack: 是否等待 ACK (None 则根据消息类型自动判断)

        Returns: True 发送成功
        """
        if not self.is_connected or not self._bleak_client:
            logger.warning("nus.send_not_connected", address=self.address)
            return False

        seq_num = self._seq_manager.next()

        if wait_ack is None:
            wait_ack = msg_type.requires_ack

        # 编码帧
        frame_bytes = self.protocol.encode_frame(msg_type, payload, seq_num)

        # 分块写入 RX Characteristic
        chunks = self.protocol.chunk_for_ble(frame_bytes)

        try:
            for chunk in chunks:
                await self._bleak_client.write_gatt_char(
                    self.config.rx_char_uuid,
                    chunk,
                    response=False,  # Write Without Response (更快)
                )
                self._bytes_sent += len(chunk)

            self._frames_sent += 1

            # 等待 ACK
            if wait_ack:
                await self._ack_tracker.track(seq_num, frame_bytes)

            logger.debug(
                "nus.sent",
                address=self.address,
                msg_type=msg_type.name,
                seq=seq_num,
                payload_len=len(payload),
                chunks=len(chunks),
            )
            return True

        except BleakError as exc:
            logger.error(
                "nus.send_failed",
                address=self.address,
                msg_type=msg_type.name,
                error=str(exc),
            )
            return False
        except Exception as exc:
            logger.error(
                "nus.send_error",
                address=self.address,
                msg_type=msg_type.name,
                error=str(exc),
            )
            return False

    async def send_raw(self, data: bytes) -> bool:
        """直接写入 RX Characteristic (不分帧, 用于 OTA 等场景)"""
        if not self.is_connected or not self._bleak_client:
            return False
        try:
            chunks = self.protocol.chunk_for_ble(data)
            for chunk in chunks:
                await self._bleak_client.write_gatt_char(
                    self.config.rx_char_uuid,
                    chunk,
                    response=False,
                )
                self._bytes_sent += len(chunk)
            return True
        except Exception as exc:
            logger.error("nus.send_raw_error", address=self.address, error=str(exc))
            return False

    async def _send_ack(self, seq_num: int) -> None:
        """发送 ACK"""
        await self.send_frame(MessageType.ACK, b"", seq_num=seq_num, wait_ack=False)

    async def _send_handshake(self) -> None:
        """发送握手请求"""
        from app.models.ble_message import HandshakePayload

        payload = HandshakePayload(
            protocol_version=1,
            device_id="bag-terminal-001",
            firmware_version="1.0.0",
            hardware_version="cm4-v1",
            ble_address=self.address,
            capabilities=0x0003,  # 支持 BLE + AI
        )
        await self.send_frame(
            MessageType.HANDSHAKE_REQUEST,
            payload.to_bytes(),
            wait_ack=True,
        )

    async def _send_handshake_ack(self, seq_num: int) -> None:
        """回复握手确认"""
        await self.send_frame(
            MessageType.HANDSHAKE_ACK,
            b"\x01",  # protocol_version=1, success
            seq_num=seq_num,
            wait_ack=False,
        )

    # -------------------------------------------------------------------
    # 心跳与 ACK 检查
    # -------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """心跳发送循环"""
        while self.is_running and self.is_connected:
            try:
                await asyncio.sleep(self.config.heartbeat_interval)
                if not self.is_connected:
                    break

                # 发送心跳
                heartbeat_frame = self.protocol.encode_heartbeat(
                    seq_num=self._seq_manager.next(),
                    battery=0,
                    rssi=self.rssi,
                )
                await self.send_raw(heartbeat_frame)

                # 检查心跳超时
                if self.last_heartbeat:
                    elapsed = (datetime.now(timezone.utc) - self.last_heartbeat).total_seconds()
                    if elapsed > self.config.heartbeat_timeout:
                        logger.warning(
                            "nus.heartbeat_timeout",
                            address=self.address,
                            elapsed=round(elapsed, 1),
                        )
                        await self.disconnect()
                        break

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("nus.heartbeat_error", address=self.address, error=str(exc))

    async def _ack_check_loop(self) -> None:
        """ACK 超时检查循环"""
        while self.is_running and self.is_connected:
            try:
                await asyncio.sleep(1.0)  # 每秒检查一次
                await self._ack_tracker.check_timeouts()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("nus.ack_check_error", address=self.address, error=str(exc))

    # -------------------------------------------------------------------
    # 统计
    # -------------------------------------------------------------------

    @property
    def stats(self) -> dict:
        """获取统计信息"""
        return {
            "address": self.address,
            "name": self.device_name,
            "connected": self.is_connected,
            "connected_at": self.connected_at.isoformat() if self.connected_at else None,
            "battery_level": self.battery_level,
            "rssi": self.rssi,
            "mtu": self.protocol.mtu,
            "bytes_received": self._bytes_received,
            "bytes_sent": self._bytes_sent,
            "frames_received": self._frames_received,
            "frames_sent": self._frames_sent,
            "pending_acks": self._ack_tracker.pending_count,
        }
