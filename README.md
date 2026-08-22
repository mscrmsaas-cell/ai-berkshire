# Railway Inspection AR Glasses — Embedded System

铁路巡检智能眼镜嵌入式软件 Monorepo。基于 ESP32-S3 + ESP-IDF 的基础眼镜固件、基于 Raspberry Pi CM4 的挎包终端服务、磁吸摄像模块规范以及跨模块共享通信协议。

## 架构概览

```
涂鸦 IoT 云平台 (MQTT: 设备管理 / DP 上报 / OTA 分发)
    │                               │
    │ BLE (涂鸦 BLE SDK)             │ MQTT (涂鸦 Linux SDK)
    │                               │
基础眼镜(ESP32-S3)              挎包终端(CM4 Linux)
  GC9A01 LCD 240x240             BLE 网桥 (NUS Central, 最多 4 副)
  I2S 音频 (INMP441 麦 + MAX98357A 功放)
  TP4056 电池管理                 YOLOv8n 推理 (ONNX Runtime)
  Pogo Pin (OV2640 热插拔)        本地 RAG (Qdrant + BGE + Phi-3)
  BLE NUS (从设备)                5G 基站通信 (AT 命令)
    │                               │
    └───── BLE NUS (P2P) ──────────┘
                                    │
                                    │ HTTPS REST API (5G / 4G)
                                    │
                              SaaS 后端 (FastAPI)
                              PostgreSQL + MinIO + Milvus + Dify
```

## 目录结构

```
railway-ar-glasses-embedded/
├── README.md                      # 本文件
├── .gitignore                     # Git 忽略规则
├── .gitmodules                    # Git 子模块声明 (lvgl / esp32-camera / tuya SDK)
├── Makefile                       # 顶层构建入口
├── LICENSE                        # Apache 2.0
├── shared/                        # 跨模块共享
│   ├── protocols/                 # 通信协议定义
│   │   ├── ble_nus_protocol.md    # BLE NUS 二进制帧格式规范
│   │   ├── tuya_dp_schema.json    # 涂鸦 DP 数据点 Schema (14 个)
│   │   ├── message_types.h        # C 语言消息类型枚举 (35 个值)
│   │   ├── message_types.py       # Python IntEnum 枚举 (与 C 一一对应)
│   │   ├── saas_api_contract.md   # SaaS API 对接契约
│   │   ├── test_consistency.py    # 协议一致性测试
│   │   └── __init__.py            # Python 包初始化
│   └── docs/
│       └── architecture.md        # 系统架构文档
├── glasses-firmware/              # 眼镜端 ESP32-S3 固件 (ESP-IDF v5.1)
│   ├── CMakeLists.txt             # 顶层 CMake
│   ├── sdkconfig.defaults         # 默认 SDK 配置
│   ├── partitions.csv             # 分区表
│   ├── main/                      # 主程序组件
│   │   ├── CMakeLists.txt
│   │   ├── Kconfig.projbuild      # 项目配置菜单
│   │   ├── main.c                 # app_main() 入口
│   │   ├── include/
│   │   │   └── app_common.h       # 公共头文件
│   │   └── bsp/                   # 板级支持包
│   │       ├── bsp_lcd_gc9a01.c/h # GC9A01 SPI LCD 驱动
│   │       ├── bsp_i2s_audio.c/h  # I2S 音频驱动
│   │       ├── bsp_tp4056.c/h     # 电池管理驱动
│   │       └── bsp_pogo_pin.c/h   # 热插拔检测驱动
│   └── (BLE / display / audio / power / camera / tuya — 后续批次)
├── camera-module/                 # 磁吸摄像模块规范 (后续批次)
├── bag-terminal/                  # 挎包终端 Python 服务 (后续批次)
├── saas-extensions/               # SaaS 后端扩展 (后续批次)
└── .github/workflows/            # CI/CD 流水线 (后续批次)
```

## 技术选型

| 模块 | 硬件平台 | 核心技术 |
|------|----------|---------|
| 基础眼镜 | ESP32-S3 (双核 240MHz, WiFi + BLE) | ESP-IDF v5.1 + LVGL + 涂鸦 BLE SDK |
| 磁吸摄像 | OV2640 + Pogo Pin | 8 位 DVP 并行总线 + SCCB |
| 挎包终端 | Raspberry Pi CM4 (ARM Linux) | Python + FastAPI + Bleak + ONNX Runtime |

## 快速开始

### 前置要求

- ESP-IDF v5.1 (含 CMake / Ninja)
- Python 3.11+
- Docker (挎包终端)
- Git

### 编译眼镜固件

```bash
cd glasses-firmware
idf.py set-target esp32s3
idf.py menuconfig    # 可选: 调整引脚 / 涂鸦开关
idf.py build
idf.py -p /dev/ttyACM0 flash monitor
```

### 运行协议一致性测试

```bash
make protocol-test
# 或
python shared/protocols/test_consistency.py
```

### 顶层 Makefile 目标

```bash
make glasses        # 编译眼镜固件
make bag            # 构建挎包终端 Docker 镜像
make protocol-test  # 运行协议一致性测试
make saas-ext       # 应用 SaaS 后端扩展
```

## 通信协议

### BLE NUS 帧格式

```
SYNC(0xAA 0x55) | MSG_TYPE(1B) | SEQ(2B BE) | LEN(2B BE) | DATA(NB) | CRC16(2B LE)
```

详见 [`shared/protocols/ble_nus_protocol.md`](shared/protocols/ble_nus_protocol.md)。

### 消息类型

8 个范围段共 35 个枚举值 (0x00-0x8F)，C 与 Python 实现一一对应。详见
[`shared/protocols/message_types.h`](shared/protocols/message_types.h)。

## 许可证

Apache License 2.0 — 详见 [`LICENSE`](LICENSE)。
