/**
 * @file power_manager.c
 * @brief Power management: dynamic frequency scaling, auto screen-off,
 *        deep sleep + wake-source configuration.
 *
 * The glasses firmware uses a three-tier power strategy:
 *
 *   1. ACTIVE     — CPU at 240 MHz, full peripherals, screen on.
 *   2. IDLE       — CPU drops to 80 MHz automatically (ESP-IDF PM), screen
 *                   dims to 0 % after `auto_sleep_ms` of no activity. BLE
 *                   stays connected so the bag terminal can wake the device
 *                   via a notification or the user via a GPIO button.
 *   3. DEEP SLEEP — CPU off, RAM/RTC retained, radio off. Woken by GPIO
 *                   (Pogo Pin attach / wake button) or a periodic timer.
 *                   Used on critical battery or explicit shutdown.
 *
 * Wake sources registered by power_manager_init():
 *   * ext1: Pogo Pin detect GPIO (level-low wakes on camera attach).
 *   * ext1: Wake button GPIO (level-low).
 *   * Timer: every POWER_DEEP_SLEEP_TIMER_WAKE_S the glasses wake briefly
 *     to check for pending BLE messages and refresh the heartbeat.
 *
 * Public API declared at the top of this file.
 */
#include "app_common.h"

#include <string.h>

#include "esp_log.h"
#include "esp_err.h"
#include "esp_pm.h"
#include "esp_sleep.h"
#include "esp_timer.h"
#include "driver/rtc_io.h"
#include "driver/gpio.h"
#include "soc/rtc.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/timers.h"

#include "sdkconfig.h"

static const char *TAG = TAG_POWER;

/* ------------------------------------------------------------------------- *
 * Public API (no dedicated header — declared here)
 * ------------------------------------------------------------------------- */

esp_err_t power_manager_init(void);
void      power_manager_activity_notify(void);
void      power_manager_set_auto_screen_off(uint32_t timeout_ms);
void      power_manager_screen_off(void);
void      power_manager_screen_on(void);
void      power_manager_enter_deep_sleep(void);

/* Forward-declared from display_manager.c. */
void display_manager_set_brightness(uint8_t percent);

/* ------------------------------------------------------------------------- *
 * Configuration constants
 * ------------------------------------------------------------------------- */

#define PM_MAX_FREQ_MHZ    240
#define PM_MIN_FREQ_MHZ    80
#define PM_LIGHT_SLEEP     false   /* keep BLE light-sleep off; controller manages */

/* Periodic deep-sleep wake timer. The glasses wake every 60 s to flush
 * pending telemetry. Set to 0 to disable. */
#define PM_DEEP_SLEEP_TIMER_WAKE_S  60ULL

/* Auto-screen-off defaults. */
#define PM_DEFAULT_AUTO_SLEEP_MS    30000U
#define PM_DIMMED_BRIGHTNESS_PCT    0U     /* 0 % when dimmed */
#define PM_ACTIVE_BRIGHTNESS_PCT    60U    /* 60 % when active */

/* ------------------------------------------------------------------------- *
 * Module state
 * ------------------------------------------------------------------------- */

static struct {
    bool             initialised;
    TimerHandle_t    screen_off_tmr;
    _Atomic uint32_t auto_sleep_ms;
    _Atomic bool     screen_on;
    _Atomic uint32_t last_activity_tick_ms;
} s = {0};

/* GPIOs that can wake the device from deep sleep. By default we wake on
 * the Pogo Pin detect pin (camera attach). Add a wake button here when
 * the hardware has one. */
static const gpio_num_t s_wake_gpios[] = {
#ifdef CONFIG_BSP_POGO_DETECT_GPIO
    (gpio_num_t)CONFIG_BSP_POGO_DETECT_GPIO,
#endif
};
static const size_t s_wake_gpios_n =
    sizeof(s_wake_gpios) / sizeof(s_wake_gpios[0]);

/* ------------------------------------------------------------------------- *
 * Auto-screen-off timer
 * ------------------------------------------------------------------------- */

static void screen_off_timer_cb(TimerHandle_t tmr)
{
    (void)tmr;
    if (!s.screen_on) {
        return;  /* already off */
    }
    uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
    uint32_t last = atomic_load(&s.last_activity_tick_ms);
    uint32_t timeout = atomic_load(&s.auto_sleep_ms);
    if ((now - last) >= timeout) {
        ESP_LOGI(TAG, "auto screen-off after %ums idle", timeout);
        power_manager_screen_off();
    } else {
        /* Not enough idle time yet — re-arm the timer for the remainder. */
        uint32_t remain = timeout - (now - last);
        xTimerChangePeriod(tmr, pdMS_TO_TICKS(remain), 0);
        xTimerStart(tmr, 0);
    }
}

/* ------------------------------------------------------------------------- *
 * Public API
 * ------------------------------------------------------------------------- */

esp_err_t power_manager_init(void)
{
    if (s.initialised) {
        return ESP_OK;
    }

    /* --- 1. Dynamic frequency scaling (ESP-IDF Power Management) ---
     * Allows the system to drop CPU frequency when idle. The BLE stack
     * holds a PM lock during radio activity, so DFS does not affect the
     * radio's real-time behaviour. */
    esp_pm_config_esp32s3_t pm_cfg = {
        .max_freq_mhz       = PM_MAX_FREQ_MHZ,
        .min_freq_mhz       = PM_MIN_FREQ_MHZ,
        .light_sleep_enable = PM_LIGHT_SLEEP,
    };
    esp_err_t ret = esp_pm_configure(&pm_cfg);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "esp_pm_configure failed: %s (continuing without DFS)",
                 esp_err_to_name(ret));
        /* Non-fatal: continue without DFS. */
    } else {
        ESP_LOGI(TAG, "DFS enabled: %u-%u MHz",
                 PM_MIN_FREQ_MHZ, PM_MAX_FREQ_MHZ);
    }

    /* --- 2. Auto-screen-off timer --- */
    atomic_store(&s.auto_sleep_ms, PM_DEFAULT_AUTO_SLEEP_MS);
    atomic_store(&s.screen_on, true);
    atomic_store(&s.last_activity_tick_ms,
                  (uint32_t)(esp_timer_get_time() / 1000));
    s.screen_off_tmr = xTimerCreate(
        "pm_scr_off", pdMS_TO_TICKS(PM_DEFAULT_AUTO_SLEEP_MS),
        pdFALSE, NULL, screen_off_timer_cb);
    if (s.screen_off_tmr == NULL) {
        return ESP_ERR_NO_MEM;
    }
    xTimerStart(s.screen_off_tmr, 0);

    /* --- 3. Wake sources for deep sleep ---
     * Configure ext1 wake (multiple GPIOs, any low) so any wake source
     * can rouse the device. */
    if (s_wake_gpios_n > 0U) {
        uint64_t mask = 0ULL;
        for (size_t i = 0U; i < s_wake_gpios_n; ++i) {
            mask |= (1ULL << (uint64_t)s_wake_gpios[i]);
        }
        ret = esp_sleep_enable_ext1_wakeup(
            mask, ESP_EXT1_WAKEUP_ANY_LOW);
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "ext1 wakeup config failed: %s",
                     esp_err_to_name(ret));
        }
        /* Hold the GPIO RTC state so we don't lose the level when entering
         * deep sleep. */
        for (size_t i = 0U; i < s_wake_gpios_n; ++i) {
            if (rtc_gpio_is_valid_gpio(s_wake_gpios[i])) {
                rtc_gpio_pullup_en(s_wake_gpios[i]);
            }
        }
    }

    /* Optional periodic timer wake (every 60 s) to flush telemetry even
     * during deep sleep. */
#if PM_DEEP_SLEEP_TIMER_WAKE_S > 0
    esp_sleep_enable_timer_wakeup(
        PM_DEEP_SLEEP_TIMER_WAKE_S * 1000ULL * 1000ULL);
#endif

    s.initialised = true;
    ESP_LOGI(TAG, "power manager initialised (auto-screen-off=%ums, "
                  "wake_gpios=%zu)",
             PM_DEFAULT_AUTO_SLEEP_MS, s_wake_gpios_n);
    return ESP_OK;
}

void power_manager_activity_notify(void)
{
    /* User activity (touch, button, BLE message) — reset the screen-off
     * timer and ensure the screen is back on. */
    atomic_store(&s.last_activity_tick_ms,
                  (uint32_t)(esp_timer_get_time() / 1000));
    if (!s.screen_on) {
        power_manager_screen_on();
    }
    if (s.screen_off_tmr != NULL) {
        xTimerReset(s.screen_off_tmr, 0);
    }
}

void power_manager_set_auto_screen_off(uint32_t timeout_ms)
{
    atomic_store(&s.auto_sleep_ms, timeout_ms);
    if (s.screen_off_tmr != NULL) {
        xTimerChangePeriod(s.screen_off_tmr, pdMS_TO_TICKS(timeout_ms), 0);
        xTimerReset(s.screen_off_tmr, 0);
    }
}

void power_manager_screen_off(void)
{
    atomic_store(&s.screen_on, false);
    display_manager_set_brightness(PM_DIMMED_BRIGHTNESS_PCT);
    ESP_LOGI(TAG, "screen off (backlight 0%%)");
}

void power_manager_screen_on(void)
{
    atomic_store(&s.screen_on, true);
    atomic_store(&s.last_activity_tick_ms,
                  (uint32_t)(esp_timer_get_time() / 1000));
    display_manager_set_brightness(PM_ACTIVE_BRIGHTNESS_PCT);
    if (s.screen_off_tmr != NULL) {
        xTimerReset(s.screen_off_tmr, 0);
    }
    ESP_LOGI(TAG, "screen on (backlight %u%%)", PM_ACTIVE_BRIGHTNESS_PCT);
}

void power_manager_enter_deep_sleep(void)
{
    ESP_LOGW(TAG, "entering deep sleep — BLE will disconnect; "
                  "wake on GPIO or %llus timer",
             (unsigned long long)PM_DEEP_SLEEP_TIMER_WAKE_S);

    /* Stop the auto-screen-off timer (the screen is irrelevant in deep
     * sleep). */
    if (s.screen_off_tmr != NULL) {
        xTimerStop(s.screen_off_tmr, 0);
    }

    /* Dim the screen before sleeping. */
    display_manager_set_brightness(0U);

    /* Configure the IO state to minimise leakage. */
    esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_PERIPH, ESP_PD_OPTION_ON);
    esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_SLOW_MEM, ESP_PD_OPTION_ON);
    esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_FAST_MEM, ESP_PD_OPTION_ON);
    esp_sleep_pd_config(ESP_PD_DOMAIN_XTAL, ESP_PD_OPTION_OFF);
    esp_sleep_pd_config(ESP_PD_DOMAIN_RTC8M, ESP_PD_OPTION_OFF);

    /* Enter deep sleep. On wake the device reboots (cold boot). */
    esp_deep_sleep_start();
    /* unreachable */
}
