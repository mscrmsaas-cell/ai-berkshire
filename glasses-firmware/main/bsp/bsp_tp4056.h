/**
 * @file bsp_tp4056.h
 * @brief TP4056 电池管理驱动接口 — ADC 电压检测 + 充电状态 GPIO
 *
 * 硬件: TP4056 锂电池充电管理芯片
 *   - CHRG 引脚 (低有效): 充电中
 *   - STDBY 引脚 (低有效): 充电完成
 *   - 电池电压: ADC 分压采样
 *
 * @copyright Copyright (c) 2024 Railway Inspection AR Glasses Project
 * @license Apache-2.0
 */
#ifndef BSP_TP4056_H
#define BSP_TP4056_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* =========================================================================
 * 电池规格常量
 * ========================================================================= */
#define BSP_BATT_MAX_LEVEL     100     /**< 最大电量百分比 */
#define BSP_BATT_MIN_VOLTAGE_MV 3300   /**< 最低电池电压 (mV), 0% */
#define BSP_BATT_MAX_VOLTAGE_MV 4200   /**< 最高电池电压 (mV), 100% */
#define BSP_BATT_FULL_VOLTAGE_MV 4150  /**< 满电电压 (mV) */
#define BSP_BATT_WARN_VOLTAGE_MV 3700  /**< 低电量告警电压 (mV) */

/* =========================================================================
 * 类型定义
 * ========================================================================= */

/**
 * @brief 低电量回调函数类型
 * @param level 当前电量百分比
 */
typedef void (*bsp_battery_low_cb_t)(uint8_t level);

/**
 * @brief 充电状态变化回调函数类型
 * @param charging true=开始充电, false=充电结束
 */
typedef void (*bsp_battery_charging_cb_t)(bool charging);

/* =========================================================================
 * 公共接口函数
 * ========================================================================= */

/**
 * @brief 初始化 TP4056 电池管理 (ADC + 充电状态 GPIO + 轮询定时器)
 *
 * 初始化步骤:
 *   1. 配置 ADC (电池电压检测通道)
 *   2. 配置 CHRG / STDBY GPIO (输入)
 *   3. 创建周期性轮询定时器
 *
 * @return ESP_OK 成功, 其他为错误码
 */
esp_err_t bsp_battery_init(void);

/**
 * @brief 反初始化电池管理 (释放资源, 停止轮询)
 * @return ESP_OK
 */
esp_err_t bsp_battery_deinit(void);

/**
 * @brief 获取电池电量百分比 (0-100)
 *
 * 通过 ADC 读取电池电压, 线性映射到 0-100%。
 * 结果经过滑动平均滤波, 避免跳变。
 *
 * @return 电量百分比 0-100
 */
uint8_t bsp_battery_get_level(void);

/**
 * @brief 获取电池电压 (毫伏)
 * @return 电压值 (mV)
 */
uint16_t bsp_battery_get_voltage_mv(void);

/**
 * @brief 判断是否正在充电
 * @return true 充电中, false 未充电
 */
bool bsp_battery_is_charging(void);

/**
 * @brief 判断是否充电完成
 * @return true 充电完成, false 未完成
 */
bool bsp_battery_is_charge_complete(void);

/**
 * @brief 注册低电量回调函数
 * @param cb 回调函数 (NULL 取消注册)
 */
void bsp_battery_register_low_callback(bsp_battery_low_cb_t cb);

/**
 * @brief 注册充电状态变化回调函数
 * @param cb 回调函数 (NULL 取消注册)
 */
void bsp_battery_register_charging_callback(bsp_battery_charging_cb_t cb);

#ifdef __cplusplus
}
#endif

#endif /* BSP_TP4056_H */
