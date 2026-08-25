/**
 * @file bsp_ov2640.c
 * @brief OV2640 DVP driver implementation — wraps esp32-camera component.
 *
 * The esp32-camera component (vendored as a git submodule — see .gitmodules)
 * handles the low-level SCCB register writes, DVP DMA capture and sensor
 * runtime control. This BSP layer:
 *   * Maps the railway-glasses Pogo Pin DVP/SCCB pinout to a camera_config_t.
 *   * Pre-bakes railway defaults: JPEG@QVGA (320x240), quality 10,
 *     double-buffered frame buffers.
 *   * Exposes a thin bsp_camera_* API used by camera_module.c.
 *
 * Frame buffer lifecycle:
 *   The esp32-camera component returns camera_fb_t* objects from a fixed
 *   pool. bsp_camera_capture() stores that pointer in a module-static
 *   single-slot "loan table" so the application's bsp_camera_fb_t descriptor
 *   does not have to expose the underlying type. bsp_camera_return_frame()
 *   consumes the loan and hands the buffer back to the pool. The railway
 *   capture path is single-threaded so a single-slot table is sufficient.
 *
 * All pin numbers can be overridden at sdkconfig time by defining the
 * corresponding CONFIG_BSP_CAM_* options (see the fallback block below).
 */
#include "bsp_ov2640.h"
#include "app_common.h"

#include <string.h>

#include "esp_log.h"
#include "esp_err.h"
#include "esp_timer.h"

#include "esp_camera.h"

static const char *TAG = TAG_CAMERA;

/* ------------------------------------------------------------------------- *
 * Pin configuration — fallback defaults (override via sdkconfig).
 *
 * The Pogo Pin connector routes the OV2640 DVP signals to ESP32-S3 GPIOs.
 * These defaults match a typical ESP32-S3 + OV2640 board layout. Override
 * by adding CONFIG_BSP_CAM_* options to Kconfig.projbuild.
 * ------------------------------------------------------------------------- */

#ifndef CONFIG_BSP_CAM_PIN_D0
#define CONFIG_BSP_CAM_PIN_D0     4
#endif
#ifndef CONFIG_BSP_CAM_PIN_D1
#define CONFIG_BSP_CAM_PIN_D1     5
#endif
#ifndef CONFIG_BSP_CAM_PIN_D2
#define CONFIG_BSP_CAM_PIN_D2     6
#endif
#ifndef CONFIG_BSP_CAM_PIN_D3
#define CONFIG_BSP_CAM_PIN_D3     7
#endif
#ifndef CONFIG_BSP_CAM_PIN_D4
#define CONFIG_BSP_CAM_PIN_D4    14
#endif
#ifndef CONFIG_BSP_CAM_PIN_D5
#define CONFIG_BSP_CAM_PIN_D5    15
#endif
#ifndef CONFIG_BSP_CAM_PIN_D6
#define CONFIG_BSP_CAM_PIN_D6    16
#endif
#ifndef CONFIG_BSP_CAM_PIN_D7
#define CONFIG_BSP_CAM_PIN_D7    17
#endif
#ifndef CONFIG_BSP_CAM_PIN_VSYNC
#define CONFIG_BSP_CAM_PIN_VSYNC 18
#endif
#ifndef CONFIG_BSP_CAM_PIN_HREF
#define CONFIG_BSP_CAM_PIN_HREF  12
#endif
#ifndef CONFIG_BSP_CAM_PIN_PCLK
#define CONFIG_BSP_CAM_PIN_PCLK  11
#endif
#ifndef CONFIG_BSP_CAM_PIN_XCLK
#define CONFIG_BSP_CAM_PIN_XCLK  10
#endif
#ifndef CONFIG_BSP_CAM_PIN_SIOD
#define CONFIG_BSP_CAM_PIN_SIOD   8    /* SCCB SDA */
#endif
#ifndef CONFIG_BSP_CAM_PIN_SIOC
#define CONFIG_BSP_CAM_PIN_SIOC   9    /* SCCB SCL */
#endif
#ifndef CONFIG_BSP_CAM_PIN_PWDN
#define CONFIG_BSP_CAM_PIN_PWDN  -1    /* unused on the OV2640 module */
#endif
#ifndef CONFIG_BSP_CAM_PIN_RESET
#define CONFIG_BSP_CAM_PIN_RESET -1
#endif

/* ------------------------------------------------------------------------- *
 * Module state
 * ------------------------------------------------------------------------- */

static struct {
    bool              initialised;
    camera_config_t   config;
    uint8_t           jpeg_quality;
    uint16_t          width;
    uint16_t          height;
    camera_fb_t      *loan;             /* currently-loaned esp32-camera fb */
} s = {0};

/* ------------------------------------------------------------------------- *
 * Helper: build a camera_config_t from the railway defaults.
 * ------------------------------------------------------------------------- */

static void build_default_config(camera_config_t *c)
{
    memset(c, 0, sizeof(*c));

    /* DVP 8-bit data bus. */
    c->pin_d0    = CONFIG_BSP_CAM_PIN_D0;
    c->pin_d1    = CONFIG_BSP_CAM_PIN_D1;
    c->pin_d2    = CONFIG_BSP_CAM_PIN_D2;
    c->pin_d3    = CONFIG_BSP_CAM_PIN_D3;
    c->pin_d4    = CONFIG_BSP_CAM_PIN_D4;
    c->pin_d5    = CONFIG_BSP_CAM_PIN_D5;
    c->pin_d6    = CONFIG_BSP_CAM_PIN_D6;
    c->pin_d7    = CONFIG_BSP_CAM_PIN_D7;
    c->pin_vsync = CONFIG_BSP_CAM_PIN_VSYNC;
    c->pin_href  = CONFIG_BSP_CAM_PIN_HREF;
    c->pin_pclk  = CONFIG_BSP_CAM_PIN_PCLK;

    /* XCLK driven by LEDC inside esp32-camera (separate channel from
     * the LCD backlight to avoid contention). */
    c->pin_xclk  = CONFIG_BSP_CAM_PIN_XCLK;
    c->xclk_freq_hz = BSP_CAM_XCLK_FREQ_HZ;
    c->ledc_timer   = LEDC_TIMER_1;
    c->ledc_channel = LEDC_CHANNEL_1;

    /* SCCB (I2C-like) bus for register config. */
    c->pin_sccb_sda = CONFIG_BSP_CAM_PIN_SIOD;
    c->pin_sccb_scl = CONFIG_BSP_CAM_PIN_SIOC;
    c->sccb_i2c_port = 0;

    /* Power-down / reset unused. */
    c->pin_pwdn  = CONFIG_BSP_CAM_PIN_PWDN;
    c->pin_reset = CONFIG_BSP_CAM_PIN_RESET;

    /* Format / resolution / quality. */
    c->pixel_format  = PIXFORMAT_JPEG;
    c->frame_size    = FRAMESIZE_QVGA;     /* 320x240 */
    c->jpeg_quality  = BSP_CAM_JPEG_QUALITY;
    c->fb_count      = BSP_CAM_FB_COUNT;
#if CONFIG_SPIRAM
    c->fb_location   = CAMERA_FB_IN_PSRAM;
#else
    c->fb_location   = CAMERA_FB_IN_DRAM;
#endif
}

/* ------------------------------------------------------------------------- *
 * Public API
 * ------------------------------------------------------------------------- */

esp_err_t bsp_camera_init(const bsp_camera_config_t *cfg)
{
    if (s.initialised) {
        return ESP_ERR_INVALID_STATE;
    }

    build_default_config(&s.config);

    /* Apply caller overrides. */
    if (cfg != NULL) {
        if (cfg->width == 320 && cfg->height == 240) {
            s.config.frame_size = FRAMESIZE_QVGA;
        } else if (cfg->width == 640 && cfg->height == 480) {
            s.config.frame_size = FRAMESIZE_VGA;
        } else if (cfg->width == 800 && cfg->height == 600) {
            s.config.frame_size = FRAMESIZE_SVGA;
        } else if (cfg->width > 0U && cfg->height > 0U) {
            ESP_LOGW(TAG, "unsupported %ux%u — defaulting to QVGA",
                     cfg->width, cfg->height);
        }
        if (cfg->jpeg_quality <= 63U) {
            s.config.jpeg_quality = cfg->jpeg_quality;
        }
        if (cfg->use_psram) {
            s.config.fb_location = CAMERA_FB_IN_PSRAM;
        }
    }

    s.jpeg_quality = s.config.jpeg_quality;
    switch (s.config.frame_size) {
    case FRAMESIZE_QVGA: s.width = 320; s.height = 240; break;
    case FRAMESIZE_VGA:  s.width = 640; s.height = 480; break;
    case FRAMESIZE_SVGA: s.width = 800; s.height = 600; break;
    default:             s.width = 320; s.height = 240; break;
    }

    esp_err_t ret = esp_camera_init(&s.config);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "esp_camera_init failed: %s", esp_err_to_name(ret));
        return ret;
    }

    s.initialised = true;
    ESP_LOGI(TAG, "OV2640 initialised: %ux%u JPEG q=%u, xclk=%uHz, "
                  "fb_count=%d (location=%d)",
             s.width, s.height, s.jpeg_quality,
             s.config.xclk_freq_hz, s.config.fb_count,
             (int)s.config.fb_location);
    APP_SET_BIT(EVT_BIT_CAMERA_READY);
    return ESP_OK;
}

esp_err_t bsp_camera_deinit(void)
{
    if (!s.initialised) {
        return ESP_OK;
    }
    /* Return any loaned frame to the pool before tearing down. */
    if (s.loan != NULL) {
        esp_camera_fb_return(s.loan);
        s.loan = NULL;
    }
    esp_err_t ret = esp_camera_deinit();
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "esp_camera_deinit: %s", esp_err_to_name(ret));
    }
    s.initialised = false;
    if (g_sys_events != NULL) {
        xEventGroupClearBits(g_sys_events, EVT_BIT_CAMERA_READY);
    }
    ESP_LOGI(TAG, "OV2640 de-initialised");
    return ret;
}

esp_err_t bsp_camera_capture(bsp_camera_fb_t *out_fb)
{
    if (out_fb == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!s.initialised) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s.loan != NULL) {
        ESP_LOGW(TAG, "previous frame not returned — auto-returning");
        esp_camera_fb_return(s.loan);
        s.loan = NULL;
    }

    camera_fb_t *fb = esp_camera_fb_get();
    if (fb == NULL) {
        ESP_LOGW(TAG, "esp_camera_fb_get returned NULL (timeout?)");
        return APP_ERR_CAMERA;
    }
    s.loan = fb;
    out_fb->buf          = fb->buf;
    out_fb->len          = fb->len;
    out_fb->timestamp_ms = (uint32_t)(esp_timer_get_time() / 1000);
    out_fb->width        = s.width;
    out_fb->height       = s.height;
    return ESP_OK;
}

esp_err_t bsp_camera_return_frame(bsp_camera_fb_t *fb)
{
    if (fb == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s.loan == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    esp_camera_fb_return(s.loan);
    s.loan = NULL;
    fb->buf = NULL;
    fb->len = 0U;
    return ESP_OK;
}

esp_err_t bsp_camera_set_jpeg_quality(uint8_t quality)
{
    if (quality > 63U) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!s.initialised) {
        return ESP_ERR_INVALID_STATE;
    }
    sensor_t *sensor = esp_camera_sensor_get();
    if (sensor == NULL || sensor->set_quality == NULL) {
        return APP_ERR_NOT_SUPPORTED;
    }
    int ret = sensor->set_quality(sensor, (int)quality);
    if (ret != 0) {
        ESP_LOGW(TAG, "set_quality failed: %d", ret);
        return ESP_FAIL;
    }
    s.jpeg_quality = quality;
    ESP_LOGI(TAG, "JPEG quality set to %u", quality);
    return ESP_OK;
}

uint8_t bsp_camera_get_jpeg_quality(void)
{
    return s.jpeg_quality;
}

bool bsp_camera_is_ready(void)
{
    return s.initialised;
}
