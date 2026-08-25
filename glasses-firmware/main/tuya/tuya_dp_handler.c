/**
 * @file tuya_dp_handler.c
 * @brief Tuya Data Point (DP) serialisation, despatch and inbound handling.
 *
 * Responsibilities:
 *   * Pack 10 upload DPs (device -> cloud) into a batch report payload.
 *   * Parse 4 download DPs (cloud -> device) received via BLE NUS and
 *     apply them to the glasses hardware (brightness, volume, mode, reboot).
 *   * Provide per-DP getter helpers that read the current device state.
 *
 * The DP handler does NOT own transport — it produces and consumes byte
 * buffers. The caller (tuya_ble_adapter.c) wraps these buffers in
 * MSG_DEVICE_STATUS (upload) or MSG_DEVICE_CONFIG (download) and hands
 * them to ble_transport_send().
 *
 * On-wire DP frame format (little-endian):
 *   [dp_id:2B] [dp_type:1B] [len:2B] [value:lenB]
 *
 * Multiple DPs are concatenated into a single batch payload up to
 * TUYA_DP_REPORT_MAX_PAYLOAD bytes.
 *
 * @copyright Copyright (c) 2024 Railway Inspection AR Glasses Project
 * @license Apache-2.0
 */

#include "tuya_dp_defs.h"
#include "app_common.h"
#include "ble_transport.h"
#include "message_types.h"

#include <string.h>
#include <stdio.h>

#include "esp_log.h"
#include "esp_err.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

/* Forward-declared from other modules (avoid circular header includes). */
extern app_config_t g_app_config;
extern void display_manager_set_brightness(uint8_t percent);
extern void bsp_i2s_set_volume(uint8_t percent);

static const char *TAG = TAG_TUYA;

/* ------------------------------------------------------------------------- *
 * Internal helpers — DP value serialisation
 * ------------------------------------------------------------------------- */

/**
 * @brief Write a little-endian uint16 to a buffer.
 */
static inline void put_u16_le(uint8_t *buf, uint16_t val)
{
    buf[0] = (uint8_t)(val & 0xFF);
    buf[1] = (uint8_t)((val >> 8) & 0xFF);
}

/**
 * @brief Read a little-endian uint16 from a buffer.
 */
static inline uint16_t get_u16_le(const uint8_t *buf)
{
    return (uint16_t)((uint16_t)buf[0] | ((uint16_t)buf[1] << 8));
}

/**
 * @brief Serialise a single DP value into the frame buffer.
 *
 * @param id     DP ID.
 * @param type   DP type.
 * @param value  Pointer to the value data.
 * @param vlen   Value length in bytes.
 * @param out    Output buffer (at least TUYA_DP_FRAME_HDR_SIZE + vlen).
 * @return Total bytes written (header + value), or 0 on error.
 */
static size_t serialise_dp(uint16_t id, dp_type_t type,
                           const uint8_t *value, uint16_t vlen,
                           uint8_t *out, size_t out_cap)
{
    size_t total = (size_t)TUYA_DP_FRAME_HDR_SIZE + vlen;
    if (total > out_cap || total > TUYA_DP_MAX_FRAME_SIZE) {
        ESP_LOGE(TAG, "DP %u serialise overflow: need %zu, cap %zu",
                 id, total, out_cap);
        return 0;
    }
    put_u16_le(out, id);
    out[2] = (uint8_t)type;
    put_u16_le(out + 3, vlen);
    if (vlen > 0U && value != NULL) {
        memcpy(out + TUYA_DP_FRAME_HDR_SIZE, value, vlen);
    }
    return total;
}

/* ------------------------------------------------------------------------- *
 * Upload DP getters — read current device state for each DP
 * ------------------------------------------------------------------------- */

/** DP 1: device_online — always true once BLE is connected. */
static bool get_device_online(void)
{
    return ble_transport_is_connected();
}

/** DP 2: battery_level — 0-100% (forward-declared from bsp_tp4056). */
extern uint8_t bsp_tp4056_get_battery_percent(void);

/** DP 3: charging_status — TP4056 STDBY/CHRG state. */
extern bool bsp_tp4056_is_charging(void);

/** DP 4: camera_connected — Pogo Pin state. */
extern bool bsp_pogo_pin_is_attached(void);

/** DP 6: device_mode — map app_mode_t to dp_mode_t. */
static dp_mode_t get_device_mode(void)
{
    switch (g_app_config.mode) {
    case APP_MODE_STANDBY:    return DP_MODE_STANDBY;
    case APP_MODE_INSPECTING: return DP_MODE_INSPECTING;
    case APP_MODE_ALERT:      return DP_MODE_ALERT;
    case APP_MODE_CHARGING:   return DP_MODE_CHARGING;
    case APP_MODE_OTA:        return DP_MODE_OTA;
    default:                  return DP_MODE_STANDBY;
    }
}

/** DP 10: ble_signal — RSSI (forward-declared from ble_gap_manager). */
extern int8_t ble_gap_get_rssi(void);

/* ------------------------------------------------------------------------- *
 * Upload: build a batch DP report
 *
 * Packs all 10 upload DPs into a single buffer. The caller sends this
 * as the payload of a MSG_DEVICE_STATUS message via ble_transport.
 * ------------------------------------------------------------------------- */

/**
 * @brief Build the full upload DP report (all 10 DPs).
 *
 * @param out       Output buffer (at least TUYA_DP_REPORT_MAX_PAYLOAD).
 * @param out_cap   Buffer capacity.
 * @return Total bytes written, or 0 on error.
 */
size_t tuya_dp_build_upload_report(uint8_t *out, size_t out_cap)
{
    if (out == NULL || out_cap == 0U) {
        return 0;
    }

    size_t offset = 0U;
    uint8_t value_buf[TUYA_DP_MAX_VALUE_SIZE];
    size_t n;

    /* DP 1: device_online (bool) */
    {
        uint8_t bval = get_device_online() ? 1U : 0U;
        n = serialise_dp(DP_ID_DEVICE_ONLINE, DP_TYPE_BOOL,
                         &bval, 1, out + offset, out_cap - offset);
        offset += n;
    }

    /* DP 2: battery_level (value, int32 LE) */
    {
        int32_t pct = (int32_t)bsp_tp4056_get_battery_percent();
        memcpy(value_buf, &pct, 4);
        n = serialise_dp(DP_ID_BATTERY_LEVEL, DP_TYPE_VALUE,
                         value_buf, 4, out + offset, out_cap - offset);
        offset += n;
    }

    /* DP 3: charging_status (bool) */
    {
        uint8_t bval = bsp_tp4056_is_charging() ? 1U : 0U;
        n = serialise_dp(DP_ID_CHARGING_STATUS, DP_TYPE_BOOL,
                         &bval, 1, out + offset, out_cap - offset);
        offset += n;
    }

    /* DP 4: camera_connected (bool) */
    {
        uint8_t bval = bsp_pogo_pin_is_attached() ? 1U : 0U;
        n = serialise_dp(DP_ID_CAMERA_CONNECTED, DP_TYPE_BOOL,
                         &bval, 1, out + offset, out_cap - offset);
        offset += n;
    }

    /* DP 5: inspection_task_id (string) — empty until a task starts */
    {
        /* Placeholder: the inspection module should set this via a setter. */
        const char *task_id = "";  /* TODO: replace with runtime value */
        uint16_t slen = (uint16_t)strlen(task_id);
        if (slen > DP_MAXLEN_INSPECTION_TASK_ID) {
            slen = DP_MAXLEN_INSPECTION_TASK_ID;
        }
        n = serialise_dp(DP_ID_INSPECTION_TASK_ID, DP_TYPE_STRING,
                         (const uint8_t *)task_id, slen,
                         out + offset, out_cap - offset);
        offset += n;
    }

    /* DP 6: device_mode (enum, uint32 LE) */
    {
        uint32_t mode = (uint32_t)get_device_mode();
        memcpy(value_buf, &mode, 4);
        n = serialise_dp(DP_ID_DEVICE_MODE, DP_TYPE_ENUM,
                         value_buf, 4, out + offset, out_cap - offset);
        offset += n;
    }

    /* DP 7: ai_detection_summary (string) — latest AI result JSON */
    {
        const char *summary = "";  /* TODO: replace with runtime value */
        uint16_t slen = (uint16_t)strlen(summary);
        if (slen > DP_MAXLEN_AI_DETECTION_SUMMARY) {
            slen = DP_MAXLEN_AI_DETECTION_SUMMARY;
        }
        n = serialise_dp(DP_ID_AI_DETECTION_SUMMARY, DP_TYPE_STRING,
                         (const uint8_t *)summary, slen,
                         out + offset, out_cap - offset);
        offset += n;
    }

    /* DP 8: telemetry (string) — JSON with CPU/mem/temp/fps */
    {
        /* Build a minimal telemetry JSON. */
        int tlen = snprintf((char *)value_buf, sizeof(value_buf) - 1,
            "{\"cpu\":%u,\"heap\":%u,\"uptime\":%llu}",
            (unsigned)(240),  /* TODO: read actual CPU freq */
            (unsigned)esp_get_free_heap_size(),
            (unsigned long long)(esp_timer_get_time() / 1000000));
        if (tlen < 0) tlen = 0;
        if (tlen > DP_MAXLEN_TELEMETRY) tlen = DP_MAXLEN_TELEMETRY;
        n = serialise_dp(DP_ID_TELEMETRY, DP_TYPE_STRING,
                         value_buf, (uint16_t)tlen,
                         out + offset, out_cap - offset);
        offset += n;
    }

    /* DP 9: firmware_version (string) */
    {
        const char *ver = g_app_config.firmware_version;
        uint16_t slen = (uint16_t)strlen(ver);
        if (slen > DP_MAXLEN_FIRMWARE_VERSION) {
            slen = DP_MAXLEN_FIRMWARE_VERSION;
        }
        n = serialise_dp(DP_ID_FIRMWARE_VERSION, DP_TYPE_STRING,
                         (const uint8_t *)ver, slen,
                         out + offset, out_cap - offset);
        offset += n;
    }

    /* DP 10: ble_signal (value, int32 LE — RSSI) */
    {
        int32_t rssi = (int32_t)ble_gap_get_rssi();
        memcpy(value_buf, &rssi, 4);
        n = serialise_dp(DP_ID_BLE_SIGNAL, DP_TYPE_VALUE,
                         value_buf, 4, out + offset, out_cap - offset);
        offset += n;
    }

    ESP_LOGD(TAG, "upload report: %zu bytes, %u DPs", offset, 10);
    return offset;
}

/* ------------------------------------------------------------------------- *
 * Upload: send the report via BLE
 * ------------------------------------------------------------------------- */

esp_err_t tuya_dp_send_upload_report(void)
{
    static uint8_t report_buf[TUYA_DP_REPORT_MAX_PAYLOAD];

    size_t len = tuya_dp_build_upload_report(report_buf, sizeof(report_buf));
    if (len == 0U) {
        ESP_LOGE(TAG, "failed to build upload DP report");
        return ESP_FAIL;
    }

    esp_err_t ret = ble_transport_send(MSG_DEVICE_STATUS,
                                       report_buf,
                                       (uint16_t)len,
                                       false);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "ble_transport_send(DEVICE_STATUS) failed: %s",
                 esp_err_to_name(ret));
    } else {
        ESP_LOGD(TAG, "upload DP report sent (%zu bytes)", len);
    }
    return ret;
}

/* ------------------------------------------------------------------------- *
 * Download: parse and dispatch inbound DP commands
 *
 * Expected payload: one or more concatenated DP frames, each:
 *   [dp_id:2B] [dp_type:1B] [len:2B] [value:lenB]
 *
 * Returns ESP_OK if all DPs were parsed and applied successfully.
 * ------------------------------------------------------------------------- */

esp_err_t tuya_dp_handle_download(const uint8_t *payload, uint16_t len)
{
    if (payload == NULL || len == 0U) {
        return ESP_ERR_INVALID_ARG;
    }

    uint16_t offset = 0U;
    uint8_t dp_count = 0U;

    while (offset + TUYA_DP_FRAME_HDR_SIZE <= len) {
        uint16_t dp_id    = get_u16_le(payload + offset);
        dp_type_t dp_type = (dp_type_t)payload[offset + 2];
        uint16_t vlen      = get_u16_le(payload + offset + 3);

        if (offset + TUYA_DP_FRAME_HDR_SIZE + vlen > len) {
            ESP_LOGW(TAG, "DP %u: truncated value (need %u, have %u)",
                     dp_id, vlen, len - offset - TUYA_DP_FRAME_HDR_SIZE);
            break;
        }

        const uint8_t *value = payload + offset + TUYA_DP_FRAME_HDR_SIZE;
        esp_err_t apply_ret = ESP_OK;

        switch (dp_id) {

        /* DP 101: remote_reboot (bool) — true triggers reboot */
        case DP_ID_REMOTE_REBOOT:
        {
            bool reboot = (vlen >= 1U) && (value[0] != 0U);
            if (reboot) {
                ESP_LOGI(TAG, "remote reboot requested");
                /* Graceful delay so the ACK can go out first. */
                vTaskDelay(pdMS_TO_TICKS(500));
                esp_restart();
            }
            break;
        }

        /* DP 102: mode_switch (enum, uint32 LE) */
        case DP_ID_MODE_SWITCH:
        {
            if (vlen >= 4U) {
                uint32_t mode_val;
                memcpy(&mode_val, value, 4);
                app_mode_t new_mode = APP_MODE_STANDBY;
                switch (mode_val) {
                case 0: new_mode = APP_MODE_STANDBY;    break;
                case 1: new_mode = APP_MODE_INSPECTING;  break;
                case 2: new_mode = APP_MODE_ALERT;      break;
                case 3: new_mode = APP_MODE_CHARGING;    break;
                case 4: new_mode = APP_MODE_OTA;         break;
                default:
                    ESP_LOGW(TAG, "mode_switch: invalid enum %u", mode_val);
                    apply_ret = ESP_ERR_INVALID_ARG;
                    break;
                }
                if (apply_ret == ESP_OK) {
                    g_app_config.mode = new_mode;
                    ESP_LOGI(TAG, "mode switched to %s",
                             app_mode_name(new_mode));
                }
            }
            break;
        }

        /* DP 103: brightness (value, int32 LE, 0-255) */
        case DP_ID_BRIGHTNESS:
        {
            if (vlen >= 4U) {
                int32_t val;
                memcpy(&val, value, 4);
                if (val < DP_BRIGHTNESS_MIN) val = DP_BRIGHTNESS_MIN;
                if (val > DP_BRIGHTNESS_MAX) val = DP_BRIGHTNESS_MAX;
                g_app_config.brightness = (uint8_t)val;
                display_manager_set_brightness((uint8_t)val);
                ESP_LOGI(TAG, "brightness set to %u", (unsigned)val);
            }
            break;
        }

        /* DP 104: volume (value, int32 LE, 0-100) */
        case DP_ID_VOLUME:
        {
            if (vlen >= 4U) {
                int32_t val;
                memcpy(&val, value, 4);
                if (val < DP_VOLUME_MIN) val = DP_VOLUME_MIN;
                if (val > DP_VOLUME_MAX) val = DP_VOLUME_MAX;
                g_app_config.volume = (uint8_t)val;
                bsp_i2s_set_volume((uint8_t)val);
                ESP_LOGI(TAG, "volume set to %u%%", (unsigned)val);
            }
            break;
        }

        default:
            ESP_LOGW(TAG, "unknown download DP id=%u, skipping", dp_id);
            apply_ret = ESP_ERR_NOT_SUPPORTED;
            break;
        }

        if (apply_ret != ESP_OK) {
            ESP_LOGW(TAG, "DP %u apply failed: %s",
                     dp_id, esp_err_to_name(apply_ret));
        }

        offset += TUYA_DP_FRAME_HDR_SIZE + vlen;
        dp_count++;
    }

    ESP_LOGI(TAG, "download DPs processed: %u (%u bytes)", dp_count, len);
    return (dp_count > 0U) ? ESP_OK : ESP_ERR_INVALID_ARG;
}

/* ------------------------------------------------------------------------- *
 * Per-DP upload helpers (for targeted, non-batch reporting)
 * ------------------------------------------------------------------------- */

esp_err_t tuya_dp_report_single(uint16_t dp_id)
{
    uint8_t buf[TUYA_DP_FRAME_HDR_SIZE + TUYA_DP_MAX_VALUE_SIZE];
    uint8_t value[TUYA_DP_MAX_VALUE_SIZE];
    dp_type_t type = DP_TYPE_BOOL;
    uint16_t vlen = 0U;

    switch (dp_id) {
    case DP_ID_DEVICE_ONLINE:
        type = DP_TYPE_BOOL;
        value[0] = get_device_online() ? 1U : 0U;
        vlen = 1;
        break;

    case DP_ID_BATTERY_LEVEL:
    {
        type = DP_TYPE_VALUE;
        int32_t pct = (int32_t)bsp_tp4056_get_battery_percent();
        memcpy(value, &pct, 4);
        vlen = 4;
        break;
    }

    case DP_ID_CHARGING_STATUS:
        type = DP_TYPE_BOOL;
        value[0] = bsp_tp4056_is_charging() ? 1U : 0U;
        vlen = 1;
        break;

    case DP_ID_CAMERA_CONNECTED:
        type = DP_TYPE_BOOL;
        value[0] = bsp_pogo_pin_is_attached() ? 1U : 0U;
        vlen = 1;
        break;

    case DP_ID_DEVICE_MODE:
    {
        type = DP_TYPE_ENUM;
        uint32_t mode = (uint32_t)get_device_mode();
        memcpy(value, &mode, 4);
        vlen = 4;
        break;
    }

    case DP_ID_BLE_SIGNAL:
    {
        type = DP_TYPE_VALUE;
        int32_t rssi = (int32_t)ble_gap_get_rssi();
        memcpy(value, &rssi, 4);
        vlen = 4;
        break;
    }

    case DP_ID_FIRMWARE_VERSION:
    {
        type = DP_TYPE_STRING;
        vlen = (uint16_t)strlen(g_app_config.firmware_version);
        if (vlen > DP_MAXLEN_FIRMWARE_VERSION) {
            vlen = DP_MAXLEN_FIRMWARE_VERSION;
        }
        memcpy(value, g_app_config.firmware_version, vlen);
        break;
    }

    default:
        ESP_LOGW(TAG, "single report not supported for DP %u", dp_id);
        return ESP_ERR_NOT_SUPPORTED;
    }

    size_t total = serialise_dp(dp_id, type, value, vlen,
                                buf, sizeof(buf));
    if (total == 0U) {
        return ESP_FAIL;
    }

    return ble_transport_send(MSG_DEVICE_STATUS,
                              buf, (uint16_t)total, false);
}
