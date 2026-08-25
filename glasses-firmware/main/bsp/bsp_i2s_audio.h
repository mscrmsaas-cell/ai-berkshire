/**
 * @file bsp_i2s_audio.h
 * @brief I2S 音频驱动接口 — INMP441 麦克风 + MAX98357A 功放
 *
 * 硬件:
 *   - INMP441: I2S 数字 MEMS 麦克风 (RX 方向, 录音)
 *   - MAX98357A: I2S D 类功放 (TX 方向, 播放)
 *
 * 配置: 16kHz 采样率, 16-bit, 单声道, DMA 双缓冲
 *
 * @copyright Copyright (c) 2024 Railway Inspection AR Glasses Project
 * @license Apache-2.0
 */
#ifndef BSP_I2S_AUDIO_H
#define BSP_I2S_AUDIO_H

#include <stdint.h>
#include <stddef.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* =========================================================================
 * 音频规格常量
 * ========================================================================= */
#define BSP_AUDIO_SAMPLE_RATE    16000   /**< 采样率 Hz */
#define BSP_AUDIO_BITS_PER_SAMPLE 16     /**< 每样本位数 */
#define BSP_AUDIO_CHANNELS       1       /**< 单声道 */
#define BSP_AUDIO_DMA_BUF_COUNT  6       /**< DMA 缓冲区数量 */
#define BSP_AUDIO_DMA_BUF_LEN    1024    /**< 每个 DMA 缓冲区帧数 */
#define BSP_AUDIO_READ_BUF_SIZE  2048    /**< 录音读取缓冲区字节数 */
#define BSP_AUDIO_WRITE_BUF_SIZE 2048   /**< 播放写入缓冲区字节数 */

#define BSP_AUDIO_VOLUME_MAX     100     /**< 最大音量 */
#define BSP_AUDIO_VOLUME_DEFAULT 50      /**< 默认音量 */

/* =========================================================================
 * 类型定义
 * ========================================================================= */

/**
 * @brief 音频数据回调函数类型 (录音数据到达时调用)
 * @param data 音频数据指针
 * @param len 数据长度 (字节)
 * @param user_data 用户数据
 */
typedef void (*bsp_audio_rx_cb_t)(const uint8_t *data, size_t len, void *user_data);

/* =========================================================================
 * 公共接口函数
 * ========================================================================= */

/**
 * @brief 初始化 I2S 音频 (TX 播放 + RX 录音双通道)
 *
 * 初始化步骤:
 *   1. 配置 I2S TX (MAX98357A): BCLK / WS / DOUT
 *   2. 配置 I2S RX (INMP441): BCLK / WS / DIN
 *   3. 分配 DMA 缓冲区
 *
 * @return ESP_OK 成功, 其他为错误码
 */
esp_err_t bsp_audio_init(void);

/**
 * @brief 反初始化 I2S 音频 (释放资源)
 * @return ESP_OK
 */
esp_err_t bsp_audio_deinit(void);

/**
 * @brief 设置音量 (软件增益 0-100%)
 * @param volume 音量 0-100
 */
void bsp_audio_set_volume(uint8_t volume);

/**
 * @brief 获取当前音量
 * @return 音量值 0-100
 */
uint8_t bsp_audio_get_volume(void);

/**
 * @brief 从麦克风读取音频数据 (阻塞)
 * @param dest 目标缓冲区
 * @param len 期望读取的字节数
 * @return 实际读取的字节数, <0 表示错误
 */
int bsp_audio_read(uint8_t *dest, size_t len);

/**
 * @brief 向功放写入音频数据 (阻塞)
 * @param src 源数据缓冲区
 * @param len 待写入的字节数
 * @return 实际写入的字节数, <0 表示错误
 */
int bsp_audio_write(const uint8_t *src, size_t len);

/**
 * @brief 播放短促提示音 (开机自检用)
 * @return ESP_OK 或错误码
 */
esp_err_t bsp_audio_play_beep(void);

/**
 * @brief 注册录音数据回调 (非阻塞模式)
 * @param cb 回调函数
 * @param user_data 用户数据
 */
void bsp_audio_register_rx_callback(bsp_audio_rx_cb_t cb, void *user_data);

/**
 * @brief 启动后台录音任务 (在独立 FreeRTOS 任务中读取麦克风数据)
 * @return ESP_OK 或错误码
 */
esp_err_t bsp_audio_start_rx_task(void);

/**
 * @brief 停止后台录音任务
 */
void bsp_audio_stop_rx_task(void);

#ifdef __cplusplus
}
#endif

#endif /* BSP_I2S_AUDIO_H */
