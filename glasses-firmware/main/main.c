/**
 * @file main.c
 * @brief 铁路巡检智能眼镜 — ESP32-S3 固件主入口
 *
 * app_main() 初始化顺序:
 *   NVS → 系统事件组 → BSP (LCD → I2S → TP4056 → Pogo Pin)
 *   → 显示欢迎界面 → 音频自检 → BLE → 摄像头 → 涂鸦 (可选) → 电源管理 → 事件循环
 *
 * @copyright Copyright (c) 2024 Railway Inspection AR Glasses Project
 * @license Apache-2.0
 */

#include <stdio.h>
#include <string.h>
#include "esp_log.h"
#include "esp_system.h"
#include "esp_event.h"
#include "nvs_flash.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "app_common.h"
#include "bsp_lcd_gc9a01.h"
#include "bsp_i2s_audio.h"
#include "bsp_tp4056.h"
#include "bsp_pogo_pin.h"
#include "bsp_ov2640.h"

/* BLE NUS 通信栈 */
#include "ble_common.h"
#include "ble_transport.h"

/* 显示管理 */
esp_err_t display_manager_init(void);
void      display_manager_set_brightness(uint8_t percent);

/* 音频管线 */
esp_err_t audio_pipeline_init(void);
esp_err_t audio_pipeline_start_recording(void);
esp_err_t audio_pipeline_start_playback(void);

/* 电源管理 */
esp_err_t power_manager_init(void);

/* 摄像头模块 */
esp_err_t camera_module_init(void);

/* 涂鸦 BLE SDK (条件编译) */
#if CONFIG_TUYA_ENABLE
esp_err_t tuya_ble_adapter_init(void);
#endif

static const char *TAG = TAG_MAIN;

/* =========================================================================
 * 全局变量定义
 * ========================================================================= */
EventGroupHandle_t g_sys_events = NULL;
app_config_t       g_app_config = {
    .brightness       = 128,
    .volume           = 50,
    .mode             = APP_MODE_STANDBY,
    .firmware_version = "1.0.0",
    .auto_sleep_ms    = 30000,
};

/* =========================================================================
 * 公共函数实现
 * ========================================================================= */

const char *app_mode_name(app_mode_t mode)
{
    switch (mode) {
        case APP_MODE_STANDBY:    return "standby";
        case APP_MODE_INSPECTING: return "inspecting";
        case APP_MODE_ALERT:      return "alert";
        case APP_MODE_CHARGING:   return "charging";
        case APP_MODE_OTA:        return "ota";
        default:                   return "unknown";
    }
}

esp_err_t app_nvs_init(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "NVS 分区需要擦除, 正在重新格式化...");
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
    ESP_LOGI(TAG, "NVS Flash 初始化完成");
    return ESP_OK;
}

esp_err_t app_events_init(void)
{
    g_sys_events = xEventGroupCreate();
    if (g_sys_events == NULL) {
        ESP_LOGE(TAG, "创建系统事件组失败");
        return ESP_ERR_NO_MEM;
    }
    ESP_LOGI(TAG, "系统事件组创建完成");
    return ESP_OK;
}

/* =========================================================================
 * BSP 初始化
 * ========================================================================= */
static esp_err_t app_bsp_init(void)
{
    esp_err_t ret;

    /* 1. LCD GC9A01 */
    ESP_LOGI(TAG, "初始化 GC9A01 LCD...");
    ret = bsp_lcd_init();
    APP_CHECK(ret, TAG_LCD, err_lcd, "LCD 初始化失败: %s", esp_err_to_name(ret));
    bsp_lcd_set_brightness(g_app_config.brightness);
    APP_SET_BIT(EVT_BIT_LCD_READY);
    ESP_LOGI(TAG, "LCD 初始化完成 (240x240 RGB565, SPI %dHz)", CONFIG_BSP_LCD_SPI_FREQ_HZ);

    /* 2. I2S 音频 */
    ESP_LOGI(TAG, "初始化 I2S 音频 (INMP441 + MAX98357A)...");
    ret = bsp_audio_init();
    APP_CHECK(ret, TAG_I2S, err_i2s, "I2S 初始化失败: %s", esp_err_to_name(ret));
    bsp_audio_set_volume(g_app_config.volume);
    APP_SET_BIT(EVT_BIT_I2S_READY);
    ESP_LOGI(TAG, "I2S 音频初始化完成 (%dHz)", CONFIG_BSP_I2S_SAMPLE_RATE);

    /* 3. TP4056 电池管理 */
    ESP_LOGI(TAG, "初始化 TP4056 电池管理...");
    ret = bsp_battery_init();
    APP_CHECK(ret, TAG_BATTERY, err_batt, "电池管理初始化失败: %s", esp_err_to_name(ret));
    APP_SET_BIT(EVT_BIT_BATTERY_CHARGING);  /* 占位, 实际状态由轮询更新 */
    ESP_LOGI(TAG, "电池管理初始化完成");

    /* 4. Pogo Pin 热插拔检测 */
    ESP_LOGI(TAG, "初始化 Pogo Pin 热插拔检测...");
    ret = bsp_pogo_pin_init();
    APP_CHECK(ret, TAG_POGO, err_pogo, "Pogo Pin 初始化失败: %s", esp_err_to_name(ret));
    ESP_LOGI(TAG, "Pogo Pin 热插拔检测初始化完成 (GPIO %d)", CONFIG_BSP_POGO_DETECT_GPIO);

    return ESP_OK;

err_pogo:
err_batt:
    bsp_audio_deinit();
err_i2s:
    bsp_lcd_deinit();
err_lcd:
    return ret;
}

/* =========================================================================
 * 电池状态监控回调
 * ========================================================================= */
static void on_battery_low(uint8_t level)
{
    ESP_LOGW(TAG, "低电量告警: %d%%", level);
    APP_SET_BIT(EVT_BIT_BATTERY_LOW);
    g_app_config.mode = APP_MODE_STANDBY;
}

static void on_battery_charging(bool charging)
{
    if (charging) {
        ESP_LOGI(TAG, "开始充电");
        APP_SET_BIT(EVT_BIT_BATTERY_CHARGING);
    } else {
        ESP_LOGI(TAG, "充电结束");
        xEventGroupClearBits(g_sys_events, EVT_BIT_BATTERY_CHARGING);
    }
}

/* =========================================================================
 * Pogo Pin 热插拔回调
 * ========================================================================= */
static void on_camera_attached(void)
{
    ESP_LOGI(TAG, ">>> 摄像头已接入 (CAMERA_ATTACHED)");
    APP_SET_BIT(EVT_BIT_CAMERA_ATTACHED);
}

static void on_camera_detached(void)
{
    ESP_LOGI(TAG, ">>> 摄像头已断开 (CAMERA_DETACHED)");
    xEventGroupClearBits(g_sys_events, EVT_BIT_CAMERA_ATTACHED);
}

/* =========================================================================
 * 系统信息打印
 * ========================================================================= */
static void app_print_system_info(void)
{
    esp_chip_info_t chip_info;
    esp_chip_info(&chip_info);

    ESP_LOGI(TAG, "========================================");
    ESP_LOGI(TAG, " 铁路巡检智能眼镜 — ESP32-S3");
    ESP_LOGI(TAG, "========================================");
    ESP_LOGI(TAG, " 芯片: %s (Rev %d, %d 核)",
             chip_info.model == CHIP_ESP32S3 ? "ESP32-S3" : "Unknown",
             chip_info.revision, chip_info.cores);
    ESP_LOGI(TAG, " Flash: %dMB %s",
             spi_flash_get_chip_size() / (1024 * 1024),
             (chip_info.features & CHIP_FEATURE_EMB_FLASH) ? "内嵌" : "外置");
    ESP_LOGI(TAG, " PSRAM: %s",
             (chip_info.features & CHIP_FEATURE_SPIRAM) ? "已检测" : "未检测");
    ESP_LOGI(TAG, " IDF:   %s", esp_get_idf_version());
    ESP_LOGI(TAG, " 固件:  v%s", g_app_config.firmware_version);
    ESP_LOGI(TAG, " 模式:  %s", app_mode_name(g_app_config.mode));
    ESP_LOGI(TAG, " 涂鸦:  %s", CONFIG_TUYA_ENABLE ? "启用" : "禁用");
    ESP_LOGI(TAG, "========================================");
}

/* =========================================================================
 * 主事件循环 (阻塞, 保持任务存活)
 * ========================================================================= */
static void app_main_loop(void)
{
    ESP_LOGI(TAG, "进入主事件循环");

    /* 等待所有 BSP 初始化完成 */
    APP_SET_BIT(EVT_BIT_ALL_INIT_DONE);

    uint32_t loop_count = 0;
    while (true) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        loop_count++;

        /* 每 30 秒打印一次心跳信息 */
        if (loop_count % 30 == 0) {
            uint8_t batt = bsp_battery_get_level();
            bool chg     = bsp_battery_is_charging();
            bool cam     = (xEventGroupGetBits(g_sys_events) &
                            EVT_BIT_CAMERA_ATTACHED) != 0;
            ESP_LOGI(TAG, "[心跳] uptime=%lus, batt=%d%%, chg=%d, cam=%d, mode=%s",
                     loop_count, batt, chg, cam,
                     app_mode_name(g_app_config.mode));
        }
    }
}

/* =========================================================================
 * app_main — ESP-IDF 入口
 * ========================================================================= */
void app_main(void)
{
    ESP_LOGI(TAG, "========================================");
    ESP_LOGI(TAG, " 铁路巡检智能眼镜固件启动");
    ESP_LOGI(TAG, "========================================");

    app_print_system_info();

    /* 1. NVS Flash */
    esp_err_t ret = app_nvs_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "NVS 初始化失败, 系统无法继续");
        return;
    }

    /* 2. 系统事件组 */
    ret = app_events_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "事件组初始化失败");
        return;
    }

    /* 3. 默认事件循环 */
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    /* 4. BSP 初始化 */
    ret = app_bsp_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "BSP 初始化失败: %s", esp_err_to_name(ret));
        /* 即使部分失败也继续运行 (降级模式) */
    }

    /* 5. 注册电池回调 */
    bsp_battery_register_low_callback(on_battery_low);
    bsp_battery_register_charging_callback(on_battery_charging);

    /* 6. 注册 Pogo Pin 回调 */
    bsp_pogo_pin_register_callbacks(on_camera_attached, on_camera_detached);

    /* 7. 显示管理初始化 (LVGL + 双缓冲) */
    if (xEventGroupGetBits(g_sys_events) & EVT_BIT_LCD_READY) {
        ret = display_manager_init();
        if (ret == ESP_OK) {
            ESP_LOGI(TAG, "显示管理初始化完成 (LVGL)");
            APP_SET_BIT(EVT_BIT_ALL_INIT_DONE);
        } else {
            ESP_LOGW(TAG, "显示管理初始化失败: %s (降级运行)", esp_err_to_name(ret));
        }
    }

    /* 8. 音频自检 (播放短促提示音) */
    if (xEventGroupGetBits(g_sys_events) & EVT_BIT_I2S_READY) {
        bsp_audio_play_beep();
        ESP_LOGI(TAG, "音频自检完成");
        ret = audio_pipeline_init();
        if (ret == ESP_OK) {
            audio_pipeline_start_recording();
            audio_pipeline_start_playback();
            ESP_LOGI(TAG, "音频管线启动完成 (录音+播放)");
        } else {
            ESP_LOGW(TAG, "音频管线初始化失败: %s", esp_err_to_name(ret));
        }
    }

    /* 9. BLE NUS 通信栈初始化 */
    ESP_LOGI(TAG, "初始化 BLE NUS 通信栈...");
    ret = ble_transport_init();
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "BLE 传输层初始化失败: %s", esp_err_to_name(ret));
    } else {
        ret = ble_nus_service_init();
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "NUS GATT 服务注册失败: %s", esp_err_to_name(ret));
        } else {
            ret = ble_gap_manager_init();
            if (ret != ESP_OK) {
                ESP_LOGW(TAG, "GAP 管理器初始化失败: %s", esp_err_to_name(ret));
            } else {
                ret = ble_gap_start_advertising();
                if (ret == ESP_OK) {
                    ESP_LOGI(TAG, "BLE NUS 广播已启动 (设备名: %s)", BLE_GAP_DEVICE_NAME);
                }
            }
        }
    }

    /* 10. 摄像头模块 (监听 Pogo Pin 热插拔) */
    ESP_LOGI(TAG, "初始化摄像头模块...");
    ret = camera_module_init();
    if (ret == ESP_OK) {
        ESP_LOGI(TAG, "摄像头模块初始化完成 (监听 Pogo Pin 事件)");
    } else {
        ESP_LOGW(TAG, "摄像头模块初始化失败: %s (降级运行)", esp_err_to_name(ret));
    }

    /* 11. 涂鸦 BLE SDK (可选) */
#if CONFIG_TUYA_ENABLE
    ESP_LOGI(TAG, "初始化涂鸦 BLE SDK...");
    ret = tuya_ble_adapter_init();
    if (ret == ESP_OK) {
        ESP_LOGI(TAG, "涂鸦 BLE SDK 初始化完成");
    } else {
        ESP_LOGW(TAG, "涂鸦 BLE SDK 初始化失败: %s", esp_err_to_name(ret));
    }
#else
    ESP_LOGI(TAG, "涂鸦 SDK 已禁用 (CONFIG_TUYA_ENABLE=n)");
#endif

    /* 12. 电源管理 */
    ESP_LOGI(TAG, "初始化电源管理...");
    ret = power_manager_init();
    if (ret == ESP_OK) {
        ESP_LOGI(TAG, "电源管理初始化完成 (深度睡眠+动态调频)");
    } else {
        ESP_LOGW(TAG, "电源管理初始化失败: %s", esp_err_to_name(ret));
    }

    /* 13. 进入主事件循环 */
    app_main_loop();
}
