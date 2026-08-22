/**
 * @file bsp_i2s_audio.c
 * @brief I2S 音频驱动实现 — INMP441 麦克风 + MAX98357A 功放
 *
 * 实现 I2S 双向音频:
 *   - TX (播放): MAX98357A D 类功放, I2S → 扬声器
 *   - RX (录音): INMP441 MEMS 麦克风, 麦克 → I2S
 *
 * 16kHz 采样率, 16-bit, 单声道, DMA 双缓冲。
 *
 * @copyright Copyright (c) 2024 Railway Inspection AR Glasses Project
 * @license Apache-2.0
 */

#include "bsp_i2s_audio.h"
#include "app_common.h"

#include "esp_log.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/i2s_std.h"

static const char *TAG = TAG_I2S;

/* =========================================================================
 * 模块内部状态
 * ========================================================================= */
static i2s_chan_handle_t s_tx_handle = NULL;
static i2s_chan_handle_t s_rx_handle = NULL;
static bool s_initialized = false;
static uint8_t s_volume = BSP_AUDIO_VOLUME_DEFAULT;

static bsp_audio_rx_cb_t s_rx_cb = NULL;
static void *s_rx_cb_user_data = NULL;
static TaskHandle_t s_rx_task_handle = NULL;
static bool s_rx_task_running = false;

/* =========================================================================
 * 后台录音任务
 * ========================================================================= */
static void bsp_audio_rx_task(void *arg)
{
    (void)arg;

    uint8_t *rx_buf = heap_caps_malloc(BSP_AUDIO_READ_BUF_SIZE,
                                        MALLOC_CAP_DMA);
    if (rx_buf == NULL) {
        ESP_LOGE(TAG, "录音任务: 分配缓冲区失败");
        vTaskDelete(NULL);
        return;
    }

    ESP_LOGI(TAG, "录音任务已启动 (缓冲区 %d 字节)", BSP_AUDIO_READ_BUF_SIZE);

    while (s_rx_task_running) {
        size_t bytes_read = 0;
        esp_err_t ret = i2s_channel_read(s_rx_handle, rx_buf,
                                          BSP_AUDIO_READ_BUF_SIZE,
                                          &bytes_read, pdMS_TO_TICKS(100));
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "I2S 读取失败: %s", esp_err_to_name(ret));
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }

        if (s_rx_cb && bytes_read > 0) {
            s_rx_cb(rx_buf, bytes_read, s_rx_cb_user_data);
        }
    }

    free(rx_buf);
    ESP_LOGI(TAG, "录音任务已停止");
    s_rx_task_handle = NULL;
    vTaskDelete(NULL);
}

/* =========================================================================
 * 公共接口实现
 * ========================================================================= */

esp_err_t bsp_audio_init(void)
{
    if (s_initialized) {
        ESP_LOGW(TAG, "I2S 音频已初始化, 跳过");
        return ESP_OK;
    }

    esp_err_t ret;

    /* 1. 创建 I2S TX 通道 (MAX98357A 播放) */
    i2s_chan_config_t tx_chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(
        CONFIG_BSP_I2S_NUM, I2S_ROLE_MASTER);
    tx_chan_cfg.dma_desc_num = BSP_AUDIO_DMA_BUF_COUNT;
    tx_chan_cfg.dma_frame_num = BSP_AUDIO_DMA_BUF_LEN;

    ret = i2s_new_channel(&tx_chan_cfg, &s_tx_handle, NULL);
    APP_CHECK(ret, TAG, err_tx, "创建 TX 通道失败: %s", esp_err_to_name(ret));

    /* 2. 配置 TX 标准模式 */
    i2s_std_config_t tx_std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(CONFIG_BSP_I2S_SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
            I2S_DATA_BIT_WIDTH_16BIT,
            I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .bclk = CONFIG_BSP_I2S_TX_BCLK_GPIO,
            .ws   = CONFIG_BSP_I2S_TX_WS_GPIO,
            .dout = CONFIG_BSP_I2S_TX_DOUT_GPIO,
            .din  = -1,
            .mclk = -1,
        },
    };

    ret = i2s_channel_init_std_mode(s_tx_handle, &tx_std_cfg);
    APP_CHECK(ret, TAG, err_tx_init, "TX 标准模式初始化失败: %s",
              esp_err_to_name(ret));

    ret = i2s_channel_enable(s_tx_handle);
    APP_CHECK(ret, TAG, err_tx_init, "TX 通道使能失败: %s",
              esp_err_to_name(ret));

    /* 3. 创建 I2S RX 通道 (INMP441 录音) */
    i2s_chan_config_t rx_chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(
        CONFIG_BSP_I2S_NUM, I2S_ROLE_MASTER);
    rx_chan_cfg.dma_desc_num = BSP_AUDIO_DMA_BUF_COUNT;
    rx_chan_cfg.dma_frame_num = BSP_AUDIO_DMA_BUF_LEN;

    ret = i2s_new_channel(&rx_chan_cfg, NULL, &s_rx_handle);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "创建独立 RX 通道失败 (%s), 使用同一通道 RX",
                 esp_err_to_name(ret));
        /* 如果端口已占用, 尝试复用 — ESP32-S3 单端口下可能需要全双工 */
        s_rx_handle = s_tx_handle;
    }

    if (s_rx_handle != s_tx_handle) {
        /* 独立 RX 通道配置 */
        i2s_std_config_t rx_std_cfg = {
            .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(CONFIG_BSP_I2S_SAMPLE_RATE),
            .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
                I2S_DATA_BIT_WIDTH_16BIT,
                I2S_SLOT_MODE_MONO),
            .gpio_cfg = {
                .bclk = CONFIG_BSP_I2S_RX_BCLK_GPIO,
                .ws   = CONFIG_BSP_I2S_RX_WS_GPIO,
                .dout = -1,
                .din  = CONFIG_BSP_I2S_RX_DIN_GPIO,
                .mclk = -1,
            },
        };

        ret = i2s_channel_init_std_mode(s_rx_handle, &rx_std_cfg);
        APP_CHECK(ret, TAG, err_rx_init, "RX 标准模式初始化失败: %s",
                  esp_err_to_name(ret));

        ret = i2s_channel_enable(s_rx_handle);
        APP_CHECK(ret, TAG, err_rx_init, "RX 通道使能失败: %s",
                  esp_err_to_name(ret));
    }

    s_initialized = true;
    ESP_LOGI(TAG, "I2S 音频初始化完成: %dHz, 16-bit, mono, TX=GPIO%d/%d/%d, RX=GPIO%d/%d/%d",
             CONFIG_BSP_I2S_SAMPLE_RATE,
             CONFIG_BSP_I2S_TX_BCLK_GPIO, CONFIG_BSP_I2S_TX_WS_GPIO,
             CONFIG_BSP_I2S_TX_DOUT_GPIO,
             CONFIG_BSP_I2S_RX_BCLK_GPIO, CONFIG_BSP_I2S_RX_WS_GPIO,
             CONFIG_BSP_I2S_RX_DIN_GPIO);
    return ESP_OK;

err_rx_init:
    i2s_del_channel(s_rx_handle);
    s_rx_handle = NULL;
err_tx_init:
    i2s_del_channel(s_tx_handle);
    s_tx_handle = NULL;
err_tx:
    return ret;
}

esp_err_t bsp_audio_deinit(void)
{
    if (!s_initialized) {
        return ESP_OK;
    }

    bsp_audio_stop_rx_task();

    if (s_rx_handle && s_rx_handle != s_tx_handle) {
        i2s_channel_disable(s_rx_handle);
        i2s_del_channel(s_rx_handle);
        s_rx_handle = NULL;
    }
    if (s_tx_handle) {
        i2s_channel_disable(s_tx_handle);
        i2s_del_channel(s_tx_handle);
        s_tx_handle = NULL;
    }

    s_initialized = false;
    ESP_LOGI(TAG, "I2S 音频已反初始化");
    return ESP_OK;
}

void bsp_audio_set_volume(uint8_t volume)
{
    if (volume > BSP_AUDIO_VOLUME_MAX) {
        volume = BSP_AUDIO_VOLUME_MAX;
    }
    s_volume = volume;
    ESP_LOGD(TAG, "音量设置: %d%%", volume);
}

uint8_t bsp_audio_get_volume(void)
{
    return s_volume;
}

int bsp_audio_read(uint8_t *dest, size_t len)
{
    if (!s_initialized || !s_rx_handle) {
        return -1;
    }

    size_t bytes_read = 0;
    esp_err_t ret = i2s_channel_read(s_rx_handle, dest, len,
                                      &bytes_read, pdMS_TO_TICKS(1000));
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "I2S 读取失败: %s", esp_err_to_name(ret));
        return -1;
    }
    return (int)bytes_read;
}

int bsp_audio_write(const uint8_t *src, size_t len)
{
    if (!s_initialized || !s_tx_handle) {
        return -1;
    }

    size_t bytes_written = 0;

    /* 软件音量增益: 根据 s_volume 缩放 16-bit 采样值 */
    if (s_volume < BSP_AUDIO_VOLUME_MAX) {
        /* 分配临时缓冲区, 应用增益后写入 */
        uint8_t *tmp = heap_caps_malloc(len, MALLOC_CAP_DMA);
        if (tmp == NULL) {
            /* 内存不足, 直接写入原始数据 */
            i2s_channel_write(s_tx_handle, src, len, &bytes_written,
                              pdMS_TO_TICKS(1000));
            return (int)bytes_written;
        }

        memcpy(tmp, src, len);
        float gain = (float)s_volume / BSP_AUDIO_VOLUME_MAX;

        /* 对 16-bit 样本应用增益 (单声道, 小端序) */
        size_t samples = len / 2;
        int16_t *s16 = (int16_t *)tmp;
        for (size_t i = 0; i < samples; i++) {
            int32_t scaled = (int32_t)(s16[i] * gain);
            if (scaled > INT16_MAX) scaled = INT16_MAX;
            if (scaled < INT16_MIN) scaled = INT16_MIN;
            s16[i] = (int16_t)scaled;
        }

        esp_err_t ret = i2s_channel_write(s_tx_handle, tmp, len,
                                           &bytes_written, pdMS_TO_TICKS(1000));
        free(tmp);
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "I2S 写入失败: %s", esp_err_to_name(ret));
            return -1;
        }
    } else {
        /* 100% 音量, 直接写入 */
        esp_err_t ret = i2s_channel_write(s_tx_handle, src, len,
                                           &bytes_written, pdMS_TO_TICKS(1000));
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "I2S 写入失败: %s", esp_err_to_name(ret));
            return -1;
        }
    }

    return (int)bytes_written;
}

esp_err_t bsp_audio_play_beep(void)
{
    if (!s_initialized || !s_tx_handle) {
        return ESP_ERR_INVALID_STATE;
    }

    /* 生成 1kHz 正弦波, 持续 200ms */
    const int freq = 1000;
    const int duration_ms = 200;
    const int samples = (CONFIG_BSP_I2S_SAMPLE_RATE * duration_ms) / 1000;
    const size_t buf_size = samples * sizeof(int16_t);

    int16_t *beep_buf = heap_caps_malloc(buf_size, MALLOC_CAP_DMA);
    if (beep_buf == NULL) {
        ESP_LOGE(TAG, "分配 beep 缓冲区失败");
        return ESP_ERR_NO_MEM;
    }

    /* 生成正弦波, 带 20ms 淡入淡出 */
    const int fade_samples = (CONFIG_BSP_I2S_SAMPLE_RATE * 20) / 1000;
    for (int i = 0; i < samples; i++) {
        float t = (float)i / CONFIG_BSP_I2S_SAMPLE_RATE;
        float amp = 0.5f;  /* 幅度 50% */
        /* 淡入 */
        if (i < fade_samples) {
            amp *= (float)i / fade_samples;
        }
        /* 淡出 */
        if (i > samples - fade_samples) {
            amp *= (float)(samples - i) / fade_samples;
        }
        beep_buf[i] = (int16_t)(amp * 32767.0f *
                                 sinf(2.0f * 3.14159265f * freq * t));
    }

    size_t bytes_written = 0;
    esp_err_t ret = i2s_channel_write(s_tx_handle, beep_buf, buf_size,
                                       &bytes_written, pdMS_TO_TICKS(1000));
    free(beep_buf);

    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "beep 播放失败: %s", esp_err_to_name(ret));
        return ret;
    }

    ESP_LOGI(TAG, "beep 播放完成 (%dHz, %dms)", freq, duration_ms);
    return ESP_OK;
}

void bsp_audio_register_rx_callback(bsp_audio_rx_cb_t cb, void *user_data)
{
    s_rx_cb = cb;
    s_rx_cb_user_data = user_data;
}

esp_err_t bsp_audio_start_rx_task(void)
{
    if (s_rx_task_handle != NULL) {
        ESP_LOGW(TAG, "录音任务已在运行");
        return ESP_OK;
    }

    s_rx_task_running = true;
    BaseType_t ret = xTaskCreatePinnedToCore(
        bsp_audio_rx_task,
        "bsp_audio_rx",
        4096,
        NULL,
        5,  /* 优先级 5 (中等) */
        &s_rx_task_handle,
        0   /* 固定到 Core 0 */
    );

    if (ret != pdPASS) {
        ESP_LOGE(TAG, "创建录音任务失败");
        s_rx_task_running = false;
        return ESP_ERR_NO_MEM;
    }

    return ESP_OK;
}

void bsp_audio_stop_rx_task(void)
{
    if (s_rx_task_handle == NULL) {
        return;
    }

    s_rx_task_running = false;
    /* 等待任务退出 (最多 1 秒) */
    for (int i = 0; i < 100 && s_rx_task_handle != NULL; i++) {
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}
