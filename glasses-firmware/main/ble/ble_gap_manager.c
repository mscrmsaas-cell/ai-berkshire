/**
 * @file ble_gap_manager.c
 * @brief GAP layer management for the glasses' peripheral (slave) role.
 *
 * Responsibilities:
 *   * Configure advertising payload (local name + NUS 128-bit service UUID).
 *   * Configure scan response (manufacturer data, TX power).
 *   * Start / stop connectable advertising.
 *   * Handle GAP events: connect, disconnect, subscribe, conn-param update,
 *     MTU exchange. On disconnect we automatically restart advertising so the
 *     bag terminal can re-establish the link.
 *   * Negotiate slave-preferred connection parameters (7.5 - 30 ms interval,
 *     supervision timeout 6 s).
 *
 * The GAP manager is the single NimBLE GAP event handler owner. It routes
 * subscribe events to ble_nus_set_subscription() and connection lifecycle
 * events to ble_transport_on_connect/on_disconnect() so the transport layer
 * can start/stop its heartbeat and retransmit logic.
 *
 * Stack: NimBLE host (CONFIG_BT_NIMBLE_ENABLED) on ESP-IDF v5.1.
 */
#include "ble_common.h"
#include "ble_transport.h"

#include <string.h>

#include "esp_log.h"
#include "esp_err.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "nimble/nimble/host.h"
#include "nimble/nimble/host/services/gap.h"
#include "host/ble_gap.h"
#include "host/ble_hs.h"
#include "host/ble_uuid.h"

static const char *TAG = "ble_gap";

/* ------------------------------------------------------------------------- *
 * Module state
 * ------------------------------------------------------------------------- */

static uint16_t s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
static bool s_advertising = false;
static bool s_initialised = false;

/* ------------------------------------------------------------------------- *
 * Forward declarations
 * ------------------------------------------------------------------------- */

static int  gap_event_cb(struct ble_gap_event *event, void *arg);
static void on_sync_cb(void);
static void on_reset_cb(int reason);

/* ------------------------------------------------------------------------- *
 * Advertising payload
 * ------------------------------------------------------------------------- */

/**
 * Build the primary advertising data and scan response. NimBLE host owns the
 * buffers after ble_gap_adv_set_fields() / ble_gap_adv_set_scan_rsp_fields().
 */
static esp_err_t set_advertising_payload(void)
{
    /* --- Primary advertising data --- */
    ble_hs_adv_fields fields = {0};

    /* Discoverable + BLE-only (no Classic BR/EDR support on ESP32-S3). */
    fields.flags = (uint8_t)(BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP);

    /* Complete local name — fits inside the 31-byte adv payload. */
    const char *name = BLE_GAP_DEVICE_NAME;
    fields.name = (const uint8_t *)name;
    fields.name_len = (uint8_t)strlen(name);
    fields.name_is_complete = 1;

    /* Complete list of 128-bit service UUIDs (just NUS). */
    static const ble_uuid128_t nus_adv_uuid =
        BLE_UUID128_INIT(BLE_NUS_SERVICE_UUID128);
    fields.uuids128 = &nus_adv_uuid.u;
    fields.num_uuids128 = 1;
    fields.uuids128_is_complete = 1;

    /* Advertised TX power level (cap at 0 dBm to save battery). */
    fields.tx_pwr_lvl = 0;
    fields.tx_pwr_lvl_is_present = 1;

    int rc = ble_gap_adv_set_fields(&fields);
    if (rc != 0) {
        ESP_LOGE(TAG, "ble_gap_adv_set_fields failed: %d", rc);
        return ESP_FAIL;
    }

    /* --- Scan response (extra data that doesn't fit in 31 bytes) --- */
    ble_hs_adv_fields rsp = {0};
    /* Manufacturer-specific data placeholder (vendor_id 0xFFFF = test). */
    static const uint8_t mfr_data[3] = { 0xFF, 0xFF, 0x01 };
    rsp.mfg_data = mfr_data;
    rsp.mfg_data_len = sizeof(mfr_data);

    rc = ble_gap_adv_set_scan_rsp(&rsp);
    if (rc != 0) {
        ESP_LOGW(TAG, "ble_gap_adv_set_scan_rsp failed: %d (continuing)", rc);
    }
    return ESP_OK;
}

/* ------------------------------------------------------------------------- *
 * Advertising control
 * ------------------------------------------------------------------------- */

esp_err_t ble_gap_start_advertising(void)
{
    if (!s_initialised) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_advertising) {
        return ESP_OK;
    }

    esp_err_t ret = set_advertising_payload();
    if (ret != ESP_OK) {
        return ret;
    }

    struct ble_gap_adv_params adv_params = {0};
    adv_params.conn_mode = BLE_GAP_CONN_MODE_UND; /* undirected connectable */
    adv_params.disc_mode = BLE_GAP_DISC_MODE_GEN; /* general discoverable  */

    /* Advertise every 100 ms (units of 0.625 ms). */
    adv_params.itvl_min = 0x00A0U; /* 100 ms */
    adv_params.itvl_max = 0x00C8U; /* 120 ms */

    /* Use the public address by default; for random address (CONFIG_BT_NIMBLE_RANDOM_ADDR)
     * use BLE_OWN_ADDR_RANDOM. */
    uint8_t own_addr_type = BLE_OWN_ADDR_PUBLIC;
    int rc = ble_gap_adv_start(own_addr_type, NULL, BLE_HS_FOREVER,
                               &adv_params, gap_event_cb, NULL);
    if (rc != 0) {
        ESP_LOGE(TAG, "ble_gap_adv_start failed: %d", rc);
        return ESP_FAIL;
    }
    s_advertising = true;
    ESP_LOGI(TAG, "advertising started (\"%s\")", BLE_GAP_DEVICE_NAME);
    return ESP_OK;
}

esp_err_t ble_gap_stop_advertising(void)
{
    if (!s_advertising) {
        return ESP_OK;
    }
    int rc = ble_gap_adv_stop();
    if (rc != 0 && rc != BLE_HS_EALREADY && rc != BLE_HS_EBUSY) {
        ESP_LOGW(TAG, "ble_gap_adv_stop failed: %d", rc);
        return ESP_FAIL;
    }
    s_advertising = false;
    return ESP_OK;
}

/* ------------------------------------------------------------------------- *
 * Connection parameter update
 * ------------------------------------------------------------------------- */

esp_err_t ble_gap_update_conn_params(uint16_t conn_handle)
{
    struct ble_gap_upd_params params = {
        .itvl_min           = BLE_NUS_CONN_ITVL_MIN,
        .itvl_max           = BLE_NUS_CONN_ITVL_MAX,
        .latency            = BLE_NUS_CONN_SLAVE_LATENCY,
        .supervision_timeout = BLE_NUS_CONN_SUP_TIMEOUT,
    };
    int rc = ble_gap_update_params(conn_handle, &params);
    if (rc != 0) {
        ESP_LOGW(TAG, "ble_gap_update_params failed: %d", rc);
        return ESP_FAIL;
    }
    return ESP_OK;
}

uint16_t ble_gap_get_conn_handle(void)
{
    return s_conn_handle;
}

/* ------------------------------------------------------------------------- *
 * GAP event handler
 * ------------------------------------------------------------------------- */

static int gap_event_cb(struct ble_gap_event *event, void *arg)
{
    (void)arg;

    switch (event->type) {
    case BLE_GAP_EVENT_CONNECT: {
        /* Connection attempt result. */
        int status = event->connect.status;
        if (status != 0) {
            ESP_LOGW(TAG, "connect failed status=%d — restarting adv", status);
            s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
            /* Retry advertising after a short backoff. */
            vTaskDelay(pdMS_TO_TICKS(200));
            ble_gap_start_advertising();
            return 0;
        }
        s_conn_handle = event->connect.conn_handle;
        s_advertising = false; /* controller stops adv automatically */
        ESP_LOGI(TAG, "connected conn=%u", s_conn_handle);

        /* Request a larger MTU to fit full NUS notify payloads. */
        int rc = ble_att_set_preferred_mtu(BLE_NUS_ATT_MTU_DEFAULT);
        if (rc != 0) {
            ESP_LOGW(TAG, "set_preferred_mtu failed: %d", rc);
        }

        /* Tell the NUS service + transport layer that the link is up. */
        ble_nus_set_connection(s_conn_handle, true);
        ble_transport_on_connect(s_conn_handle);

        /* Slave-initiated connection parameter update (NimBLE will send
         * the L2CAP Connection Parameter Update Request). */
        ble_gap_update_conn_params(s_conn_handle);
        return 0;
    }

    case BLE_GAP_EVENT_DISCONNECT: {
        ESP_LOGI(TAG, "disconnect conn=%u reason=0x%02X",
                 event->disconnect.conn.conn_handle,
                 event->disconnect.reason);
        s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
        /* Notify NUS service + transport that the link is down. */
        ble_nus_set_connection(event->disconnect.conn.conn_handle, false);
        ble_transport_on_disconnect(event->disconnect.conn.conn_handle);
        /* Automatic reconnect = restart advertising. */
        vTaskDelay(pdMS_TO_TICKS(500));
        ble_gap_start_advertising();
        return 0;
    }

    case BLE_GAP_EVENT_ADV_COMPLETE: {
        ESP_LOGI(TAG, "advertising complete (transient stop)");
        s_advertising = false;
        return 0;
    }

    case BLE_GAP_EVENT_SUBSCRIBE: {
        /* A central subscribed/unsubscribed to a characteristic's
         * notifications or indications. We forward to the NUS service so it
         * can track the TX subscription state. */
        bool subscribed = (event->subscribe.cur_notify != 0) ||
                          (event->subscribe.cur_indicate != 0);
        ESP_LOGI(TAG, "subscribe attr=%u cur_notify=%d cur_indicate=%d",
                 event->subscribe.attr_handle,
                 event->subscribe.cur_notify,
                 event->subscribe.cur_indicate);
        ble_nus_set_subscription(event->subscribe.attr_handle, subscribed);
        return 0;
    }

    case BLE_GAP_EVENT_MTU: {
        ESP_LOGI(TAG, "MTU updated conn=%u mtu=%u",
                 event->mtu.conn_handle, event->mtu.value);
        return 0;
    }

    case BLE_GAP_EVENT_CONN_UPDATE: {
        int status = event->conn_update.status;
        if (status == 0) {
            ESP_LOGI(TAG, "conn params updated: itvl=%u latency=%u sup=%u",
                     event->conn_update.conn_params.itvl,
                     event->conn_update.conn_params.latency,
                     event->conn_update.conn_params.supervision_timeout);
        } else {
            ESP_LOGW(TAG, "conn update failed status=%d", status);
        }
        return 0;
    }

    case BLE_GAP_EVENT_CONN_UPDATE_REQ: {
        /* Slave accepts all connection parameter updates proposed by the
         * central (the bag terminal). The defaults are already sane. */
        return 0;
    }

    case BLE_GAP_EVENT_REPEAT_PAIRING: {
        /* Delete the old bond and accept the new pairing attempt. */
        ESP_LOGW(TAG, "repeat pairing — removing old bond");
        return BLE_GAP_REPEAT_PAIRING_RETRY;
    }

    default:
        ESP_LOGD(TAG, "GAP event type=%d (unhandled)", event->type);
        return 0;
    }
}

/* ------------------------------------------------------------------------- *
 * NimBLE host callbacks
 * ------------------------------------------------------------------------- */

static void on_sync_cb(void)
{
    /* Host has synced with the controller — safe to use the BLE address
     * and to begin advertising. */
    ESP_LOGI(TAG, "NimBLE host synced — starting advertising");
    /* Make sure we use the controller's address (public or static random). */
    int rc = ble_hs_id_use_addr(ble_hs_cfg.our_addr_type);
    if (rc != 0) {
        ESP_LOGW(TAG, "ble_hs_id_use_addr failed: %d", rc);
    }
    ble_gap_start_advertising();
}

static void on_reset_cb(int reason)
{
    ESP_LOGW(TAG, "NimBLE host reset reason=%d", reason);
}

/* ------------------------------------------------------------------------- *
 * Initialisation
 * ------------------------------------------------------------------------- */

esp_err_t ble_gap_manager_init(void)
{
    if (s_initialised) {
        return ESP_OK;
    }

    /* Register the host sync/reset callbacks BEFORE nimble_host_run() is
     * called by the application. nimble_host_init() must have already been
     * invoked (typically via nimble_port_init() in app_main). */
    ble_hs_cfg.sync_cb  = on_sync_cb;
    ble_hs_cfg.reset_cb = on_reset_cb;

    /* Set the device name advertised in the GAP service. */
    int rc = ble_svc_gap_device_name_set(BLE_GAP_DEVICE_NAME);
    if (rc != 0) {
        ESP_LOGE(TAG, "ble_svc_gap_device_name_set failed: %d", rc);
        return ESP_FAIL;
    }

    s_initialised = true;
    ESP_LOGI(TAG, "gap manager initialised (device=\"%s\")",
             BLE_GAP_DEVICE_NAME);
    return ESP_OK;
}
