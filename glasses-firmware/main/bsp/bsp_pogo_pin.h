/**
 * @file bsp_pogo_pin.h
 * @brief Pogo Pin 热插拔检测接口 — GPIO 下降沿中断 + 消抖
 *
 * 硬件: 磁吸摄像模块通过 Pogo Pin 连接眼镜。
 *   - DETECT 引脚: 连接时拉低 (下降沿), 断开时拉高 (上升沿)
 *   - 内部上拉使能, 确保断开时为高电平
 *
 * 功能:
 *   - GPIO 下降沿/上升沿中断检测
 *   - 软件消抖定时器 (默认 50ms)
 *   - 连接/断开回调通知
 *
 * @copyright Copyright (c) 2024 Railway Inspection AR Glasses Project
 * @license Apache-2.0
 */
#ifndef BSP_POGO_PIN_H
#define BSP_POGO_PIN_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* =========================================================================
 * Pogo Pin 事件枚举
 * ========================================================================= */

/**
 * @brief Pogo Pin 热插拔事件类型
 */
typedef enum {
    BSP_POGO_EVENT_ATTACHED = 0,  /**< 摄像头接入 (Pogo Pin 连接) */
    BSP_POGO_EVENT_DETACHED = 1,  /**< 摄像头断开 (Pogo Pin 断开) */
} bsp_pogo_event_t;

/* =========================================================================
 * 回调函数类型
 * ========================================================================= */

/**
 * @brief Pogo Pin 事件回调函数类型
 * @param event 事件类型
 * @param user_data 用户数据
 */
typedef void (*bsp_pogo_event_cb_t)(bsp_pogo_event_t event, void *user_data);

/**
 * @brief 摄像头接入回调 (便捷简写)
 */
typedef void (*bsp_pogo_attached_cb_t)(void);

/**
 * @brief 摄像头断开回调 (便捷简写)
 */
typedef void (*bsp_pogo_detached_cb_t)(void);

/* =========================================================================
 * 公共接口函数
 * ========================================================================= */

/**
 * @brief 初始化 Pogo Pin 热插拔检测
 *
 * 初始化步骤:
 *   1. 配置 DETECT GPIO 为输入 (内部上拉)
 *   2. 注册双边沿中断 (下降沿=接入, 上升沿=断开)
 *   3. 创建消抖定时器
 *   4. 读取当前引脚状态确定初始连接状态
 *
 * @return ESP_OK 成功, 其他为错误码
 */
esp_err_t bsp_pogo_pin_init(void);

/**
 * @brief 反初始化 Pogo Pin 检测 (释放资源)
 * @return ESP_OK
 */
esp_err_t bsp_pogo_pin_deinit(void);

/**
 * @brief 获取当前连接状态
 * @return true 摄像头已接入, false 未接入
 */
bool bsp_pogo_pin_is_attached(void);

/**
 * @brief 注册事件回调 (连接/断开统一回调)
 * @param cb 回调函数 (NULL 取消注册)
 * @param user_data 用户数据指针
 */
void bsp_pogo_pin_register_event_callback(bsp_pogo_event_cb_t cb, void *user_data);

/**
 * @brief 注册便捷回调 (分别注册接入和断开回调)
 * @param attached_cb 接入回调 (可为 NULL)
 * @param detached_cb 断开回调 (可为 NULL)
 */
void bsp_pogo_pin_register_callbacks(bsp_pogo_attached_cb_t attached_cb,
                                      bsp_pogo_detached_cb_t detached_cb);

/**
 * @brief 手动触发状态检测 (用于初始化后的首次检测)
 * @return 当前连接状态 (true=已接入)
 */
bool bsp_pogo_pin_poll(void);

#ifdef __cplusplus
}
#endif

#endif /* BSP_POGO_PIN_H */
