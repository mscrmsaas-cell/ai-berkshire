/**
 * @file audio_pipeline.c
 * @brief Bidirectional audio pipeline: record / playback over BLE NUS.
 *
 * Recording path (glasses -> bag terminal):
 *   INMP441 (I2S RX) -> bsp_audio_read() -> VAD -> audio_codec_encode()
 *     -> ble_transport_send(MSG_AUDIO_CHUNK)
 *
 * Playback path (bag terminal -> glasses):
 *   ble_transport_register_handler(MSG_AUDIO_CHUNK)
 *     -> playback queue -> MAX98357A via bsp_audio_write()
 *
 * Design notes:
 *   * 15 ms frames @ 16 kHz = 240 samples. PCM-16 = 480 bytes raw,
 *     G.711 μ-law = 240 bytes encoded (fits in a single BLE NUS frame).
 *   * VAD gates transmission: silent frames are dropped (only ~5 % of the
 *     worst-case BLE bandwidth is needed for typical speech).
 *   * Audio is best-effort traffic (MSG_AUDIO_CHUNK is fire-and-forget per
 *     the transport reliability policy in ble_transport.c).
 *   * Playback uses a separate FreeRTOS task so a blocking I2S write does
 *     not stall the BLE transport task.
 *
 * Public API declared at the top of this file.
 */
#include "app_common.h"
#include "bsp_i2s_audio.h"

#include <string.h>
#include <stdatomic.h>

#include "esp_log.h"
#include "esp_err.h"
#include "esp_check.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"

/* Forward-declared from ble_transport.h. */
#include "ble_transport.h"
/* Forward-declared from audio_codec.c. */
typedef enum {
    AUDIO_CODEC_G711_ULAW = 0,
    AUDIO_CODEC_G711_ALAW = 1,
    AUDIO_CODEC_OPUS       = 2,
} audio_codec_t;
typedef struct {
    audio_codec_t codec;
    uint16_t      frame_ms;
    uint16_t      sample_rate_hz;
    uint8_t       vad_threshold_dbfs;
} audio_codec_cfg_t;
esp_err_t audio_codec_init(const audio_codec_cfg_t *cfg);
int audio_codec_encode(const int16_t *pcm_in, size_t pcm_samples,
                       uint8_t *out_buf, size_t out_cap);
int audio_codec_decode(const uint8_t *in_buf, size_t in_len,
                       int16_t *pcm_out, size_t pcm_cap);
bool audio_codec_vad(const int16_t *pcm_in, size_t pcm_samples);

static const char *TAG = TAG_AUDIO;

/* ------------------------------------------------------------------------- *
 * Message-type values (mirror of shared/protocols/message_types.h)
 *
 * Duplicated as #defines here so this module builds whether or not
 * message_types.h uses the same enum identifiers.
 * ------------------------------------------------------------------------- */

#define MSG_AUDIO_CHUNK       0x30U
#define MSG_ASR_RESULT        0x31U
#define MSG_TTS_REQUEST       0x32U
#define MSG_TTS_AUDIO         0x33U

/* ------------------------------------------------------------------------- *
 * Frame geometry
 * ------------------------------------------------------------------------- */

#define FRAME_MS              15U
#define SAMPLE_RATE_HZ        BSP_AUDIO_SAMPLE_RATE
/* 15 ms @ 16 kHz = 240 samples per frame. */
#define SAMPLES_PER_FRAME     (SAMPLE_RATE_HZ * FRAME_MS / 1000U)
#define PCM_BYTES_PER_FRAME   (SAMPLES_PER_FRAME * sizeof(int16_t))
#define ENCODED_BYTES_PER_FRAME SAMPLES_PER_FRAME  /* G.711 = 8 bits/sample */

#define PLAYBACK_QUEUE_LEN    8U

/* ------------------------------------------------------------------------- *
 * Public API (no dedicated header — declared here)
 * ------------------------------------------------------------------------- */

esp_err_t audio_pipeline_init(void);
esp_err_t audio_pipeline_start_recording(void);
void      audio_pipeline_stop_recording(void);
esp_err_t audio_pipeline_start_playback(void);
void      audio_pipeline_stop_playback(void);

/* ------------------------------------------------------------------------- *
 * Module state
 * ------------------------------------------------------------------------- */

static struct {
    bool             initialised;
    bool             recording;
    bool             playing;
    TaskHandle_t     rec_task;
    TaskHandle_t     pb_task;
    QueueHandle_t    pb_queue;          /* encoded frames waiting to play */
    SemaphoreHandle_t pb_lock;
    /* Statistics for debugging / DP reporting. */
    _Atomic uint32_t frames_sent;
    _Atomic uint32_t frames_received;
    _Atomic uint32_t frames_dropped_vad;
} s = {0};

/* ------------------------------------------------------------------------- *
 * Inbound (BLE -> I2S) handler
 *
 * Runs in the BLE transport task context. We copy the encoded bytes onto
 * the playback queue (fast, non-blocking) and let the dedicated playback
 * task perform the blocking I2S write. This keeps the BLE transport task
 * responsive.
 * ------------------------------------------------------------------------- */

typedef struct {
    uint8_t  encoded[ENCODED_BYTES_PER_FRAME];
    uint16_t len;
} playback_chunk_t;

static void on_audio_chunk_rx(uint8_t msg_type, uint16_t seq,
                              const uint8_t *payload, uint16_t payload_len,
                              void *user_ctx)
{
    (void)msg_type;
    (void)seq;
    (void)user_ctx;

    if (payload_len > ENCODED_BYTES_PER_FRAME) {
        payload_len = ENCODED_BYTES_PER_FRAME;
    }
    playback_chunk_t chunk = {0};
    chunk.len = payload_len;
    memcpy(chunk.encoded, payload, payload_len);

    if (s.pb_queue != NULL) {
        /* Don't block — if the queue is full the playback task is behind,
         * drop this frame to maintain real-time pacing. */
        if (xQueueSend(s.pb_queue, &chunk, 0) != pdPASS) {
            ESP_LOGD(TAG, "playback queue full — dropping frame");
        } else {
            atomic_fetch_add(&s.frames_received, 1U);
        }
    }
}

/* ------------------------------------------------------------------------- *
 * Playback task — drains the queue, decodes, writes to I2S.
 * ------------------------------------------------------------------------- */

static void playback_task(void *arg)
{
    (void)arg;
    static int16_t pcm_buf[SAMPLES_PER_FRAME];
    playback_chunk_t chunk;
    ESP_LOGI(TAG, "playback task started");
    while (s.playing) {
        if (xQueueReceive(s.pb_queue, &chunk, pdMS_TO_TICKS(100)) != pdPASS) {
            /* Queue empty: write silence to keep the I2S clock ticking
             * (avoids clicks/pops on the next real frame). */
            memset(pcm_buf, 0, sizeof(pcm_buf));
            (void)bsp_audio_write((const uint8_t *)pcm_buf,
                                   sizeof(pcm_buf));
            continue;
        }
        int n = audio_codec_decode(chunk.encoded, chunk.len,
                                    pcm_buf, SAMPLES_PER_FRAME);
        if (n <= 0) {
            ESP_LOGW(TAG, "decode failed (%d)", n);
            continue;
        }
        size_t bytes = (size_t)n * sizeof(int16_t);
        int written = bsp_audio_write((const uint8_t *)pcm_buf, bytes);
        if (written < 0) {
            ESP_LOGW(TAG, "i2s write failed (%d)", written);
        }
    }
    ESP_LOGI(TAG, "playback task exiting");
    vTaskDelete(NULL);
}

/* ------------------------------------------------------------------------- *
 * Recording task — reads I2S, runs VAD, encodes, ships via BLE.
 * ------------------------------------------------------------------------- */

static void recording_task(void *arg)
{
    (void)arg;
    static int16_t pcm_buf[SAMPLES_PER_FRAME];
    static uint8_t enc_buf[ENCODED_BYTES_PER_FRAME];

    ESP_LOGI(TAG, "recording task started (frame=%ums sr=%uHz)",
             FRAME_MS, SAMPLE_RATE_HZ);
    while (s.recording) {
        /* bsp_audio_read blocks until PCM is available from the I2S DMA. */
        int n = bsp_audio_read((uint8_t *)pcm_buf, PCM_BYTES_PER_FRAME);
        if (n < (int)PCM_BYTES_PER_FRAME) {
            ESP_LOGW(TAG, "i2s short read: %d/%u", n, PCM_BYTES_PER_FRAME);
            vTaskDelay(pdMS_TO_TICKS(5));
            continue;
        }

        /* VAD: skip transmission during silence to save BLE bandwidth.
         * We still consume the I2S buffer to avoid backpressure. */
        if (!audio_codec_vad(pcm_buf, SAMPLES_PER_FRAME)) {
            atomic_fetch_add(&s.frames_dropped_vad, 1U);
            continue;
        }

        int enc = audio_codec_encode(pcm_buf, SAMPLES_PER_FRAME,
                                      enc_buf, sizeof(enc_buf));
        if (enc <= 0) {
            ESP_LOGW(TAG, "encode failed (%d)", enc);
            continue;
        }
        /* Audio is best-effort: send unreliable so the transport does not
         * queue ACK retransmits for streaming media. */
        esp_err_t err = ble_transport_send(MSG_AUDIO_CHUNK,
                                            enc_buf, (uint16_t)enc, false);
        if (err == ESP_OK) {
            atomic_fetch_add(&s.frames_sent, 1U);
        } else if (err != ESP_ERR_TIMEOUT) {
            ESP_LOGW(TAG, "ble send failed: 0x%X", err);
        }
    }
    ESP_LOGI(TAG, "recording task exiting");
    vTaskDelete(NULL);
}

/* ------------------------------------------------------------------------- *
 * Public API
 * ------------------------------------------------------------------------- */

esp_err_t audio_pipeline_init(void)
{
    if (s.initialised) {
        return ESP_OK;
    }

    /* Init codec with the railway default: μ-law @ 16 kHz, VAD -35 dBFS. */
    audio_codec_cfg_t cfg = {
        .codec             = AUDIO_CODEC_G711_ULAW,
        .frame_ms          = FRAME_MS,
        .sample_rate_hz    = SAMPLE_RATE_HZ,
        .vad_threshold_dbfs = 35U,
    };
    esp_err_t ret = audio_codec_init(&cfg);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "audio_codec_init failed: 0x%X", ret);
        return ret;
    }

    /* Playback queue (encoded frames waiting for I2S write). */
    s.pb_queue = xQueueCreate(PLAYBACK_QUEUE_LEN, sizeof(playback_chunk_t));
    s.pb_lock  = xSemaphoreCreateMutex();
    if (s.pb_queue == NULL || s.pb_lock == NULL) {
        return ESP_ERR_NO_MEM;
    }

    /* Subscribe to inbound audio chunks. */
    ret = ble_transport_register_handler(MSG_AUDIO_CHUNK,
                                         on_audio_chunk_rx, NULL);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "register_handler failed: 0x%X", ret);
        return ret;
    }
    /* Subscribe to TTS audio frames as well — they are decoded and queued
     * for playback just like live audio chunks. */
    (void)ble_transport_register_handler(MSG_TTS_AUDIO,
                                          on_audio_chunk_rx, NULL);

    s.initialised = true;
    ESP_LOGI(TAG, "audio pipeline initialised (pcm=%uB enc=%uB)",
             PCM_BYTES_PER_FRAME, ENCODED_BYTES_PER_FRAME);
    return ESP_OK;
}

esp_err_t audio_pipeline_start_recording(void)
{
    if (!s.initialised) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s.recording) {
        return ESP_OK;
    }
    s.recording = true;
    BaseType_t ok = xTaskCreatePinnedToCore(
        recording_task, "audio_rec", 4096, NULL, 6, &s.rec_task, 1);
    if (ok != pdPASS) {
        s.recording = false;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void audio_pipeline_stop_recording(void)
{
    s.recording = false;
    /* The task self-deletes on next loop iteration. */
}

esp_err_t audio_pipeline_start_playback(void)
{
    if (!s.initialised) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s.playing) {
        return ESP_OK;
    }
    s.playing = true;
    BaseType_t ok = xTaskCreatePinnedToCore(
        playback_task, "audio_pb", 4096, NULL, 6, &s.pb_task, 1);
    if (ok != pdPASS) {
        s.playing = false;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void audio_pipeline_stop_playback(void)
{
    s.playing = false;
    /* Drain any queued frames so the task exits cleanly. */
    if (s.pb_queue != NULL) {
        xQueueReset(s.pb_queue);
    }
}
