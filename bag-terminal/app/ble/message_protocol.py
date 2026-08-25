"""
BLE NUS 消息协议 — 二进制帧解析、CRC16、分包重组、ACK

帧格式:
    SYNC(0xAA 0x55) | MSG_TYPE(1B) | SEQ_NUM(2B大端) | PAYLOAD_LEN(2B大端) | PAYDATA(NB) | CRC16(2B小端)

消息类型枚举与 shared/protocols/message_types.py 对齐 (此处从 app.models.ble_message 引入)。

功能:
    - MessageProtocol: 帧编码/解码/CRC16 计算
    - FrameAssembler: 分包重组 (大数据分多帧传输)
    - AckTracker: ACK 确认追踪 (超时重传)
"""

from __future__ import annotations

import asyncio
import struct
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Awaitable

import structlog

from app.models.ble_message import BleFrame, BleHeader, FrameType, MessageType

logger = structlog.get_logger(__name__)


# ===========================================================================
# 异常定义
# ===========================================================================

class ProtocolError(Exception):
    """协议错误基类"""


class CRCError(ProtocolError):
    """CRC 校验失败"""


class FrameIncompleteError(ProtocolError):
    """帧数据不完整"""


class UnknownMessageTypeError(ProtocolError):
    """未知消息类型"""


# ===========================================================================
# CRC16-CCITT 计算 (与 ESP32 端一致)
# ===========================================================================

# CRC16-CCITT-FALSE 多项式: 0x1021, 初始值: 0xFFFF
_CRC16_TABLE: list[int] | None = None


def _build_crc16_table() -> list[int]:
    """构建 CRC16-CCITT 查找表"""
    table = []
    for i in range(256):
        crc = i << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
        table.append(crc)
    return table


def crc16_ccitt(data: bytes, initial: int = 0xFFFF) -> int:
    """
    计算 CRC16-CCITT-FALSE 校验值

    与 ESP32 固件端使用的 CRC16 算法一致。
    """
    global _CRC16_TABLE
    if _CRC16_TABLE is None:
        _CRC16_TABLE = _build_crc16_table()

    crc = initial
    for byte in data:
        crc = ((_CRC16_TABLE[(crc >> 8) ^ byte] << 8) | (crc & 0xFF)) & 0xFFFF
        # Python shift already handled, but ensure 16-bit
        crc = crc & 0xFFFF
    return crc


# ===========================================================================
# 消息协议
# ===========================================================================

class MessageProtocol:
    """
    BLE NUS 消息协议

    负责帧的编码与解码, CRC 校验, 分包 (chunking)。
    所有方法均为同步, 无阻塞 I/O。
    """

    SYNC_PATTERN = b"\xAA\x55"
    HEADER_SIZE = BleHeader.size()
    CRC_SIZE = 2
    MIN_FRAME_SIZE = HEADER_SIZE + CRC_SIZE  # 无 payload 的最小帧

    def __init__(self, max_payload_size: int = 4096, mtu: int = 247):
        """
        Args:
            max_payload_size: 单帧最大 payload 字节
            mtu: BLE MTU 大小, 决定 BLE 写入分块大小
        """
        self.max_payload_size = max_payload_size
        self.mtu = mtu
        # BLE 写入有效负载 = MTU - 3 (ATT header)
        self.ble_write_chunk_size = max(20, mtu - 3)

    # --- 编码 ---

    def encode_frame(
        self,
        msg_type: MessageType,
        payload: bytes = b"",
        seq_num: int = 0,
    ) -> bytes:
        """
        编码完整帧为字节

        Returns: SYNC + MSG_TYPE + SEQ + LEN + PAYLOAD + CRC16
        """
        if len(payload) > self.max_payload_size:
            raise ProtocolError(
                f"payload 过大: {len(payload)} > {self.max_payload_size}"
            )

        header = BleHeader(
            msg_type=msg_type.value,
            seq_num=seq_num,
            payload_len=len(payload),
        )
        header_bytes = header.to_bytes()

        # CRC16 覆盖 header + payload (不含 SYNC 和 CRC 字段本身)
        crc_data = header_bytes[2:] + payload  # 跳过 SYNC (2B)
        crc = crc16_ccitt(crc_data)

        frame = header_bytes + payload + struct.pack("<H", crc)
        return frame

    def encode_ack(self, seq_num: int) -> bytes:
        """编码 ACK 帧"""
        return self.encode_frame(MessageType.ACK, b"", seq_num=seq_num)

    def encode_heartbeat(self, seq_num: int, battery: int = 0, rssi: int = -100) -> bytes:
        """编码心跳帧"""
        timestamp_ms = int(time.time() * 1000)
        payload = (
            struct.pack(">Q", timestamp_ms)
            + bytes([battery])
            + struct.pack(">b", rssi)
            + struct.pack(">I", 0)  # free_heap
        )
        return self.encode_frame(MessageType.HEARTBEAT, payload, seq_num=seq_num)

    def chunk_for_ble(self, frame_bytes: bytes) -> list[bytes]:
        """
        将完整帧按 BLE MTU 分块

        Bleak 的 write_gatt_char 一次最多写入 (MTU - 3) 字节。
        """
        chunk_size = self.ble_write_chunk_size
        return [
            frame_bytes[i:i + chunk_size]
            for i in range(0, len(frame_bytes), chunk_size)
        ]

    # --- 解码 ---

    def decode_frame(self, data: bytes) -> BleFrame:
        """
        从字节解码完整帧

        Raises:
            FrameIncompleteError: 数据不完整
            CRCError: CRC 校验失败
            UnknownMessageTypeError: 未知消息类型
        """
        if len(data) < self.MIN_FRAME_SIZE:
            raise FrameIncompleteError(
                f"数据不足: {len(data)} < {self.MIN_FRAME_SIZE}"
            )

        # 校验同步字节
        if data[0:2] != self.SYNC_PATTERN:
            raise ProtocolError(
                f"同步字节错误: 期望 {self.SYNC_PATTERN.hex()}, 收到 {data[0:2].hex()}"
            )

        # 解析帧头
        header = BleHeader.from_bytes(data)

        # 校验完整长度
        expected_len = self.HEADER_SIZE + header.payload_len + self.CRC_SIZE
        if len(data) < expected_len:
            raise FrameIncompleteError(
                f"帧不完整: 期望 {expected_len} 字节, 收到 {len(data)} 字节"
            )

        # 提取 payload 和 CRC
        payload_start = self.HEADER_SIZE
        payload_end = payload_start + header.payload_len
        payload = data[payload_start:payload_end]
        received_crc = struct.unpack_from("<H", data, payload_end)[0]

        # CRC 校验 (覆盖 header(跳过SYNC) + payload)
        crc_data = data[2:payload_end]
        calculated_crc = crc16_ccitt(crc_data)

        if received_crc != calculated_crc:
            raise CRCError(
                f"CRC 校验失败: 收到 0x{received_crc:04X}, 计算 0x{calculated_crc:04X}"
            )

        # 消息类型转换
        msg_type = MessageType.from_value(header.msg_type)
        if msg_type is None:
            raise UnknownMessageTypeError(
                f"未知消息类型: 0x{header.msg_type:02X}"
            )

        return BleFrame(
            msg_type=msg_type,
            seq_num=header.seq_num,
            payload=payload,
            crc16=received_crc,
        )

    @staticmethod
    def find_frame_start(data: bytes, offset: int = 0) -> int:
        """
        在字节流中查找帧起始位置 (SYNC 0xAA 0x55)

        Returns: 帧起始索引, -1 表示未找到
        """
        pattern = MessageProtocol.SYNC_PATTERN
        return data.find(pattern, offset)

    @staticmethod
    def calculate_frame_size(payload_len: int) -> int:
        """计算完整帧大小"""
        return MessageProtocol.HEADER_SIZE + payload_len + MessageProtocol.CRC_SIZE

    @staticmethod
    def peek_payload_len(data: bytes) -> int | None:
        """
        从流数据中预读 payload 长度 (不验证)

        Returns: payload 长度, 或 None (数据不足)
        """
        if len(data) < MessageProtocol.HEADER_SIZE:
            return None
        header = BleHeader.from_bytes(data)
        return header.payload_len


# ===========================================================================
# 流式解析器 — 从 BLE 通知数据流中提取完整帧
# ===========================================================================

class StreamParser:
    """
    BLE 数据流解析器

    处理来自 BLE TX 通知的碎片化数据, 缓冲并提取完整帧。
    """

    def __init__(self, protocol: MessageProtocol):
        self.protocol = protocol
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[BleFrame]:
        """
        喂入数据, 返回提取出的完整帧列表

        处理逻辑:
            1. 追加到缓冲区
            2. 查找 SYNC 模式
            3. 尝试解析完整帧
            4. 验证 CRC
            5. 成功则移出缓冲区, 继续解析下一帧
            6. 失败则跳过 SYNC 继续查找
        """
        frames: list[BleFrame] = []
        self._buffer.extend(data)

        while True:
            # 查找 SYNC
            sync_pos = self._buffer.find(MessageProtocol.SYNC_PATTERN)
            if sync_pos < 0:
                # 无 SYNC, 清空缓冲区
                self._buffer.clear()
                break

            # 丢弃 SYNC 之前的无效数据
            if sync_pos > 0:
                del self._buffer[:sync_pos]

            # 检查是否有足够数据读取帧头
            if len(self._buffer) < MessageProtocol.HEADER_SIZE:
                break

            # 预读 payload 长度
            payload_len = MessageProtocol.peek_payload_len(bytes(self._buffer))
            if payload_len is None:
                break

            # 计算完整帧大小
            frame_size = MessageProtocol.calculate_frame_size(payload_len)
            if len(self._buffer) < frame_size:
                # 数据不完整, 等待更多数据
                break

            # 尝试解码帧
            frame_data = bytes(self._buffer[:frame_size])
            try:
                frame = self.protocol.decode_frame(frame_data)
                frames.append(frame)
                # 移出已解析的帧
                del self._buffer[:frame_size]
            except CRCError as exc:
                logger.warning("stream.crc_error", error=str(exc))
                # 跳过 SYNC, 继续查找下一帧
                del self._buffer[:1]
            except UnknownMessageTypeError as exc:
                logger.warning("stream.unknown_type", error=str(exc))
                del self._buffer[:1]
            except ProtocolError as exc:
                logger.warning("stream.protocol_error", error=str(exc))
                del self._buffer[:1]

        return frames

    def reset(self) -> None:
        """重置缓冲区"""
        self._buffer.clear()


# ===========================================================================
# 分包重组器 — 处理大数据 (如 JPEG 图片) 的多帧传输
# ===========================================================================

@dataclass
class _ReassemblyBuffer:
    """单次分片重组缓冲区"""
    msg_type: MessageType
    total_size: int
    received_size: int = 0
    chunks: dict[int, bytes] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


class FrameAssembler:
    """
    分包重组器

    处理大于单帧 MTU 的数据 (如 JPEG 图片), 按帧序号重组。
    每个分包消息通过 seq_num 关联。
    """

    def __init__(self, timeout_seconds: float = 30.0, max_packets: int = 64):
        """
        Args:
            timeout_seconds: 分包超时时间 (超时后丢弃不完整数据)
            max_packets: 最大分包数 (防止内存耗尽)
        """
        self.timeout_seconds = timeout_seconds
        self.max_packets = max_packets
        self._buffers: dict[int, _ReassemblyBuffer] = {}
        # 回调: (seq_num, msg_type, complete_data) -> None
        self._on_complete: Callable[[int, MessageType, bytes], Awaitable[None]] | None = None

    def set_complete_callback(
        self, callback: Callable[[int, MessageType, bytes], Awaitable[None]]
    ) -> None:
        """设置重组完成回调"""
        self._on_complete = callback

    def add_chunk(
        self,
        seq_num: int,
        msg_type: MessageType,
        chunk_offset: int,
        chunk_data: bytes,
        total_size: int,
    ) -> bool:
        """
        添加分片数据

        Returns: True 如果该消息已完成重组
        """
        # 超时清理
        self._cleanup_expired()

        # 创建或获取缓冲区
        if seq_num not in self._buffers:
            if len(self._buffers) >= self.max_packets:
                logger.warning("assembler.max_buffers_exceeded", count=len(self._buffers))
                # 移除最旧的
                oldest_seq = min(self._buffers.keys())
                del self._buffers[oldest_seq]
            self._buffers[seq_num] = _ReassemblyBuffer(
                msg_type=msg_type,
                total_size=total_size,
            )

        buf = self._buffers[seq_num]

        # 忽略重复分片
        if chunk_offset in buf.chunks:
            logger.debug("assembler.duplicate_chunk", seq=seq_num, offset=chunk_offset)
            return False

        buf.chunks[chunk_offset] = chunk_data
        buf.received_size += len(chunk_data)

        # 检查是否完成
        if buf.received_size >= buf.total_size:
            # 重组
            complete_data = b""
            offset = 0
            for off in sorted(buf.chunks.keys()):
                if off != offset:
                    logger.warning(
                        "assembler.gap_detected",
                        seq=seq_num,
                        expected=offset,
                        got=off,
                    )
                    break
                complete_data += buf.chunks[off]
                offset += len(buf.chunks[off])

            if len(complete_data) == buf.total_size:
                del self._buffers[seq_num]
                logger.debug(
                    "assembler.complete",
                    seq=seq_num,
                    msg_type=msg_type.name,
                    size=len(complete_data),
                )
                return True
            else:
                logger.warning(
                    "assembler.size_mismatch",
                    seq=seq_num,
                    expected=buf.total_size,
                    got=len(complete_data),
                )

        return False

    def get_assembled_data(self, seq_num: int) -> bytes | None:
        """获取已重组的完整数据 (如果完成)"""
        if seq_num not in self._buffers:
            return None
        buf = self._buffers[seq_num]
        if buf.received_size < buf.total_size:
            return None
        complete = b""
        for off in sorted(buf.chunks.keys()):
            complete += buf.chunks[off]
        return complete

    def _cleanup_expired(self) -> None:
        """清理超时的不完整缓冲区"""
        now = time.time()
        expired = [
            seq for seq, buf in self._buffers.items()
            if now - buf.created_at > self.timeout_seconds
        ]
        for seq in expired:
            buf = self._buffers[seq]
            logger.warning(
                "assembler.timeout",
                seq=seq,
                received=buf.received_size,
                total=buf.total_size,
            )
            del self._buffers[seq]

    def reset(self) -> None:
        """重置所有缓冲区"""
        self._buffers.clear()


# ===========================================================================
# ACK 追踪器 — 确认机制 + 超时重传
# ===========================================================================

@dataclass
class _PendingAck:
    """待确认的发送帧"""
    seq_num: int
    frame_bytes: bytes
    sent_at: float = field(default_factory=time.time)
    retry_count: int = 0


class AckTracker:
    """
    ACK 确认追踪器

    管理需要 ACK 确认的已发送帧, 支持超时重传。
    """

    def __init__(
        self,
        timeout_seconds: float = 3.0,
        max_retries: int = 3,
    ):
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._pending: dict[int, _PendingAck] = {}
        self._retry_callback: Callable[[bytes], Awaitable[None]] | None = None

    def set_retry_callback(
        self, callback: Callable[[bytes], Awaitable[None]]
    ) -> None:
        """设置重传回调 (重新发送帧)"""
        self._retry_callback = callback

    async def track(
        self,
        seq_num: int,
        frame_bytes: bytes,
    ) -> None:
        """注册一个待确认帧"""
        self._pending[seq_num] = _PendingAck(
            seq_num=seq_num,
            frame_bytes=frame_bytes,
        )

    def acknowledge(self, seq_num: int) -> bool:
        """
        确认一个已发送帧

        Returns: True 如果该帧在等待 ACK (正常确认), False 如果不在
        """
        if seq_num in self._pending:
            pending = self._pending.pop(seq_num)
            rtt = time.time() - pending.sent_at
            logger.debug("ack.received", seq=seq_num, rtt_ms=round(rtt * 1000, 2))
            return True
        return False

    async def check_timeouts(self) -> list[int]:
        """
        检查超时帧, 触发重传

        Returns: 触发重传的 seq_num 列表
        """
        now = time.time()
        retried: list[int] = []

        for seq_num, pending in list(self._pending.items()):
            elapsed = now - pending.sent_at
            if elapsed < self.timeout_seconds:
                continue

            if pending.retry_count >= self.max_retries:
                # 超过最大重试, 放弃
                logger.error(
                    "ack.max_retries_exceeded",
                    seq=seq_num,
                    retries=pending.retry_count,
                )
                del self._pending[seq_num]
                continue

            # 重传
            pending.retry_count += 1
            pending.sent_at = now
            retried.append(seq_num)

            if self._retry_callback:
                try:
                    await self._retry_callback(pending.frame_bytes)
                    logger.debug("ack.retried", seq=seq_num, retry=pending.retry_count)
                except Exception as exc:
                    logger.error("ack.retry_failed", seq=seq_num, error=str(exc))

        return retried

    @property
    def pending_count(self) -> int:
        """等待 ACK 的帧数"""
        return len(self._pending)

    def reset(self) -> None:
        """重置所有待确认帧"""
        self._pending.clear()


# ===========================================================================
# 序列号管理器
# ===========================================================================

class SequenceNumberManager:
    """序列号管理器 (16-bit 循环)"""

    def __init__(self, start: int = 0):
        self._counter = start & 0xFFFF

    def next(self) -> int:
        """获取下一个序列号"""
        seq = self._counter
        self._counter = (self._counter + 1) & 0xFFFF
        return seq

    def peek(self) -> int:
        """预览下一个序列号 (不消费)"""
        return self._counter

    def reset(self, value: int = 0) -> None:
        """重置序列号"""
        self._counter = value & 0xFFFF
