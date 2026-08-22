/**
 * @file ble_nus_service.c
 * @brief Nordic UART Service (NUS) GATT server implementation on NimBLE.
 *
 * Registers a primary service with three characteristics:
 *   * RX (UUID 6E400002-..., client -> server, write + write-no-response)
 *   * TX (UUID 6E400003-..., server -> client, notify with auto CCCD)
 *
 * The slave-role (peripheral) service is exposed by the glasses firmware so a
 * single central (the bag terminal) can connect. RX writes are forwarded to
 * the transport layer (ble_transport_feed_rx); outbound frames are pushed to
 * the central via ble_nus_send_notification(). Subscription state is updated
 * by the GAP manager whenever a BLE_GAP_EVENT_SUBSCRIBE event arrives (the
 * notify characteristic's access callback is not invoked on CCCD writes in
 * NimBLE — the host manages the CCCD internally).
 *
 * Public API surface is declared in ble_common.h.
 *
 * Stack: NimBLE host (CONFIG_BT_NIMBLE_ENABLED) — recommended for ESP32-S3
 * BLE-only builds in ESP-IDF v5.1.
 */
#include "ble_common.h"
#include "ble_transport.h"

#include <string.h>

#include "esp_log.h"
#include "esp_err.h"

/* NimBLE host headers (component: esp_nimble). */
#include "nimble/nimble/host.h"
#include "nimble/nimble/host/services/gap.h"
#include "host/ble_gatt.h"
#include "host/ble_uuid.h"
#include "host/ble_hs.h"
#include "os/os_mbuf.h"

static const char *TAG = "ble_nus";

/* ------------------------------------------------------------------------- *
 * 128-bit UUID objects
 *
 * BLE_UUID128_INIT is a NimBLE macro that initialises a ble_uuid128_t from
 * 16 bytes in the order they appear on the wire (LSB-first).
 * ------------------------------------------------------------------------- */

static const ble_uuid128_t nus_service_uuid =
    BLE_UUID128_INIT(BLE_NUS_SERVICE_UUID128);

static const ble_uuid128_t nus_rx_uuid =
    BLE_UUID128_INIT(BLE_NUS_RX_CHAR_UUID128);

static const ble_uuid128_t nus_tx_uuid =
    BLE_UUID128_INIT(BLE_NUS_TX_CHAR_UUID128);

/* ------------------------------------------------------------------------- *
 * Runtime state
 * ------------------------------------------------------------------------- */

static uint16_t s_nus_rx_attr_handle = 0U;
static uint16_t s_nus_tx_attr_handle = 0U;

static uint16_t s_active_conn_handle = BLE_HS_CONN_HANDLE_NONE;
static bool s_notifications_enabled = false;
static SemaphoreHandle_t s_conn_lock = NULL;

/* ------------------------------------------------------------------------- *
 * GATT access callbacks
 * ------------------------------------------------------------------------- */

/**
 * RX characteristic access callback. The central writes a payload chunk here;
 * we forward the raw bytes verbatim to the transport layer for frame parsing.
 *
 * NimBLE calls this for both BLE_GATT_ACCESS_OP_WRITE_CHR (write with
 * response) and write-without-response. We accept both.
 */
static int nus_rx_access_cb(uint16_t conn_handle, uint16_t attr_handle,
                            struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    (void)conn_handle;
    (void)attr_handle;
    (void)arg;

    if (ctxt->op != BLE_GATT_ACCESS_OP_WRITE_CHR) {
        /* RX characteristic is write-only: reads are not permitted. */
        return BLE_ATT_ERR_READ_NOT_PERMITTED;
    }

    uint16_t om_len = OS_MBUF_PKTLEN(ctxt->om);
    if (om_len == 0U) {
        return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    }
    if (om_len > BLE_NUS_MAX_NOTIFY_PAYLOAD) {
        om_len = BLE_NUS_MAX_NOTIFY_PAYLOAD;
    }

    /* os_mbuf_copydata copies a contiguous range out of an mbuf chain
     * (handles fragmentation across chain links). The buffer is static so
     * it is not allocated on the BLE host task stack. */
    static uint8_t rx_buf[BLE_NUS_MAX_NOTIFY_PAYLOAD];
    int rc = os_mbuf_copydata(ctxt->om, 0, om_len, rx_buf);
    if (rc != 0) {
        ESP_LOGE(TAG, "rx: os_mbuf_copydata failed (%d)", rc);
        return BLE_ATT_ERR_UNLIKELY;
    }

    /* Track the connection that just wrote so we can route notifications
     * back to the same peer. */
    ble_nus_set_connection(conn_handle, true);

    /* Hand the bytes to the transport layer for frame parsing. */
    ble_transport_feed_rx(rx_buf, (size_t)om_len);
    return 0; /* BLE_ATT_ERR_SUCCESS */
}

/**
 * TX characteristic access callback. NimBLE only invokes this for explicit
 * reads on the characteristic value; CCCD writes are handled by the host
 * and surface as BLE_GAP_EVENT_SUBSCRIBE in the GAP event handler. We
 * refuse reads because the TX characteristic is notify-only.
 */
static int nus_tx_access_cb(uint16_t conn_handle, uint16_t attr_handle,
                            struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    (void)conn_handle;
    (void)attr_handle;
    (void)arg;
    (void)ctxt;
    return BLE_ATT_ERR_READ_NOT_PERMITTED;
}

/* ------------------------------------------------------------------------- *
 * Service definition table
 * ------------------------------------------------------------------------- */

static const struct ble_gatt_svc_def s_nus_svcs[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &nus_service_uuid.u,
        .characteristics = (struct ble_gatt_chr_def[]) {
            {
                /* RX: central -> glasses (write). */
                .uuid = &nus_rx_uuid.u,
                .access_cb = nus_rx_access_cb,
                .flags = BLE_GATT_CHR_F_WRITE |
                         BLE_GATT_CHR_F_WRITE_NO_RSP,
                .val_handle = &s_nus_rx_attr_handle,
            },
            {
                /* TX: glasses -> central (notify). */
                .uuid = &nus_tx_uuid.u,
                .access_cb = nus_tx_access_cb,
                .flags = BLE_GATT_CHR_F_NOTIFY,
                .val_handle = &s_nus_tx_attr_handle,
            },
            { 0 } /* end of characteristics */
        },
    },
    { 0 } /* end of services */
};

/* ------------------------------------------------------------------------- *
 * Public API
 * ------------------------------------------------------------------------- */

esp_err_t ble_nus_service_init(void)
{
    if (s_conn_lock == NULL) {
        s_conn_lock = xSemaphoreCreateMutex();
        if (s_conn_lock == NULL) {
            return ESP_ERR_NO_MEM;
        }
    }

    /* Count attribute records the host must allocate for the NUS table. */
    int rc = ble_gatts_count_cfg(s_nus_svcs);
    if (rc != 0) {
        ESP_LOGE(TAG, "ble_gatts_count_cfg failed: %d", rc);
        return ESP_FAIL;
    }

    /* Queue the service for registration. Handles are populated when the
     * host calls ble_gatts_start() (driven by the GAP manager). */
    rc = ble_gatts_add_svcs(s_nus_svcs);
    if (rc != 0) {
        ESP_LOGE(TAG, "ble_gatts_add_svcs failed: %d", rc);
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "NUS service registered (UUID " \
                  "6E400001-B5A3-F393-E0A9-E50E24DCCA9E)");
    return ESP_OK;
}

esp_err_t ble_nus_send_notification(const uint8_t *data, size_t len)
{
    if (data == NULL || len == 0U) {
        return ESP_ERR_INVALID_ARG;
    }
    if (len > BLE_NUS_MAX_NOTIFY_PAYLOAD) {
        len = BLE_NUS_MAX_NOTIFY_PAYLOAD;
    }

    xSemaphoreTake(s_conn_lock, portMAX_DELAY);
    uint16_t conn_handle = s_active_conn_handle;
    bool enabled = s_notifications_enabled;
    uint16_t attr = s_nus_tx_attr_handle;
    xSemaphoreGive(s_conn_lock);

    if (!enabled || conn_handle == BLE_HS_CONN_HANDLE_NONE) {
        return ESP_ERR_INVALID_STATE;
    }

    /* ble_gattc_notify_custom returns 0 on success. NimBLE drops the
     * notification silently if the peer unsubscribed between our check and
     * the actual send (rare race). */
    int rc = ble_gattc_notify_custom(conn_handle, attr, data, len);
    if (rc != 0) {
        ESP_LOGW(TAG, "ble_gattc_notify_custom failed: %d", rc);
        return ESP_FAIL;
    }
    return ESP_OK;
}

void ble_nus_set_connection(uint16_t conn_handle, bool connected)
{
    xSemaphoreTake(s_conn_lock, portMAX_DELAY);
    if (connected) {
        s_active_conn_handle = conn_handle;
    } else if (s_active_conn_handle == conn_handle ||
               conn_handle == BLE_HS_CONN_HANDLE_NONE) {
        s_active_conn_handle = BLE_HS_CONN_HANDLE_NONE;
        s_notifications_enabled = false;
    }
    xSemaphoreGive(s_conn_lock);
}

void ble_nus_set_subscription(uint16_t attr_handle, bool subscribed)
{
    xSemaphoreTake(s_conn_lock, portMAX_DELAY);
    /* Only track subscription on the TX characteristic's CCCD. */
    if (attr_handle == s_nus_tx_attr_handle + 1U ||
        attr_handle == s_nus_tx_attr_handle) {
        s_notifications_enabled = subscribed;
    }
    xSemaphoreGive(s_conn_lock);
    ESP_LOGI(TAG, "subscription on attr=%u -> %s",
             attr_handle, subscribed ? "ON" : "OFF");
}

uint16_t ble_nus_get_attr_handle(ble_nus_char_t chr)
{
    switch (chr) {
    case BLE_NUS_CHAR_RX: return s_nus_rx_attr_handle;
    case BLE_NUS_CHAR_TX: return s_nus_tx_attr_handle;
    case BLE_NUS_CHAR_TX_CCCD:
        /* CCCD immediately follows the characteristic value in NimBLE. */
        return (uint16_t)(s_nus_tx_attr_handle + 1U);
    default: return 0U;
    }
}

bool ble_nus_is_subscribed(void)
{
    bool enabled;
    xSemaphoreTake(s_conn_lock, portMAX_DELAY);
    enabled = s_notifications_enabled;
    xSemaphoreGive(s_conn_lock);
    return enabled;
}
