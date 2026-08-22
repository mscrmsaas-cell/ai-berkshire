/**
 * @file ble_common.h
 * @brief BLE NUS (Nordic UART Service) common definitions for the railway AR glasses.
 *
 * This header centralises the NUS 128-bit UUIDs, MTU negotiation constants and
 * maximum payload limits shared by the GATT service, transport layer and GAP
 * manager. All values are aligned with the shared protocol specification at
 * `shared/protocols/ble_nus_protocol.md`.
 *
 * @note The message type enums used across the BLE stack live in the shared
 *       protocol header `shared/protocols/message_types.h` and are pulled in
 *       via the build system INCLUDE_DIRS (see glasses-firmware/main/CMakeLists.txt).
 *
 * Stack: NimBLE (BLE-only configuration on ESP32-S3, ESP-IDF v5.1).
 */
#ifndef BLE_COMMON_H
#define BLE_COMMON_H

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------------- *
 * Nordic UART Service 128-bit UUIDs
 *
 *   Service:    6E400001-B5A3-F393-E0A9-E50E24DCCA9E
 *   RX Char:    6E400002-B5A3-F393-E0A9-E50E24DCCA9E  (client -> server, write)
 *   TX Char:    6E400003-B5A3-F393-E0A9-E50E24DCCA9E  (server -> client, notify)
 * ------------------------------------------------------------------------- */

/** Canonical NUS 128-bit service UUID (big-endian byte order). */
#define BLE_NUS_SERVICE_UUID128 \
    0x9E, 0xCA, 0xDC, 0x24, 0x0E, 0xE5, 0xA9, 0xE0, \
    0x93, 0xF3, 0xA3, 0xB5, 0x01, 0x00, 0x40, 0x6E

/** RX characteristic UUID (client writes the glasses). */
#define BLE_NUS_RX_CHAR_UUID128 \
    0x9E, 0xCA, 0xDC, 0x24, 0x0E, 0xE5, 0xA9, 0xE0, \
    0x93, 0xF3, 0xA3, 0xB5, 0x02, 0x00, 0x40, 0x6E

/** TX characteristic UUID (glasses notify the client). */
#define BLE_NUS_TX_CHAR_UUID128 \
    0x9E, 0xCA, 0xDC, 0x24, 0x0E, 0xE5, 0xA9, 0xE0, \
    0x93, 0xF3, 0xA3, 0xB5, 0x03, 0x00, 0x40, 0x6E

/** Advertised short local name (max 26 bytes for BLE adv payload). */
#define BLE_GAP_DEVICE_NAME "RAIL-AR-GLASS"

/* ------------------------------------------------------------------------- *
 * MTU / payload limits
 * ------------------------------------------------------------------------- */

/** Negotiated ATT MTU. After MTU exchange this is the agreed value. */
#define BLE_NUS_ATT_MTU_DEFAULT 247U

/** Minimum MTU we accept during connection parameter negotiation. */
#define BLE_NUS_ATT_MTU_MIN 23U

/**
 * Maximum ATT payload that can fit in a single notification.
 * ATT_MTU - 3 (opcode + handle) = 247 - 3 = 244 bytes.
 */
#define BLE_NUS_MAX_NOTIFY_PAYLOAD (BLE_NUS_ATT_MTU_DEFAULT - 3U)

/**
 * Maximum payload per fragmented BLE frame. The transport slices long
 * application messages into chunks of this size before wrapping with the
 * NUS frame header (see ble_nus_protocol.md).
 */
#define BLE_NUS_MAX_FRAME_PAYLOAD 240U

/** Total on-wire size of a maximally-fragmented NUS frame. */
#define BLE_NUS_MAX_FRAME_SIZE (BLE_NUS_MAX_FRAME_PAYLOAD + 9U)

/* ------------------------------------------------------------------------- *
 * Timing / connection parameters
 * ------------------------------------------------------------------------- */

/** Slave latency (number of connection events the slave may skip). */
#define BLE_NUS_CONN_SLAVE_LATENCY 0U

/** Supervision timeout in 10ms units -> 6 seconds. */
#define BLE_NUS_CONN_SUP_TIMEOUT 600U

/** Minimum connection interval in 1.25ms units -> 7.5 ms. */
#define BLE_NUS_CONN_ITVL_MIN 6U

/** Maximum connection interval in 1.25ms units -> 30 ms. */
#define BLE_NUS_CONN_ITVL_MAX 24U

/** Heartbeat interval (ms) — see ble_transport.c. */
#define BLE_NUS_HEARTBEAT_INTERVAL_MS 10000U

/** ACK retransmit timeout (ms). */
#define BLE_NUS_ACK_TIMEOUT_MS 3000U

/** Maximum concurrent peripheral connections (slave role = 1). */
#define BLE_NUS_MAX_CONNECTIONS 1U

/* ------------------------------------------------------------------------- *
 * Public API surface
 *
 * The NUS service and GAP manager do not own a dedicated header file in this
 * project layout — their public functions are declared here because
 * ble_common.h is already the shared BLE header pulled in by every BLE
 * module. The transport layer (ble_transport.h) re-includes ble_common.h so
 * callers automatically pick these up.
 * ------------------------------------------------------------------------- */

/* Forward declaration of the ble_gatt_svc_def table for use in app_main. */
struct ble_gatt_svc_def;

/** Characteristic selector for ble_nus_get_attr_handle(). */
typedef enum {
    BLE_NUS_CHAR_RX = 0,
    BLE_NUS_CHAR_TX = 1,
    BLE_NUS_CHAR_TX_CCCD = 2,
} ble_nus_char_t;

/* --- ble_nus_service.c --------------------------------------------------- */

/**
 * Register the NUS GATT service with the NimBLE host. Must be called after
 * nimble_host_init() and before ble_gatts_start() (which is invoked from
 * the GAP manager once device identity is set).
 */
esp_err_t ble_nus_service_init(void);

/**
 * Push bytes to the connected central via the TX characteristic. The call
 * is non-blocking: NimBLE queues the notification internally. If no peer is
 * subscribed the call returns ESP_ERR_INVALID_STATE.
 *
 * @param data  Payload to notify.
 * @param len   Payload length (will be clamped to BLE_NUS_MAX_NOTIFY_PAYLOAD).
 */
esp_err_t ble_nus_send_notification(const uint8_t *data, size_t len);

/**
 * Update the active connection handle and subscription state. Called from
 * the GAP event handler on connect/disconnect and on subscribe/unsubscribe.
 */
void ble_nus_set_connection(uint16_t conn_handle, bool connected);

/** Update the subscription flag for the TX characteristic. */
void ble_nus_set_subscription(uint16_t attr_handle, bool subscribed);

/** Get the runtime attribute handle for a NUS characteristic. */
uint16_t ble_nus_get_attr_handle(ble_nus_char_t chr);

/** @return true if the connected central has enabled TX notifications. */
bool ble_nus_is_subscribed(void);

/* --- ble_gap_manager.c --------------------------------------------------- */

/** Initialise the GAP manager (advertising payload, callbacks). */
esp_err_t ble_gap_manager_init(void);

/** Start peripheral advertising with the NUS service in the UUID list. */
esp_err_t ble_gap_start_advertising(void);

/** Stop advertising (called on connect). */
esp_err_t ble_gap_stop_advertising(void);

/** Request a connection parameter update from the slave side. */
esp_err_t ble_gap_update_conn_params(uint16_t conn_handle);

/** @return the active peripheral connection handle, or BLE_HS_CONN_HANDLE_NONE. */
uint16_t ble_gap_get_conn_handle(void);

#ifdef __cplusplus
}
#endif

#endif /* BLE_COMMON_H */
