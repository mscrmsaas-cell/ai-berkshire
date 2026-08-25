/**
 * @file ui_renderer.c
 * @brief Reusable UI widgets: status bar, notification cards, AR annotation boxes.
 *
 * The renderer provides widget factories that are shared across all screens
 * (defined in ui_screens.c). The status bar lives on lv_layer_top() so it
 * remains visible across screen transitions; notification cards are
 * transient pop-ups that auto-dismiss; AR annotation boxes are drawn on the
 * active screen (per-screen annotation, cleared on screen switch).
 *
 * All public functions acquire the LVGL recursive mutex (display_manager_lock)
 * internally, so they are safe to call from any task.
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

esp_err_t ui_renderer_init(void);
void      ui_renderer_update_status_bar(uint8_t batt_pct, bool charging,
                                        const char *time_str,
                                        int8_t rssi_dbm,
                                        const char *mode_str);
void      ui_renderer_show_notification(const char *title,
                                        const char *body,
                                        uint32_t duration_ms);
void      ui_renderer_clear_notifications(void);
void      ui_renderer_show_ar_box(int16_t x, int16_t y,
                                  uint16_t w, uint16_t h,
                                  const char *label,
                                  lv_color_t border_color);
void      ui_renderer_clear_ar_boxes(void);

/* Forward-declared from display_manager.c. */
void display_manager_lock(void);
void display_manager_unlock(void);
lv_obj_t *display_manager_get_active_screen(void);

/* ------------------------------------------------------------------------- *
 * Module state — widgets owned by the renderer
 * ------------------------------------------------------------------------- */

/* Status bar widgets (live on lv_layer_top()). */
static lv_obj_t *s_status_bar    = NULL;
static lv_obj_t *s_lbl_battery   = NULL;
static lv_obj_t *s_lbl_time      = NULL;
static lv_obj_t *s_lbl_signal    = NULL;
static lv_obj_t *s_lbl_mode      = NULL;

/* Notification stack (live on lv_layer_top()). */
#define MAX_NOTIFICATIONS 4U
typedef struct {
    lv_obj_t *card;
    lv_timer_t *auto_dismiss_tmr;
    bool in_use;
} notif_slot_t;
static notif_slot_t s_notifs[MAX_NOTIFICATIONS];
static uint8_t      s_notif_count = 0U;

/* AR annotation boxes — drawn on the active screen. */
#define MAX_AR_BOXES 16U
typedef struct {
    lv_obj_t *box;
    lv_obj_t *label;
} ar_box_t;
static ar_box_t s_ar_boxes[MAX_AR_BOXES];

/* ------------------------------------------------------------------------- *
 * Style helpers
 * ------------------------------------------------------------------------- */

static lv_style_t s_style_status_bar;
static lv_style_t s_style_card;
static lv_style_t s_style_ar_label;
static bool       s_styles_inited = false;

static void ensure_styles(void)
{
    if (s_styles_inited) {
        return;
    }

    /* Status bar: dark translucent strip across the top. */
    lv_style_init(&s_style_status_bar);
    lv_style_set_bg_color(&s_style_status_bar, lv_color_hex(0x000000));
    lv_style_set_bg_opa(&s_style_status_bar, LV_OPA_70);
    lv_style_set_radius(&s_style_status_bar, 0);
    lv_style_set_pad_ver(&s_style_status_bar, 2);
    lv_style_set_pad_hor(&s_style_status_bar, 4);
    lv_style_set_border_width(&s_style_status_bar, 0);
    lv_style_set_text_color(&s_style_status_bar, lv_color_hex(0xFFFFFF));
    lv_style_set_text_font(&s_style_status_bar, &lv_font_montserrat_14);

    /* Notification card: white rounded panel with a subtle shadow. */
    lv_style_init(&s_style_card);
    lv_style_set_bg_color(&s_style_card, lv_color_hex(0xFAFAFA));
    lv_style_set_bg_opa(&s_style_card, LV_OPA_95);
    lv_style_set_radius(&s_style_card, 8);
    lv_style_set_border_color(&s_style_card, lv_color_hex(0x4A90E2));
    lv_style_set_border_width(&s_style_card, 1);
    lv_style_set_pad_all(&s_style_card, 8);
    lv_style_set_shadow_width(&s_style_card, 12);
    lv_style_set_shadow_color(&s_style_card, lv_color_hex(0x000000));
    lv_style_set_shadow_opa(&s_style_card, LV_OPA_40);

    /* AR box label: dark pill with bright text. */
    lv_style_init(&s_style_ar_label);
    lv_style_set_bg_color(&s_style_ar_label, lv_color_hex(0xCC0000));
    lv_style_set_bg_opa(&s_style_ar_label, LV_OPA_80);
    lv_style_set_radius(&s_style_ar_label, 4);
    lv_style_set_pad_ver(&s_style_ar_label, 1);
    lv_style_set_pad_hor(&s_style_ar_label, 4);
    lv_style_set_text_color(&s_style_ar_label, lv_color_hex(0xFFFFFF));
    lv_style_set_text_font(&s_style_ar_label, &lv_font_montserrat_12);

    s_styles_inited = true;
}

/* ------------------------------------------------------------------------- *
 * Status bar
 * ------------------------------------------------------------------------- */

static const char *battery_symbol_for(uint8_t pct, bool charging)
{
    if (charging) {
        return LV_SYMBOL_CHARGE;
    }
    if (pct >= 87U) return LV_SYMBOL_BATTERY_FULL;
    if (pct >= 62U) return LV_SYMBOL_BATTERY_3_4;
    if (pct >= 37U) return LV_SYMBOL_BATTERY_2_4;
    if (pct >= 12U) return LV_SYMBOL_BATTERY_1_4;
    return LV_SYMBOL_BATTERY_EMPTY;
}

static const char *signal_symbol_for(int8_t rssi_dbm)
{
    if (rssi_dbm >= -50) return LV_SYMBOL_WIFI; /* full  */
    if (rssi_dbm >= -70) return LV_SYMBOL_WIFI;  /* mid   */
    return LV_SYMBOL_WIFI;                       /* weak  */
    /* LVGL does not ship distinct wifi-strength glyphs by default; the RSSI
     * dBm value is appended as text in the label for finer granularity. */
}

esp_err_t ui_renderer_init(void)
{
    display_manager_lock();
    ensure_styles();

    lv_obj_t *top = lv_layer_top();
    /* Status bar: a horizontal container pinned to the top of the screen. */
    s_status_bar = lv_obj_create(top);
    lv_obj_remove_style_all(s_status_bar);
    lv_obj_add_style(s_status_bar, &s_style_status_bar, 0);
    lv_obj_set_size(s_status_bar, BSP_LCD_H_RES, 18);
    lv_obj_align(s_status_bar, LV_ALIGN_TOP_MID, 0, 0);
    lv_obj_set_flex_flow(s_status_bar, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(s_status_bar,
                          LV_FLEX_ALIGN_SPACE_BETWEEN,
                          LV_FLEX_ALIGN_CENTER,
                          LV_FLEX_ALIGN_CENTER);

    s_lbl_battery = lv_label_create(s_status_bar);
    lv_label_set_text(s_lbl_battery, LV_SYMBOL_BATTERY_FULL " 100%");

    s_lbl_time = lv_label_create(s_status_bar);
    lv_label_set_text(s_lbl_time, "--:--");

    s_lbl_signal = lv_label_create(s_status_bar);
    lv_label_set_text(s_lbl_signal, LV_SYMBOL_WIFI " -");

    s_lbl_mode = lv_label_create(s_status_bar);
    lv_label_set_text(s_lbl_mode, "standby");

    display_manager_unlock();
    ESP_LOGI(TAG, "renderer initialised (status bar on layer_top)");
    return ESP_OK;
}

void ui_renderer_update_status_bar(uint8_t batt_pct, bool charging,
                                   const char *time_str,
                                   int8_t rssi_dbm,
                                   const char *mode_str)
{
    if (s_lbl_battery == NULL) {
        (void)ui_renderer_init();
    }
    display_manager_lock();
    char buf[24];
    snprintf(buf, sizeof(buf), "%s %u%%",
             battery_symbol_for(batt_pct, charging),
             batt_pct > 100U ? 100U : batt_pct);
    lv_label_set_text(s_lbl_battery, buf);
    if (time_str != NULL) {
        lv_label_set_text(s_lbl_time, time_str);
    }
    snprintf(buf, sizeof(buf), "%s %ddBm",
             signal_symbol_for(rssi_dbm), (int)rssi_dbm);
    lv_label_set_text(s_lbl_signal, buf);
    if (mode_str != NULL) {
        lv_label_set_text(s_lbl_mode, mode_str);
    }
    display_manager_unlock();
}

/* ------------------------------------------------------------------------- *
 * Notification cards
 * ------------------------------------------------------------------------- */

static void notification_dismiss_cb(lv_timer_t *tmr)
{
    lv_obj_t *card = (lv_obj_t *)lv_timer_get_user_data(tmr);
    if (card == NULL) {
        return;
    }
    /* Free the slot. */
    for (uint8_t i = 0U; i < MAX_NOTIFICATIONS; ++i) {
        if (s_notifs[i].card == card) {
            s_notifs[i].in_use = false;
            s_notifs[i].card   = NULL;
            s_notifs[i].auto_dismiss_tmr = NULL;
            if (s_notif_count > 0U) {
                s_notif_count--;
            }
            break;
        }
    }
    lv_obj_del_async_safe(card);  /* safe async delete */
    /* Re-stack remaining notifications. */
    uint8_t idx = 0U;
    for (uint8_t i = 0U; i < MAX_NOTIFICATIONS; ++i) {
        if (s_notifs[i].in_use && s_notifs[i].card != NULL) {
            lv_obj_align(s_notifs[i].card, LV_ALIGN_TOP_MID, 0,
                         22 + (int16_t)idx * 36);
            idx++;
        }
    }
}

void ui_renderer_show_notification(const char *title,
                                  const char *body,
                                  uint32_t duration_ms)
{
    if (title == NULL && body == NULL) {
        return;
    }
    if (duration_ms == 0U) {
        duration_ms = 3000U;
    }
    display_manager_lock();
    ensure_styles();

    /* Find a free slot. */
    notif_slot_t *slot = NULL;
    for (uint8_t i = 0U; i < MAX_NOTIFICATIONS; ++i) {
        if (!s_notifs[i].in_use) {
            slot = &s_notifs[i];
            break;
        }
    }
    if (slot == NULL) {
        /* All slots in use — force-dismiss the oldest (index 0). */
        notif_slot_t *oldest = &s_notifs[0];
        if (oldest->auto_dismiss_tmr != NULL) {
            lv_timer_del(oldest->auto_dismiss_tmr);
            oldest->auto_dismiss_tmr = NULL;
        }
        if (oldest->card != NULL) {
            lv_obj_del_async_safe(oldest->card);
            oldest->card = NULL;
        }
        oldest->in_use = false;
        if (s_notif_count > 0U) {
            s_notif_count--;
        }
        slot = oldest;
    }

    lv_obj_t *top = lv_layer_top();
    lv_obj_t *card = lv_obj_create(top);
    lv_obj_remove_style_all(card);
    lv_obj_add_style(card, &s_style_card, 0);
    lv_obj_set_size(card, BSP_LCD_H_RES - 16, 32);
    lv_obj_align(card, LV_ALIGN_TOP_MID, 0,
                 22 + (int16_t)s_notif_count * 36);

    lv_obj_t *lbl = lv_label_create(card);
    lv_label_set_recolor(lbl, true);
    char rich[160];
    snprintf(rich, sizeof(rich), "#4A90E2 %s# %s",
             title ? title : "",
             body  ? body  : "");
    lv_label_set_text(lbl, rich);
    lv_obj_set_width(lbl, BSP_LCD_H_RES - 32);
    lv_label_set_long_mode(lbl, LV_LABEL_LONG_DOT);
    lv_obj_center(lbl);

    slot->card   = card;
    slot->in_use = true;
    slot->auto_dismiss_tmr = lv_timer_create(
        notification_dismiss_cb, duration_ms, card);
    lv_timer_set_repeat_count(slot->auto_dismiss_tmr, 1);
    s_notif_count++;

    display_manager_unlock();
}

void ui_renderer_clear_notifications(void)
{
    display_manager_lock();
    for (uint8_t i = 0U; i < MAX_NOTIFICATIONS; ++i) {
        if (s_notifs[i].in_use) {
            if (s_notifs[i].auto_dismiss_tmr != NULL) {
                lv_timer_del(s_notifs[i].auto_dismiss_tmr);
                s_notifs[i].auto_dismiss_tmr = NULL;
            }
            if (s_notifs[i].card != NULL) {
                lv_obj_del_async_safe(s_notifs[i].card);
            }
            s_notifs[i].card   = NULL;
            s_notifs[i].in_use = false;
        }
    }
    s_notif_count = 0U;
    display_manager_unlock();
}

/* ------------------------------------------------------------------------- *
 * AR annotation boxes
 * ------------------------------------------------------------------------- */

void ui_renderer_show_ar_box(int16_t x, int16_t y,
                             uint16_t w, uint16_t h,
                             const char *label,
                             lv_color_t border_color)
{
    if (w == 0U || h == 0U) {
        return;
    }
    display_manager_lock();
    ensure_styles();

    /* Find a free AR-box slot. */
    ar_box_t *slot = NULL;
    for (uint8_t i = 0U; i < MAX_AR_BOXES; ++i) {
        if (s_ar_boxes[i].box == NULL) {
            slot = &s_ar_boxes[i];
            break;
        }
    }
    if (slot == NULL) {
        /* Recycle slot 0 if the box is full. */
        slot = &s_ar_boxes[0];
        if (slot->box != NULL) {
            lv_obj_del_async_safe(slot->box);
            slot->box   = NULL;
            slot->label = NULL;
        }
    }

    lv_obj_t *parent = display_manager_get_active_screen();
    if (parent == NULL) {
        display_manager_unlock();
        return;
    }

    lv_obj_t *box = lv_obj_create(parent);
    lv_obj_remove_style_all(box);
    lv_obj_set_pos(box, x, y);
    lv_obj_set_size(box, w, h);
    lv_obj_set_style_border_color(box, border_color, 0);
    lv_obj_set_style_border_width(box, 2, 0);
    lv_obj_set_style_border_opa(box, LV_OPA_90, 0);
    lv_obj_set_style_bg_opa(box, LV_OPA_TRANSP, 0);
    lv_obj_set_style_radius(box, 0, 0);
    lv_obj_clear_flag(box, LV_OBJ_FLAG_SCROLLABLE);

    if (label != NULL && label[0] != '\0') {
        lv_obj_t *lbl = lv_label_create(parent);
        lv_obj_remove_style_all(lbl);
        lv_obj_add_style(lbl, &s_style_ar_label, 0);
        lv_label_set_text(lbl, label);
        /* Place the label just above the box; clip to screen top. */
        int16_t lbl_y = (y >= 14) ? (int16_t)(y - 14) : 0;
        lv_obj_set_pos(lbl, x, lbl_y);
        slot->label = lbl;
    } else {
        slot->label = NULL;
    }
    slot->box = box;

    display_manager_unlock();
}

void ui_renderer_clear_ar_boxes(void)
{
    display_manager_lock();
    for (uint8_t i = 0U; i < MAX_AR_BOXES; ++i) {
        if (s_ar_boxes[i].box != NULL) {
            lv_obj_del_async_safe(s_ar_boxes[i].box);
            s_ar_boxes[i].box = NULL;
        }
        if (s_ar_boxes[i].label != NULL) {
            lv_obj_del_async_safe(s_ar_boxes[i].label);
            s_ar_boxes[i].label = NULL;
        }
    }
    display_manager_unlock();
}
