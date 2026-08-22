/**
 * @file app_common.h
 * @brief 铁路巡检智能眼镜 — 公共头文件
 *
 * 定义全局错误码枚举、事件组位掩码、系统配置结构体和日志 TAG 宏。
 * 供 main.c 及所有 BSP / 业务模块引用。
 *
 * @copyright Copyright (c) 2024 Railway Inspection AR Glasses Project
 * @license Apache-2.0
 */
#ifndef APP_COMMON_H
#define APP_COMMON_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "sdkconfig.h"

#ifdef __cplusplus
extern "C" {
#endif

/* =========================================================================
 * 日志 TAG 宏
 * ========================================================================= */
#define TAG_MAIN       "app_main"
#define TAG_LCD        "bsp_lcd"
#define TAG_I2S        "bsp_i2s"
#define TAG_BATTERY    "bsp_batt"
#define TAG_POGO       "bsp_pogo"
#define TAG_BLE        "ble"
#define TAG_DISPLAY    "display"
#define TAG_AUDIO      "audio"
#define TAG_POWER      "power"
#define TAG_CAMERA     "camera"
#define TAG_TUYA       "tuya"

/* =========================================================================
 * 全局错误码
 * ========================================================================= */
typedef enum {
    /* 成功 */
    APP_OK                 = 0,       /**< 成功 */

    /* 通用错误 (0x01-0x0F) */
    APP_ERR_INVALID_PARAM  = 0x01,    /**< 无效参数 */
    APP_ERR_NO_MEM         = 0x02,    /**< 内存不足 */
    APP_ERR_TIMEOUT        = 0x03,    /**< 超时 */
    APP_ERR_NOT_READY      = 0x04,    /**< 设备未就绪 */
    APP_ERR_BUSY           = 0x05,    /**< 设备忙 */
    APP_ERR_NOT_SUPPORTED  = 0x06,    /**< 不支持的操作 */

    /* 硬件错误 (0x10-0x1F) */
    APP_ERR_HW_INIT       = 0x10,    /**< 硬件初始化失败 */
    APP_ERR_HW_SPI        = 0x11,    /**< SPI 通信失败 */
    APP_ERR_HW_I2S        = 0x12,    /**< I2S 通信失败 */
    APP_ERR_HW_ADC        = 0x13,    /**< ADC 读取失败 */
    APP_ERR_HW_GPIO       = 0x14,    /**< GPIO 操作失败 */

    /* 协议错误 (0x20-0x2F) */
    APP_ERR_BLE_NOT_CONN  = 0x20,    /**< BLE 未连接 */
    APP_ERR_BLE_FRAME     = 0x21,    /**< BLE 帧格式错误 */
    APP_ERR_BLE_CRC       = 0x22,    /**< CRC 校验失败 */
    APP_ERR_BLE_TIMEOUT   = 0x23,    /**< BLE 通信超时 */

    /* 业务错误 (0x30-0x3F) */
    APP_ERR_NVS           = 0x30,    /**< NVS 存储失败 */
    APP_ERR_CAMERA        = 0x31,    /**< 摄像头错误 */
    APP_ERR_LOW_BATTERY   = 0x32,    /**< 电量不足 */
} app_err_t;

/* =========================================================================
 * 系统事件组位定义
 * ========================================================================= */
#define EVT_BIT_LCD_READY        (1 << 0)  /**< LCD 初始化完成 */
#define EVT_BIT_I2S_READY        (1 << 1)  /**< I2S 初始化完成 */
#define EVT_BIT_BLE_READY        (1 << 2)  /**< BLE 协议栈就绪 */
#define EVT_BIT_BLE_CONNECTED    (1 << 3)  /**< BLE NUS 连接建立 */
#define EVT_BIT_CAMERA_READY     (1 << 4)  /**< 摄像头就绪 */
#define EVT_BIT_CAMERA_ATTACHED  (1 << 5)  /**< 摄像头已接入 (Pogo Pin) */
#define EVT_BIT_BATTERY_LOW      (1 << 6)  /**< 低电量 */
#define EVT_BIT_BATTERY_CHARGING (1 << 7)  /**< 正在充电 */
#define EVT_BIT_TUYA_READY       (1 << 8)  /**< 涂鸦 SDK 就绪 */
#define EVT_BIT_ALL_INIT_DONE    (1 << 9)  /**< 所有 BSP 初始化完成 */

/* =========================================================================
 * 设备工作模式
 * ========================================================================= */
typedef enum {
    APP_MODE_STANDBY    = 0,  /**< 待机模式 */
    APP_MODE_INSPECTING = 1,  /**< 巡检中 */
    APP_MODE_ALERT      = 2,  /**< 告警模式 */
    APP_MODE_CHARGING   = 3,  /**< 充电模式 */
    APP_MODE_OTA        = 4,  /**< OTA 升级模式 */
} app_mode_t;

/* =========================================================================
 * 全局配置结构体
 * ========================================================================= */
typedef struct {
    /* LCD */
    uint8_t  brightness;        /**< 背光亮度 (0-255) */
    /* 音频 */
    uint8_t  volume;            /**< 音量 (0-100) */
    /* 设备 */
    app_mode_t mode;            /**< 当前工作模式 */
    char     firmware_version[16]; /**< 固件版本号 */
    uint32_t auto_sleep_ms;     /**< 自动熄屏超时 (毫秒) */
} app_config_t;

/* =========================================================================
 * 全局变量声明 (在 main.c 中定义)
 * ========================================================================= */
extern EventGroupHandle_t g_sys_events;  /**< 系统事件组 */
extern app_config_t       g_app_config;  /**< 全局配置 */

/* =========================================================================
 * 便捷宏
 * ========================================================================= */

/** 检查 ESP-IDF 返回值，失败时跳转到 goto tag */
#define APP_CHECK(ret, tag, goto_tag, fmt, ...) \
    do { \
        if ((ret) != ESP_OK) { \
            ESP_LOGE(tag, fmt, ##__VA_ARGS__); \
            goto goto_tag; \
        } \
    } while (0)

/** 设置事件位 */
#define APP_SET_BIT(bit) \
    do { \
        if (g_sys_events) { \
            xEventGroupSetBits(g_sys_events, (bit)); \
        } \
    } while (0)

/** 等待事件位 (带超时) */
#define APP_WAIT_BIT(bit, timeout_ms) \
    (g_sys_events ? \
        xEventGroupWaitBits(g_sys_events, (bit), pdFALSE, pdTRUE, \
                            pdMS_TO_TICKS(timeout_ms)) : 0)

/* =========================================================================
 * 函数声明
 * ========================================================================= */

/**
 * @brief 获取工作模式名称字符串
 */
const char *app_mode_name(app_mode_t mode);

/**
 * @brief 初始化 NVS Flash
 * @return ESP_OK 或错误码
 */
esp_err_t app_nvs_init(void);

/**
 * @brief 创建系统事件组
 * @return ESP_OK 或错误码
 */
esp_err_t app_events_init(void);

#ifdef __cplusplus
}
#endif

#endif /* APP_COMMON_H */
