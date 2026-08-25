/**
 * @file bsp_lcd_gc9a01.h
 * @brief GC9A01 SPI LCD 驱动接口 — 240x240 RGB565 圆形显示屏
 *
 * 硬件: GC9A01 控制器, SPI 接口, 240x240 像素, RGB565 格式
 * 配置: SPI 40MHz, 背光 LEDC PWM 调光
 *
 * @copyright Copyright (c) 2024 Railway Inspection AR Glasses Project
 * @license Apache-2.0
 */
#ifndef BSP_LCD_GC9A01_H
#define BSP_LCD_GC9A01_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
#include "esp_lcd_types.h"

#ifdef __cplusplus
extern "C" {
#endif

/* =========================================================================
 * LCD 规格常量
 * ========================================================================= */
#define BSP_LCD_H_RES       240     /**< 水平分辨率 */
#define BSP_LCD_V_RES       240     /**< 垂直分辨率 */
#define BSP_LCD_BITS_PER_PIXEL  16  /**< 每像素位数 (RGB565) */
#define BSP_LCD_DRAW_BUFFERS    2  /**< 双缓冲数量 */
#define BSP_LCD_BUFFER_LINES    40 /**< 每个绘制缓冲的行数 (240*40*2 = 19200B) */

/* =========================================================================
 * GC9A01 命令定义
 * ========================================================================= */
#define GC9A01_CMD_SWRESET      0x01  /**< 软件复位 */
#define GC9A01_CMD_SLPIN        0x10  /**< 进入睡眠模式 */
#define GC9A01_CMD_SLPOUT      0x11  /**< 退出睡眠模式 */
#define GC9A01_CMD_INVOFF      0x20  /**< 关闭反色 */
#define GC9A01_CMD_DISPON      0x29  /**< 开启显示 */
#define GC9A01_CMD_CASET        0x2A  /**< 列地址设置 */
#define GC9A01_CMD_RASET        0x2B  /**< 行地址设置 */
#define GC9A01_CMD_RAMWR        0x2C  /**< 显存写入 */
#define GC9A01_CMD_COLMOD       0x3A  /**< 像素格式设置 */
#define GC9A01_CMD_MADCTL       0x36  /**< 内存访问控制 */
#define GC9A01_CMD_FRMCTR1      0xB1  /**< 帧率控制 (正常模式) */
#define GC9A01_CMD_DISPVCTR     0xC5  /**< VCOM 电压控制 */
#define GC9A01_CMD_DFUNCTR      0xB6  /**< 显示功能控制 */
#define GC9A01_CMD_PWCTR1       0xC0  /**< 电源控制 1 */
#define GC9A01_CMD_PWCTR2       0xC1  /**< 电源控制 2 */
#define GC9A01_CMD_VMCTR1       0xC5  /**< VCOM 控制 */
#define GC9A01_CMD_GAMCTRP1     0xE0  /**< 伽马校正 (正极性) */
#define GC9A01_CMD_GAMCTRN1     0xE1  /**< 伽马校正 (负极性) */

/* MADCTL 位定义 */
#define MADCTL_MY   0x80  /**< 行地址翻转 */
#define MADCTL_MX   0x40  /**< 列地址翻转 */
#define MADCTL_MV   0x20  /**< 行列交换 */
#define MADCTL_ML   0x10  /**< 垂直扫描方向 */
#define MADCTL_RGB  0x00  /**< RGB 顺序 */
#define MADCTL_BGR  0x08  /**< BGR 顺序 */
#define MADCTL_MH   0x04  /**< 水平扫描方向 */

/* =========================================================================
 * 类型定义
 * ========================================================================= */

/**
 * @brief LVGL 刷新完成回调函数类型
 * @param disp 驱动句柄
 */
typedef void (*bsp_lcd_flush_done_cb_t)(void *user_data);

/* =========================================================================
 * 公共接口函数
 * ========================================================================= */

/**
 * @brief 初始化 GC9A01 LCD (SPI + 背光 PWM + 初始化命令序列)
 *
 * 初始化步骤:
 *   1. 配置 SPI 主机 (40MHz, MODE 0)
 *   2. 配置 DC / CS / RST GPIO
 *   3. 配置背光 LEDC PWM
 *   4. 发送 GC9A01 初始化命令序列
 *   5. 退出睡眠, 开启显示
 *
 * @return ESP_OK 成功, 其他为错误码
 */
esp_err_t bsp_lcd_init(void);

/**
 * @brief 反初始化 LCD (释放资源)
 * @return ESP_OK
 */
esp_err_t bsp_lcd_deinit(void);

/**
 * @brief 设置背光亮度 (PWM 调光)
 * @param brightness 亮度 0-255
 */
void bsp_lcd_set_brightness(uint8_t brightness);

/**
 * @brief 获取当前背光亮度
 * @return 亮度值 0-255
 */
uint8_t bsp_lcd_get_brightness(void);

/**
 * @brief 整屏填充单色
 * @param color RGB565 颜色值
 */
void bsp_lcd_fill_color(uint16_t color);

/**
 * @brief 写入一个矩形区域的像素数据
 * @param x 起始列
 * @param y 起始行
 * @param w 宽度
 * @param h 高度
 * @param data RGB565 像素数据缓冲区 (大小 = w * h * 2 字节)
 */
void bsp_lcd_draw_bitmap(int x, int y, int w, int h, const uint16_t *data);

/**
 * @brief 获取 esp_lcd 面板句柄 (供 LVGL 等上层使用)
 * @return 面板句柄, 未初始化返回 NULL
 */
esp_lcd_panel_handle_t bsp_lcd_get_panel(void);

/**
 * @brief LVGL flush 回调 (供 display_manager 注册)
 *
 * 将 LVGL 绘制缓冲区的数据通过 SPI 推送到 LCD 显存。
 * 刷新完成后调用 lv_disp_flush_ready()。
 *
 * @param drv LVGL 显示驱动 (未使用, 保留兼容)
 * @param area 刷新区域
 * @param color_map RGB565 像素数据
 */
void bsp_lcd_lvgl_flush_cb(void *drv, const void *area, const void *color_map);

/**
 * @brief 注册 flush 完成回调 (LVGL 集成用)
 * @param cb 回调函数
 * @param user_data 用户数据指针
 */
void bsp_lcd_register_flush_done_cb(bsp_lcd_flush_done_cb_t cb, void *user_data);

#ifdef __cplusplus
}
#endif

#endif /* BSP_LCD_GC9A01_H */
