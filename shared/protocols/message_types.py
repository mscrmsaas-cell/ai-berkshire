"""
message_types.py
================

铁路巡检智能眼镜 — BLE NUS 消息类型枚举定义 (Python)。

定义 8 个范围段共 35 个消息类型枚举值 (0x00-0x8F)。
Python IntEnum 值与 C 头文件 ``message_types.h`` 严格一一对应，
由 ``test_consistency.py`` 解析两份文件并比对一致性。

消息类型范围:
    0x00-0x0F  通用控制 (General)
    0x10-0x1F  设备状态 (Device)
    0x20-0x2F  摄像头   (Camera)
    0x30-0x3F  音频     (Audio)
    0x40-0x4F  RAG      (Retrieval Augmented Generation)
    0x50-0x5F  巡检     (Inspection)
    0x60-0x6F  告警     (Alert)
    0x70-0x7F  OTA      (Over-The-Air Update)
    0x80-0x8F  显示     (Display)

:copyright: Copyright (c) 2024 Railway Inspection AR Glasses Project
:license: Apache-2.0
"""

from __future__ import annotations

from enum import IntEnum, unique


@unique
class MessageType(IntEnum):
    """BLE NUS 消息类型枚举 (与 C ``message_types.h`` 一一对应)。"""

    # =====================================================================
    # 0x00-0x0F — 通用控制 (General Control)
    # =====================================================================
    HANDSHAKE_REQ = 0x00       #: 握手请求 — 连接建立后由外设发送
    HANDSHAKE_ACK = 0x01       #: 握手确认 — DATA: {version, capabilities}
    HEARTBEAT = 0x02           #: 心跳 — DATA: 4B Unix 时间戳 (BE)
    DISCONNECT = 0x03         #: 断开连接 — DATA: 1B 原因码
    ACK = 0x0F                 #: 通用确认 — DATA: 2B 被确认的 SEQ (BE)

    # =====================================================================
    # 0x10-0x1F — 设备状态 (Device Status)
    # =====================================================================
    DEVICE_STATUS = 0x10       #: 设备状态上报 — 电量/模式/温度/信号
    DEVICE_CONFIG = 0x11       #: 配置下发 — 亮度/音量/采样率等
    CAMERA_ATTACHED = 0x12     #: 摄像头接入通知 (Pogo Pin 连接)
    CAMERA_DETACHED = 0x13     #: 摄像头断开通知 (Pogo Pin 断开)
    LOW_BATTERY = 0x14        #: 低电量告警 — DATA: 1B 电量百分比

    # =====================================================================
    # 0x20-0x2F — 摄像头 (Camera)
    # =====================================================================
    CAMERA_FRAME = 0x20        #: 摄像头帧数据 — JPEG 分片 (分包传输)
    CAMERA_FRAME_END = 0x21    #: 帧传输结束标记 — DATA: 4B 帧大小 (BE)
    DETECT_REQUEST = 0x22      #: 检测请求 — 触发 YOLO 推理
    DETECT_RESULT = 0x23       #: 检测结果 — JSON 缺陷列表

    # =====================================================================
    # 0x30-0x3F — 音频 (Audio)
    # =====================================================================
    AUDIO_CHUNK = 0x30          #: 音频数据块 — Opus/G.711 编码
    ASR_RESULT = 0x31           #: 语音识别 (ASR) 结果文本
    TTS_REQUEST = 0x32          #: 语音合成 (TTS) 请求文本
    TTS_AUDIO = 0x33            #: TTS 合成音频数据

    # =====================================================================
    # 0x40-0x4F — RAG (检索增强生成)
    # =====================================================================
    RAG_QUERY = 0x40            #: RAG 问答请求 — 用户问题文本
    RAG_ANSWER = 0x41           #: RAG 回答 — 生成结果 + 来源引用
    DISPLAY_INSTR = 0x42        #: 显示指令 — RAG 生成的 UI 渲染指令

    # =====================================================================
    # 0x50-0x5F — 巡检 (Inspection)
    # =====================================================================
    INSPECTION_START = 0x50     #: 巡检任务开始 — DATA: 任务元信息
    INSPECTION_END = 0x51       #: 巡检任务结束 — DATA: 统计摘要
    TAKE_PHOTO = 0x52           #: 拍照指令 — DATA: 分辨率/质量参数
    WORK_ORDER_STEP = 0x54      #: 工单步骤 — DATA: 步骤序号+描述

    # =====================================================================
    # 0x60-0x6F — 告警 (Alert)
    # =====================================================================
    ALERT = 0x60                #: 告警上报 — 类型/严重度/描述/截图引用
    ALERT_ACK = 0x61            #: 告警确认 — DATA: 告警ID

    # =====================================================================
    # 0x70-0x7F — OTA (固件升级)
    # =====================================================================
    OTA_NOTIFY = 0x70           #: OTA 通知 — 新版本信息/大小/校验和
    OTA_REQUEST = 0x71          #: OTA 请求 — 请求指定分块
    OTA_DATA = 0x72             #: OTA 数据块 — 偏移+数据
    OTA_STATUS = 0x73           #: OTA 状态 — 进度/成功/失败

    # =====================================================================
    # 0x80-0x8F — 显示 (Display)
    # =====================================================================
    DISPLAY_TEXT = 0x80         #: 显示文本 — 坐标+颜色+内容
    CLEAR_SCREEN = 0x81         #: 清屏 — DATA: 区域参数 (可选)
    NOTIFICATION = 0x82         #: 通知卡片 — 标题+正文+图标
    NAVIGATION = 0x83           #: 导航指令 — 方向+距离+目的地

    # ---------------------------------------------------------------------
    # 便捷方法
    # ---------------------------------------------------------------------

    @property
    def range_base(self) -> int:
        """返回所属范围段的基址 (高 4 位，如 0x00, 0x10, 0x20 ...)。"""
        return self.value & 0xF0

    @property
    def range_name(self) -> str:
        """返回所属范围段的中文名称。"""
        _RANGE_NAMES = {
            0x00: "通用控制",
            0x10: "设备状态",
            0x20: "摄像头",
            0x30: "音频",
            0x40: "RAG",
            0x50: "巡检",
            0x60: "告警",
            0x70: "OTA",
            0x80: "显示",
        }
        return _RANGE_NAMES.get(self.range_base, "未知")

    @classmethod
    def all_values(cls) -> dict[str, int]:
        """返回 {名称: 值} 的有序字典，用于一致性比对。"""
        return {member.name: member.value for member in cls}

    @classmethod
    def from_value(cls, value: int) -> MessageType:
        """根据整数值返回枚举成员，未知值抛出 ValueError。"""
        return cls(value)


# 模块级常量: 期望的枚举总数，用于一致性测试快速校验
EXPECTED_COUNT: int = 35

# 范围段定义: {基址: 段名称}
MESSAGE_RANGES: dict[int, str] = {
    0x00: "General",
    0x10: "Device",
    0x20: "Camera",
    0x30: "Audio",
    0x40: "RAG",
    0x50: "Inspection",
    0x60: "Alert",
    0x70: "OTA",
    0x80: "Display",
}
