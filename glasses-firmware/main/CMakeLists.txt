# =============================================================================
# glasses-firmware/main/CMakeLists.txt
# main 组件 CMake 配置 — 注册源文件和头文件目录
# =============================================================================

# 设置组件源文件 — 全部模块
set(SRCS
    "main.c"
    # --- BSP 板级支持包 ---
    "bsp/bsp_lcd_gc9a01.c"
    "bsp/bsp_i2s_audio.c"
    "bsp/bsp_tp4056.c"
    "bsp/bsp_pogo_pin.c"
    "bsp/bsp_ov2640.c"
    # --- BLE 通信 ---
    "ble/ble_nus_service.c"
    "ble/ble_transport.c"
    "ble/ble_gap_manager.c"
    # --- 显示 ---
    "display/display_manager.c"
    "display/ui_renderer.c"
    "display/ui_screens.c"
    # --- 音频 ---
    "audio/audio_pipeline.c"
    "audio/audio_codec.c"
    # --- 电源管理 ---
    "power/power_manager.c"
    # --- 摄像头 ---
    "camera/camera_module.c"
)

# 涂鸦模块 (条件编译)
if(CONFIG_TUYA_ENABLE)
    list(APPEND SRCS
        "tuya/tuya_ble_adapter.c"
        "tuya/tuya_dp_handler.c"
    )
endif()

idf_component_register(
    SRCS ${SRCS}
    INCLUDE_DIRS
        "include"
        "bsp"
        "ble"
        "display"
        "audio"
        "power"
        "camera"
        "tuya"
        # 包含共享协议头文件 (message_types.h)
        "${CMAKE_CURRENT_LIST_DIR}/../../shared/protocols"
    REQUIRES
        driver
        esp_timer
        nvs_flash
        esp_lcd
        esp_psram
        bt
)

# 如果 LVGL 组件存在, 添加依赖
if(IDF_COMPONENT_lvgl)
    idf_component_optional_requires(PRIVATE lvgl)
endif()

# 如果 esp32-camera 组件存在, 添加依赖
if(IDF_COMPONENT_esp_camera)
    idf_component_optional_requires(PRIVATE esp_camera)
endif()

# 如果启用了涂鸦 SDK
if(CONFIG_TUYA_ENABLE AND IDF_COMPONENT_tuya_iot_core_sdk)
    idf_component_optional_requires(PRIVATE tuya_iot_core_sdk)
endif()
