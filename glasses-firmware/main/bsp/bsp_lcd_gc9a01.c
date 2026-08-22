/**
 * @file bsp_lcd_gc9a01.c
 * @brief GC9A01 SPI LCD 驱动实现 — 240x240 RGB565 圆形显示屏
 *
 * 实现 SPI 初始化 (40MHz)、GC9A01 寄存器配置序列、
 * RGB565 像素写入、背光 PWM 调光和 LVGL flush 回调。
 *
 * @copyright Copyright (c) 2024 Railway Inspection AR Glasses Project
 * @license Apache-2.0
 */

#include "bsp_lcd_gc9a01.h"
#include "app_common.h"

#include "esp_log.h"
#include "esp_heap_caps.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_dev.h"
#include "driver/spi_master.h"
#include "driver/gpio.h"
#include "driver/ledc.h"

static const char *TAG = TAG_LCD;

/* =========================================================================
 * 模块内部状态
 * ========================================================================= */
static esp_lcd_panel_handle_t s_panel_handle = NULL;
static esp_lcd_panel_io_handle_t s_io_handle = NULL;
static spi_device_handle_t s_spi_handle = NULL;
static bool s_initialized = false;
static uint8_t s_brightness = 128;

static bsp_lcd_flush_done_cb_t s_flush_done_cb = NULL;
static void *s_flush_done_user_data = NULL;

/* =========================================================================
 * 背光 PWM (LEDC) 配置
 * ========================================================================= */
#define BL_LEDC_TIMER       LEDC_TIMER_0
#define BL_LEDC_MODE        LEDC_LOW_SPEED_MODE
#define BL_LEDC_CHANNEL     LEDC_CHANNEL_0
#define BL_LEDC_DUTY_RES    LEDC_TIMER_8_BIT  /* 8 位分辨率 0-255 */

/**
 * @brief 初始化背光 LEDC PWM
 */
static esp_err_t bsp_lcd_backlight_init(void)
{
    ledc_timer_config_t timer_cfg = {
        .speed_mode      = BL_LEDC_MODE,
        .timer_num       = BL_LEDC_TIMER,
        .duty_resolution = BL_LEDC_DUTY_RES,
        .freq_hz         = CONFIG_BSP_LCD_BL_PWM_FREQ_HZ,
        .clk_cfg         = LEDC_AUTO_CLK,
    };
    esp_err_t ret = ledc_timer_config(&timer_cfg);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "LEDC 定时器配置失败: %s", esp_err_to_name(ret));
        return ret;
    }

    ledc_channel_config_t ch_cfg = {
        .gpio_num   = CONFIG_BSP_LCD_BL_GPIO,
        .speed_mode = BL_LEDC_MODE,
        .channel    = BL_LEDC_CHANNEL,
        .intr_type  = LEDC_INTR_DISABLE,
        .timer_sel  = BL_LEDC_TIMER,
        .duty       = s_brightness,
        .hpoint     = 0,
    };
    ret = ledc_channel_config(&ch_cfg);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "LEDC 通道配置失败: %s", esp_err_to_name(ret));
        return ret;
    }

    ESP_LOGI(TAG, "背光 PWM 初始化完成 (GPIO%d, %dHz, 8-bit)",
             CONFIG_BSP_LCD_BL_GPIO, CONFIG_BSP_LCD_BL_PWM_FREQ_HZ);
    return ESP_OK;
}

/* =========================================================================
 * GC9A01 初始化命令序列
 * ========================================================================= */

/** GC9A01 初始化命令结构 */
typedef struct {
    uint8_t cmd;        /**< 命令字节 */
    uint8_t data[16];  /**< 参数数据 */
    uint8_t data_len;  /**< 参数长度 */
} gc9a01_init_cmd_t;

/**
 * GC9A01 初始化命令序列 (RGB565, 240x240)
 * 参考 GC9A01 数据手册和开源初始化代码。
 */
static const gc9a01_init_cmd_t s_gc9a01_init_cmds[] = {
    /* 软件复位 */
    {GC9A01_CMD_SWRESET, {0}, 0},
    /* 延迟 120ms (由调用方处理) */

    /* 退出睡眠模式 */
    {GC9A01_CMD_SLPOUT, {0}, 0},
    /* 延迟 120ms */

    /* 像素格式: RGB565 (16-bit/pixel) */
    {GC9A01_CMD_COLMOD, {0x05}, 1},

    /* 内存访问控制: MX 翻转 (适配屏幕方向) */
    {GC9A01_CMD_MADCTL, {MADCTL_MX | MADCTL_BGR}, 1},

    /* 帧率控制: 60Hz */
    {GC9A01_CMD_FRMCTR1, {0x00, 0x06, 0x03}, 3},

    /* 显示功能控制 */
    {GC9A01_CMD_DFUNCTR, {0x08, 0x82, 0x27}, 3},

    /* 电源控制 1 */
    {GC9A01_CMD_PWCTR1, {0x24}, 1},

    /* 电源控制 2 */
    {GC9A01_CMD_PWCTR2, {0x02}, 1},

    /* VCOM 控制 */
    {GC9A01_CMD_VMCTR1, {0x3C, 0x38}, 2},

    /* 伽马校正 (正极性) */
    {GC9A01_CMD_GAMCTRP1, {
        0x45, 0x09, 0x08, 0x08, 0x26, 0x2A, 0x25, 0x2E,
        0x26, 0x2F, 0x2B, 0x38, 0x00, 0x00, 0x02, 0x0A
    }, 16},

    /* 伽马校正 (负极性) */
    {GC9A01_CMD_GAMCTRN1, {
        0x0B, 0x24, 0x25, 0x25, 0x1C, 0x1B, 0x1B, 0x1B,
        0x1A, 0x1F, 0x22, 0x2E, 0x00, 0x00, 0x03, 0x0A
    }, 16},

    /* 开启显示 */
    {GC9A01_CMD_DISPON, {0}, 0},
};

/* =========================================================================
 * SPI 初始化
 * ========================================================================= */
static esp_err_t bsp_lcd_spi_init(void)
{
    spi_bus_config_t buscfg = {
        .miso_io_num     = -1,  /* GC9A01 仅需写, 不接 MISO */
        .mosi_io_num     = CONFIG_BSP_LCD_SPI_MOSI_GPIO,
        .sclk_io_num     = CONFIG_BSP_LCD_SPI_CLK_GPIO,
        .quadwp_io_num   = -1,
        .quadhd_io_num   = -1,
        .max_transfer_sz = BSP_LCD_H_RES * BSP_LCD_BUFFER_LINES *
                           BSP_LCD_BITS_PER_PIXEL / 8,
    };

    ESP_LOGI(TAG, "初始化 SPI%d (MOSI=%d, CLK=%d, %dMHz)",
             CONFIG_BSP_LCD_SPI_HOST,
             CONFIG_BSP_LCD_SPI_MOSI_GPIO,
             CONFIG_BSP_LCD_SPI_CLK_GPIO,
             CONFIG_BSP_LCD_SPI_FREQ_HZ / 1000000);

    esp_err_t ret = spi_bus_initialize(
        CONFIG_BSP_LCD_SPI_HOST, &buscfg, SPI_DMA_CH_AUTO);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "SPI 总线初始化失败: %s", esp_err_to_name(ret));
        return ret;
    }

    return ESP_OK;
}

/* =========================================================================
 * 公共接口实现
 * ========================================================================= */

esp_err_t bsp_lcd_init(void)
{
    if (s_initialized) {
        ESP_LOGW(TAG, "LCD 已初始化, 跳过");
        return ESP_OK;
    }

    esp_err_t ret;

    /* 1. 初始化背光 PWM */
    ret = bsp_lcd_backlight_init();
    APP_CHECK(ret, TAG, err, "背光初始化失败");

    /* 2. 初始化 SPI 总线 */
    ret = bsp_lcd_spi_init();
    APP_CHECK(ret, TAG, err, "SPI 初始化失败");

    /* 3. 配置 esp_lcd IO 接口 */
    esp_lcd_panel_io_spi_config_t io_config = {
        .dc_gpio_num       = CONFIG_BSP_LCD_DC_GPIO,
        .cs_gpio_num       = CONFIG_BSP_LCD_CS_GPIO,
        .pclk_hz           = CONFIG_BSP_LCD_SPI_FREQ_HZ,
        .lcd_cmd_bits      = 8,
        .lcd_param_bits    = 8,
        .spi_mode          = 0,
        .trans_queue_depth = 10,
        .on_color_trans_done = NULL,
        .user_ctx          = NULL,
    };

    ret = esp_lcd_new_panel_io_spi(
        (esp_lcd_spi_bus_handle_t)CONFIG_BSP_LCD_SPI_HOST,
        &io_config, &s_io_handle);
    APP_CHECK(ret, TAG, err, "面板 IO 创建失败");

    /* 4. 配置 RST 引脚 (通过通用面板配置) */
    esp_lcd_panel_dev_config_t panel_config = {
        .reset_gpio_num = CONFIG_BSP_LCD_RST_GPIO,
        .color_space    = ESP_LCD_COLOR_SPACE_BGR,
        .bits_per_pixel = BSP_LCD_BITS_PER_PIXEL,
    };

    /* 5. 创建 GC9A01 面板 (使用通用 RGB 面板 + 自定义初始化) */
    ret = esp_lcd_new_panel_gc9a01(s_io_handle, &panel_config, &s_panel_handle);
    if (ret != ESP_OK) {
        /* 如果 esp_lcd 不支持 GC9A01 内置驱动, 手动发送初始化序列 */
        ESP_LOGW(TAG, "esp_lcd_new_panel_gc9a01 不可用 (%s), 使用手动初始化",
                esp_err_to_name(ret));

        /* 手动复位 */
        if (CONFIG_BSP_LCD_RST_GPIO >= 0) {
            gpio_config_t rst_conf = {
                .pin_bit_mask = (1ULL << CONFIG_BSP_LCD_RST_GPIO),
                .mode = GPIO_MODE_OUTPUT,
                .pull_up_en = GPIO_PULLUP_DISABLE,
                .pull_down_en = GPIO_PULLDOWN_DISABLE,
                .intr_type = GPIO_INTR_DISABLE,
            };
            gpio_config(&rst_conf);
            gpio_set_level(CONFIG_BSP_LCD_RST_GPIO, 0);
            vTaskDelay(pdMS_TO_TICKS(100));
            gpio_set_level(CONFIG_BSP_LCD_RST_GPIO, 1);
            vTaskDelay(pdMS_TO_TICKS(120));
        }

        /* 手动发送初始化命令序列 */
        size_t cmd_count = sizeof(s_gc9a01_init_cmds) / sizeof(s_gc9a01_init_cmds[0]);
        for (size_t i = 0; i < cmd_count; i++) {
            const gc9a01_init_cmd_t *cmd = &s_gc9a01_init_cmds[i];
            ret = esp_lcd_panel_io_tx_param(s_io_handle, cmd->cmd,
                                             cmd->data, cmd->data_len);
            if (ret != ESP_OK) {
                ESP_LOGE(TAG, "发送初始化命令 0x%02X 失败: %s",
                         cmd->cmd, esp_err_to_name(ret));
                goto err;
            }

            /* SWRESET 和 SLPOUT 后需要延迟 */
            if (cmd->cmd == GC9A01_CMD_SWRESET) {
                vTaskDelay(pdMS_TO_TICKS(120));
            } else if (cmd->cmd == GC9A01_CMD_SLPOUT) {
                vTaskDelay(pdMS_TO_TICKS(120));
            } else if (cmd->cmd == GC9A01_CMD_DISPON) {
                vTaskDelay(pdMS_TO_TICKS(50));
            }
        }

        ESP_LOGI(TAG, "GC9A01 手动初始化完成 (%d 条命令)", cmd_count);
    } else {
        /* 使用 esp_lcd 内置 GC9A01 驱动: reset + init */
        ret = esp_lcd_panel_reset(s_panel_handle);
        APP_CHECK(ret, TAG, err, "面板复位失败");

        ret = esp_lcd_panel_init(s_panel_handle);
        APP_CHECK(ret, TAG, err, "面板初始化失败");

        /* 退出睡眠 */
        ret = esp_lcd_panel_disp_on(s_panel_handle, true);
        APP_CHECK(ret, TAG, err, "开启显示失败");
    }

    /* 6. 设置颜色反转 (关闭) */
    if (s_panel_handle) {
        esp_lcd_panel_invert_color(s_panel_handle, false);
    }

    s_initialized = true;
    ESP_LOGI(TAG, "GC9A01 LCD 初始化完成: %dx%d RGB565, SPI %dMHz",
             BSP_LCD_H_RES, BSP_LCD_V_RES,
             CONFIG_BSP_LCD_SPI_FREQ_HZ / 1000000);
    return ESP_OK;

err:
    if (s_io_handle) {
        esp_lcd_panel_io_del(s_io_handle);
        s_io_handle = NULL;
    }
    return ret;
}

esp_err_t bsp_lcd_deinit(void)
{
    if (!s_initialized) {
        return ESP_OK;
    }

    if (s_panel_handle) {
        esp_lcd_panel_del(s_panel_handle);
        s_panel_handle = NULL;
    }
    if (s_io_handle) {
        esp_lcd_panel_io_del(s_io_handle);
        s_io_handle = NULL;
    }

    spi_bus_free(CONFIG_BSP_LCD_SPI_HOST);
    ledc_stop(BL_LEDC_MODE, BL_LEDC_CHANNEL, 0);

    s_initialized = false;
    ESP_LOGI(TAG, "LCD 已反初始化");
    return ESP_OK;
}

void bsp_lcd_set_brightness(uint8_t brightness)
{
    s_brightness = brightness;
    if (s_initialized) {
        ledc_set_duty(BL_LEDC_MODE, BL_LEDC_CHANNEL, brightness);
        ledc_update_duty(BL_LEDC_MODE, BL_LEDC_CHANNEL);
    }
    ESP_LOGD(TAG, "背光亮度设置: %d", brightness);
}

uint8_t bsp_lcd_get_brightness(void)
{
    return s_brightness;
}

void bsp_lcd_fill_color(uint16_t color)
{
    if (!s_initialized || !s_panel_handle) {
        return;
    }

    /* 分配单行缓冲区, 逐行填充 */
    uint16_t *line_buf = heap_caps_malloc(
        BSP_LCD_H_RES * sizeof(uint16_t), MALLOC_CAP_DMA);
    if (line_buf == NULL) {
        ESP_LOGE(TAG, "分配行缓冲失败");
        return;
    }

    for (int i = 0; i < BSP_LCD_H_RES; i++) {
        line_buf[i] = color;
    }

    for (int y = 0; y < BSP_LCD_V_RES; y++) {
        esp_lcd_panel_draw_bitmap(s_panel_handle, 0, y,
                                  BSP_LCD_H_RES, y + 1, line_buf);
    }

    free(line_buf);
}

void bsp_lcd_draw_bitmap(int x, int y, int w, int h, const uint16_t *data)
{
    if (!s_initialized || !s_panel_handle) {
        return;
    }
    esp_lcd_panel_draw_bitmap(s_panel_handle, x, y, x + w, y + h, data);
}

esp_lcd_panel_handle_t bsp_lcd_get_panel(void)
{
    return s_panel_handle;
}

void bsp_lcd_lvgl_flush_cb(void *drv, const void *area, const void *color_map)
{
    (void)drv;

    if (!s_initialized || !s_panel_handle) {
        goto done;
    }

    /* area 是 lv_area_t: {x1, y1, x2, y2} */
    const int *a = (const int *)area;
    int x1 = a[0];
    int y1 = a[1];
    int x2 = a[2];
    int y2 = a[3];

    int w = x2 - x1 + 1;
    int h = y2 - y1 + 1;

    esp_lcd_panel_draw_bitmap(s_panel_handle, x1, y1, x2 + 1, y2 + 1, color_map);

done:
    /* 通知 LVGL 刷新完成 */
    if (s_flush_done_cb) {
        s_flush_done_cb(s_flush_done_user_data);
    }
}

void bsp_lcd_register_flush_done_cb(bsp_lcd_flush_done_cb_t cb, void *user_data)
{
    s_flush_done_cb = cb;
    s_flush_done_user_data = user_data;
}

/* esp_lcd GC9A01 驱动声明 (ESP-IDF v5.1 可能内置) */
/* 如果不存在则上面会走手动初始化分支 */
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 1, 0)
/* esp_lcd_panel_gc9a01.h 在较新版本中提供 */
#endif
