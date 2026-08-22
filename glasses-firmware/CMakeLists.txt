# =============================================================================
# glasses-firmware/CMakeLists.txt
# 铁路巡检智能眼镜 — ESP32-S3 固件顶层 CMake 配置
#
# 依赖: ESP-IDF v5.1+
# 目标芯片: ESP32-S3 (双核 240MHz, WiFi + BLE, 8MB Flash, PSRAM)
# =============================================================================
#
# 本文件是 ESP-IDF 项目的顶层 CMakeLists.txt。
# 通过 EXTRA_COMPONENT_DIRS 声明第三方组件目录 (lvgl / esp32-camera / tuya SDK)。
#
# 构建命令:
#   idf.py set-target esp32s3
#   idf.py build
# =============================================================================

# ESP-IDF 最低版本要求
cmake_minimum_required(VERSION 3.16)

# 引入 ESP-IDF 构建系统 (idf.py 会自动注入 IDF_PATH)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)

# -----------------------------------------------------------------------------
# 项目名称
# -----------------------------------------------------------------------------
project(railway_ar_glasses)

# -----------------------------------------------------------------------------
# 额外组件目录 (通过 Git 子模块管理)
#
# - components/lvgl:           LVGL v8.3 图形库
# - components/esp32-camera:    Espressif 官方 DVP 摄像头驱动 (OV2640)
# - components/tuya-iot-core-sdk: 涂鸦 IoT Core SDK (可选, 受 CONFIG_TUYA_ENABLE 控制)
# -----------------------------------------------------------------------------
set(EXTRA_COMPONENT_DIRS
    "${CMAKE_CURRENT_LIST_DIR}/components/lvgl"
    "${CMAKE_CURRENT_LIST_DIR}/components/esp32-camera"
)

# 涂鸦 SDK 仅在启用时加入编译 (子模块可能不存在时跳过)
if(EXISTS "${CMAKE_CURRENT_LIST_DIR}/components/tuya-iot-core-sdk/CMakeLists.txt")
    list(APPEND EXTRA_COMPONENT_DIRS
        "${CMAKE_CURRENT_LIST_DIR}/components/tuya-iot-core-sdk"
    )
endif()

# -----------------------------------------------------------------------------
# 项目级编译选项
# -----------------------------------------------------------------------------
# 使用 C11 标准
set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

# 编译警告 (开发阶段开启 -Wall -Werror, 生产可适当放宽)
add_compile_options(
    -Wall
    -Wextra
    -Wno-unused-parameter
    -Wno-missing-field-initializers
)

# 对 main 组件开启更严格的警告
target_compile_options(${CMAKE_PROJECT_NAME}.elf PRIVATE
    -Werror=implicit-function-declaration
)

# -----------------------------------------------------------------------------
# 打印构建信息 (便于调试)
# -----------------------------------------------------------------------------
message(STATUS "========================================")
message(STATUS "Project:  railway_ar_glasses (ESP32-S3)")
message(STATUS "IDF Ver:  ${IDF_VERSION}")
message(STATUS "Build:    ${CMAKE_BUILD_TYPE}")
message(STATUS "========================================")
