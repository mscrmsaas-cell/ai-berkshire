/**
 * @file bsp_ov2640.h
 * @brief OV2640 DVP camera driver interface — SCCB config, JPEG capture.
 *
 * Wraps the esp32-camera component (vendored as a git submodule — see
 * .gitmodules) with railway-glasses-specific defaults: JPEG format,
 * QVGA (320x240) resolution (matches the round LCD aspect), quality 10.
 *
 * Hardware:
 *   - OV2640 image sensor connected via the Pogo Pin 8-bit DVP parallel
 *     bus (D0..D7, VSYNC, HREF, PCLK).
 *   - SCCB configuration bus (2-wire I2C-like) over the same Pogo Pin
 *     SDA/SCL pair.
 *   - XCLK driven by ESP32-S3 LEDC or a dedicated CLK_OUT pin.
 *
 * Connection lifecycle:
 *   1. Pogo Pin attach event fires (bsp_pogo_pin) → main.c sets
 *      EVT_BIT_CAMERA_ATTACHED.
 *   2. camera_module.c calls bsp_camera_init() to bring the OV2640 up.
 *   3. Each capture: bsp_camera_capture() returns a JPEG frame buffer.
 *   4. Pogo Pin detach → bsp_camera_deinit() releases the resource.
 *
 * @copyright Copyright (c) 2024 Railway Inspection AR Glasses Project
 * @license Apache-2.0
 */
#ifndef BSP_OV2640_H
#define BSP_OV2640_H

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* =========================================================================
 * Camera specifications
 * ========================================================================= */

#define BSP_CAM_H_RES         320    /**< Capture width (QVGA)        */
#define BSP_CAM_V_RES         240    /**< Capture height (QVGA)       */
#define BSP_CAM_JPEG_QUALITY  10     /**< 0-63; lower = higher quality */
#define BSP_CAM_XCLK_FREQ_HZ  20000000  /**< 20 MHz external clock */
#define BSP_CAM_FB_COUNT       2      /**< Double-buffered frame buffers */

/* =========================================================================
 * Types
 * ========================================================================= */

/**
 * @brief Owned JPEG frame buffer returned by bsp_camera_capture().
 *
 * The buffer is loaned to the caller until bsp_camera_return_frame() is
 * called. Do not free the pointer — the underlying esp32-camera pool
 * manages its lifetime.
 */
typedef struct {
    uint8_t *buf;      /**< Pointer to JPEG data            */
    size_t   len;      /**< JPEG payload length in bytes    */
    uint32_t timestamp_ms; /**< Capture timestamp (millis)  */
    uint16_t width;    /**< Source frame width              */
    uint16_t height;   /**< Source frame height             */
} bsp_camera_fb_t;

/**
 * @brief Camera capture configuration (resolution / quality overrides).
 */
typedef struct {
    uint16_t width;
    uint16_t height;
    uint8_t  jpeg_quality;   /**< 0-63 (lower = higher quality) */
    bool     use_psram;      /**< Allocate frame buffers from PSRAM */
} bsp_camera_config_t;

/* =========================================================================
 * Public API
 * ========================================================================= */

/**
 * @brief Initialise the OV2640 over DVP + SCCB.
 *
 * Configures SCCB SDA/SCL, DVP 8-bit data bus, VSYNC/HREF/PCLK, XCLK, then
 * pushes the OV2640 register sequence for JPEG@QVGA. Idempotent — calling
 * again while already initialised returns ESP_ERR_INVALID_STATE.
 *
 * @param cfg  Capture configuration (NULL = use defaults: QVGA, q=10).
 * @return ESP_OK on success, ESP_ERR_INVALID_STATE if already initialised,
 *         or another error code on hardware failure.
 */
esp_err_t bsp_camera_init(const bsp_camera_config_t *cfg);

/**
 * @brief De-initialise the camera (release DVP/SCCB resources).
 *
 * Called on Pogo Pin detach. Safe to call when not initialised.
 */
esp_err_t bsp_camera_deinit(void);

/**
 * @brief Capture a single JPEG frame.
 *
 * Blocks until a complete frame is available (typ. 33-100 ms at 20 MHz XCLK).
 * The returned buffer must be released with bsp_camera_return_frame().
 *
 * @param out_fb  Receives the frame descriptor.
 * @return ESP_OK on success, ESP_ERR_INVALID_STATE if not initialised,
 *         ESP_ERR_TIMEOUT if no frame arrived in time.
 */
esp_err_t bsp_camera_capture(bsp_camera_fb_t *out_fb);

/**
 * @brief Return a frame buffer to the esp32-camera pool.
 *
 * Must be called exactly once per successful bsp_camera_capture().
 */
esp_err_t bsp_camera_return_frame(bsp_camera_fb_t *fb);

/**
 * @brief Set the JPEG quality (0-63) at runtime without re-initialising.
 *
 * Adjusts the OV2640 quantization table via SCCB. The next capture uses
 * the new quality setting.
 */
esp_err_t bsp_camera_set_jpeg_quality(uint8_t quality);

/**
 * @brief Get the configured JPEG quality.
 */
uint8_t bsp_camera_get_jpeg_quality(void);

/**
 * @brief @return true if the camera is initialised and ready to capture.
 */
bool bsp_camera_is_ready(void);

#ifdef __cplusplus
}
#endif

#endif /* BSP_OV2640_H */
