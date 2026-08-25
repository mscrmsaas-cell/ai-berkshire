/**
 * @file ble_transport.c
 * @brief Frame-level transport for the BLE NUS protocol.
 *
 * Wire format (matches shared/protocols/ble_nus_protocol.md):
 *
 *   +------+------+------+-----+----------+-------+
 *   |SYNC0 |SYNC1 | TYPE | SEQ(2B BE) | LEN(2B BE) | PAYDATA | CRC16(2B LE) |
 *   +------+------+------+-----+----------+-------+
 *   | 0xAA | 0x55 |  1B  |     2B    |     2B     |   NB   |     2B        |
 *   +------+------+------+-----+----------+-------+
 *
 * The transport provides:
 *   * Frame parsing state machine fed by ble_transport_feed_rx().
 *   * Outbound fragmentation: messages > BLE_FRAME_MAX_PAYLOAD are sliced
 *     into FRAME_DATA chunks (intermediate) and a FRAME_END chunk (final),
 *     each prefixed with (orig_msg_type, offset) so the peer can reassemble.
 *   * Inbound reassembly with a single-slot 8 KiB buffer (slave role = 1 peer).
 *   * Reliable delivery: per-sequence ACK with retransmit on timeout.
 *   * 10 s heartbeat keep-alive task.
 *   * FreeRTOS send queue with a priority bypass for ACK / control frames.
 *
 * Transport-internal message type values (must match shared/protocols/
 * message_types.h). The values are duplicated here as #define constants so
 * this module remains buildable even if message_types.h uses different
 * enum identifiers — only the numeric values must agree.
 */
#include "ble_transport.h"
#include "ble_common.h"

#include <string.h>
#include <stdatomic.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/timers.h"

#include "esp_log.h"
#include "esp_err.h"
#include "esp_timer.h"
#include "esp_check.h"

static const char *TAG = "ble_transport";

/* ------------------------------------------------------------------------- *
 * Transport-internal message type values (mirror of message_types.h)
 * ------------------------------------------------------------------------- */

#define MSG_HEARTBEAT          0x02U  /* keep-alive, no payload */
#define MSG_ACK                0x0FU  /* payload = acked SEQ (2B BE) */
#define MSG_FRAME_DATA         0x20U  /* intermediate fragment */
#define MSG_FRAME_END          0x21U  /* final fragment */

/* Per-fragment header bytes prefixed in the payload of FRAME_DATA/_END. */
#define FRAG_HDR_SIZE          3U     /* orig_type(1) + offset(2 BE) */

/* ------------------------------------------------------------------------- *
 * Reliability policy
 *
 * The wire frame has no flags byte (see ble_nus_protocol.md), so reliability
 * is decided by message-type convention: control / status / RAG / inspection /
 * alert / OTA / display frames are reliable (peer ACKs), while streaming
 * media (audio chunks, camera frame fragments, heartbeat, ACK) are
 * fire-and-forget. The sender's `reliable` argument overrides the default
 * for unusual cases (e.g. an OTA chunk sent reliably even though OTA lives
 * in a range that is reliable by default).
 * ------------------------------------------------------------------------- */

static bool is_reliable_msg_type(uint8_t msg_type)
{
    switch (msg_type) {
    /* Fire-and-forget: heartbeat + ACK itself + streaming media. */
    case 0x02U: /* MSG_HEARTBEAT */
    case 0x0FU: /* MSG_ACK */
    case 0x20U: /* MSG_FRAME_DATA (intermediate camera fragment) */
    case 0x21U: /* MSG_FRAME_END  (final camera fragment) */
    case 0x30U: /* MSG_AUDIO_CHUNK */
    case 0x33U: /* MSG_TTS_AUDIO */
        return false;
    default:
        /* Everything else (control / device / detection result / RAG /
         * inspection / alert / OTA / display) is reliable. */
        return true;
    }
}

/* ------------------------------------------------------------------------- *
 * CRC16-CCITT (XMODEM, poly 0x1021, init 0x0000) — table-driven.
 * ------------------------------------------------------------------------- */

static const uint16_t s_crc16_table[256] = {
    0x0000,0x1021,0x2042,0x3063,0x4084,0x50A5,0x60C6,0x70E7,
    0x8108,0x9129,0xA14A,0xB16B,0xC18C,0xD1AD,0xE1CE,0xF1EF,
    0x1231,0x0210,0x3273,0x2252,0x52B5,0x4294,0x72F7,0x62D6,
    0x9339,0x8318,0xB37B,0xA35A,0xD3BD,0xC39C,0xF3FF,0xE3DE,
    0x2462,0x3443,0x0420,0x1401,0x64E6,0x74C7,0x44A4,0x5485,
    0xA56A,0xB54B,0x8528,0x9509,0xE5EE,0xF5CF,0xC5AC,0xD58D,
    0x3653,0x2672,0x1611,0x0630,0x76D7,0x66F6,0x5695,0x46B4,
    0xB75B,0xA77A,0x9719,0x8738,0xF7DF,0xE7FE,0xD79D,0xC7BC,
    0x48C4,0x58E5,0x6886,0x78A7,0x0840,0x1861,0x2802,0x3823,
    0xC9CC,0xD9ED,0xE98E,0xF9AF,0x8948,0x9969,0xA90A,0xB92B,
    0x5AF5,0x4AD4,0x7AB7,0x6A96,0x1A71,0x0A50,0x3A33,0x2A12,
    0xDBFD,0xCBDC,0xFBBF,0xEB9E,0x9B79,0x8B58,0xBB3B,0xAB1A,
    0x6CA6,0x7C87,0x4CE4,0x5CC5,0x2C22,0x3C03,0x0C60,0x1C41,
    0xEDAE,0xFD8F,0xCDEC,0xDDCD,0xAD2A,0xBD0B,0x8D68,0x9D49,
    0x7E97,0x6EB6,0x5ED5,0x4EF4,0x3E13,0x2E32,0x1E51,0x0E70,
    0xFF9F,0xEFBE,0xDFDD,0xCFFC,0xBF1B,0xAF3A,0x9F59,0x8F78,
    0x9188,0x81A9,0xB1CA,0xA1EB,0xD10C,0xC12D,0xF14E,0xE16F,
    0x1080,0x00A1,0x30C2,0x20E3,0x5004,0x4025,0x7046,0x6067,
    0x83B9,0x9398,0xA3FB,0xB3DA,0xC33D,0xD31C,0xE37F,0xF35E,
    0x02B1,0x1290,0x22F3,0x32D2,0x4235,0x5214,0x6277,0x7256,
    0xB5EA,0xA5CB,0x95A8,0x8589,0xF56E,0xE54F,0xD52C,0xC50D,
    0x34E2,0x24C3,0x14A0,0x0481,0x7466,0x6447,0x5424,0x4405,
    0xA7DB,0xB7FA,0x8799,0x97B8,0xE75F,0xF77E,0xC71D,0xD73C,
    0x26D3,0x36F2,0x0691,0x16B0,0x6657,0x7676,0x4615,0x5634,
    0xD94C,0xC96D,0xF90E,0xE92F,0x99C8,0x89E9,0xB98A,0xA9AB,
    0x5844,0x4865,0x7806,0x6827,0x18C0,0x08E1,0x3882,0x28A3,
    0xCB7D,0xDB5C,0xEB3F,0xFB1E,0x8BF9,0x9BD8,0xABBB,0xBB9A,
    0x4A75,0x5A54,0x6A37,0x7A16,0x0AF1,0x1AD0,0x2AB3,0x3A92,
    0xFD2E,0xED0F,0xDD6C,0xCD4D,0xBDAA,0xAD8B,0x9DE8,0x8DC9,
    0x7C26,0x6C07,0x5C64,0x4C45,0x3CA2,0x2C83,0x1CE0,0x0CC1,
    0xEF1F,0xFF3E,0xCF5D,0xDF7C,0xAF9B,0xBFBA,0x8FD9,0x9FF8,
    0x6E17,0x7E36,0x4E55,0x5E74,0x2E93,0x3EB2,0x0ED1,0x1EF0
};

static inline uint16_t crc16_ccitt(const uint8_t *data, size_t len)
{
    uint16_t crc = 0x0000U;
    for (size_t i = 0U; i < len; ++i) {
        crc = (uint16_t)((crc << 8) ^
                        s_crc16_table[((crc >> 8) ^ data[i]) & 0xFFU]);
    }
    return crc;
}

/* ------------------------------------------------------------------------- *
 * Send-queue item
 * ------------------------------------------------------------------------- */

typedef struct {
    uint8_t  msg_type;
    uint16_t seq;
    bool     reliable;     /* if true, await ACK before retirement */
    uint16_t payload_len;
    /* Payload follows; for fragmented sends the payload is a single
     * pre-framed chunk (including the FRAG_HDR prefix if applicable). */
    uint8_t  payload[BLE_FRAME_MAX_PAYLOAD];
} tx_item_t;

/* Reassembly slot (single peer — slave role). */
typedef struct {
    bool     active;
    uint8_t  orig_type;
    uint16_t expected_offset;
    uint16_t total_len;
    uint8_t  buf[BLE_FRAME_REASSEMBLY_BUF];
} reasm_slot_t;

/* ACK tracking table — small ring of in-flight reliable frames. */
#define ACK_TABLE_SIZE 8U
typedef struct {
    bool     in_use;
    uint16_t seq;
    uint16_t msg_type;
    uint16_t payload_len;
    uint8_t  payload[BLE_FRAME_MAX_PAYLOAD];
    uint32_t tx_tick_ms;
    uint8_t  retries;
} ack_entry_t;

/* Handler slot — one per msg_type. */
typedef struct {
    bool                       in_use;
    uint8_t                    msg_type;
    ble_transport_rx_cb_t     cb;
    void                      *user_ctx;
} handler_slot_t;

#define MAX_HANDLERS 32U

/* ------------------------------------------------------------------------- *
 * Module state
 * ------------------------------------------------------------------------- */

static struct {
    bool                initialised;
    bool                connected;
    uint16_t            conn_handle;
    _Atomic uint16_t    next_seq;        /* monotonic TX sequence */
    SemaphoreHandle_t   lock;            /* protects ACK table + handlers */
    QueueHandle_t        send_queue;     /* tx_item_t items */
    TaskHandle_t         task;           /* transport task */
    TimerHandle_t        heartbeat_tmr;  /* FreeRTOS soft-timer */
    bool                 task_run;
    reasm_slot_t         reasm;
    ack_entry_t         ack_tbl[ACK_TABLE_SIZE];
    handler_slot_t      handlers[MAX_HANDLERS];
    ble_transport_conn_cb_t conn_cb;
    void               *conn_ctx;
} s = {0};

/* Inbound parser state. */
typedef enum {
    PARSE_SYNC0 = 0,
    PARSE_SYNC1,
    PARSE_TYPE,
    PARSE_SEQ_HI,
    PARSE_SEQ_LO,
    PARSE_LEN_HI,
    PARSE_LEN_LO,
    PARSE_PAYLOAD,
    PARSE_CRC_LO,
    PARSE_CRC_HI,
} parse_state_t;

typedef struct {
    parse_state_t state;
    uint8_t  msg_type;
    uint16_t seq;
    uint16_t len;
    uint16_t len_idx;       /* bytes consumed of payload so far */
    uint8_t  payload[BLE_FRAME_MAX_PAYLOAD + 8U];
    uint16_t crc;
} parse_ctx_t;

static parse_ctx_t s_parse;

/* ------------------------------------------------------------------------- *
 * Forward declarations
 * ------------------------------------------------------------------------- */

static void transport_task(void *arg);
static void heartbeat_cb(TimerHandle_t tmr);
static void process_parsed_frame(void);
static void handle_ack_payload(const uint8_t *payload, uint16_t len);
static void deliver_frame(uint8_t msg_type, uint16_t seq,
                          const uint8_t *payload, uint16_t len);
static void resend_expired_acks(void);
static esp_err_t enqueue_frame(uint8_t msg_type, uint16_t seq,
                               const uint8_t *payload, uint16_t len,
                               bool reliable, bool front);

/* ------------------------------------------------------------------------- *
 * Initialisation
 * ------------------------------------------------------------------------- */

esp_err_t ble_transport_init(void)
{
    if (s.initialised) {
        return ESP_ERR_INVALID_STATE;
    }
    s.lock = xSemaphoreCreateMutex();
    s.send_queue = xQueueCreate(BLE_TRANSPORT_SEND_QUEUE_LEN, sizeof(tx_item_t));
    if (s.lock == NULL || s.send_queue == NULL) {
        return ESP_ERR_NO_MEM;
    }

    atomic_store(&s.next_seq, 1U);
    s.initialised = true;
    s.task_run = false;

    s.heartbeat_tmr = xTimerCreate(
        "ble_hb", pdMS_TO_TICKS(BLE_NUS_HEARTBEAT_INTERVAL_MS),
        pdTRUE, /* auto-reload */
        NULL, heartbeat_cb);
    if (s.heartbeat_tmr == NULL) {
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG, "transport initialised (queue=%d, mtu=%u, hb=%ums)",
             BLE_TRANSPORT_SEND_QUEUE_LEN,
             BLE_NUS_ATT_MTU_DEFAULT,
             BLE_NUS_HEARTBEAT_INTERVAL_MS);
    return ESP_OK;
}

esp_err_t ble_transport_start(void)
{
    if (!s.initialised) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s.task != NULL) {
        return ESP_OK; /* already running */
    }
    s.task_run = true;
    BaseType_t ok = xTaskCreatePinnedToCore(
        transport_task, "ble_tx", 4096, NULL, 5, &s.task, 1);
    if (ok != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    xTimerStart(s.heartbeat_tmr, 0);
    ESP_LOGI(TAG, "transport task started");
    return ESP_OK;
}

esp_err_t ble_transport_stop(void)
{
    if (!s.initialised) {
        return ESP_ERR_INVALID_STATE;
    }
    xTimerStop(s.heartbeat_tmr, 0);
    s.task_run = false;
    if (s.task != NULL) {
        /* Drain the queue to unblock the task. */
        xQueueSend(s.send_queue, &(tx_item_t){0}, 0);
        vTaskDelay(pdMS_TO_TICKS(50));
        vTaskDelete(s.task);
        s.task = NULL;
    }
    /* Reset reassembly state. */
    memset(&s.reasm, 0, sizeof(s.reasm));
    memset(&s_parse, 0, sizeof(s_parse));
    return ESP_OK;
}

/* ------------------------------------------------------------------------- *
 * Outbound
 * ------------------------------------------------------------------------- */

esp_err_t ble_transport_send(uint8_t msg_type,
                             const uint8_t *payload,
                             uint16_t payload_len,
                             bool reliable)
{
    if (!s.initialised) {
        return ESP_ERR_INVALID_STATE;
    }
    if (payload_len > 0U && payload == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    /* Whole message fits in a single frame. */
    if (payload_len <= BLE_FRAME_MAX_PAYLOAD) {
        uint16_t seq = atomic_fetch_add(&s.next_seq, 1U);
        return enqueue_frame(msg_type, seq, payload, payload_len,
                             reliable, false);
    }

    /* Fragment: each chunk payload = FRAG_HDR(3) + slice.
     * Final chunk uses MSG_FRAME_END; intermediate chunks MSG_FRAME_DATA. */
    uint16_t offset = 0U;
    uint16_t seq = atomic_fetch_add(&s.next_seq, 1U);
    uint16_t chunk_cap = (uint16_t)(BLE_FRAME_MAX_PAYLOAD - FRAG_HDR_SIZE);
    while (offset < payload_len) {
        uint16_t slice = (uint16_t)(payload_len - offset);
        if (slice > chunk_cap) {
            slice = chunk_cap;
        }
        bool is_last = (offset + slice) >= payload_len;
        /* Scratch buffer for the per-chunk payload (orig_type + offset + data).
         * Allocated on the heap because chunk_cap is runtime-variable and the
         * ESP-IDF toolchain does not guarantee stack VLA support. */
        uint8_t *buf = (uint8_t *)malloc((size_t)FRAG_HDR_SIZE + slice);
        if (buf == NULL) {
            return ESP_ERR_NO_MEM;
        }
        buf[0] = msg_type;
        buf[1] = (uint8_t)(offset >> 8);
        buf[2] = (uint8_t)(offset & 0xFFU);
        memcpy(buf + FRAG_HDR_SIZE, payload + offset, slice);

        esp_err_t ret = enqueue_frame(
            is_last ? MSG_FRAME_END : MSG_FRAME_DATA,
            seq, buf, (uint16_t)(FRAG_HDR_SIZE + slice),
            reliable, false);
        free(buf);
        if (ret != ESP_OK) {
            return ret;
        }
        offset = (uint16_t)(offset + slice);
    }
    return ESP_OK;
}

esp_err_t ble_transport_send_raw(const uint8_t *frame, size_t len)
{
    if (!s.initialised || frame == NULL || len == 0U) {
        return ESP_ERR_INVALID_ARG;
    }
    if (len > sizeof(tx_item_t) - offsetof(tx_item_t, msg_type)) {
        return ESP_ERR_INVALID_SIZE;
    }
    /* Wrap raw bytes as a single unreliable frame (caller pre-computed CRC). */
    tx_item_t item = {0};
    item.msg_type = 0U; /* not used — raw path bypasses the parser */
    item.seq = 0U;
    item.reliable = false;
    item.payload_len = (uint16_t)len;
    memcpy(item.payload, frame, len);
    if (xQueueSend(s.send_queue, &item, pdMS_TO_TICKS(100)) != pdPASS) {
        return ESP_ERR_TIMEOUT;
    }
    return ESP_OK;
}

static esp_err_t enqueue_frame(uint8_t msg_type, uint16_t seq,
                               const uint8_t *payload, uint16_t len,
                               bool reliable, bool front)
{
    tx_item_t item = {0};
    item.msg_type    = msg_type;
    item.seq         = seq;
    item.reliable    = reliable;
    item.payload_len = len;
    if (len > 0U) {
        if (len > BLE_FRAME_MAX_PAYLOAD) {
            return ESP_ERR_INVALID_SIZE;
        }
        memcpy(item.payload, payload, len);
    }

    /* If reliable, snapshot into the ACK table BEFORE queueing so the
     * retransmit task can find it. */
    if (reliable) {
        xSemaphoreTake(s.lock, portMAX_DELAY);
        ack_entry_t *slot = NULL;
        for (int i = 0; i < ACK_TABLE_SIZE; ++i) {
            if (!s.ack_tbl[i].in_use) {
                slot = &s.ack_tbl[i];
                break;
            }
        }
        if (slot == NULL) {
            xSemaphoreGive(s.lock);
            ESP_LOGW(TAG, "ACK table full, dropping seq=%u", seq);
            return ESP_ERR_NO_MEM;
        }
        slot->in_use       = true;
        slot->seq          = seq;
        slot->msg_type      = msg_type;
        slot->payload_len  = len;
        memcpy(slot->payload, payload, len);
        slot->tx_tick_ms   = xTaskGetTickCount() * portTICK_PERIOD_MS;
        slot->retries      = 0U;
        xSemaphoreGive(s.lock);
    }

    BaseType_t ok = front ?
        xQueueSendToFront(s.send_queue, &item, pdMS_TO_TICKS(100)) :
        xQueueSend(s.send_queue, &item, pdMS_TO_TICKS(100));
    if (ok != pdPASS) {
        /* Roll back the ACK slot. */
        if (reliable) {
            xSemaphoreTake(s.lock, portMAX_DELAY);
            for (int i = 0; i < ACK_TABLE_SIZE; ++i) {
                if (s.ack_tbl[i].in_use && s.ack_tbl[i].seq == seq) {
                    s.ack_tbl[i].in_use = false;
                    break;
                }
            }
            xSemaphoreGive(s.lock);
        }
        return ESP_ERR_TIMEOUT;
    }
    return ESP_OK;
}

/* ------------------------------------------------------------------------- *
 * Inbound: feed raw RX bytes into the parser state machine.
 * ------------------------------------------------------------------------- */

void ble_transport_feed_rx(const uint8_t *data, size_t len)
{
    if (!s.initialised || data == NULL || len == 0U) {
        return;
    }
    for (size_t i = 0U; i < len; ++i) {
        uint8_t b = data[i];
        switch (s_parse.state) {
        case PARSE_SYNC0:
            if (b == BLE_FRAME_SYNC0) {
                s_parse.state = PARSE_SYNC1;
            }
            break;
        case PARSE_SYNC1:
            if (b == BLE_FRAME_SYNC1) {
                s_parse.state = PARSE_TYPE;
            } else if (b == BLE_FRAME_SYNC0) {
                /* stay in SYNC1 state for 0xAA 0xAA 0x55 sequences */
            } else {
                s_parse.state = PARSE_SYNC0;
            }
            break;
        case PARSE_TYPE:
            s_parse.msg_type = b;
            s_parse.state = PARSE_SEQ_HI;
            break;
        case PARSE_SEQ_HI:
            s_parse.seq = (uint16_t)((uint16_t)b << 8);
            s_parse.state = PARSE_SEQ_LO;
            break;
        case PARSE_SEQ_LO:
            s_parse.seq |= (uint16_t)b;
            s_parse.state = PARSE_LEN_HI;
            break;
        case PARSE_LEN_HI:
            s_parse.len = (uint16_t)((uint16_t)b << 8);
            s_parse.state = PARSE_LEN_LO;
            break;
        case PARSE_LEN_LO:
            s_parse.len |= (uint16_t)b;
            s_parse.len_idx = 0U;
            if (s_parse.len > BLE_FRAME_MAX_PAYLOAD) {
                /* Oversized frame — discard and resync. */
                ESP_LOGW(TAG, "oversized frame len=%u, resync",
                         s_parse.len);
                s_parse.state = PARSE_SYNC0;
            } else if (s_parse.len == 0U) {
                s_parse.state = PARSE_CRC_LO;
            } else {
                s_parse.state = PARSE_PAYLOAD;
            }
            break;
        case PARSE_PAYLOAD:
            s_parse.payload[s_parse.len_idx++] = b;
            if (s_parse.len_idx >= s_parse.len) {
                s_parse.state = PARSE_CRC_LO;
            }
            break;
        case PARSE_CRC_LO:
            s_parse.crc = (uint16_t)b;
            s_parse.state = PARSE_CRC_HI;
            break;
        case PARSE_CRC_HI:
            s_parse.crc |= (uint16_t)((uint16_t)b << 8);
            /* Snapshot the complete frame into locals before resetting the
             * parser state, because process_parsed_frame() reads from
             * s_parse and may indirectly recurse the state machine. */
            process_parsed_frame();
            /* Reset parser, ready for the next frame. */
            s_parse.state    = PARSE_SYNC0;
            s_parse.msg_type = 0U;
            s_parse.seq      = 0U;
            s_parse.len      = 0U;
            s_parse.len_idx  = 0U;
            s_parse.crc      = 0U;
            break;
        }
    }
}

/* ------------------------------------------------------------------------- *
 * Parsed-frame dispatcher
 * ------------------------------------------------------------------------- */

static void process_parsed_frame(void)
{
    /* Verify CRC over header + payload (excluding the trailing CRC bytes). */
    uint8_t hdr[BLE_FRAME_HEADER_SIZE];
    hdr[0] = BLE_FRAME_SYNC0;
    hdr[1] = BLE_FRAME_SYNC1;
    hdr[2] = s_parse.msg_type;
    hdr[3] = (uint8_t)(s_parse.seq >> 8);
    hdr[4] = (uint8_t)(s_parse.seq & 0xFFU);
    hdr[5] = (uint8_t)(s_parse.len >> 8);
    hdr[6] = (uint8_t)(s_parse.len & 0xFFU);

    uint16_t calc = crc16_ccitt(hdr + 2, BLE_FRAME_HEADER_SIZE - 2U);
    if (s_parse.len > 0U) {
        /* Continue CRC over payload bytes. */
        for (uint16_t i = 0U; i < s_parse.len; ++i) {
            calc = (uint16_t)((calc << 8) ^
                    s_crc16_table[((calc >> 8) ^ s_parse.payload[i]) & 0xFFU]);
        }
    }
    if (calc != s_parse.crc) {
        ESP_LOGW(TAG, "CRC mismatch: calc=0x%04X got=0x%04X (seq=%u type=0x%02X)",
                 calc, s_parse.crc, s_parse.seq, s_parse.msg_type);
        return;
    }

    /* ACK frames are handled here, never dispatched to app handlers. */
    if (s_parse.msg_type == MSG_ACK) {
        handle_ack_payload(s_parse.payload, s_parse.len);
        return;
    }

    /* Send an ACK for reliable frames so the peer can retire its ACK
     * table entry. Reliability is determined by message-type convention
     * (see is_reliable_msg_type); the wire frame carries no flags byte. */
    if (is_reliable_msg_type(s_parse.msg_type)) {
        uint8_t ack[2] = {
            (uint8_t)(s_parse.seq >> 8),
            (uint8_t)(s_parse.seq & 0xFFU)
        };
        enqueue_frame(MSG_ACK, atomic_fetch_add(&s.next_seq, 1U),
                      ack, sizeof(ack), false, true);
    }

    /* Handle fragmentation. */
    uint8_t effective = s_parse.msg_type;
    if (effective == MSG_FRAME_DATA || effective == MSG_FRAME_END) {
        if (s_parse.len < FRAG_HDR_SIZE) {
            ESP_LOGW(TAG, "frag chunk too short len=%u", s_parse.len);
            return;
        }
        uint8_t  orig_type = s_parse.payload[0];
        uint16_t offset =
            (uint16_t)(((uint16_t)s_parse.payload[1] << 8) |
                        s_parse.payload[2]);
        uint16_t chunk_len = (uint16_t)(s_parse.len - FRAG_HDR_SIZE);

        if (!s.reasm.active) {
            s.reasm.active           = true;
            s.reasm.orig_type        = orig_type;
            s.reasm.expected_offset  = 0U;
            s.reasm.total_len        = 0U;
        }
        if (s.reasm.orig_type != orig_type) {
            ESP_LOGW(TAG, "frag orig_type mismatch (%u != %u), reset",
                     s.reasm.orig_type, orig_type);
            s.reasm.active          = true;
            s.reasm.orig_type       = orig_type;
            s.reasm.expected_offset = 0U;
            s.reasm.total_len       = 0U;
        }
        if (offset != s.reasm.expected_offset) {
            ESP_LOGW(TAG, "frag offset gap exp=%u got=%u, dropping",
                     s.reasm.expected_offset, offset);
            s.reasm.active = false;
            return;
        }
        if ((size_t)s.reasm.total_len + chunk_len > sizeof(s.reasm.buf)) {
            ESP_LOGW(TAG, "reassembled payload exceeds buffer");
            s.reasm.active = false;
            return;
        }
        memcpy(s.reasm.buf + s.reasm.total_len,
               s_parse.payload + FRAG_HDR_SIZE, chunk_len);
        s.reasm.total_len = (uint16_t)(s.reasm.total_len + chunk_len);
        s.reasm.expected_offset = (uint16_t)(s.reasm.expected_offset + chunk_len);

        if (effective == MSG_FRAME_END) {
            uint8_t  delivered_type = s.reasm.orig_type;
            uint16_t delivered_len  = s.reasm.total_len;
            /* Copy to a heap buffer so handlers can keep a reference if
             * they need to defer work. */
            uint8_t *delivered = (uint8_t *)malloc(delivered_len);
            if (delivered != NULL) {
                memcpy(delivered, s.reasm.buf, delivered_len);
                deliver_frame(delivered_type, s_parse.seq,
                              delivered, delivered_len);
                free(delivered);
            }
            s.reasm.active = false;
        }
        return;
    }

    /* Non-fragmented frame: dispatch directly. */
    deliver_frame(effective, s_parse.seq,
                  s_parse.payload, s_parse.len);
}

static void handle_ack_payload(const uint8_t *payload, uint16_t len)
{
    if (len != 2U) {
        return;
    }
    uint16_t acked_seq = (uint16_t)(((uint16_t)payload[0] << 8) |
                                     payload[1]);
    xSemaphoreTake(s.lock, portMAX_DELAY);
    for (int i = 0; i < ACK_TABLE_SIZE; ++i) {
        if (s.ack_tbl[i].in_use && s.ack_tbl[i].seq == acked_seq) {
            s.ack_tbl[i].in_use = false;
            break;
        }
    }
    xSemaphoreGive(s.lock);
}

static void deliver_frame(uint8_t msg_type, uint16_t seq,
                          const uint8_t *payload, uint16_t len)
{
    ble_transport_rx_cb_t cb = NULL;
    void *ctx = NULL;
    xSemaphoreTake(s.lock, portMAX_DELAY);
    for (int i = 0; i < MAX_HANDLERS; ++i) {
        if (s.handlers[i].in_use && s.handlers[i].msg_type == msg_type) {
            cb  = s.handlers[i].cb;
            ctx = s.handlers[i].user_ctx;
            break;
        }
    }
    xSemaphoreGive(s.lock);
    if (cb != NULL) {
        cb(msg_type, seq, payload, len, ctx);
    } else {
        ESP_LOGD(TAG, "no handler for msg_type=0x%02X (seq=%u len=%u)",
                 msg_type, seq, len);
    }
}

/* ------------------------------------------------------------------------- *
 * Transport task: drain send queue -> notify via NUS.
 * ------------------------------------------------------------------------- */

static void transport_task(void *arg)
{
    (void)arg;
    tx_item_t item;
    while (s.task_run) {
        if (xQueueReceive(s.send_queue, &item, portMAX_DELAY) != pdPASS) {
            continue;
        }
        if (!s.task_run) {
            break;
        }
        if (item.msg_type == 0U && item.payload_len == 0U && !item.reliable) {
            /* Sentinel used to unblock on stop. */
            continue;
        }

        /* Build the on-wire frame: header + payload + CRC16. */
        static uint8_t frame[BLE_FRAME_HEADER_SIZE + BLE_FRAME_MAX_PAYLOAD +
                              BLE_FRAME_CRC_SIZE];
        uint16_t total_len = (uint16_t)(BLE_FRAME_HEADER_SIZE +
                                        item.payload_len);
        frame[0] = BLE_FRAME_SYNC0;
        frame[1] = BLE_FRAME_SYNC1;
        /* The wire msg_type is the application msg_type verbatim — the wire
         * frame carries no flags byte; reliability is by message-type
         * convention (see is_reliable_msg_type). */
        frame[2] = item.msg_type;
        frame[3] = (uint8_t)(item.seq >> 8);
        frame[4] = (uint8_t)(item.seq & 0xFFU);
        frame[5] = (uint8_t)(item.payload_len >> 8);
        frame[6] = (uint8_t)(item.payload_len & 0xFFU);
        if (item.payload_len > 0U) {
            memcpy(frame + BLE_FRAME_HEADER_SIZE, item.payload,
                   item.payload_len);
        }
        uint16_t crc = crc16_ccitt(frame + 2,
                                   BLE_FRAME_HEADER_SIZE - 2U +
                                   item.payload_len);
        frame[total_len]     = (uint8_t)(crc & 0xFFU);
        frame[total_len + 1] = (uint8_t)(crc >> 8);
        total_len = (uint16_t)(total_len + BLE_FRAME_CRC_SIZE);

        esp_err_t err = ble_nus_send_notification(frame, total_len);
        if (err == ESP_ERR_INVALID_STATE) {
            /* Peer unsubscribed or disconnected — requeue for later? For
             * fire-and-forget frames we just drop; for reliable frames the
             * retransmit path will retry. */
            ESP_LOGD(TAG, "notify dropped (link down) seq=%u", item.seq);
        } else if (err != ESP_OK) {
            ESP_LOGW(TAG, "notify failed seq=%u err=0x%X", item.seq, err);
        }

        /* Refresh the ACK-table tx timestamp for reliable frames so the
         * retransmit logic does not immediately fire. */
        if (item.reliable) {
            xSemaphoreTake(s.lock, portMAX_DELAY);
            for (int i = 0; i < ACK_TABLE_SIZE; ++i) {
                if (s.ack_tbl[i].in_use &&
                    s.ack_tbl[i].seq == item.seq) {
                    s.ack_tbl[i].tx_tick_ms =
                        xTaskGetTickCount() * portTICK_PERIOD_MS;
                    break;
                }
            }
            xSemaphoreGive(s.lock);
        }

        /* Pace the outbound stream so we do not overrun the BLE link. */
        vTaskDelay(pdMS_TO_TICKS(2));
    }
    vTaskDelete(NULL);
}

/* ------------------------------------------------------------------------- *
 * Heartbeat + retransmit
 * ------------------------------------------------------------------------- */

static void heartbeat_cb(TimerHandle_t tmr)
{
    (void)tmr;
    if (!s.connected) {
        return;
    }
    /* Fire-and-forget heartbeat. */
    (void)ble_transport_send(MSG_HEARTBEAT, NULL, 0U, false);
    /* Sweep the ACK table for expired entries and retransmit. */
    resend_expired_acks();
}

static void resend_expired_acks(void)
{
    uint32_t now = xTaskGetTickCount() * portTICK_PERIOD_MS;
    xSemaphoreTake(s.lock, portMAX_DELAY);
    for (int i = 0; i < ACK_TABLE_SIZE; ++i) {
        ack_entry_t *e = &s.ack_tbl[i];
        if (!e->in_use) {
            continue;
        }
        if ((now - e->tx_tick_ms) < BLE_NUS_ACK_TIMEOUT_MS) {
            continue;
        }
        if (e->retries >= 3U) {
            ESP_LOGW(TAG, "seq=%u exhausted retries — giving up", e->seq);
            e->in_use = false;
            continue;
        }
        e->retries++;
        e->tx_tick_ms = now;
        /* Re-enqueue the frame (front of queue) for retransmit. */
        tx_item_t item = {0};
        item.msg_type    = e->msg_type;
        item.seq         = e->seq;
        item.reliable    = true;
        item.payload_len = e->payload_len;
        memcpy(item.payload, e->payload, e->payload_len);
        xQueueSendToFront(s.send_queue, &item, 0);
        ESP_LOGD(TAG, "retransmit seq=%u retry=%u", e->seq, e->retries);
    }
    xSemaphoreGive(s.lock);
}

/* ------------------------------------------------------------------------- *
 * Connection lifecycle hooks
 * ------------------------------------------------------------------------- */

void ble_transport_on_connect(uint16_t conn_handle)
{
    s.connected = true;
    s.conn_handle = conn_handle;
    ESP_LOGI(TAG, "link up conn=%u — starting heartbeat", conn_handle);
    (void)ble_transport_start();
    if (s.conn_cb != NULL) {
        s.conn_cb(true, s.conn_ctx);
    }
}

void ble_transport_on_disconnect(uint16_t conn_handle)
{
    (void)conn_handle;
    s.connected = false;
    s.conn_handle = BLE_HS_CONN_HANDLE_NONE;
    ESP_LOGI(TAG, "link down — stopping transport");
    (void)ble_transport_stop();
    if (s.conn_cb != NULL) {
        s.conn_cb(false, s.conn_ctx);
    }
}

bool ble_transport_is_connected(void)
{
    return s.connected;
}

/* ------------------------------------------------------------------------- *
 * Handler registration
 * ------------------------------------------------------------------------- */

esp_err_t ble_transport_register_handler(uint8_t msg_type,
                                         ble_transport_rx_cb_t cb,
                                         void *user_ctx)
{
    if (cb == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    esp_err_t ret = ESP_ERR_NO_MEM;
    xSemaphoreTake(s.lock, portMAX_DELAY);
    for (int i = 0; i < MAX_HANDLERS; ++i) {
        if (!s.handlers[i].in_use) {
            s.handlers[i].in_use    = true;
            s.handlers[i].msg_type  = msg_type;
            s.handlers[i].cb        = cb;
            s.handlers[i].user_ctx  = user_ctx;
            ret = ESP_OK;
            break;
        }
    }
    xSemaphoreGive(s.lock);
    return ret;
}

esp_err_t ble_transport_register_conn_cb(ble_transport_conn_cb_t cb,
                                         void *user_ctx)
{
    if (cb == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    s.conn_cb   = cb;
    s.conn_ctx  = user_ctx;
    return ESP_OK;
}
