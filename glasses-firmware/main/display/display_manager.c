/**
 * @file display_manager.c
 * @brief LVGL display manager for the GC9A01 240x240 round LCD.
 *
 * Responsibilities:
 *   * Boot LVGL core (lv_init) and register a display driver with double
 *     buffering wired to the BSP's LVGL flush callback (bsp_lcd_lvgl_flush_cb).
 *   * Provide a recursive mutex so non-LVGL tasks can safely call LVGL APIs
 *     (display_manager_lock / display_manager_unlock).
 *   * Spawn a dedicated "lvgl" task that drives lv_timer_handler() at the
 *     cadence requested by LVGL.
 *   * Provide backlight control on top of bsp_lcd_set_brightness().
 *
 * LVGL tick is sourced from the ESP-IDF microsecond timer via
 * lv_tick_set_cb() (LVGL v8.3+) — no separate tick task is needed.
 *
 * Targets LVGL v8.3 (vendored as a git submodule — see .gitmodules).
 * The code is forward-compatible with LVGL v9 if the three LVGL display
 * driver init calls (lv_disp_draw_buf_init / lv_disp_drv_init /
 * lv_disp_drv_register) are renamed to their v9 equivalents.
 *
 * Public API declared at the top of this file (display_manager has no
 * dedicated header in the project layout).
 */
#include "lvgl.h"
#include "bsp_lcd_gc9a01.h"
#include "app_common.h"

#include <string.h>

#include "esp_log.h"
#include "esp_err.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

static const char *TAG = TAG_DISPLAY;

/* ------------------------------------------------------------------------- *
 * Public API (no separate header in this layout — declared here)
 * ------------------------------------------------------------------------- */

esp_err_t display_manager_init(void);
void      display_manager_set_brightness(uint8_t percent);
uint8_t   display_manager_get_brightness(void);
void      display_manager_lock(void);
void      display_manager_unlock(void);
lv_obj_t *display_manager_get_active_screen(void);
void      display_manager_set_default_screen(lv_obj_t *scr);

/* ------------------------------------------------------------------------- *
 * Module state
 * ------------------------------------------------------------------------- */

#define DISP_HOR_RES    BSP_LCD_H_RES
#define DISP_VER_RES    BSP_LCD_V_RES

/* Partial-buffer double buffering: each buffer holds 40 lines of RGB565
 * (240 * 40 * 2 = 19200 bytes). Drawn from DMA-capable memory because the
 * esp_lcd SPI driver DMAs the pixel payload. */
#define DISP_BUF_LINES  BSP_LCD_BUFFER_LINES
#define DISP_BUF_PX     (DISP_HOR_RES * DISP_BUF_LINES)

static lv_disp_draw_buf_t  s_draw_buf;
static lv_color_t         *s_buf1;
static lv_color_t         *s_buf2;

static lv_disp_drv_t      s_disp_drv;
static lv_disp_t         *s_disp = NULL;

static SemaphoreHandle_t   s_lv_mutex = NULL;       /* recursive */
static TaskHandle_t        s_lv_task = NULL;
static bool                s_initialised = false;

/* ------------------------------------------------------------------------- *
 * LVGL tick source — fed by the ESP-IDF microsecond timer.
 * ------------------------------------------------------------------------- */

static uint32_t lv_tick_cb(void)
{
    /* esp_timer_get_time() returns int64_t microseconds since boot. */
    return (uint32_t)(esp_timer_get_time() / 1000);
}

/* ------------------------------------------------------------------------- *
 * LVGL flush callback: delegate to the BSP which queues the SPI transaction.
 *
 * The BSP invokes the registered flush-done callback (on_flush_done) once
 * esp_lcd_panel_draw_bitmap() returns; on_flush_done in turn calls
 * lv_disp_flush_ready() so LVGL knows the buffer can be reused.
 * ------------------------------------------------------------------------- */

static void on_flush_done(void *user_data)
{
    lv_disp_drv_t *drv = (lv_disp_drv_t *)user_data;
    if (drv != NULL) {
        lv_disp_flush_ready(drv);
    }
}

static void disp_flush_cb(lv_disp_drv_t *drv,
                          const lv_area_t *area,
                          lv_color_t *color_map)
{
    if (area == NULL || color_map == NULL || drv == NULL) {
        lv_disp_flush_ready(drv);
        return;
    }
    /* Save the current drv pointer in user_data so on_flush_done (called by
     * the BSP after esp_lcd_panel_draw_bitmap) can notify the right driver.
     * Single display so a static pointer would suffice, but we use the
     * per-call user_data slot to remain correct under future multi-display
     * extensions. */
    bsp_lcd_register_flush_done_cb(on_flush_done, drv);
    bsp_lcd_lvgl_flush_cb(drv, area, color_map);
}

/* ------------------------------------------------------------------------- *
 * LVGL task — drives lv_timer_handler at the cadence LVGL requests.
 * ------------------------------------------------------------------------- */

static void lvgl_task(void *arg)
{
    (void)arg;
    ESP_LOGI(TAG, "LVGL task started on core %d", xPortGetCoreID());
    const TickType_t min_delay = pdMS_TO_TICKS(2);
    while (true) {
        /* LVGL is not thread-safe: hold the mutex around handler invocations
         * AND around any LVGL API call from another task. */
        xSemaphoreTakeRecursive(s_lv_mutex, portMAX_DELAY);
        uint32_t next_ms = lv_timer_handler();
        xSemaphoreGiveRecursive(s_lv_mutex);

        TickType_t sleep = pdMS_TO_TICKS((TickType_t)next_ms);
        if (sleep < min_delay || sleep == 0) {
            sleep = min_delay;
        }
        vTaskDelay(sleep);
    }
}

/* ------------------------------------------------------------------------- *
 * Public API implementation
 * ------------------------------------------------------------------------- */

esp_err_t display_manager_init(void)
{
    if (s_initialised) {
        return ESP_OK;
    }
    /* BSP LCD must be initialised first — main.c does this in app_bsp_init()
     * before invoking display_manager_init(). Verify it is up. */
    if (bsp_lcd_get_panel() == NULL) {
        ESP_LOGE(TAG, "BSP LCD not initialised — call bsp_lcd_init() first");
        return ESP_ERR_INVALID_STATE;
    }

    /* Recursive mutex so that LVGL callbacks (which may call back into
     * display_manager APIs) do not self-deadlock. */
    s_lv_mutex = xSemaphoreCreateRecursiveMutex();
    if (s_lv_mutex == NULL) {
        return ESP_ERR_NO_MEM;
    }

    /* 1. LVGL core. */
    lv_init();
    /* Tell LVGL to source its tick from the ESP timer — no separate
     * lv_tick_inc task is needed. */
    lv_tick_set_cb(lv_tick_cb);

    /* 2. Draw buffers (double-buffered partial mode). */
    s_buf1 = (lv_color_t *)heap_caps_malloc(
        sizeof(lv_color_t) * DISP_BUF_PX * 2,
        MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
    s_buf2 = (lv_color_t *)heap_caps_malloc(
        sizeof(lv_color_t) * DISP_BUF_PX * 2,
        MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
    if (s_buf1 == NULL || s_buf2 == NULL) {
        ESP_LOGE(TAG, "failed to allocate LVGL draw buffers");
        return ESP_ERR_NO_MEM;
    }
    lv_disp_draw_buf_init(&s_draw_buf, s_buf1, s_buf2, DISP_BUF_PX * 2);

    /* 3. Display driver. */
    lv_disp_drv_init(&s_disp_drv);
    s_disp_drv.hor_res  = DISP_HOR_RES;
    s_disp_drv.ver_res  = DISP_VER_RES;
    s_disp_drv.draw_buf = &s_draw_buf;
    s_disp_drv.flush_cb = disp_flush_cb;
    /* Round display: enable anti-aliasing for nicer circle edges. */
    s_disp_drv.antialiasing = 1;
    s_disp = lv_disp_drv_register(&s_disp_drv);
    if (s_disp == NULL) {
        ESP_LOGE(TAG, "lv_disp_drv_register failed");
        return ESP_FAIL;
    }

    /* 4. Apply default backlight from the global config. */
    bsp_lcd_set_brightness(g_app_config.brightness);

    /* 5. Launch the LVGL task pinned to core 1 (app_main runs on 0). */
    BaseType_t ok = xTaskCreatePinnedToCore(
        lvgl_task, "lvgl", 6144, NULL, 5, &s_lv_task, 1);
    if (ok != pdPASS) {
        ESP_LOGE(TAG, "failed to create LVGL task");
        return ESP_ERR_NO_MEM;
    }

    s_initialised = true;
    ESP_LOGI(TAG, "LVGL ready: %dx%d RGB565, double-buffered, core 1",
             DISP_HOR_RES, DISP_VER_RES);
    APP_SET_BIT(EVT_BIT_LCD_READY);
    return ESP_OK;
}

void display_manager_set_brightness(uint8_t percent)
{
    if (percent > 100U) {
        percent = 100U;
    }
    /* BSP expects 0-255 range. */
    uint8_t level = (uint8_t)((uint32_t)percent * 255U / 100U);
    bsp_lcd_set_brightness(level);
    /* Persist so the global config matches the hardware. */
    g_app_config.brightness = level;
}

uint8_t display_manager_get_brightness(void)
{
    /* Convert BSP 0-255 back to 0-100 percent. */
    uint8_t level = bsp_lcd_get_brightness();
    return (uint8_t)((uint32_t)level * 100U / 255U);
}

void display_manager_lock(void)
{
    if (s_lv_mutex != NULL) {
        xSemaphoreTakeRecursive(s_lv_mutex, portMAX_DELAY);
    }
}

void display_manager_unlock(void)
{
    if (s_lv_mutex != NULL) {
        xSemaphoreGiveRecursive(s_lv_mutex);
    }
}

lv_obj_t *display_manager_get_active_screen(void)
{
    display_manager_lock();
    lv_obj_t *scr = lv_scr_act();
    display_manager_unlock();
    return scr;
}

void display_manager_set_default_screen(lv_obj_t *scr)
{
    display_manager_lock();
    lv_disp_load_scr(scr);
    display_manager_unlock();
}
