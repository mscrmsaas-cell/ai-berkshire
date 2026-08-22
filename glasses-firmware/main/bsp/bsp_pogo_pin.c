/**
 * @file bsp_pogo_pin.c
 * @brief Pogo Pin 热插拔检测驱动实现 — GPIO 下降沿中断 + 消抖定时器
 *
 * 工作原理:
 *   - DETECT 引脚内部上拉, 磁吸连接时物理拉低 (下降沿 → 接入)
 *   - 磁吸断开时引脚恢复高电平 (上升沿 → 断开)
 *   - GPIO 中断触发后启动消抖定时器, 延时后再次读取引脚电平确认
 *   - 确认状态变化后调用注册的回调函数
 *
 * @copyright Copyright (c) 2024 Railway Inspection AR Glasses Project
 * @license Apache-2.0
 */

#include "bsp_pogo_pin.h"
#include "app_common.h"

#include "esp_log.h"
#include "driver/gpio.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

static const char *TAG = TAG_POGO;

/* =========================================================================
 * 模块内部状态
 * ========================================================================= */
static bool s_initialized = false;
static bool s_is_attached = false;       /**< 当前连接状态 */
static esp_timer_handle_t s_debounce_timer = NULL;
static volatile bool s_debounce_pending = false;  /**< 消抖定时器是否在运行 */

/* 回调函数 */
static bsp_pogo_event_cb_t s_event_cb = NULL;
static void *s_event_cb_user_data = NULL;
static bsp_pogo_attached_cb_t s_attached_cb = NULL;
static bsp_pogo_detached_cb_t s_detached_cb = NULL;

/* 事件队列 (用于从中断上下文传递到任务上下文调用回调) */
static QueueHandle_t s_event_queue = NULL;

/* =========================================================================
 * 消抖定时器回调 (定时器上下文中执行)
 *
 * 延时消抖后重新读取 GPIO 电平，确认实际状态。
 * ========================================================================= */
static void bsp_pogo_debounce_timer_callback(void *arg)
{
    (void)arg;

    /* 读取当前 GPIO 电平确认状态 */
    int level = gpio_get_level(CONFIG_BSP_POGO_DETECT_GPIO);

    /* level=0 (低) → 已接入; level=1 (高) → 已断开 */
    bool new_attached = (level == 0);

    s_debounce_pending = false;

    if (new_attached == s_is_attached) {
        /* 状态未变化 (抖动), 忽略 */
        ESP_LOGD(TAG, "消抖后状态未变化: attached=%d", new_attached);
        return;
    }

    /* 状态确实变化 */
    bool was_attached = s_is_attached;
    s_is_attached = new_attached;

    bsp_pogo_event_t event = new_attached ?
        BSP_POGO_EVENT_ATTACHED : BSP_POGO_EVENT_DETACHED;

    ESP_LOGI(TAG, "Pogo Pin 状态变化: %s → %s (%s)",
             was_attached ? "ATTACHED" : "DETACHED",
             new_attached ? "ATTACHED" : "DETACHED",
             new_attached ? "摄像头接入" : "摄像头断开");

    /* 将事件发送到队列, 在任务上下文中调用回调 */
    if (s_event_queue) {
        bsp_pogo_event_t q_event = event;
        xQueueSendFromISR(s_event_queue, &q_event, NULL);
    }
}

/* =========================================================================
 * GPIO 中断处理函数 (ISR 上下文)
 * ========================================================================= */
static void IRAM_ATTR bsp_pogo_gpio_isr_handler(void *arg)
{
    (void)arg;

    /* 防止消抖期间重复触发 */
    if (s_debounce_pending) {
        return;
    }

    s_debounce_pending = true;

    /* 启动消抖定时器 */
    if (s_debounce_timer) {
        esp_timer_stop(s_debounce_timer);
        esp_timer_start_once(s_debounce_timer,
                             CONFIG_BSP_POGO_DEBOUNCE_MS * 1000);
    }
}

/* =========================================================================
 * 事件处理任务 (任务上下文)
 *
 * 从队列中取出事件, 调用用户注册的回调函数。
 * 避免 ISR 中直接调用回调 (可能阻塞或操作 FreeRTOS 资源)。
 * ========================================================================= */
static void bsp_pogo_event_task(void *arg)
{
    (void)arg;

    bsp_pogo_event_t event;

    while (true) {
        if (xQueueReceive(s_event_queue, &event, portMAX_DELAY) == pdTRUE) {
            /* 调用统一事件回调 */
            if (s_event_cb) {
                s_event_cb(event, s_event_cb_user_data);
            }

            /* 调用便捷回调 */
            if (event == BSP_POGO_EVENT_ATTACHED) {
                if (s_attached_cb) {
                    s_attached_cb();
                }
            } else {
                if (s_detached_cb) {
                    s_detached_cb();
                }
            }
        }
    }
}

/* =========================================================================
 * 公共接口实现
 * ========================================================================= */

esp_err_t bsp_pogo_pin_init(void)
{
    if (s_initialized) {
        ESP_LOGW(TAG, "Pogo Pin 已初始化, 跳过");
        return ESP_OK;
    }

    esp_err_t ret;

    /* 1. 配置 GPIO 为输入 (内部上拉, 双边沿中断) */
    gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << CONFIG_BSP_POGO_DETECT_GPIO),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_ANYEDGE,  /* 双边沿触发 */
    };
    ret = gpio_config(&io_conf);
    APP_CHECK(ret, TAG, err, "GPIO 配置失败: %s", esp_err_to_name(ret));

    /* 2. 创建事件队列 */
    s_event_queue = xQueueCreate(8, sizeof(bsp_pogo_event_t));
    if (s_event_queue == NULL) {
        ESP_LOGE(TAG, "创建事件队列失败");
        ret = ESP_ERR_NO_MEM;
        goto err;
    }

    /* 3. 创建事件处理任务 */
    BaseType_t task_ret = xTaskCreatePinnedToCore(
        bsp_pogo_event_task,
        "pogo_evt",
        2048,
        NULL,
        5,
        NULL,
        0  /* Core 0 */
    );
    if (task_ret != pdPASS) {
        ESP_LOGE(TAG, "创建事件处理任务失败");
        ret = ESP_ERR_NO_MEM;
        goto err_queue;
    }

    /* 4. 创建消抖定时器 */
    esp_timer_create_args_t timer_args = {
        .callback = bsp_pogo_debounce_timer_callback,
        .arg = NULL,
        .name = "pogo_debounce",
    };
    ret = esp_timer_create(&timer_args, &s_debounce_timer);
    APP_CHECK(ret, TAG, err_queue, "消抖定时器创建失败: %s", esp_err_to_name(ret));

    /* 5. 安装 GPIO ISR 服务 */
    ret = gpio_install_isr_service(ESP_INTR_FLAG_IRAM);
    if (ret != ESP_OK && ret != ESP_ERR_INVALID_STATE) {
        /* ESP_ERR_INVALID_STATE 表示 ISR 服务已安装, 忽略 */
        ESP_LOGE(TAG, "ISR 服务安装失败: %s", esp_err_to_name(ret));
        goto err_timer;
    }

    /* 6. 添加 GPIO 中断处理函数 */
    ret = gpio_isr_handler_add(CONFIG_BSP_POGO_DETECT_GPIO,
                                bsp_pogo_gpio_isr_handler, NULL);
    APP_CHECK(ret, TAG, err_timer, "ISR handler 添加失败: %s",
              esp_err_to_name(ret));

    /* 7. 读取初始状态 */
    int init_level = gpio_get_level(CONFIG_BSP_POGO_DETECT_GPIO);
    s_is_attached = (init_level == 0);

    s_initialized = true;
    ESP_LOGI(TAG, "Pogo Pin 初始化完成: GPIO%d, 消抖 %dms, 初始状态: %s",
             CONFIG_BSP_POGO_DETECT_GPIO,
             CONFIG_BSP_POGO_DEBOUNCE_MS,
             s_is_attached ? "ATTACHED" : "DETACHED");
    return ESP_OK;

err_timer:
    esp_timer_delete(s_debounce_timer);
    s_debounce_timer = NULL;
err_queue:
    if (s_event_queue) {
        vQueueDelete(s_event_queue);
        s_event_queue = NULL;
    }
err:
    return ret;
}

esp_err_t bsp_pogo_pin_deinit(void)
{
    if (!s_initialized) {
        return ESP_OK;
    }

    gpio_isr_handler_remove(CONFIG_BSP_POGO_DETECT_GPIO);

    if (s_debounce_timer) {
        esp_timer_stop(s_debounce_timer);
        esp_timer_delete(s_debounce_timer);
        s_debounce_timer = NULL;
    }

    if (s_event_queue) {
        vQueueDelete(s_event_queue);
        s_event_queue = NULL;
    }

    gpio_reset_pin(CONFIG_BSP_POGO_DETECT_GPIO);

    s_initialized = false;
    s_is_attached = false;
    ESP_LOGI(TAG, "Pogo Pin 已反初始化");
    return ESP_OK;
}

bool bsp_pogo_pin_is_attached(void)
{
    return s_is_attached;
}

void bsp_pogo_pin_register_event_callback(bsp_pogo_event_cb_t cb, void *user_data)
{
    s_event_cb = cb;
    s_event_cb_user_data = user_data;
}

void bsp_pogo_pin_register_callbacks(bsp_pogo_attached_cb_t attached_cb,
                                      bsp_pogo_detached_cb_t detached_cb)
{
    s_attached_cb = attached_cb;
    s_detached_cb = detached_cb;
}

bool bsp_pogo_pin_poll(void)
{
    if (!s_initialized) {
        return false;
    }

    int level = gpio_get_level(CONFIG_BSP_POGO_DETECT_GPIO);
    s_is_attached = (level == 0);

    return s_is_attached;
}
