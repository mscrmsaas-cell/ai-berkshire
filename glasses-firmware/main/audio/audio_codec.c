/**
 * @file audio_codec.c
 * @brief Audio codec: G.711 μ-law/A-law encode/decode + energy-based VAD.
 *
 * The glasses firmware targets low-bandwidth BLE NUS (244-byte payload per
 * notification), so a 16 kHz / 16-bit PCM source is reduced to 8-bit
 * G.711 μ-law (8 KB/s) before transmission. A small energy-based Voice
 * Activity Detector (VAD) gates transmission during silence so the BLE link
 * is not saturated with comfort-noise frames.
 *
 * An Opus path is gated behind CONFIG_AUDIO_CODEC_OPUS (not enabled by
 * default — Opus requires a third-party component such as
 * espressif/esp-opus). The G.711 path is the always-available fallback.
 *
 * Frame layout:
 *   15 ms @ 16 kHz = 240 samples
 *   PCM 16-bit    = 480 bytes raw
 *   G.711 μ-law   = 240 bytes encoded (fits in one BLE NUS frame)
 *
 * Public API declared at the top of this file (no separate header).
 */
#include "app_common.h"

#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <math.h>

#include "esp_log.h"
#include "esp_err.h"
#include "sdkconfig.h"

static const char *TAG = TAG_AUDIO;

/* ------------------------------------------------------------------------- *
 * Public API (no dedicated header — declared here)
 * ------------------------------------------------------------------------- */

typedef enum {
    AUDIO_CODEC_G711_ULAW = 0,  /**< ITU-T G.711 μ-law (North America/JP). */
    AUDIO_CODEC_G711_ALAW = 1,  /**< ITU-T G.711 A-law (Europe). */
    AUDIO_CODEC_OPUS       = 2,  /**< Opus (requires CONFIG_AUDIO_CODEC_OPUS). */
} audio_codec_t;

typedef struct {
    audio_codec_t codec;
    uint16_t      frame_ms;       /**< Frame duration in ms (15 typical). */
    uint16_t      sample_rate_hz;
    uint8_t       vad_threshold_dbfs;  /**< Voice detection threshold. */
} audio_codec_cfg_t;

esp_err_t audio_codec_init(const audio_codec_cfg_t *cfg);

/* Encode a PCM-16 frame into the codec's wire format. */
int audio_codec_encode(const int16_t *pcm_in, size_t pcm_samples,
                       uint8_t *out_buf, size_t out_cap);

/* Decode a wire-format frame back to PCM-16. */
int audio_codec_decode(const uint8_t *in_buf, size_t in_len,
                       int16_t *pcm_out, size_t pcm_cap);

/* Run VAD on a PCM-16 frame; returns true if voice activity is detected. */
bool audio_codec_vad(const int16_t *pcm_in, size_t pcm_samples);

/* Set/get the VAD threshold (dBFS, negative). */
void audio_codec_set_vad_threshold(uint8_t threshold_dbfs);
uint8_t audio_codec_get_vad_threshold(void);

/* ------------------------------------------------------------------------- *
 * Internal state
 * ------------------------------------------------------------------------- */

static audio_codec_cfg_t s_cfg = {
    .codec             = AUDIO_CODEC_G711_ULAW,
    .frame_ms          = 15U,
    .sample_rate_hz    = BSP_AUDIO_SAMPLE_RATE,
    .vad_threshold_dbfs = 35U,   /* -35 dBFS — typical quiet-office threshold */
};

/* VAD state: hangover counter so brief dips below threshold do not chop
 * trailing word ends. */
#define VAD_HANGOVER_FRAMES 5U
static uint8_t s_vad_hangover = 0U;

/* ------------------------------------------------------------------------- *
 * G.711 μ-law (ITU-T G.711 Annex 2)
 * ------------------------------------------------------------------------- */

/* Exponent lookup: 256-entry table indexed by bits [14:7] of the biased
 * magnitude. Maps the leading-1 position to a segment index 0..7. */
static const int s_ulaw_exp_lut[256] = {
    0,0,1,1,2,2,2,2,3,3,3,3,3,3,3,3,
    4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,
    5,5,5,5,5,5,5,5,5,5,5,5,5,5,5,5,
    5,5,5,5,5,5,5,5,5,5,5,5,5,5,5,5,
    6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,
    6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,
    6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,
    6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,
    7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
    7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
    7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
    7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
    7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
    7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
    7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
    7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
};

#define ULAW_BIAS  0x84
#define ULAW_CLIP 32635

static uint8_t pcm_ulaw_encode(int16_t sample)
{
    /* Canonical ITU-T G.711 μ-law encoder. */
    int sign = (sample >> 8) & 0x80;     /* 0x80 if negative, 0 if positive */
    int mag = sample;
    if (sign != 0) {
        mag = -sample;                  /* magnitude (always >= 0) */
    }
    if (mag > ULAW_CLIP) {
        mag = ULAW_CLIP;                /* clamp to 15-bit range */
    }
    mag += ULAW_BIAS;                   /* apply +132 bias */

    /* Determine the segment index 0..7 from bits [14:7] of the biased
     * magnitude via the lookup table. */
    int exponent = s_ulaw_exp_lut[(mag >> 7) & 0xFF];

    /* Mantissa is the 4 bits immediately below the leading-1 segment. */
    int mantissa = (mag >> (exponent + 3)) & 0x0F;

    /* Compose and invert (μ-law uses inverted bit pattern on the wire). */
    int ulawbyte = ~(sign | (exponent << 4) | mantissa);
    return (uint8_t)(ulawbyte & 0xFF);
}

static int16_t pcm_ulaw_decode(uint8_t ulaw)
{
    /* Standard μ-law decode table — computed inline to save flash. */
    static const int16_t ulaw_table[256] = {
        /* Standard G.711 μ-law decode table (ITU-T G.711 Annex 2). */
        -32124,-31100,-30076,-29052,-28028,-27004,-25980,-24956,
        -23932,-22908,-21884,-20860,-19836,-18812,-17788,-16764,
        -15996,-15484,-14972,-14460,-13948,-13436,-12924,-12412,
        -11900,-11388,-10876,-10364, -9852, -9340, -8828, -8316,
         -7932, -7676, -7420, -7164, -6908, -6652, -6396, -6140,
         -5884, -5628, -5372, -5116, -4860, -4604, -4348, -4092,
         -3900, -3772, -3644, -3516, -3388, -3260, -3132, -3004,
         -2876, -2748, -2620, -2492, -2364, -2236, -2108, -1980,
         -1884, -1820, -1756, -1692, -1628, -1564, -1500, -1436,
         -1372, -1308, -1244, -1180, -1116, -1052,  -988,  -924,
          -876,  -844,  -812,  -780,  -748,  -716,  -684,  -652,
          -620,  -588,  -556,  -524,  -492,  -460,  -428,  -396,
          -372,  -356,  -340,  -324,  -308,  -292,  -276,  -260,
          -244,  -228,  -212,  -196,  -180,  -164,  -148,  -132,
          -120,  -112,  -104,   -96,   -88,   -80,   -72,   -64,
           -56,   -48,   -40,   -32,   -24,   -16,    -8,     0,
          32124, 31100, 30076, 29052, 28028, 27004, 25980, 24956,
          23932, 22908, 21884, 20860, 19836, 18812, 17788, 16764,
          15996, 15484, 14972, 14460, 13948, 13436, 12924, 12412,
          11900, 11388, 10876, 10364,  9852,  9340,  8828,  8316,
           7932,  7676,  7420,  7164,  6908,  6652,  6396,  6140,
           5884,  5628,  5372,  5116,  4860,  4604,  4348,  4092,
           3900,  3772,  3644,  3516,  3388,  3260,  3132,  3004,
           2876,  2748,  2620,  2492,  2364,  2236,  2108,  1980,
           1884,  1820,  1756,  1692,  1628,  1564,  1500,  1436,
           1372,  1308,  1244,  1180,  1116,  1052,   988,   924,
            876,   844,   812,   780,   748,   716,   684,   652,
            620,   588,   556,   524,   492,   460,   428,   396,
            372,   356,   340,   324,   308,   292,   276,   260,
            244,   228,   212,   196,   180,   164,   148,   132,
            120,   112,   104,    96,    88,    80,    72,    64,
             56,    48,    40,    32,    24,    16,     8,     1,
    };
    return ulaw_table[ulaw];
}

/* ------------------------------------------------------------------------- *
 * G.711 A-law (ITU-T G.711 Annex 1)
 * ------------------------------------------------------------------------- */

#define ALAW_CLIP 32767

static uint8_t pcm_alaw_encode(int16_t sample)
{
    /* Canonical ITU-T G.711 A-law encoder. */
    int sign = ((~sample) >> 8) & 0x80;  /* 0x80 for non-negative input */
    int mag = sample;
    if (sign == 0) {
        /* sample was negative -> take magnitude */
        mag = -sample;
    }
    if (mag > ALAW_CLIP) {
        mag = ALAW_CLIP;
    }

    int exponent = 0;
    int mantissa;
    if (mag < 256) {
        /* Segment 0: linear encoding of the high nibble. */
        exponent = 0;
        mantissa = (mag >> 4) & 0x0F;
    } else {
        /* Find the segment containing the magnitude. */
        exponent = 1;
        while ((mag & (1 << (exponent + 4))) == 0 && exponent < 7) {
            exponent++;
        }
        mantissa = (mag >> (exponent + 3)) & 0x0F;
    }
    /* Compose and XOR with 0x55 (A-law quirk: alternate bits flipped). */
    int alawbyte = (sign | (exponent << 4) | mantissa) ^ 0x55;
    return (uint8_t)(alawbyte & 0xFF);
}

static int16_t pcm_alaw_decode(uint8_t alaw)
{
    alaw ^= 0x55U;                 /* undo A-law inversion */
    uint8_t sign = alaw & 0x80U;
    uint8_t segment = (alaw >> 4) & 0x07U;
    uint8_t mantissa = alaw & 0x0FU;
    int32_t t;
    if (segment == 0) {
        t = ((int32_t)(mantissa << 4)) + 8;
    } else {
        t = (((int32_t)(mantissa << 4)) + 0x108U) << (segment - 1);
    }
    if (sign) t = -t;
    return (int16_t)t;
}

/* ------------------------------------------------------------------------- *
 * Initialisation
 * ------------------------------------------------------------------------- */

esp_err_t audio_codec_init(const audio_codec_cfg_t *cfg)
{
    if (cfg == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (cfg->codec == AUDIO_CODEC_OPUS &&
        !CONFIG_AUDIO_CODEC_OPUS) {
        ESP_LOGE(TAG, "Opus codec selected but CONFIG_AUDIO_CODEC_OPUS is off");
        return APP_ERR_NOT_SUPPORTED;
    }
    s_cfg = *cfg;
    s_vad_hangover = 0U;
    ESP_LOGI(TAG, "codec initialised: codec=%d, frame=%ums, sr=%uHz, vad=%ddBFS",
             (int)s_cfg.codec, s_cfg.frame_ms, s_cfg.sample_rate_hz,
             -(int)s_cfg.vad_threshold_dbfs);
    return ESP_OK;
}

void audio_codec_set_vad_threshold(uint8_t threshold_dbfs)
{
    s_cfg.vad_threshold_dbfs = threshold_dbfs;
}

uint8_t audio_codec_get_vad_threshold(void)
{
    return s_cfg.vad_threshold_dbfs;
}

/* ------------------------------------------------------------------------- *
 * Encode / decode
 * ------------------------------------------------------------------------- */

int audio_codec_encode(const int16_t *pcm_in, size_t pcm_samples,
                       uint8_t *out_buf, size_t out_cap)
{
    if (pcm_in == NULL || out_buf == NULL) {
        return -1;
    }
    switch (s_cfg.codec) {
    case AUDIO_CODEC_G711_ULAW:
        if (out_cap < pcm_samples) {
            ESP_LOGW(TAG, "ulaw encode: out_cap=%u < %u",
                     (unsigned)out_cap, (unsigned)pcm_samples);
            return -1;
        }
        for (size_t i = 0U; i < pcm_samples; ++i) {
            out_buf[i] = pcm_ulaw_encode(pcm_in[i]);
        }
        return (int)pcm_samples;

    case AUDIO_CODEC_G711_ALAW:
        if (out_cap < pcm_samples) {
            return -1;
        }
        for (size_t i = 0U; i < pcm_samples; ++i) {
            out_buf[i] = pcm_alaw_encode(pcm_in[i]);
        }
        return (int)pcm_samples;

    case AUDIO_CODEC_OPUS:
#ifdef CONFIG_AUDIO_CODEC_OPUS
        /* Real Opus path would invoke libopus_encode() here. The user is
         * expected to link a third-party Opus component. */
        ESP_LOGE(TAG, "Opus encode path not implemented — add libopus");
#else
        ESP_LOGE(TAG, "Opus not compiled in");
#endif
        return -1;

    default:
        return -1;
    }
}

int audio_codec_decode(const uint8_t *in_buf, size_t in_len,
                       int16_t *pcm_out, size_t pcm_cap)
{
    if (in_buf == NULL || pcm_out == NULL) {
        return -1;
    }
    if (pcm_cap < in_len) {
        return -1;
    }
    switch (s_cfg.codec) {
    case AUDIO_CODEC_G711_ULAW:
        for (size_t i = 0U; i < in_len; ++i) {
            pcm_out[i] = pcm_ulaw_decode(in_buf[i]);
        }
        return (int)in_len;

    case AUDIO_CODEC_G711_ALAW:
        for (size_t i = 0U; i < in_len; ++i) {
            pcm_out[i] = pcm_alaw_decode(in_buf[i]);
        }
        return (int)in_len;

    case AUDIO_CODEC_OPUS:
#ifdef CONFIG_AUDIO_CODEC_OPUS
        ESP_LOGE(TAG, "Opus decode path not implemented — add libopus");
#else
        ESP_LOGE(TAG, "Opus not compiled in");
#endif
        return -1;

    default:
        return -1;
    }
}

/* ------------------------------------------------------------------------- *
 * VAD — energy-based with hangover hysteresis
 * ------------------------------------------------------------------------- */

bool audio_codec_vad(const int16_t *pcm_in, size_t pcm_samples)
{
    if (pcm_in == NULL || pcm_samples == 0U) {
        return false;
    }
    /* Compute RMS in fixed-point (Q.15) to avoid pulling in libm. */
    int64_t sum_sq = 0LL;
    for (size_t i = 0U; i < pcm_samples; ++i) {
        int32_t v = pcm_in[i];
        sum_sq += (int64_t)v * (int64_t)v;
    }
    /* rms = sqrt(sum_sq / N). Use integer sqrt for production simplicity. */
    int64_t mean_sq = sum_sq / (int64_t)pcm_samples;
    if (mean_sq <= 0LL) {
        s_vad_hangover = 0U;
        return false;
    }
    /* dBFS = 20 * log10(rms / 32768). We compute the |dBFS| without log
     * by comparing rms against threshold-magnitude lookup:
     *   threshold_dbfs = -X dBFS  =>  rms = 32768 * 10^(-X/20)
     * Precompute powers of 10 via pow10_lookup[] for the integer thresholds. */
    static const uint32_t rms_threshold[64] = {
        /* 0 dBFS..63 dBFS attenuation -> rms threshold */
        32768, 29204, 26028, 23197, 20675, 18426, 16422, 14637,
        13045, 11626, 10362,  9234,  8231,  7337,  6537,  5825,
         5191,  4626,  4123,  3674,  3276,  2921,  2605,  2322,
         2069,  1845,  1645,  1467,  1308,  1166,  1039,   926,
          825,   736,   656,   584,   521,   464,   414,   369,
          329,   293,   261,   233,   207,   185,   165,   147,
          131,   117,   104,    93,    83,    74,    66,    59,
           53,    47,    42,    37,    33,    29,    26,    23,
    };
    /* Integer sqrt of mean_sq (Newton-Raphson, 4 iterations). */
    uint64_t r = mean_sq;
    uint64_t x = 32768U;  /* initial guess */
    for (int i = 0; i < 4; ++i) {
        if (x == 0U) break;
        x = (x + r / x) >> 1;
    }
    uint32_t rms = (uint32_t)x;

    uint8_t thresh_dbfs = s_cfg.vad_threshold_dbfs;
    if (thresh_dbfs >= 64U) thresh_dbfs = 63U;
    uint32_t thresh = rms_threshold[thresh_dbfs];

    if (rms >= thresh) {
        s_vad_hangover = VAD_HANGOVER_FRAMES;
        return true;
    }
    if (s_vad_hangover > 0U) {
        s_vad_hangover--;
        return true; /* hangover: keep "active" for a few frames */
    }
    return false;
}
