/**
 * @file message_types.h
 * @brief 铁路巡检智能眼镜 — BLE NUS 消息类型枚举定义 (C 语言)
 *
 * 定义 8 个范围段共 35 个消息类型枚举值 (0x00-0x8F)。
 * C 枚举值与 Python message_types.py IntEnum 严格一一对应，
 * 由 test_consistency.py 校验一致性。
 *
 * 消息类型范围:
 *   0x00-0x0F  通用控制 (General)
 *   0x10-0x1F  设备状态 (Device)
 *   0x20-0x2F  摄像头   (Camera)
 *   0x30-0x3F  音频     (Audio)
 *   0x40-0x4F  RAG      (Retrieval Augmented Generation)
 *   0x50-0x5F  巡检     (Inspection)
 *   0x60-0x6F  告警     (Alert)
 *   0x70-0x7F  OTA      (Over-The-Air Update)
 *   0x80-0x8F  显示     (Display)
 *
 * @copyright Copyright (c) 2024 Railway Inspection AR Glasses Project
 * @license Apache-2.0
 */
#ifndef MESSAGE_TYPES_H
#define MESSAGE_TYPES_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief BLE NUS 消息类型枚举
 *
 * 范围段定义说明:
 * - 每个范围段预留 16 个槽位 (0xN0-0xNF)，当前仅定义必要值。
 * - MSG_TYPE_MAX 为已定义的最大值，用于接收端范围校验。
 */
typedef enum {
    /* ====================================================================
     * 0x00-0x0F — 通用控制 (General Control)
     * ==================================================================== */
    MSG_HANDSHAKE_REQ   = 0x00, /**< 握手请求 — 连接建立后由外设发送 */
    MSG_HANDSHAKE_ACK   = 0x01, /**< 握手确认 — DATA: {version, capabilities} */
    MSG_HEARTBEAT       = 0x02, /**< 心跳 — DATA: 4B Unix 时间戳 (BE) */
    MSG_DISCONNECT      = 0x03, /**< 断开连接 — DATA: 1B 原因码 */
    MSG_ACK             = 0x0F, /**< 通用确认 — DATA: 2B 被确认的 SEQ (BE) */

    /* ====================================================================
     * 0x10-0x1F — 设备状态 (Device Status)
     * ==================================================================== */
    MSG_DEVICE_STATUS   = 0x10, /**< 设备状态上报 — 电量/模式/温度/信号 */
    MSG_DEVICE_CONFIG   = 0x11, /**< 配置下发 — 亮度/音量/采样率等 */
    MSG_CAMERA_ATTACHED = 0x12, /**< 摄像头接入通知 (Pogo Pin 连接) */
    MSG_CAMERA_DETACHED = 0x13, /**< 摄像头断开通知 (Pogo Pin 断开) */
    MSG_LOW_BATTERY     = 0x14, /**< 低电量告警 — DATA: 1B 电量百分比 */

    /* ====================================================================
     * 0x20-0x2F — 摄像头 (Camera)
     * ==================================================================== */
    MSG_CAMERA_FRAME    = 0x20, /**< 摄像头帧数据 — JPEG 分片 (分包传输) */
    MSG_CAMERA_FRAME_END = 0x21, /**< 帧传输结束标记 — DATA: 4B 帧大小 (BE) */
    MSG_DETECT_REQUEST  = 0x22, /**< 检测请求 — 触发 YOLO 推理 */
    MSG_DETECT_RESULT   = 0x23, /**< 检测结果 — JSON 缺陷列表 */

    /* ====================================================================
     * 0x30-0x3F — 音频 (Audio)
     * ==================================================================== */
    MSG_AUDIO_CHUNK     = 0x30, /**< 音频数据块 — Opus/G.711 编码 */
    MSG_ASR_RESULT      = 0x31, /**< 语音识别 (ASR) 结果文本 */
    MSG_TTS_REQUEST     = 0x32, /**< 语音合成 (TTS) 请求文本 */
    MSG_TTS_AUDIO       = 0x33, /**< TTS 合成音频数据 */

    /* ====================================================================
     * 0x40-0x4F — RAG (检索增强生成)
     * ==================================================================== */
    MSG_RAG_QUERY       = 0x40, /**< RAG 问答请求 — 用户问题文本 */
    MSG_RAG_ANSWER      = 0x41, /**< RAG 回答 — 生成结果 + 来源引用 */
    MSG_DISPLAY_INSTR   = 0x42, /**< 显示指令 — RAG 生成的 UI 渲染指令 */

    /* ====================================================================
     * 0x50-0x5F — 巡检 (Inspection)
     * ==================================================================== */
    MSG_INSPECTION_START = 0x50, /**< 巡检任务开始 — DATA: 任务元信息 */
    MSG_INSPECTION_END  = 0x51, /**< 巡检任务结束 — DATA: 统计摘要 */
    MSG_TAKE_PHOTO      = 0x52, /**< 拍照指令 — DATA: 分辨率/质量参数 */
    MSG_WORK_ORDER_STEP = 0x54, /**< 工单步骤 — DATA: 步骤序号+描述 */

    /* ====================================================================
     * 0x60-0x6F — 告警 (Alert)
     * ==================================================================== */
    MSG_ALERT           = 0x60, /**< 告警上报 — 类型/严重度/描述/截图引用 */
    MSG_ALERT_ACK       = 0x61, /**< 告警确认 — DATA: 告警ID */

    /* ====================================================================
     * 0x70-0x7F — OTA (固件升级)
     * ==================================================================== */
    MSG_OTA_NOTIFY      = 0x70, /**< OTA 通知 — 新版本信息/大小/校验和 */
    MSG_OTA_REQUEST     = 0x71, /**< OTA 请求 — 请求指定分块 */
    MSG_OTA_DATA        = 0x72, /**< OTA 数据块 — 偏移+数据 */
    MSG_OTA_STATUS      = 0x73, /**< OTA 状态 — 进度/成功/失败 */

    /* ====================================================================
     * 0x80-0x8F — 显示 (Display)
     * ==================================================================== */
    MSG_DISPLAY_TEXT    = 0x80, /**< 显示文本 — 坐标+颜色+内容 */
    MSG_CLEAR_SCREEN    = 0x81, /**< 清屏 — DATA: 区域参数 (可选) */
    MSG_NOTIFICATION    = 0x82, /**< 通知卡片 — 标题+正文+图标 */
    MSG_NAVIGATION      = 0x83, /**< 导航指令 — 方向+距离+目的地 */

    /** 已定义的最大消息类型值 (用于接收端范围校验) */
    MSG_TYPE_MAX        = MSG_NAVIGATION,
} message_type_t;

/**
 * @brief 范围段定义 — 用于快速判断消息类型所属类别
 */
typedef enum {
    MSG_RANGE_GENERAL    = 0x00, /**< 0x00-0x0F 通用控制 */
    MSG_RANGE_DEVICE     = 0x10, /**< 0x10-0x1F 设备状态 */
    MSG_RANGE_CAMERA     = 0x20, /**< 0x20-0x2F 摄像头 */
    MSG_RANGE_AUDIO      = 0x30, /**< 0x30-0x3F 音频 */
    MSG_RANGE_RAG        = 0x40, /**< 0x40-0x4F RAG */
    MSG_RANGE_INSPECTION = 0x50, /**< 0x50-0x5F 巡检 */
    MSG_RANGE_ALERT      = 0x60, /**< 0x60-0x6F 告警 */
    MSG_RANGE_OTA        = 0x70, /**< 0x70-0x7F OTA */
    MSG_RANGE_DISPLAY    = 0x80, /**< 0x80-0x8F 显示 */
} message_range_t;

/**
 * @brief 判断消息类型是否合法 (在已定义范围内)
 * @param type 消息类型值
 * @return 1 合法, 0 非法
 */
static inline int message_type_is_valid(uint8_t type)
{
    switch (type) {
        /* 通用控制 */
        case MSG_HANDSHAKE_REQ:
        case MSG_HANDSHAKE_ACK:
        case MSG_HEARTBEAT:
        case MSG_DISCONNECT:
        case MSG_ACK:
        /* 设备状态 */
        case MSG_DEVICE_STATUS:
        case MSG_DEVICE_CONFIG:
        case MSG_CAMERA_ATTACHED:
        case MSG_CAMERA_DETACHED:
        case MSG_LOW_BATTERY:
        /* 摄像头 */
        case MSG_CAMERA_FRAME:
        case MSG_CAMERA_FRAME_END:
        case MSG_DETECT_REQUEST:
        case MSG_DETECT_RESULT:
        /* 音频 */
        case MSG_AUDIO_CHUNK:
        case MSG_ASR_RESULT:
        case MSG_TTS_REQUEST:
        case MSG_TTS_AUDIO:
        /* RAG */
        case MSG_RAG_QUERY:
        case MSG_RAG_ANSWER:
        case MSG_DISPLAY_INSTR:
        /* 巡检 */
        case MSG_INSPECTION_START:
        case MSG_INSPECTION_END:
        case MSG_TAKE_PHOTO:
        case MSG_WORK_ORDER_STEP:
        /* 告警 */
        case MSG_ALERT:
        case MSG_ALERT_ACK:
        /* OTA */
        case MSG_OTA_NOTIFY:
        case MSG_OTA_REQUEST:
        case MSG_OTA_DATA:
        case MSG_OTA_STATUS:
        /* 显示 */
        case MSG_DISPLAY_TEXT:
        case MSG_CLEAR_SCREEN:
        case MSG_NOTIFICATION:
        case MSG_NAVIGATION:
            return 1;
        default:
            return 0;
    }
}

/**
 * @brief 获取消息类型所属范围段
 * @param type 消息类型值
 * @return 范围段枚举值 (message_range_t)
 */
static inline message_range_t message_type_get_range(uint8_t type)
{
    return (message_range_t)(type & 0xF0);
}

/**
 * @brief 获取消息类型的字符串名称 (调试用)
 * @param type 消息类型值
 * @return 对应的静态字符串
 */
static inline const char *message_type_name(uint8_t type)
{
    switch (type) {
        case MSG_HANDSHAKE_REQ:    return "HANDSHAKE_REQ";
        case MSG_HANDSHAKE_ACK:    return "HANDSHAKE_ACK";
        case MSG_HEARTBEAT:        return "HEARTBEAT";
        case MSG_DISCONNECT:       return "DISCONNECT";
        case MSG_ACK:              return "ACK";
        case MSG_DEVICE_STATUS:    return "DEVICE_STATUS";
        case MSG_DEVICE_CONFIG:    return "DEVICE_CONFIG";
        case MSG_CAMERA_ATTACHED:  return "CAMERA_ATTACHED";
        case MSG_CAMERA_DETACHED:  return "CAMERA_DETACHED";
        case MSG_LOW_BATTERY:      return "LOW_BATTERY";
        case MSG_CAMERA_FRAME:     return "CAMERA_FRAME";
        case MSG_CAMERA_FRAME_END: return "CAMERA_FRAME_END";
        case MSG_DETECT_REQUEST:   return "DETECT_REQUEST";
        case MSG_DETECT_RESULT:    return "DETECT_RESULT";
        case MSG_AUDIO_CHUNK:      return "AUDIO_CHUNK";
        case MSG_ASR_RESULT:       return "ASR_RESULT";
        case MSG_TTS_REQUEST:      return "TTS_REQUEST";
        case MSG_TTS_AUDIO:        return "TTS_AUDIO";
        case MSG_RAG_QUERY:        return "RAG_QUERY";
        case MSG_RAG_ANSWER:       return "RAG_ANSWER";
        case MSG_DISPLAY_INSTR:    return "DISPLAY_INSTR";
        case MSG_INSPECTION_START: return "INSPECTION_START";
        case MSG_INSPECTION_END:   return "INSPECTION_END";
        case MSG_TAKE_PHOTO:       return "TAKE_PHOTO";
        case MSG_WORK_ORDER_STEP:  return "WORK_ORDER_STEP";
        case MSG_ALERT:            return "ALERT";
        case MSG_ALERT_ACK:        return "ALERT_ACK";
        case MSG_OTA_NOTIFY:       return "OTA_NOTIFY";
        case MSG_OTA_REQUEST:      return "OTA_REQUEST";
        case MSG_OTA_DATA:         return "OTA_DATA";
        case MSG_OTA_STATUS:       return "OTA_STATUS";
        case MSG_DISPLAY_TEXT:     return "DISPLAY_TEXT";
        case MSG_CLEAR_SCREEN:     return "CLEAR_SCREEN";
        case MSG_NOTIFICATION:     return "NOTIFICATION";
        case MSG_NAVIGATION:       return "NAVIGATION";
        default:                   return "UNKNOWN";
    }
}

#ifdef __cplusplus
}
#endif

#endif /* MESSAGE_TYPES_H */
