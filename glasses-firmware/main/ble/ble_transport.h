/**
 * @file ble_transport.h
 * @brief Frame-level transport layer for the BLE NUS protocol.
 *
 * Responsibilities:
 *   * Frame parse / serialise (SYNC | MSG_TYPE | SEQ | LEN | DATA | CRC16).
 *   * Outbound fragmentation: long application payloads are sliced into
 *     BLE_NUS_MAX_FRAME_PAYLOAD chunks and reassembled by the peer.
 *   * Inbound reassembly: partial frames are buffered until a final chunk
 *     with MSG_TYPE_FRAME_END arrives.
 *   * Reliable delivery: per-sequence ACK with retransmit on timeout.
 *   * 10 s heartbeat keep-alive.
 *   * FreeRTOS send queue with priority slot for ACK / control frames.
 *
 * The transport is single-threaded: a dedicated `ble_transport_task` drains
 * the send queue, hands packets to the GATT notifier, and a NimBLE GAP
 * callback feeds inbound RX writes into the parser. Application modules
 * register typed handlers via ble_transport_register_handler().
 */
#ifndef BLE_TRANSPORT_H
#define BLE_TRANSPORT_H

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"

#include "ble_common.h"
/* Message type enums live in the shared protocol header. */
#include "message_types.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------------- *
 * Frame layout
 * ------------------------------------------------------------------------- */

/** Frame SYNC bytes (big-endian order on the wire). */
#define BLE_FRAME_SYNC0 0xAAU
#define BLE_FRAME_SYNC1 0x55U

/** Fixed header size: SYNC(2) + TYPE(1) + SEQ(2) + LEN(2) = 7 bytes. */
#define BLE_FRAME_HEADER_SIZE 7U

/** CRC16 trailer size. */
#define BLE_FRAME_CRC_SIZE 2U

/** Maximum payload in a single non-fragmented frame. */
#define BLE_FRAME_MAX_PAYLOAD BLE_NUS_MAX_FRAME_PAYLOAD

/** Maximum reassembled payload size (e.g. one JPEG chunk). */
#define BLE_FRAME_REASSEMBLY_BUF 8192U

/** Send queue depth. */
#define BLE_TRANSPORT_SEND_QUEUE_LEN 16U

/* ------------------------------------------------------------------------- *
 * Public data types
 * ------------------------------------------------------------------------- */

/**
 * Inbound handler prototype. Invoked by the transport task whenever a
 * complete (reassembled) frame is received.
 *
 * @param msg_type  Message type from message_types.h.
 * @param seq       Sequence number from the frame header (host order).
 * @param payload   Pointer to the reassembled payload (transport-owned).
 * @param payload_len  Payload length in bytes.
 * @param user_ctx  Caller-supplied context registered with the handler.
 */
typedef void (*ble_transport_rx_cb_t)(uint8_t msg_type,
                                      uint16_t seq,
                                      const uint8_t *payload,
                                      uint16_t payload_len,
                                      void *user_ctx);

/** Connection state change callback. */
typedef void (*ble_transport_conn_cb_t)(bool connected, void *user_ctx);

/* ------------------------------------------------------------------------- *
 * Initialisation & lifecycle
 * ------------------------------------------------------------------------- */

/**
 * Initialise the transport layer. Must be called once from app_main() after
 * NimBLE host is started. Creates the send queue, reassembly buffer, ACK
 * table and spawns the transport task.
 *
 * @return ESP_OK on success.
 */
esp_err_t ble_transport_init(void);

/** Start the heartbeat / retransmit task. Called once BLE is connected. */
esp_err_t ble_transport_start(void);

/** Stop the transport task and flush queues (called on disconnect). */
esp_err_t ble_transport_stop(void);

/* ------------------------------------------------------------------------- *
 * Outbound API
 * ------------------------------------------------------------------------- */

/**
 * Send an application message. Long payloads are fragmented transparently.
 * The call blocks until the frame is queued (up to 100 ms) and returns
 * immediately; actual GATT transmission happens asynchronously from the
 * transport task.
 *
 * @param msg_type  Message type (see message_types.h).
 * @param payload    Payload bytes (may be NULL if payload_len == 0).
 * @param payload_len Payload length in bytes.
 * @param reliable   If true, the peer must ACK before the frame is retired;
 *                   otherwise fire-and-forget (used for streaming media).
 * @return ESP_OK on success, ESP_ERR_TIMEOUT if the queue is full.
 */
esp_err_t ble_transport_send(uint8_t msg_type,
                              const uint8_t *payload,
                              uint16_t payload_len,
                              bool reliable);

/**
 * Send a pre-formed raw frame (used by OTA / debug paths). The buffer must
 * contain the full on-wire frame including SYNC and CRC16.
 */
esp_err_t ble_transport_send_raw(const uint8_t *frame, size_t len);

/* ------------------------------------------------------------------------- *
 * Inbound API
 * ------------------------------------------------------------------------- */

/**
 * Feed raw bytes received on the NUS RX characteristic into the parser.
 * Called by ble_nus_service.c. Internally thread-safe.
 */
void ble_transport_feed_rx(const uint8_t *data, size_t len);

/** Register a typed inbound handler. */
esp_err_t ble_transport_register_handler(uint8_t msg_type,
                                         ble_transport_rx_cb_t cb,
                                         void *user_ctx);

/** Notify the transport layer that the link is up/down. */
void ble_transport_on_connect(uint16_t conn_handle);
void ble_transport_on_disconnect(uint16_t conn_handle);

/** Register a connection state observer. */
esp_err_t ble_transport_register_conn_cb(ble_transport_conn_cb_t cb,
                                         void *user_ctx);

/** @return true if the link is currently connected. */
bool ble_transport_is_connected(void);

#ifdef __cplusplus
}
#endif

#endif /* BLE_TRANSPORT_H */
