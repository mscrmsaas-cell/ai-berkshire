/**
 * @file ui_screens.c
 * @brief Top-level screen layouts: standby / inspecting / alert / charging.
 *
 * Each screen creates a fresh lv_obj_t (parent = NULL) and loads it via
 * display_manager_set_default_screen(). The renderer's status bar lives on
 * lv_layer_top() so it is shared across all screens.
 *
 * Screens own their background style, primary content (clock / alert text /
-  charging animation) and any local interaction handlers. AR annotation
 * boxes (from ui_renderer) attach to the active screen so they automatically
 * disappear when the screen is destroyed on transition.
 *
 * Targets LVGL v8.3.
 */
#include "lvgl.h"
#include "app_common.h"
#include "esp_log.h"

#include <string.h>
#include <stdio.h>

static const char *TAG = TAG_DISPLAY;

/* ------------------------------------------------------------------------- *
 * Public API (no separate header in this layout — declared here)
 * ------------------------------------------------------------------------- */

void ui_screens_show_standby(void);
void ui_screens_show_inspecting(void);
void ui_screens_show_alert(const char *alert_title, const char *alert_body);
void ui_screens_show_charging(uint8_t battery_pct);

/* Forward-declared from display_manager.c. */
void display_manager_lock(void);
void display_manager_unlock(void);
void display_manager_set_default_screen(lv_obj_t *scr);

/* Forward-declared from ui_renderer.c. */
esp_err_t ui_renderer_init(void);
void      ui_renderer_clear_ar_boxes(void);

/* ------------------------------------------------------------------------- *
 * Shared screen factory
 * ------------------------------------------------------------------------- */

/**
 * Create a new screen object and apply the standard dark railway palette.
 */
static lv_obj_t *screen_create_base(lv_color_t bg_color)
{
    lv_obj_t *scr = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(scr, bg_color, 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(scr, 0, 0);
    lv_obj_set_style_pad_all(scr, 0, 0);
    lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);
    return scr;
}

static void screen_load(lv_obj_t *scr)
{
    /* Tear down any per-screen AR boxes from the previous screen first. */
    ui_renderer_clear_ar_boxes();
    display_manager_set_default_screen(scr);
}

/* ------------------------------------------------------------------------- *
 * Standby screen
 * ------------------------------------------------------------------------- */

void ui_screens_show_standby(void)
{
    display_manager_lock();
    /* Initialise the renderer if it hasn't been (status bar overlay). */
    (void)ui_renderer_init();

    lv_obj_t *scr = screen_create_base(lv_color_hex(0x101418));

    /* Large monospaced clock centred on the screen. */
    lv_obj_t *clock = lv_label_create(scr);
    lv_obj_set_style_text_font(clock, &lv_font_montserrat_28, 0);
    lv_obj_set_style_text_color(clock, lv_color_hex(0xE8E8E8), 0);
    lv_label_set_text(clock, "--:--");
    lv_obj_align(clock, LV_ALIGN_CENTER, 0, -10);

    /* Date / mode hint beneath the clock. */
    lv_obj_t *hint = lv_label_create(scr);
    lv_obj_set_style_text_color(hint, lv_color_hex(0x707070), 0);
    lv_label_set_text(hint, "standby");
    lv_obj_align(hint, LV_ALIGN_CENTER, 0, 24);

    screen_load(scr);
    display_manager_unlock();
    ESP_LOGI(TAG, "screen -> standby");
}

/* ------------------------------------------------------------------------- *
 * Inspecting screen — AR overlay canvas + bottom action bar
 * ------------------------------------------------------------------------- */

/**
 * Build the bottom action bar with three placeholder buttons (capture /
 * next-step / pause). The action bar lives at the bottom of the screen
 * above the status bar (the status bar is on layer_top, so no overlap).
 */
static void build_action_bar(lv_obj_t *parent)
{
    lv_obj_t *bar = lv_obj_create(parent);
    lv_obj_set_size(bar, BSP_LCD_H_RES, 40);
    lv_obj_align(bar, LV_ALIGN_BOTTOM_MID, 0, 0);
    lv_obj_set_style_bg_color(bar, lv_color_hex(0x1B1F24), 0);
    lv_obj_set_style_bg_opa(bar, LV_OPA_80, 0);
    lv_obj_set_style_radius(bar, 0, 0);
    lv_obj_set_style_border_width(bar, 0, 0);
    lv_obj_set_flex_flow(bar, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(bar, LV_FLEX_ALIGN_SPACE_EVENLY,
                          LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);

    static const char *const labels[3] = {
        LV_SYMBOL_IMAGE, LV_SYMBOL_OK, LV_SYMBOL_PAUSE
    };
    for (int i = 0; i < 3; ++i) {
        lv_obj_t *btn = lv_label_create(bar);
        lv_obj_set_style_text_color(btn, lv_color_hex(0xFFFFFF), 0);
        lv_obj_set_style_text_font(btn, &lv_font_montserrat_16, 0);
        lv_label_set_text(btn, labels[i]);
    }
}

void ui_screens_show_inspecting(void)
{
    display_manager_lock();
    (void)ui_renderer_init();

    lv_obj_t *scr = screen_create_base(lv_color_hex(0x080A0C));

    /* Centre crosshair for AR alignment. */
    lv_obj_t *xh = lv_label_create(scr);
    lv_obj_set_style_text_color(xh, lv_color_hex(0x40C060), 0);
    lv_label_set_text(xh, "+");
    lv_obj_set_style_text_font(xh, &lv_font_montserrat_28, 0);
    lv_obj_align(xh, LV_ALIGN_CENTER, 0, -10);

    /* Mode label at top-centre (below the status bar). */
    lv_obj_t *mode = lv_label_create(scr);
    lv_obj_set_style_text_color(mode, lv_color_hex(0x80FFA0), 0);
    lv_label_set_text(mode, "INSPECTING");
    lv_obj_align(mode, LV_ALIGN_TOP_MID, 0, 22);

    build_action_bar(scr);

    screen_load(scr);
    display_manager_unlock();
    ESP_LOGI(TAG, "screen -> inspecting");
}

/* ------------------------------------------------------------------------- *
 * Alert screen — pulsing red, dismissable
 * ------------------------------------------------------------------------- */

static void alert_pulse_cb(void *var, int32_t v)
{
    lv_obj_t *bg = (lv_obj_t *)var;
    /* v ranges 0..255 via the animation; map to opacity. */
    lv_obj_set_style_bg_opa(bg, (lv_opa_t)v, 0);
}

static void alert_ack_event_cb(lv_event_t *e)
{
    (void)e;
    /* Acknowledge the alert and return to inspecting/standby. */
    ui_screens_show_standby();
}

/**
 * Animation callback wrapper for setting text opacity (the raw LVGL setter
 * has a 3-arg signature that doesn't match lv_anim_exec_xcb_t).
 */
static void text_opa_anim_cb(void *obj, int32_t v)
{
    lv_obj_set_style_text_opa((lv_obj_t *)obj, (lv_opa_t)v, 0);
}

void ui_screens_show_alert(const char *alert_title, const char *alert_body)
{
    display_manager_lock();
    (void)ui_renderer_init();

    /* Background: bright red pulsing panel. */
    lv_obj_t *scr = screen_create_base(lv_color_hex(0x600000));

    /* Animate the screen background opacity between 60% and 100%. */
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, scr);
    lv_anim_set_values(&a, LV_OPA_60, LV_OPA_COVER);
    lv_anim_set_time(&a, 500);
    lv_anim_set_playback_time(&a, 500);
    lv_anim_set_repeat_count(&a, LV_ANIM_REPEAT_INFINITE);
    lv_anim_set_exec_cb(&a, alert_pulse_cb);
    lv_anim_start(&a);

    /* Big warning glyph. */
    lv_obj_t *icon = lv_label_create(scr);
    lv_obj_set_style_text_color(icon, lv_color_hex(0xFFE0E0), 0);
    lv_obj_set_style_text_font(icon, &lv_font_montserrat_28, 0);
    lv_label_set_text(icon, LV_SYMBOL_WARNING);
    lv_obj_align(icon, LV_ALIGN_TOP_MID, 0, 30);

    /* Title. */
    lv_obj_t *title = lv_label_create(scr);
    lv_obj_set_style_text_color(title, lv_color_hex(0xFFFFFF), 0);
    lv_obj_set_style_text_font(title, &lv_font_montserrat_16, 0);
    lv_label_set_text(title, alert_title != NULL ? alert_title : "ALERT");
    lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 70);

    /* Body — wraps within a fixed-width label. */
    lv_obj_t *body = lv_label_create(scr);
    lv_obj_set_style_text_color(body, lv_color_hex(0xFFE0E0), 0);
    lv_obj_set_width(body, BSP_LCD_H_RES - 24);
    lv_label_set_long_mode(body, LV_LABEL_LONG_WRAP);
    lv_label_set_text(body,
                      alert_body != NULL ? alert_body : "Tap to acknowledge.");
    lv_obj_align(body, LV_ALIGN_CENTER, 0, 20);

    /* Dismiss button at the bottom. */
    lv_obj_t *btn = lv_btn_create(scr);
    lv_obj_set_size(btn, 120, 32);
    lv_obj_align(btn, LV_ALIGN_BOTTOM_MID, 0, -10);
    lv_obj_set_style_bg_color(btn, lv_color_hex(0xAA0000), 0);
    lv_obj_set_style_radius(btn, 6, 0);
    lv_obj_add_event_cb(btn, alert_ack_event_cb, LV_EVENT_CLICKED, NULL);

    lv_obj_t *btn_lbl = lv_label_create(btn);
    lv_obj_set_style_text_color(btn_lbl, lv_color_hex(0xFFFFFF), 0);
    lv_label_set_text(btn_lbl, "ACK");
    lv_obj_center(btn_lbl);

    screen_load(scr);
    display_manager_unlock();
    ESP_LOGW(TAG, "screen -> alert (%s)", alert_title ? alert_title : "-");
}

/* ------------------------------------------------------------------------- *
 * Charging screen
 * ------------------------------------------------------------------------- */

void ui_screens_show_charging(uint8_t battery_pct)
{
    display_manager_lock();
    (void)ui_renderer_init();

    lv_obj_t *scr = screen_create_base(lv_color_hex(0x101418));

    /* Battery glyph (charging). */
    lv_obj_t *glyph = lv_label_create(scr);
    lv_obj_set_style_text_color(glyph, lv_color_hex(0x40C060), 0);
    lv_obj_set_style_text_font(glyph, &lv_font_montserrat_28, 0);
    lv_label_set_text(glyph, LV_SYMBOL_CHARGE);
    lv_obj_align(glyph, LV_ALIGN_TOP_MID, 0, 30);

    /* Percentage. */
    char pct_buf[8];
    snprintf(pct_buf, sizeof(pct_buf), "%u%%",
             battery_pct > 100U ? 100U : battery_pct);
    lv_obj_t *pct = lv_label_create(scr);
    lv_obj_set_style_text_color(pct, lv_color_hex(0xE8E8E8), 0);
    lv_obj_set_style_text_font(pct, &lv_font_montserrat_20, 0);
    lv_label_set_text(pct, pct_buf);
    lv_obj_align(pct, LV_ALIGN_TOP_MID, 0, 80);

    /* Charging hint. */
    lv_obj_t *hint = lv_label_create(scr);
    lv_obj_set_style_text_color(hint, lv_color_hex(0x707070), 0);
    lv_label_set_text(hint, "charging...");
    lv_obj_align(hint, LV_ALIGN_TOP_MID, 0, 110);

    /* Pulsing animation on the charging glyph to show the device is alive. */
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, glyph);
    lv_anim_set_values(&a, 100, 255);          /* text opacity swing */
    lv_anim_set_time(&a, 800);
    lv_anim_set_playback_time(&a, 800);
    lv_anim_set_repeat_count(&a, LV_ANIM_REPEAT_INFINITE);
    lv_anim_set_exec_cb(&a, text_opa_anim_cb);
    lv_anim_start(&a);

    screen_load(scr);
    display_manager_unlock();
    ESP_LOGI(TAG, "screen -> charging (%u%%)", battery_pct);
}
