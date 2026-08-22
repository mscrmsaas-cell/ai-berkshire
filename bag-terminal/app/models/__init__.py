"""
数据模型包 — Pydantic 模型定义

子模块:
    ble_message       — BLE 消息类型枚举 + 帧模型 + payload 模型
    device_state      — 设备状态模型 (眼镜/挎包终端)
    inspection_record  — 巡检记录模型 (任务/照片/检测结果/告警)
"""

from app.models.ble_message import (
    MessageType,
    FrameType,
    BleFrame,
    BleHeader,
    HandshakePayload,
    HeartbeatPayload,
    DeviceStatusPayload,
    CameraFramePayload,
    DetectionResultPayload,
    RagQueryPayload,
    RagAnswerPayload,
    InspectionStartPayload,
    InspectionEndPayload,
    PhotoCapturePayload,
    AlertPayload,
    OtaNotifyPayload,
    DisplayTextPayload,
)
from app.models.device_state import (
    DeviceMode,
    GlassesState,
    BagTerminalState,
    TelemetryData,
)
from app.models.inspection_record import (
    InspectionTask,
    InspectionPhoto,
    DetectionResult,
    Alert,
    AlertSeverity,
    SyncStatus,
)

__all__ = [
    # ble_message
    "MessageType",
    "FrameType",
    "BleFrame",
    "BleHeader",
    "HandshakePayload",
    "HeartbeatPayload",
    "DeviceStatusPayload",
    "CameraFramePayload",
    "DetectionResultPayload",
    "RagQueryPayload",
    "RagAnswerPayload",
    "InspectionStartPayload",
    "InspectionEndPayload",
    "PhotoCapturePayload",
    "AlertPayload",
    "OtaNotifyPayload",
    "DisplayTextPayload",
    # device_state
    "DeviceMode",
    "GlassesState",
    "BagTerminalState",
    "TelemetryData",
    # inspection_record
    "InspectionTask",
    "InspectionPhoto",
    "DetectionResult",
    "Alert",
    "AlertSeverity",
    "SyncStatus",
]
