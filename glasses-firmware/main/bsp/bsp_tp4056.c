/**
 * @file bsp_tp4056.c
 * @brief TP4056 电池管理驱动实现 — ADC 电压检测 + 充电状态 GPIO
 *
 * 功能:
 *   - ADC 读取电池电压 → 线性映射到 0-100% 电量
 *   - 滑动平均滤波平滑电压读数
 *   - CHRG/STDBY GPIO 检测充电状态
 *   - 周期性轮询 (默认 10 秒)
 *   - 低电量回调 / 充电状态变化回调
 *
 * @copyright Copyright (c) 2024 Railway Inspection AR Glasses Project
 * @license Apache-2.0
 */

#include "bsp_tp4056.h"
#include "app_common.h"

#include "esp_log.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "driver/gpio.h"
#include "esp_timer.h"

static const char *TAG = TAG_BATTERY;

/* =========================================================================
 * 模块内部状态
 * ========================================================================= */
static bool s_initialized = false;
static adc_oneshot_unit_handle_t s_adc_handle = NULL;
static adc_cali_handle_t s_adc_cali_handle = NULL;

/* 电压滑动平均缓冲区 */
#define VOLTAGE_AVG_WINDOW 8
static uint16_t s_voltage_buf[VOLTAGE_AVG_WINDOW];
static int s_voltage_buf_idx = 0;
static int s_voltage_buf_count = 0;

/* 当前状态缓存 */
static uint16_t s_current_voltage_mv = BSP_BATT_MAX_VOLTAGE_MV;
static uint8_t s_current_level = 100;
static bool s_is_charging = false;
static bool s_charge_complete = false;
static bool s_low_warned = false;  /* 是否已发出低电量告警 (防重复) */

/* 回调函数 */
static bsp_battery_low_cb_t s_low_cb = NULL;
static bsp_battery_charging_cb_t s_charging_cb = NULL;

/* 轮询定时器 */
static esp_timer_handle_t s_poll_timer = NULL;

/* =========================================================================
 * ADC 校准初始化
 * ========================================================================= */
static esp_err_t bsp_battery_adc_cali_init(void)
{
#if ADC_CALI_SCHEME_CURVE_FITTING_SUPPORTED
    adc_cali_curve_fitting_config_t cali_cfg = {
        .unit_id = (CONFIG_BSP_BATT_ADC_UNIT == 1) ? ADC_UNIT_1 : ADC_UNIT_2,
        .chan = (adc_channel_t)CONFIG_BSP_BATT_ADC_CHANNEL,
        .atten = ADC_ATTEN_DB_11,
        .bitwidth = ADC_BITWIDTH_12,
    };
    esp_err_t ret = adc_cali_create_curve_fitting_unit(&cali_cfg, &s_adc_cali_handle);
    if (ret == ESP_OK) {
        ESP_LOGI(TAG, "ADC 校准: Curve Fitting 模式");
        return ESP_OK;
    }
    ESP_LOGW(TAG, "Curve Fitting 校准不可用 (%s), 尝试 Line Fitting",
             esp_err_to_name(ret));
#endif

#if ADC_CALI_SCHEME_LINE_FITTING_SUPPORTED
    adc_cali_line_fitting_config_t line_cfg = {
        .unit_id = (CONFIG_BSP_BATT_ADC_UNIT == 1) ? ADC_UNIT_1 : ADC_UNIT_2,
        .atten = ADC_ATTEN_DB_11,
        .bitwidth = ADC_BITWIDTH_12,
    };
    esp_err_t ret = adc_cali_create_line_fitting_unit(&line_cfg, &s_adc_cali_handle);
    if (ret == ESP_OK) {
        ESP_LOGI(TAG, "ADC 校准: Line Fitting 模式");
        return ESP_OK;
    }
    ESP_LOGW(TAG, "Line Fitting 校准失败: %s", esp_err_to_name(ret));
#endif

    ESP_LOGW(TAG, "ADC 校准不可用, 使用原始 ADC 值估算");
    return ESP_ERR_NOT_SUPPORTED;
}

/* =========================================================================
 * 电压读取与电量映射
 * ========================================================================= */

/**
 * @brief 读取 ADC 原始值并转换为电压 (mV)
 *
 * 分压电路假设: V_batt → R1 → ADC → R2 → GND
 * R1 = 100kΩ, R2 = 100kΩ → 分压比 = 0.5
 * 因此实际电池电压 = ADC 电压 / 0.5 = ADC 电压 * 2
 */
#define VOLTAGE_DIVIDER_RATIO  2.0f  /* 分压比 R1+R2 / R2 */

static uint16_t bsp_battery_read_voltage_mv(void)
{
    if (s_adc_handle == NULL) {
        return 0;
    }

    int adc_raw = 0;
    esp_err_t ret = adc_oneshot_read(s_adc_handle,
                                      (adc_channel_t)CONFIG_BSP_BATT_ADC_CHANNEL,
                                      &adc_raw);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "ADC 读取失败: %s", esp_err_to_name(ret));
        return s_current_voltage_mv;  /* 返回上次缓存值 */
    }

    int voltage_mv = 0;
    if (s_adc_cali_handle) {
        ret = adc_cali_raw_to_voltage(s_adc_cali_handle, adc_raw, &voltage_mv);
        if (ret != ESP_OK) {
            /* 校准转换失败, 使用原始值估算 */
            voltage_mv = (int)((float)adc_raw * 3300.0f / 4095.0f);
        }
    } else {
        /* 无校准, 使用参考电压 3.3V / 12-bit (4096) 估算 */
        voltage_mv = (int)((float)adc_raw * 3300.0f / 4095.0f);
    }

    /* 补偿分压电路 */
    uint16_t batt_mv = (uint16_t)((float)voltage_mv * VOLTAGE_DIVIDER_RATIO);

    return batt_mv;
}

/**
 * @brief 更新滑动平均电压
 */
static uint16_t bsp_battery_update_avg_voltage(uint16_t voltage_mv)
{
    s_voltage_buf[s_voltage_buf_idx] = voltage_mv;
    s_voltage_buf_idx = (s_voltage_buf_idx + 1) % VOLTAGE_AVG_WINDOW;
    if (s_voltage_buf_count < VOLTAGE_AVG_WINDOW) {
        s_voltage_buf_count++;
    }

    uint32_t sum = 0;
    for (int i = 0; i < s_voltage_buf_count; i++) {
        sum += s_voltage_buf[i];
    }
    return (uint16_t)(sum / s_voltage_buf_count);
}

/**
 * @brief 电压 → 电量百分比线性映射
 */
static uint8_t bsp_battery_voltage_to_level(uint16_t voltage_mv)
{
    if (voltage_mv <= BSP_BATT_MIN_VOLTAGE_MV) {
        return 0;
    }
    if (voltage_mv >= BSP_BATT_MAX_VOLTAGE_MV) {
        return 100;
    }

    /* 线性映射: 3300mV→0%, 4200mV→100% */
    uint32_t range = BSP_BATT_MAX_VOLTAGE_MV - BSP_BATT_MIN_VOLTAGE_MV;
    uint32_t val = voltage_mv - BSP_BATT_MIN_VOLTAGE_MV;
    return (uint8_t)((val * 100) / range);
}

/* =========================================================================
 * 充电状态读取
 * ========================================================================= */
static void bsp_battery_read_charge_status(void)
{
    bool was_charging = s_is_charging;

    /* CHRG = 0 (低) 表示充电中; STDBY = 0 (低) 表示充电完成 */
    int chrg_level = 1;
    int stdby_level = 1;

    if (CONFIG_BSP_BATT_CHRG_GPIO >= 0) {
        chrg_level = gpio_get_level(CONFIG_BSP_BATT_CHRG_GPIO);
    }
    if (CONFIG_BSP_BATT_STDBY_GPIO >= 0) {
        stdby_level = gpio_get_level(CONFIG_BSP_BATT_STDBY_GPIO);
    }

    s_is_charging = (chrg_level == 0);
    s_charge_complete = (stdby_level == 0);

    /* 充电状态变化时触发回调 */
    if (s_is_charging != was_charging) {
        if (s_charging_cb) {
            s_charging_cb(s_is_charging);
        }
    }
}

/* =========================================================================
 * 轮询定时器回调
 * ========================================================================= */
static void bsp_battery_poll_callback(void *arg)
{
    (void)arg;

    /* 1. 读取电池电压 (滑动平均) */
    uint16_t raw_mv = bsp_battery_read_voltage_mv();
    s_current_voltage_mv = bsp_battery_update_avg_voltage(raw_mv);
    s_current_level = bsp_battery_voltage_to_level(s_current_voltage_mv);

    /* 2. 读取充电状态 */
    bsp_battery_read_charge_status();

    /* 3. 低电量检测 */
    if (s_current_level <= CONFIG_BSP_BATT_LOW_THRESHOLD) {
        if (!s_low_warned) {
            s_low_warned = true;
            ESP_LOGW(TAG, "低电量: %d%% (%dmV)", s_current_level, s_current_voltage_mv);
            if (s_low_cb) {
                s_low_cb(s_current_level);
            }
        }
    } else {
        /* 电量恢复后重置告警标志 */
        if (s_current_level > CONFIG_BSP_BATT_LOW_THRESHOLD + 5) {
            s_low_warned = false;
        }
    }

    ESP_LOGD(TAG, "电池: %d%% (%dmV), 充电=%d, 完成=%d",
             s_current_level, s_current_voltage_mv,
             s_is_charging, s_charge_complete);
}

/* =========================================================================
 * 公共接口实现
 * ========================================================================= */

esp_err_t bsp_battery_init(void)
{
    if (s_initialized) {
        ESP_LOGW(TAG, "电池管理已初始化, 跳过");
        return ESP_OK;
    }

    esp_err_t ret;

    /* 1. 初始化 ADC */
    adc_oneshot_unit_init_cfg_t adc_cfg = {
        .unit_id = (CONFIG_BSP_BATT_ADC_UNIT == 1) ? ADC_UNIT_1 : ADC_UNIT_2,
    };
    ret = adc_oneshot_new_unit(&adc_cfg, &s_adc_handle);
    APP_CHECK(ret, TAG, err, "ADC 单元创建失败: %s", esp_err_to_name(ret));

    adc_oneshot_chan_cfg_t chan_cfg = {
        .atten = ADC_ATTEN_DB_11,
        .bitwidth = ADC_BITWIDTH_12,
    };
    ret = adc_oneshot_config_channel(s_adc_handle,
                                      (adc_channel_t)CONFIG_BSP_BATT_ADC_CHANNEL,
                                      &chan_cfg);
    APP_CHECK(ret, TAG, err, "ADC 通道配置失败: %s", esp_err_to_name(ret));

    /* 2. ADC 校准 */
    bsp_battery_adc_cali_init();

    /* 3. 配置 CHRG / STDBY GPIO */
    if (CONFIG_BSP_BATT_CHRG_GPIO >= 0) {
        gpio_config_t chrg_conf = {
            .pin_bit_mask = (1ULL << CONFIG_BSP_BATT_CHRG_GPIO),
            .mode = GPIO_MODE_INPUT,
            .pull_up_en = GPIO_PULLUP_ENABLE,   /* TP4056 引脚开漏, 需上拉 */
            .pull_down_en = GPIO_PULLDOWN_DISABLE,
            .intr_type = GPIO_INTR_DISABLE,
        };
        gpio_config(&chrg_conf);
    }

    if (CONFIG_BSP_BATT_STDBY_GPIO >= 0) {
        gpio_config_t stdby_conf = {
            .pin_bit_mask = (1ULL << CONFIG_BSP_BATT_STDBY_GPIO),
            .mode = GPIO_MODE_INPUT,
            .pull_up_en = GPIO_PULLUP_ENABLE,
            .pull_down_en = GPIO_PULLDOWN_DISABLE,
            .intr_type = GPIO_INTR_DISABLE,
        };
        gpio_config(&stdby_conf);
    }

    /* 4. 首次读取 */
    bsp_battery_poll_callback(NULL);

    /* 5. 创建轮询定时器 */
    esp_timer_create_args_t timer_args = {
        .callback = bsp_battery_poll_callback,
        .arg = NULL,
        .name = "batt_poll",
    };
    ret = esp_timer_create(&timer_args, &s_poll_timer);
    APP_CHECK(ret, TAG, err, "定时器创建失败: %s", esp_err_to_name(ret));

    ret = esp_timer_start_periodic(s_poll_timer,
                                    CONFIG_BSP_BATT_POLL_INTERVAL_MS * 1000);
    APP_CHECK(ret, TAG, err, "定时器启动失败: %s", esp_err_to_name(ret));

    s_initialized = true;
    ESP_LOGI(TAG, "电池管理初始化完成: ADC CH%d, 轮询间隔 %dms, 当前 %d%% (%dmV)",
             CONFIG_BSP_BATT_ADC_CHANNEL,
             CONFIG_BSP_BATT_POLL_INTERVAL_MS,
             s_current_level, s_current_voltage_mv);
    return ESP_OK;

err:
    if (s_adc_handle) {
        adc_oneshot_del_unit(s_adc_handle);
        s_adc_handle = NULL;
    }
    return ret;
}

esp_err_t bsp_battery_deinit(void)
{
    if (!s_initialized) {
        return ESP_OK;
    }

    if (s_poll_timer) {
        esp_timer_stop(s_poll_timer);
        esp_timer_delete(s_poll_timer);
        s_poll_timer = NULL;
    }
    if (s_adc_cali_handle) {
        adc_cali_delete(s_adc_cali_handle);
        s_adc_cali_handle = NULL;
    }
    if (s_adc_handle) {
        adc_oneshot_del_unit(s_adc_handle);
        s_adc_handle = NULL;
    }

    s_initialized = false;
    ESP_LOGI(TAG, "电池管理已反初始化");
    return ESP_OK;
}

uint8_t bsp_battery_get_level(void)
{
    return s_current_level;
}

uint16_t bsp_battery_get_voltage_mv(void)
{
    return s_current_voltage_mv;
}

bool bsp_battery_is_charging(void)
{
    return s_is_charging;
}

bool bsp_battery_is_charge_complete(void)
{
    return s_charge_complete;
}

void bsp_battery_register_low_callback(bsp_battery_low_cb_t cb)
{
    s_low_cb = cb;
}

void bsp_battery_register_charging_callback(bsp_battery_charging_cb_t cb)
{
    s_charging_cb = cb;
}
