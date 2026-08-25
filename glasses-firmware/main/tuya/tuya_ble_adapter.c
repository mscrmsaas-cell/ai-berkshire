/**
 * @file tuya_ble_adapter.c
 * @brief Tuya BLE adapter — pairing, binding, and MQTT proxy via the bag
 *        terminal gateway.
 *
 * Architecture:
 *
 *   Glasses (BLE peripheral)  ←—NUS—→  Bag Terminal (Tuya Gateway)  ←—MQTT—→  Tuya Cloud
 *
 * The glasses firmware does NOT connect to the Tuya cloud directly (it has
 * no Wi-Fi or 4G module). Instead, the bag terminal acts as a Tuya gateway:
 *   * DP reports (MSG_DEVICE_STATUS) are sent from glasses → bag terminal
 *     over BLE NUS. The bag terminal forwards them to the Tuya cloud via
 *     its MQTT connection (see bag-terminal/app/communication/tuya_gateway.py).
 *   * Cloud commands (MSG_DEVICE_CONFIG) arrive at the bag terminal from
 *     Tuya cloud via MQTT and are relayed to the glasses over BLE NUS.
 *
 * This adapter provides:
 *   1. Pairing state machine — advertises a Tuya pairing beacon on the
 *      NUS connection and exchanges handshake / token frames.
 *   2. Binding persistence — stores the Tuya device token in NVS so the
 *      binding survives reboots.
 *   3. Periodic DP report timer — calls tuya_dp_send_upload_report()
 *      every TUYA_REPORT_INTERVAL_MS.
 *   4. Inbound MSG_DEVICE_CONFIG handler — calls tuya_dp_handle_download().
 *
 * @copyright Copyright (c) 2024 Railway Inspection AR Glasses Project
 * @license Apache-2.0
 */

#include "tuya_dp_defs.h"
#include "app_common.h"
#include "ble_transport.h"
#include "message_types.h"

#include <string.h>

#include "esp_log.h"
#include "esp_err.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_timer.h"
#include "esp_random.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/timers.h"

static const char *TAG = TAG_TUYA;

/* Forward declarations from tuya_dp_handler.c */
extern esp_err_t tuya_dp_send_upload_report(void);
extern esp_err_t tuya_dp_handle_download(const uint8_t *payload, uint16_t len);

/* ------------------------------------------------------------------------- *
 * Configuration constants
 * ------------------------------------------------------------------------- */

/** NVS namespace for Tuya binding data. */
#define TUYA_NVS_NAMESPACE  "tuya"

/** NVS key for the Tuya device token (binding secret). */
#define TUYA_NVS_KEY_TOKEN  "dev_token"

/** NVS key for the Tuya device ID (assigned by cloud at binding time). */
#define TUYA_NVS_KEY_DEVID  "dev_id"

/** Maximum length of the Tuya device token (hex string). */
#define TUYA_TOKEN_MAX_LEN   32U

/** Maximum length of the Tuya device ID string. */
#define TUYA_DEVID_MAX_LEN   24U

/** Periodic DP report interval (ms). */
#define TUYA_REPORT_INTERVAL_MS  30000U

/** Pairing beacon interval (ms) — advertises during pairing mode only. */
#define TUYA_PAIRING_BEACON_MS   5000U

/** Pairing timeout (ms) — if not bound within this window, exit pairing. */
#define TUYA_PAIRING_TIMEOUT_MS  120000U  /* 2 minutes */

/* ------------------------------------------------------------------------- *
 * Tuya pairing state machine
 * ------------------------------------------------------------------------- */

typedef enum {
    TUYA_STATE_UNBOUND = 0,  /**< No binding stored, not pairing */
    TUYA_STATE_PAIRING,      /**< Pairing beacon active, awaiting central */
    TUYA_STATE_BOUND,        /**< Binding stored, connected to gateway */
    TUYA_STATE_ERROR,        /**< NVS or transport failure */
} tuya_state_t;

/* ------------------------------------------------------------------------- *
 * Module state
 * ------------------------------------------------------------------------- */

static struct {
    bool              initialised;
    _Atomic tuya_state_t state;
    TimerHandle_t     report_timer;    /* Periodic DP upload */
    TimerHandle_t     pairing_timer;   /* Pairing timeout */
    char              dev_token[TUYA_TOKEN_MAX_LEN + 1];
    char              dev_id[TUYA_DEVID_MAX_LEN + 1];
    bool              has_binding;      /* Binding restored from NVS */
} s = {0};

/* ------------------------------------------------------------------------- *
 * NVS helpers — load / store the Tuya binding
 * ------------------------------------------------------------------------- */

static esp_err_t load_binding_from_nvs(void)
{
    nvs_handle_t h;
    esp_err_t ret = nvs_open(TUYA_NVS_NAMESPACE, NVS_READONLY, &h);
    if (ret != ESP_OK) {
        /* Namespace doesn't exist yet — first boot. */
        s.has_binding = false;
        return ESP_OK;
    }

    size_t token_len = sizeof(s.dev_token);
    size_t devid_len = sizeof(s.dev_id);

    ret = nvs_get_str(h, TUYA_NVS_KEY_TOKEN, s.dev_token, &token_len);
    if (ret != ESP_OK) {
        s.dev_token[0] = '\0';
    }
    ret = nvs_get_str(h, TUYA_NVS_KEY_DEVID, s.dev_id, &devid_len);
    if (ret != ESP_OK) {
        s.dev_id[0] = '\0';
    }
    nvs_close(h);

    s.has_binding = (s.dev_token[0] != '\0' && s.dev_id[0] != '\0');
    ESP_LOGI(TAG, "NVS binding: %s (dev_id=%s, token=%.*s...)",
             s.has_binding ? "found" : "none",
             s.has_binding ? s.dev_id : "",
             8, s.dev_token);
    return ESP_OK;
}

static esp_err_t store_binding_to_nvs(const char *token, const char *dev_id)
{
    nvs_handle_t h;
    esp_err_t ret = nvs_open(TUYA_NVS_NAMESPACE, NVS_READWRITE, &h);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "nvs_open(RW) failed: %s", esp_err_to_name(ret));
        return ret;
    }

    ret = nvs_set_str(h, TUYA_NVS_KEY_TOKEN, token);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "nvs_set_str(token) failed: %s", esp_err_to_name(ret));
        nvs_close(h);
        return ret;
    }

    ret = nvs_set_str(h, TUYA_NVS_KEY_DEVID, dev_id);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "nvs_set_str(dev_id) failed: %s", esp_err_to_name(ret));
        nvs_close(h);
        return ret;
    }

    ret = nvs_commit(h);
    nvs_close(h);

    if (ret == ESP_OK) {
        strncpy(s.dev_token, token, TUYA_TOKEN_MAX_LEN);
        s.dev_token[TUYA_TOKEN_MAX_LEN] = '\0';
        strncpy(s.dev_id, dev_id, TUYA_DEVID_MAX_LEN);
        s.dev_id[TUYA_DEVID_MAX_LEN] = '\0';
        s.has_binding = true;
    }
    return ret;
}

static esp_err_t clear_binding_from_nvs(void)
{
    nvs_handle_t h;
    esp_err_t ret = nvs_open(TUYA_NVS_NAMESPACE, NVS_READWRITE, &h);
    if (ret != ESP_OK) return ret;

    nvs_erase_key(h, TUYA_NVS_KEY_TOKEN);
    nvs_erase_key(h, TUYA_NVS_KEY_DEVID);
    ret = nvs_commit(h);
    nvs_close(h);

    s.dev_token[0] = '\0';
    s.dev_id[0] = '\0';
    s.has_binding = false;
    return ret;
}

/* ------------------------------------------------------------------------- *
 * Periodic DP report timer
 *
 * Fires every TUYA_REPORT_INTERVAL_MS. If the link is connected and the
 * device is bound, it triggers a full upload DP report via BLE transport.
 * ------------------------------------------------------------------------- */

static void report_timer_cb(TimerHandle_t tmr)
{
    (void)tmr;

    tuya_state_t st = atomic_load(&s.state);
    if (st != TUYA_STATE_BOUND) {
        return;  /* Only report when bound */
    }

    if (!ble_transport_is_connected()) {
        return;  /* Link not up — skip this cycle */
    }

    esp_err_t ret = tuya_dp_send_upload_report();
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "periodic DP report failed: %s",
                 esp_err_to_name(ret));
    }
}

/* ------------------------------------------------------------------------- *
 * Pairing timeout timer
 * ------------------------------------------------------------------------- */

static void pairing_timeout_cb(TimerHandle_t tmr)
{
    (void)tmr;
    tuya_state_t st = atomic_load(&s.state);
    if (st == TUYA_STATE_PAIRING) {
        ESP_LOGW(TAG, "pairing timed out after %u ms", TUYA_PAIRING_TIMEOUT_MS);
        atomic_store(&s.state, s.has_binding ?
                     TUYA_STATE_BOUND : TUYA_STATE_UNBOUND);
    }
}

/* ------------------------------------------------------------------------- *
 * BLE inbound handler for MSG_DEVICE_CONFIG (download DPs)
 * ------------------------------------------------------------------------- */

static void on_device_config_rx(uint8_t msg_type, uint16_t seq,
                                const uint8_t *payload, uint16_t len,
                                void *user_ctx)
{
    (void)msg_type;
    (void)seq;
    (void)user_ctx;

    ESP_LOGI(TAG, "received MSG_DEVICE_CONFIG (%u bytes)", len);
    esp_err_t ret = tuya_dp_handle_download(payload, len);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "tuya_dp_handle_download failed: %s",
                 esp_err_to_name(ret));
    }

    /* Send an ACK so the gateway knows the command was received. */
    uint8_t ack_payload[2];
    ack_payload[0] = (uint8_t)((seq >> 8) & 0xFF);
    ack_payload[1] = (uint8_t)(seq & 0xFF);
    ble_transport_send(MSG_ACK, ack_payload, sizeof(ack_payload), false);
}

/* ------------------------------------------------------------------------- *
 * BLE connection state callback
 *
 * When the BLE link comes up, if we already have a binding stored in NVS,
 * transition directly to BOUND state. If unbound, enter PAIRING mode.
 * ------------------------------------------------------------------------- */

static void on_ble_conn_change(bool connected, void *user_ctx)
{
    (void)user_ctx;

    if (connected) {
        ESP_LOGI(TAG, "BLE connected — Tuya adapter active");
        if (s.has_binding) {
            atomic_store(&s.state, TUYA_STATE_BOUND);
            ESP_LOGI(TAG, "restored binding: dev_id=%s", s.dev_id);
            /* Send an immediate DP report. */
            tuya_dp_send_upload_report();
        } else {
            atomic_store(&s.state, TUYA_STATE_PAIRING);
            ESP_LOGI(TAG, "no binding — entering pairing mode");
            /* Start the pairing timeout timer. */
            if (s.pairing_timer != NULL) {
                xTimerChangePeriod(s.pairing_timer,
                                   pdMS_TO_TICKS(TUYA_PAIRING_TIMEOUT_MS), 0);
                xTimerStart(s.pairing_timer, 0);
            }
        }
        /* Start the periodic report timer. */
        if (s.report_timer != NULL) {
            xTimerStart(s.report_timer, 0);
        }
    } else {
        ESP_LOGI(TAG, "BLE disconnected — Tuya adapter suspended");
        atomic_store(&s.state, s.has_binding ?
                     TUYA_STATE_BOUND : TUYA_STATE_UNBOUND);
        if (s.report_timer != NULL) {
            xTimerStop(s.report_timer, 0);
        }
        if (s.pairing_timer != NULL) {
            xTimerStop(s.pairing_timer, 0);
        }
    }
}

/* ------------------------------------------------------------------------- *
 * Pairing beacon — sends a Tuya pairing frame via BLE
 *
 * The pairing frame contains:
 *   Byte 0:   protocol version (0x01)
 *   Byte 1:   pairing step (0x01 = beacon)
 *   Bytes 2-3: product ID hash (truncated)
 *   Bytes 4+: random pairing nonce (8 bytes)
 *
 * The bag terminal gateway intercepts this frame and forwards it to the
 * Tuya cloud. The cloud responds with a binding token, which arrives as
 * a MSG_DEVICE_CONFIG payload with a special sub-type.
 * ------------------------------------------------------------------------- */

static void send_pairing_beacon(void)
{
    uint8_t beacon[12];
    beacon[0] = 0x01;  /* protocol version */
    beacon[1] = 0x01;  /* step: beacon */

    /* Product ID hash (use a fixed placeholder — in production this would
     * be a hash of the product_id from tuya_dp_schema.json). */
    beacon[2] = 'R';
    beacon[3] = 'G';

    /* 8-byte random nonce — use ESP-IDF's RNG. */
    uint32_t r0 = (uint32_t)esp_random();
    uint32_t r1 = (uint32_t)esp_random();
    memcpy(beacon + 4, &r0, 4);
    memcpy(beacon + 8, &r1, 4);

    ble_transport_send(MSG_DEVICE_STATUS, beacon, sizeof(beacon), false);
    ESP_LOGD(TAG, "pairing beacon sent");
}

/* ------------------------------------------------------------------------- *
 * Tuya pairing frame handler
 *
 * When the bag terminal receives a binding response from the Tuya cloud,
 * it relays it to the glasses as a MSG_DEVICE_CONFIG payload with a
 * magic header. The adapter detects this and completes the binding.
 * ------------------------------------------------------------------------- */

static void on_pairing_response(const uint8_t *payload, uint16_t len)
{
    /* Binding response format:
     *   Byte 0:   0x02 (step: binding response)
     *   Byte 1:   token length (should be TUYA_TOKEN_MAX_LEN/2 hex chars)
     *   Bytes 2+: token hex string + dev_id string
     */
    if (len < 4U || payload[0] != 0x02U) {
        return;  /* Not a binding response */
    }

    uint8_t token_len = payload[1];
    if (token_len > TUYA_TOKEN_MAX_LEN || 2U + token_len > len) {
        ESP_LOGW(TAG, "invalid binding response: token_len=%u, payload_len=%u",
                 token_len, len);
        return;
    }

    /* Extract token and dev_id. They are separated by a null byte. */
    char token_buf[TUYA_TOKEN_MAX_LEN + 1];
    char devid_buf[TUYA_DEVID_MAX_LEN + 1];

    uint16_t off = 2U;
    uint16_t copy = token_len;
    if (copy > TUYA_TOKEN_MAX_LEN) copy = TUYA_TOKEN_MAX_LEN;
    memcpy(token_buf, payload + off, copy);
    token_buf[copy] = '\0';
    off += token_len;

    /* The remaining bytes (after token) are the dev_id. */
    uint16_t remaining = len - off;
    if (remaining > 0U) {
        uint16_t did_copy = remaining;
        if (did_copy > TUYA_DEVID_MAX_LEN) did_copy = TUYA_DEVID_MAX_LEN;
        memcpy(devid_buf, payload + off, did_copy);
        devid_buf[did_copy] = '\0';
    } else {
        devid_buf[0] = '\0';
    }

    ESP_LOGI(TAG, "binding received: dev_id=%s, token=%.*s...",
             devid_buf, 8, token_buf);

    /* Persist to NVS. */
    esp_err_t ret = store_binding_to_nvs(token_buf, devid_buf);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "failed to store binding: %s", esp_err_to_name(ret));
        atomic_store(&s.state, TUYA_STATE_ERROR);
        return;
    }

    /* Stop pairing timer and transition to BOUND. */
    if (s.pairing_timer != NULL) {
        xTimerStop(s.pairing_timer, 0);
    }
    atomic_store(&s.state, TUYA_STATE_BOUND);
    APP_SET_BIT(EVT_BIT_TUYA_READY);

    ESP_LOGI(TAG, "device bound successfully");
}

/* ------------------------------------------------------------------------- *
 * Unified BLE inbound handler — routes device config / pairing frames
 * ------------------------------------------------------------------------- */

static void on_ble_inbound(uint8_t msg_type, uint16_t seq,
                           const uint8_t *payload, uint16_t len,
                           void *user_ctx)
{
    (void)seq;
    (void)user_ctx;

    if (msg_type == MSG_DEVICE_CONFIG) {
        /* Check if this is a pairing response. */
        if (len >= 2U && payload[0] == 0x02U) {
            on_pairing_response(payload, len);
        } else {
            /* Regular download DP command. */
            on_device_config_rx(msg_type, seq, payload, len);
        }
    }
}

/* ------------------------------------------------------------------------- *
 * Pairing beacon task (low-priority, periodic during pairing mode)
 * ------------------------------------------------------------------------- */

static void pairing_task(void *arg)
{
    (void)arg;
    ESP_LOGI(TAG, "pairing beacon task started");

    while (1) {
        tuya_state_t st = atomic_load(&s.state);
        if (st == TUYA_STATE_PAIRING) {
            if (ble_transport_is_connected()) {
                send_pairing_beacon();
            }
            vTaskDelay(pdMS_TO_TICKS(TUYA_PAIRING_BEACON_MS));
        } else {
            /* Sleep in 1-second chunks so we can respond to state changes. */
            vTaskDelay(pdMS_TO_TICKS(1000));
        }
    }
}

/* ------------------------------------------------------------------------- *
 * Public API
 * ------------------------------------------------------------------------- */

/**
 * @brief Initialise the Tuya BLE adapter.
 *
 * Steps:
 *   1. Load binding from NVS.
 *   2. Register BLE inbound handler for MSG_DEVICE_CONFIG.
 *   3. Register BLE connection state callback.
 *   4. Create periodic DP report timer (not started until connected).
 *   5. Create pairing timeout timer.
 *   6. Spawn the pairing beacon task.
 */
esp_err_t tuya_ble_adapter_init(void)
{
    if (s.initialised) {
        return ESP_OK;
    }

    /* 1. Load binding from NVS. */
    load_binding_from_nvs();

    /* 2. Register BLE handlers. */
    esp_err_t ret = ble_transport_register_handler(
        MSG_DEVICE_CONFIG, on_ble_inbound, NULL);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "register MSG_DEVICE_CONFIG handler failed: %s",
                 esp_err_to_name(ret));
        return ret;
    }

    /* 3. Register connection state callback. */
    ret = ble_transport_register_conn_cb(on_ble_conn_change, NULL);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "register conn cb failed: %s (continuing)",
                 esp_err_to_name(ret));
    }

    /* 4. Periodic DP report timer. */
    s.report_timer = xTimerCreate(
        "tuya_rpt", pdMS_TO_TICKS(TUYA_REPORT_INTERVAL_MS),
        pdTRUE, NULL, report_timer_cb);
    if (s.report_timer == NULL) {
        ESP_LOGE(TAG, "failed to create report timer");
        return ESP_ERR_NO_MEM;
    }

    /* 5. Pairing timeout timer. */
    s.pairing_timer = xTimerCreate(
        "tuya_pair", pdMS_TO_TICKS(TUYA_PAIRING_TIMEOUT_MS),
        pdFALSE, NULL, pairing_timeout_cb);
    if (s.pairing_timer == NULL) {
        ESP_LOGE(TAG, "failed to create pairing timer");
        return ESP_ERR_NO_MEM;
    }

    /* 6. Pairing beacon task. */
    BaseType_t fr = xTaskCreate(pairing_task, "tuya_pair",
                                3072, NULL, 3, NULL);
    if (fr != pdPASS) {
        ESP_LOGE(TAG, "failed to create pairing task");
        return ESP_ERR_NO_MEM;
    }

    /* Set initial state. */
    atomic_store(&s.state,
                 s.has_binding ? TUYA_STATE_BOUND : TUYA_STATE_UNBOUND);

    s.initialised = true;
    ESP_LOGI(TAG, "Tuya BLE adapter initialised (binding=%s)",
             s.has_binding ? "yes" : "no");
    return ESP_OK;
}

/**
 * @brief Force unbind (factory reset the Tuya binding).
 *
 * Clears the NVS binding and returns to unbound/pairing state.
 */
esp_err_t tuya_ble_adapter_unbind(void)
{
    clear_binding_from_nvs();
    atomic_store(&s.state, TUYA_STATE_UNBOUND);
    if (g_sys_events != NULL) {
        xEventGroupClearBits(g_sys_events, EVT_BIT_TUYA_READY);
    }
    ESP_LOGI(TAG, "device unbound — entering pairing mode on next connect");
    return ESP_OK;
}

/**
 * @brief Check if the device is bound to a Tuya account.
 */
bool tuya_ble_adapter_is_bound(void)
{
    return s.has_binding;
}

/**
 * @brief Get the current Tuya adapter state.
 */
tuya_state_t tuya_ble_adapter_get_state(void)
{
    return atomic_load(&s.state);
}

/**
 * @brief Trigger an immediate DP report (bypass the periodic timer).
 */
esp_err_t tuya_ble_adapter_report_now(void)
{
    if (!s.has_binding) {
        return APP_ERR_NOT_READY;
    }
    return tuya_dp_send_upload_report();
}
