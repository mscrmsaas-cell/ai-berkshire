/**
 * @file camera_module.c
 * @brief Camera coordination: Pogo Pin events → OV2640 init → JPEG capture → BLE send.
 *
 * This module is the glue between the hot-pluggable camera hardware and the
 * BLE transport. Its lifecycle:
 *
 *   1. On init: register a Pogo Pin callback and a BLE handler for
 *      MSG_TAKE_PHOTO / MSG_DETECT_REQUEST.
 *   2. Pogo Pin ATTACH → bsp_camera_init() → notify peer with
 *      MSG_CAMERA_ATTACHED → set EVT_BIT_CAMERA_READY.
 *   3. Pogo Pin DETACH → bsp_camera_deinit() → notify peer with
 *      MSG_CAMERA_DETACHED → clear event bits.
 *   4. MSG_TAKE_PHOTO from peer → capture one JPEG frame → fragment and send
 *      as MSG_CAMERA_FRAME chunks + MSG_CAMERA_FRAME_END terminator.
 *   5. MSG_DETECT_REQUEST from peer → same capture flow but with an extra
 *      quality / resolution hint in the payload.
 *
 * Fragmentation: the BLE NUS transport layer (ble_transport.c) already
 * handles fragmentation of large payloads. The camera module simply calls
 * ble_transport_send(MSG_CAMERA_FRAME, jpeg_data, jpeg_len, false) for the
 * frame body and a separate MSG_CAMERA_FRAME_END with a 4-byte total-size
 * trailer. The transport slices the body into BLE_NUS_MAX_FRAME_PAYLOAD
 * chunks automatically. The "reliable=false" flag means fire-and-forget
 * (no per-chunk ACK) — JPEG is tolerant of occasional drops.
 *
 * @copyright Copyright (c) 2024 Railway Inspection AR Glasses Project
 * @license Apache-2.0
 */

#include "app_common.h"
#include "bsp_pogo_pin.h"
#include "bsp_ov2640.h"
#include "ble_transport.h"
#include "message_types.h"

#include <string.h>

#include "esp_log.h"
#include "esp_err.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

static const char *TAG = TAG_CAMERA;

/* ------------------------------------------------------------------------- *
 * Configuration
 * ------------------------------------------------------------------------- */

/** Capture task stack size (bytes). */
#define CAM_TASK_STACK      4096U

/** Capture task priority (below BLE transport, above idle). */
#define CAM_TASK_PRIORITY   5U

/** Command queue depth (one outstanding capture request at a time). */
#define CAM_CMD_QUEUE_LEN   4U

/** Maximum JPEG frame we will attempt to send (reject larger). */
#define CAM_MAX_JPEG_BYTES  61440U  /* 60 KB */

/** Default JPEG quality when peer does not specify. */
#define CAM_DEFAULT_QUALITY  10U

/* ------------------------------------------------------------------------- *
 * Internal types
 * ------------------------------------------------------------------------- */

typedef enum {
    CAM_CMD_NONE = 0,
    CAM_CMD_TAKE_PHOTO,     /**< MSG_TAKE_PHOTO from peer */
    CAM_CMD_DETECT_REQUEST, /**< MSG_DETECT_REQUEST from peer */
} camera_cmd_t;

typedef struct {
    camera_cmd_t cmd;
    uint8_t      quality;     /**< 0 = use default */
    uint16_t     width;       /**< 0 = use default */
    uint16_t     height;      /**< 0 = use default */
} camera_cmd_msg_t;

/* ------------------------------------------------------------------------- *
 * Module state
 * ------------------------------------------------------------------------- */

static struct {
    bool              initialised;
    bool              camera_hardware_ready;  /**< OV2640 initialised */
    SemaphoreHandle_t lock;                    /**< Guards capture path */
    QueueHandle_t      cmd_queue;              /**< Pending capture requests */
    TaskHandle_t       capture_task;           /**< Dedicated capture task  */
    bsp_camera_fb_t   fb;                      /**< Loaned frame descriptor  */
} s = {0};

/* ------------------------------------------------------------------------- *
 * Forward declarations
 * ------------------------------------------------------------------------- */

static void on_pogo_event(bsp_pogo_event_t event, void *user_data);
static void on_ble_take_photo(uint8_t msg_type, uint16_t seq,
                              const uint8_t *payload, uint16_t len,
                              void *user_ctx);
static void camera_capture_task(void *arg);
static esp_err_t send_frame_over_ble(const bsp_camera_fb_t *fb);

/* ------------------------------------------------------------------------- *
 * Pogo Pin event handler
 * ------------------------------------------------------------------------- */

static void on_pogo_event(bsp_pogo_event_t event, void *user_data)
{
    (void)user_data;

    if (event == BSP_POGO_EVENT_ATTACHED) {
        ESP_LOGI(TAG, "Pogo Pin attached — initialising OV2640");

        bsp_camera_config_t cfg = {
            .width        = BSP_CAM_H_RES,
            .height       = BSP_CAM_V_RES,
            .jpeg_quality = CAM_DEFAULT_QUALITY,
            .use_psram    = true,
        };
        esp_err_t ret = bsp_camera_init(&cfg);
        if (ret == ESP_ERR_INVALID_STATE) {
            ESP_LOGW(TAG, "camera already initialised — skipping");
        } else if (ret != ESP_OK) {
            ESP_LOGE(TAG, "bsp_camera_init failed: %s", esp_err_to_name(ret));
            return;
        }

        s.camera_hardware_ready = true;
        APP_SET_BIT(EVT_BIT_CAMERA_ATTACHED);
        APP_SET_BIT(EVT_BIT_CAMERA_READY);

        /* Notify the peer that a camera is now available. */
        uint8_t notify = 0x01;  /* attached = 1 */
        ble_transport_send(MSG_CAMERA_ATTACHED, &notify, 1, true);

        ESP_LOGI(TAG, "camera ready: %ux%u q%u",
                 BSP_CAM_H_RES, BSP_CAM_V_RES, CAM_DEFAULT_QUALITY);

    } else { /* BSP_POGO_EVENT_DETACHED */
        ESP_LOGI(TAG, "Pogo Pin detached — releasing OV2640");

        s.camera_hardware_ready = false;

        /* Return any loaned frame buffer. */
        if (s.fb.buf != NULL) {
            bsp_camera_return_frame(&s.fb);
        }

        bsp_camera_deinit();

        if (g_sys_events != NULL) {
            xEventGroupClearBits(g_sys_events,
                                 EVT_BIT_CAMERA_READY |
                                 EVT_BIT_CAMERA_ATTACHED);
        }

        /* Notify the peer. */
        uint8_t notify = 0x00;  /* detached = 0 */
        ble_transport_send(MSG_CAMERA_DETACHED, &notify, 1, true);
    }
}

/* ------------------------------------------------------------------------- *
 * BLE inbound handler for MSG_TAKE_PHOTO / MSG_DETECT_REQUEST
 * ------------------------------------------------------------------------- */

static void on_ble_take_photo(uint8_t msg_type, uint16_t seq,
                              const uint8_t *payload, uint16_t len,
                              void *user_ctx)
{
    (void)seq;
    (void)user_ctx;

    if (!s.camera_hardware_ready) {
        ESP_LOGW(TAG, "capture requested but camera not attached (msg=0x%02X)",
                 msg_type);
        return;
    }

    camera_cmd_msg_t cmd = {
        .cmd    = (msg_type == MSG_TAKE_PHOTO) ?
                  CAM_CMD_TAKE_PHOTO : CAM_CMD_DETECT_REQUEST,
        .quality = CAM_DEFAULT_QUALITY,
        .width  = 0,
        .height = 0,
    };

    /* Parse optional payload:
     *   Byte 0: quality (0-63), 0xFF = use default
     *   Bytes 1-2: width  (BE, 0 = default)
     *   Bytes 3-4: height (BE, 0 = default)
     */
    if (payload != NULL && len >= 1U && payload[0] <= 63U) {
        cmd.quality = payload[0];
    }
    if (payload != NULL && len >= 5U) {
        cmd.width  = (uint16_t)((payload[1] << 8) | payload[2]);
        cmd.height = (uint16_t)((payload[3] << 8) | payload[4]);
    }

    if (xQueueSend(s.cmd_queue, &cmd, pdMS_TO_TICKS(100)) != pdTRUE) {
        ESP_LOGW(TAG, "capture queue full — dropping request");
    }
}

/* ------------------------------------------------------------------------- *
 * Send a captured JPEG frame over BLE
 *
 * The transport layer handles fragmentation transparently. We send the
 * JPEG body as MSG_CAMERA_FRAME (fire-and-forget) and then a small
 * MSG_CAMERA_FRAME_END containing the total size as a 4-byte big-endian
 * integer, which the peer uses to validate reassembly.
 * ------------------------------------------------------------------------- */

static esp_err_t send_frame_over_ble(const bsp_camera_fb_t *fb)
{
    if (fb == NULL || fb->buf == NULL || fb->len == 0U) {
        return ESP_ERR_INVALID_ARG;
    }
    if (fb->len > CAM_MAX_JPEG_BYTES) {
        ESP_LOGW(TAG, "JPEG too large (%zu bytes) — truncating to %u",
                 fb->len, CAM_MAX_JPEG_BYTES);
        /* We cannot safely truncate a JPEG mid-stream, so send as-is
         * and let the transport fragment it. The peer reassembles. */
    }

    /* Send the JPEG body. Fire-and-forget (no ACK) — JPEG tolerates drops. */
    esp_err_t ret = ble_transport_send(MSG_CAMERA_FRAME,
                                       fb->buf,
                                       (uint16_t)fb->len,
                                       false);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "ble_transport_send(CAMERA_FRAME) failed: %s",
                 esp_err_to_name(ret));
        return ret;
    }

    /* Send the terminator with the total size. This is reliable (ACK'd)
     * so the peer knows the frame is complete. */
    uint8_t end_payload[4];
    uint32_t total = (uint32_t)fb->len;
    end_payload[0] = (uint8_t)((total >> 24) & 0xFF);
    end_payload[1] = (uint8_t)((total >> 16) & 0xFF);
    end_payload[2] = (uint8_t)((total >> 8)  & 0xFF);
    end_payload[3] = (uint8_t)(total         & 0xFF);

    ret = ble_transport_send(MSG_CAMERA_FRAME_END,
                             end_payload, sizeof(end_payload), true);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "ble_transport_send(CAMERA_FRAME_END) failed: %s",
                 esp_err_to_name(ret));
    }

    ESP_LOGI(TAG, "frame sent: %zu bytes JPEG, %ux%u",
             fb->len, fb->width, fb->height);
    return ESP_OK;
}

/* ------------------------------------------------------------------------- *
 * Capture task — drains the command queue and captures/sends JPEG frames
 * ------------------------------------------------------------------------- */

static void camera_capture_task(void *arg)
{
    (void)arg;
    camera_cmd_msg_t cmd;

    ESP_LOGI(TAG, "capture task started (stack=%u)", CAM_TASK_STACK);

    while (1) {
        if (xQueueReceive(s.cmd_queue, &cmd, portMAX_DELAY) != pdTRUE) {
            continue;
        }

        if (!s.camera_hardware_ready) {
            ESP_LOGW(TAG, "capture request but camera detached — skipping");
            continue;
        }

        /* Acquire the lock to serialise captures (single-threaded capture). */
        if (xSemaphoreTake(s.lock, pdMS_TO_TICKS(5000)) != pdTRUE) {
            ESP_LOGW(TAG, "capture lock timeout — skipping request");
            continue;
        }

        /* Apply runtime quality override if requested. */
        if (cmd.quality > 0U && cmd.quality <= 63U &&
            cmd.quality != bsp_camera_get_jpeg_quality()) {
            bsp_camera_set_jpeg_quality(cmd.quality);
        }

        /* Capture one JPEG frame. */
        memset(&s.fb, 0, sizeof(s.fb));
        esp_err_t ret = bsp_camera_capture(&s.fb);
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "bsp_camera_capture failed: %s",
                     esp_err_to_name(ret));
            xSemaphoreGive(s.lock);
            continue;
        }

        ESP_LOGI(TAG, "captured: %zu bytes @ %ums",
                 s.fb.len, s.fb.timestamp_ms);

        /* Send over BLE. */
        send_frame_over_ble(&s.fb);

        /* Return the frame buffer to the pool. */
        bsp_camera_return_frame(&s.fb);

        xSemaphoreGive(s.lock);
    }
}

/* ------------------------------------------------------------------------- *
 * Public API
 * ------------------------------------------------------------------------- */

/**
 * @brief Initialise the camera coordination module.
 *
 * Registers Pogo Pin callbacks and BLE handlers, creates the capture
 * command queue, mutex and task.
 */
esp_err_t camera_module_init(void)
{
    if (s.initialised) {
        return ESP_OK;
    }

    /* --- 1. Lock and command queue --- */
    s.lock = xSemaphoreCreateMutex();
    if (s.lock == NULL) {
        ESP_LOGE(TAG, "failed to create mutex");
        return ESP_ERR_NO_MEM;
    }

    s.cmd_queue = xQueueCreate(CAM_CMD_QUEUE_LEN, sizeof(camera_cmd_msg_t));
    if (s.cmd_queue == NULL) {
        ESP_LOGE(TAG, "failed to create command queue");
        return ESP_ERR_NO_MEM;
    }

    /* --- 2. Register BLE handlers --- */
    ble_transport_register_handler(MSG_TAKE_PHOTO,
                                   on_ble_take_photo, NULL);
    ble_transport_register_handler(MSG_DETECT_REQUEST,
                                   on_ble_take_photo, NULL);

    /* --- 3. Register Pogo Pin callback --- */
    bsp_pogo_pin_register_event_callback(on_pogo_event, NULL);

    /* --- 4. Spawn capture task --- */
    BaseType_t fr = xTaskCreate(camera_capture_task,
                                "cam_capture",
                                CAM_TASK_STACK,
                                NULL,
                                CAM_TASK_PRIORITY,
                                &s.capture_task);
    if (fr != pdPASS) {
        ESP_LOGE(TAG, "failed to create capture task");
        return ESP_ERR_NO_MEM;
    }

    /* --- 5. Check initial Pogo Pin state --- */
    if (bsp_pogo_pin_is_attached()) {
        ESP_LOGI(TAG, "camera already attached at boot — auto-initialising");
        on_pogo_event(BSP_POGO_EVENT_ATTACHED, NULL);
    }

    s.initialised = true;
    ESP_LOGI(TAG, "camera module initialised");
    return ESP_OK;
}

/**
 * @brief Trigger an immediate capture (called from main.c or app event loop).
 */
esp_err_t camera_module_capture(void)
{
    if (!s.initialised) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!s.camera_hardware_ready) {
        return APP_ERR_NOT_READY;
    }

    camera_cmd_msg_t cmd = {
        .cmd    = CAM_CMD_TAKE_PHOTO,
        .quality = 0,   /* use current */
        .width  = 0,
        .height = 0,
    };
    if (xQueueSend(s.cmd_queue, &cmd, pdMS_TO_TICKS(100)) != pdTRUE) {
        return ESP_ERR_TIMEOUT;
    }
    return ESP_OK;
}

/**
 * @brief Check if the camera hardware is attached and ready.
 */
bool camera_module_is_ready(void)
{
    return s.camera_hardware_ready;
}
