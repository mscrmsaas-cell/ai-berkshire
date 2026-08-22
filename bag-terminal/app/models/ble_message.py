"""
BLE 消息 Pydantic 模型

定义:
    - MessageType: IntEnum 枚举, 值与 shared/protocols/message_types.py 对齐
    - BleFrame / BleHeader: BLE NUS 二进制帧模型
    - 各 payload 模型: 按 MessageType 分类

帧格式:
    SYNC(0xAA 0x55) | MSG_TYPE(1B) | SEQ_NUM(2B大端) | PAYLOAD_LEN(2B大端) | PAYDATA(NB) | CRC16(2B小端)

消息类型范围:
    0x00-0x0F 通用控制 (握手/心跳/断开/ACK)
    0x10-0x1F 设备状态 (状态上报/配置下发/摄像头接入断开/低电量)
    0x20-0x2F 摄像头 (帧传输/帧结束/检测请求结果)
    0x30-0x3F 音频 (音频块/ASR结果/TTS请求音频)
    0x40-0x4F RAG (问答请求/回答/显示指令)
    0x50-0x5F 巡检 (开始/结束/拍照/工单步骤)
    0x60-0x6F 告警 (告警/确认)
    0x70-0x7F OTA (通知/请求/数据/状态)
    0x80-0x8F 显示 (文本/清屏/通知/导航)
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from pydantic import BaseModel, Field


# ===========================================================================
# 消息类型枚举 — 与 shared/protocols/message_types.py 对齐
# ===========================================================================

class MessageType(IntEnum):
    """
    BLE NUS 消息类型枚举

    值与 shared/protocols/message_types.py 及 C 头文件 message_types.h 一一对应。
    本枚举在 shared/protocols/message_types.py 创建后可直接引用替换。
    """

    # --- 通用控制 0x00-0x0F ---
    HANDSHAKE_REQUEST = 0x00       # 握手请求
    HANDSHAKE_ACK = 0x01           # 握手确认
    HEARTBEAT = 0x02               # 心跳
    DISCONNECT = 0x03              # 断开连接
    TIME_SYNC = 0x04               # 时间同步
    INFO_QUERY = 0x05             # 设备信息查询
    INFO_RESPONSE = 0x06           # 设备信息响应
    ACK = 0x0F                     # 通用 ACK

    # --- 设备状态 0x10-0x1F ---
    DEVICE_STATUS_REPORT = 0x10   # 设备状态上报
    CONFIG_PUSH = 0x11            # 配置下发
    CAMERA_ATTACHED = 0x12        # 摄像头接入
    CAMERA_DETACHED = 0x13        # 摄像头断开
    LOW_BATTERY = 0x14            # 低电量告警
    BATTERY_STATUS = 0x15         # 电池状态
    SIGNAL_REPORT = 0x16          # BLE 信号强度上报

    # --- 摄像头 0x20-0x2F ---
    CAMERA_FRAME_DATA = 0x20      # 摄像头帧数据传输
    CAMERA_FRAME_END = 0x21       # 帧结束 (完整 JPEG)
    DETECTION_REQUEST = 0x22      # AI 检测请求
    DETECTION_RESULT = 0x23       # AI 检测结果

    # --- 音频 0x30-0x3F ---
    AUDIO_CHUNK = 0x30            # 音频数据块
    ASR_RESULT = 0x31            # 语音识别结果
    TTS_REQUEST = 0x32           # TTS 请求
    TTS_AUDIO = 0x33             # TTS 音频数据

    # --- RAG 0x40-0x4F ---
    RAG_QUERY = 0x40             # RAG 问答请求
    RAG_ANSWER = 0x41            # RAG 回答
    DISPLAY_INSTRUCTION = 0x42   # 显示指令

    # --- 巡检 0x50-0x5F ---
    INSPECTION_START = 0x50      # 巡检开始
    INSPECTION_END = 0x51        # 巡检结束
    PHOTO_CAPTURE = 0x52         # 拍照请求
    PHOTO_CAPTURE_ACK = 0x53     # 拍照确认
    WORK_ORDER_STEP = 0x54       # 工单步骤

    # --- 告警 0x60-0x6F ---
    ALERT = 0x60                  # 告警上报
    ALERT_ACK = 0x61              # 告警确认

    # --- OTA 0x70-0x7F ---
    OTA_NOTIFY = 0x70            # OTA 升级通知
    OTA_REQUEST = 0x71           # OTA 请求
    OTA_DATA = 0x72              # OTA 数据传输
    OTA_STATUS = 0x73            # OTA 状态

    # --- 显示 0x80-0x8F ---
    DISPLAY_TEXT = 0x80          # 显示文本
    DISPLAY_CLEAR = 0x81         # 清屏
    DISPLAY_NOTIFICATION = 0x82  # 通知卡片
    DISPLAY_NAVIGATION = 0x83    # 导航指示

    @classmethod
    def from_value(cls, value: int) -> "MessageType | None":
        """安全地从整数值获取枚举, 未知值返回 None"""
        try:
            return cls(value)
        except ValueError:
            return None

    @property
    def category(self) -> str:
        """返回消息分类名称"""
        ranges = [
            (0x00, 0x0F, "通用控制"),
            (0x10, 0x1F, "设备状态"),
            (0x20, 0x2F, "摄像头"),
            (0x30, 0x3F, "音频"),
            (0x40, 0x4F, "RAG"),
            (0x50, 0x5F, "巡检"),
            (0x60, 0x6F, "告警"),
            (0x70, 0x7F, "OTA"),
            (0x80, 0x8F, "显示"),
        ]
        for low, high, name in ranges:
            if low <= self.value <= high:
                return name
        return "未知"

    @property
    def requires_ack(self) -> bool:
        """该消息类型是否需要 ACK 确认"""
        non_ack_types = {
            MessageType.ACK,
            MessageType.HEARTBEAT,
            MessageType.HANDSHAKE_ACK,
            MessageType.ALERT_ACK,
            MessageType.CAMERA_FRAME_DATA,
            MessageType.AUDIO_CHUNK,
            MessageType.OTA_DATA,
        }
        return self not in non_ack_types


class FrameType(IntEnum):
    """帧类型 (用于分包协议)"""
    SINGLE = 0x00      # 单帧 (完整消息)
    FIRST = 0x01       # 分包首帧
    CONTINUE = 0x02    # 分包续帧
    LAST = 0x03        # 分包末帧


# ===========================================================================
# BLE 帧模型
# ===========================================================================

class BleHeader(BaseModel):
    """BLE NUS 帧头"""
    sync_byte_1: int = Field(default=0xAA, description="同步字节 1")
    sync_byte_2: int = Field(default=0x55, description="同步字节 2")
    msg_type: int = Field(description="消息类型 (MessageType 值)")
    seq_num: int = Field(ge=0, lt=65536, description="序列号 (大端 2B)")
    payload_len: int = Field(ge=0, lt=65536, description="payload 长度 (大端 2B)")

    model_config = {"frozen": True}

    def to_bytes(self) -> bytes:
        """序列化为字节"""
        return bytes([
            self.sync_byte_1,
            self.sync_byte_2,
            self.msg_type,
        ]) + self.seq_num.to_bytes(2, "big") + self.payload_len.to_bytes(2, "big")

    @classmethod
    def from_bytes(cls, data: bytes) -> "BleHeader":
        """从字节解析帧头 (至少 7 字节)"""
        if len(data) < 7:
            raise ValueError(f"帧头至少需要 7 字节, 收到 {len(data)} 字节")
        return cls(
            sync_byte_1=data[0],
            sync_byte_2=data[1],
            msg_type=data[2],
            seq_num=int.from_bytes(data[3:5], "big"),
            payload_len=int.from_bytes(data[5:7], "big"),
        )

    @staticmethod
    def size() -> int:
        """帧头固定大小"""
        return 7

    @staticmethod
    def sync_pattern() -> bytes:
        """同步模式"""
        return b"\xAA\x55"


class BleFrame(BaseModel):
    """完整的 BLE NUS 帧"""
    msg_type: MessageType = Field(description="消息类型")
    seq_num: int = Field(ge=0, lt=65536, description="序列号")
    payload: bytes = Field(default=b"", description="负载数据")
    crc16: int = Field(default=0, ge=0, lt=65536, description="CRC16 校验值 (小端 2B)")

    @property
    def payload_len(self) -> int:
        return len(self.payload)

    def to_bytes(self) -> bytes:
        """序列化为完整帧字节"""
        header = BleHeader(
            msg_type=self.msg_type.value,
            seq_num=self.seq_num,
            payload_len=self.payload_len,
        )
        return header.to_bytes() + self.payload + self.crc16.to_bytes(2, "little")

    @classmethod
    def from_bytes(cls, data: bytes) -> "BleFrame":
        """从完整帧字节解析"""
        header = BleHeader.from_bytes(data)
        total_expected = BleHeader.size() + header.payload_len + 2  # +2 for CRC16
        if len(data) < total_expected:
            raise ValueError(
                f"数据不完整: 期望 {total_expected} 字节, 收到 {len(data)} 字节"
            )
        payload = data[BleHeader.size():BleHeader.size() + header.payload_len]
        crc16 = int.from_bytes(
            data[BleHeader.size() + header.payload_len:total_expected], "little"
        )
        msg_type = MessageType.from_value(header.msg_type)
        if msg_type is None:
            raise ValueError(f"未知消息类型: 0x{header.msg_type:02X}")
        return cls(msg_type=msg_type, seq_num=header.seq_num, payload=payload, crc16=crc16)

    @staticmethod
    def frame_size(payload_len: int) -> int:
        """计算完整帧大小"""
        return BleHeader.size() + payload_len + 2  # header + payload + crc16


# ===========================================================================
# Payload 模型 (按消息类型)
# ===========================================================================

class HandshakePayload(BaseModel):
    """握手请求/确认 payload"""
    protocol_version: int = Field(description="协议版本号")
    device_id: str = Field(description="设备 ID")
    firmware_version: str = Field(default="", description="固件版本")
    hardware_version: str = Field(default="", description="硬件版本")
    ble_address: str = Field(default="", description="BLE MAC 地址")
    capabilities: int = Field(default=0, description="能力位掩码")

    def to_bytes(self) -> bytes:
        """序列化为 payload 字节"""
        device_id_bytes = self.device_id.encode("utf-8")
        fw_bytes = self.firmware_version.encode("utf-8")
        hw_bytes = self.hardware_version.encode("utf-8")
        ble_bytes = self.ble_address.encode("utf-8")
        return (
            bytes([self.protocol_version])
            + bytes([len(device_id_bytes)]) + device_id_bytes
            + bytes([len(fw_bytes)]) + fw_bytes
            + bytes([len(hw_bytes)]) + hw_bytes
            + bytes([len(ble_bytes)]) + ble_bytes
            + self.capabilities.to_bytes(2, "big")
        )


class HeartbeatPayload(BaseModel):
    """心跳 payload"""
    timestamp: int = Field(description="时间戳 (Unix ms)")
    battery_level: int = Field(ge=0, le=100, default=0, description="电量百分比")
    rssi: int = Field(ge=-100, le=0, default=-100, description="BLE 信号强度 dBm")
    free_heap: int = Field(default=0, description="可用堆内存 (字节)")

    def to_bytes(self) -> bytes:
        return (
            self.timestamp.to_bytes(8, "big")
            + bytes([self.battery_level])
            + self.rssi.to_bytes(1, "big", signed=True)
            + self.free_heap.to_bytes(4, "big")
        )


class DeviceStatusPayload(BaseModel):
    """设备状态上报 payload"""
    battery_level: int = Field(ge=0, le=100, description="电量百分比")
    is_charging: bool = Field(description="充电状态")
    camera_connected: bool = Field(description="摄像头连接状态")
    device_mode: int = Field(description="设备模式 (0=待机 1=巡检 2=告警 3=充电)")
    firmware_version: str = Field(default="", description="固件版本")
    uptime_seconds: int = Field(default=0, description="运行时长 (秒)")

    def to_bytes(self) -> bytes:
        return (
            bytes([self.battery_level])
            + bytes([1 if self.is_charging else 0])
            + bytes([1 if self.camera_connected else 0])
            + bytes([self.device_mode])
            + self.uptime_seconds.to_bytes(4, "big")
        )


class CameraFramePayload(BaseModel):
    """摄像头帧数据 payload"""
    frame_index: int = Field(ge=0, description="帧序号")
    frame_type: FrameType = Field(description="帧类型 (单帧/分包)")
    total_size: int = Field(ge=0, description="完整帧大小 (JPEG)")
    chunk_offset: int = Field(default=0, ge=0, description="分包偏移量")
    chunk_data: bytes = Field(default=b"", description="分块数据")

    def to_bytes(self) -> bytes:
        return (
            self.frame_index.to_bytes(4, "big")
            + bytes([self.frame_type.value])
            + self.total_size.to_bytes(4, "big")
            + self.chunk_offset.to_bytes(4, "big")
            + bytes([len(self.chunk_data) >> 8, len(self.chunk_data) & 0xFF])
            + self.chunk_data
        )


class DetectionResultPayload(BaseModel):
    """AI 检测结果 payload"""
    frame_index: int = Field(description="对应帧序号")
    detection_count: int = Field(description="检测到的缺陷数量")
    detections: list[dict[str, Any]] = Field(
        default_factory=list,
        description="检测结果列表 [{class_id, class_name, confidence, x, y, w, h}]"
    )
    inference_time_ms: int = Field(default=0, description="推理耗时 (毫秒)")

    def to_bytes(self) -> bytes:
        import json
        detections_json = json.dumps(self.detections, ensure_ascii=False).encode("utf-8")
        return (
            self.frame_index.to_bytes(4, "big")
            + bytes([self.detection_count])
            + self.inference_time_ms.to_bytes(2, "big")
            + len(detections_json).to_bytes(2, "big")
            + detections_json
        )


class RagQueryPayload(BaseModel):
    """RAG 问答请求 payload"""
    query_id: int = Field(description="查询 ID")
    question: str = Field(description="用户问题")
    max_tokens: int = Field(default=512, description="最大生成 token 数")

    def to_bytes(self) -> bytes:
        q_bytes = self.question.encode("utf-8")
        return (
            self.query_id.to_bytes(2, "big")
            + self.max_tokens.to_bytes(2, "big")
            + bytes([len(q_bytes) >> 8, len(q_bytes) & 0xFF])
            + q_bytes
        )


class RagAnswerPayload(BaseModel):
    """RAG 回答 payload"""
    query_id: int = Field(description="查询 ID")
    answer: str = Field(description="回答文本")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="置信度")
    sources: list[str] = Field(default_factory=list, description="来源文档列表")

    def to_bytes(self) -> bytes:
        import json
        answer_bytes = self.answer.encode("utf-8")
        sources_json = json.dumps(self.sources, ensure_ascii=False).encode("utf-8")
        return (
            self.query_id.to_bytes(2, "big")
            + int(self.confidence * 100).to_bytes(1, "big")
            + len(answer_bytes).to_bytes(2, "big")
            + answer_bytes
            + len(sources_json).to_bytes(2, "big")
            + sources_json
        )


class InspectionStartPayload(BaseModel):
    """巡检开始 payload"""
    task_id: str = Field(description="巡检任务 ID")
    project_name: str = Field(default="", description="项目名称")
    route_name: str = Field(default="", description="线路名称")
    worker_id: str = Field(default="", description="巡检员 ID")
    start_time: int = Field(description="开始时间戳 (Unix ms)")

    def to_bytes(self) -> bytes:
        task_bytes = self.task_id.encode("utf-8")
        project_bytes = self.project_name.encode("utf-8")
        route_bytes = self.route_name.encode("utf-8")
        worker_bytes = self.worker_id.encode("utf-8")
        return (
            bytes([len(task_bytes)]) + task_bytes
            + bytes([len(project_bytes)]) + project_bytes
            + bytes([len(route_bytes)]) + route_bytes
            + bytes([len(worker_bytes)]) + worker_bytes
            + self.start_time.to_bytes(8, "big")
        )


class InspectionEndPayload(BaseModel):
    """巡检结束 payload"""
    task_id: str = Field(description="巡检任务 ID")
    end_time: int = Field(description="结束时间戳 (Unix ms)")
    photo_count: int = Field(default=0, description="照片数量")
    alert_count: int = Field(default=0, description="告警数量")
    distance_meters: float = Field(default=0.0, description="巡检距离 (米)")

    def to_bytes(self) -> bytes:
        task_bytes = self.task_id.encode("utf-8")
        return (
            bytes([len(task_bytes)]) + task_bytes
            + self.end_time.to_bytes(8, "big")
            + self.photo_count.to_bytes(2, "big")
            + self.alert_count.to_bytes(2, "big")
            + int(self.distance_meters * 100).to_bytes(4, "big")
        )


class PhotoCapturePayload(BaseModel):
    """拍照请求 payload"""
    task_id: str = Field(description="巡检任务 ID")
    photo_index: int = Field(description="照片序号")
    capture_time: int = Field(description="拍照时间戳 (Unix ms)")
    gps_lat: float = Field(default=0.0, description="纬度")
    gps_lon: float = Field(default=0.0, description="经度")
    gps_accuracy: float = Field(default=0.0, description="定位精度 (米)")

    def to_bytes(self) -> bytes:
        task_bytes = self.task_id.encode("utf-8")
        return (
            bytes([len(task_bytes)]) + task_bytes
            + self.photo_index.to_bytes(2, "big")
            + self.capture_time.to_bytes(8, "big")
            + int(self.gps_lat * 1e7).to_bytes(4, "big", signed=True)
            + int(self.gps_lon * 1e7).to_bytes(4, "big", signed=True)
            + int(self.gps_accuracy * 100).to_bytes(2, "big")
        )


class AlertPayload(BaseModel):
    """告警上报 payload"""
    alert_id: str = Field(description="告警 ID")
    alert_type: int = Field(description="告警类型 (对应 YOLO 类别)")
    severity: int = Field(description="严重度 (1=低 2=中 3=高)")
    message: str = Field(default="", description="告警描述")
    timestamp: int = Field(description="告警时间戳 (Unix ms)")
    gps_lat: float = Field(default=0.0, description="纬度")
    gps_lon: float = Field(default=0.0, description="经度")

    def to_bytes(self) -> bytes:
        alert_bytes = self.alert_id.encode("utf-8")
        msg_bytes = self.message.encode("utf-8")
        return (
            bytes([len(alert_bytes)]) + alert_bytes
            + bytes([self.alert_type])
            + bytes([self.severity])
            + bytes([len(msg_bytes)]) + msg_bytes
            + self.timestamp.to_bytes(8, "big")
            + int(self.gps_lat * 1e7).to_bytes(4, "big", signed=True)
            + int(self.gps_lon * 1e7).to_bytes(4, "big", signed=True)
        )


class OtaNotifyPayload(BaseModel):
    """OTA 升级通知 payload"""
    version: str = Field(description="目标版本号")
    file_size: int = Field(description="固件文件大小 (字节)")
    file_sha256: str = Field(description="固件 SHA-256 校验值")
    chunk_size: int = Field(default=512, description="分块大小")

    def to_bytes(self) -> bytes:
        ver_bytes = self.version.encode("utf-8")
        sha_bytes = self.file_sha256.encode("utf-8")
        return (
            bytes([len(ver_bytes)]) + ver_bytes
            + self.file_size.to_bytes(4, "big")
            + bytes([len(sha_bytes)]) + sha_bytes
            + self.chunk_size.to_bytes(2, "big")
        )


class DisplayTextPayload(BaseModel):
    """显示文本指令 payload"""
    text: str = Field(description="文本内容")
    x: int = Field(default=0, ge=0, description="X 坐标")
    y: int = Field(default=0, ge=0, description="Y 坐标")
    font_size: int = Field(default=16, description="字号")
    color: int = Field(default=0xFFFF, description="RGB565 颜色")
    duration_ms: int = Field(default=3000, description="显示时长 (毫秒)")

    def to_bytes(self) -> bytes:
        text_bytes = self.text.encode("utf-8")
        return (
            bytes([len(text_bytes)]) + text_bytes
            + self.x.to_bytes(2, "big")
            + self.y.to_bytes(2, "big")
            + bytes([self.font_size])
            + self.color.to_bytes(2, "big")
            + self.duration_ms.to_bytes(2, "big")
        )
