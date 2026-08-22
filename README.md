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
│
├── shared/                        # 跨模块共享
│   ├── protocols/                 # 通信协议定义
│   │   ├── ble_nus_protocol.md    # BLE NUS 二进制帧格式规范
│   │   ├── tuya_dp_schema.json    # 涂鸦 DP 数据点 Schema (14 个)
│   │   ├── message_types.h        # C 语言消息类型枚举 (35 个值)
│   │   ├── message_types.py       # Python IntEnum 枚举 (与 C 一一对应)
│   │   ├── saas_api_contract.md   # SaaS API 对接契约
│   │   ├── test_consistency.py    # 协议一致性测试 (25 项)
│   │   └── __init__.py            # Python 包初始化
│   └── docs/
│       └── architecture.md        # 系统架构文档
│
├── glasses-firmware/              # 眼镜端 ESP32-S3 固件 (ESP-IDF v5.1)
│   ├── CMakeLists.txt             # 顶层 CMake
│   ├── sdkconfig.defaults         # 默认 SDK 配置
│   ├── partitions.csv             # 分区表 (4MB Flash)
│   ├── tools/
│   │   ├── flash_firmware.sh      # 烧录脚本
│   │   └── generate_dp_header.py  # 涂鸦 DP 头文件生成器
│   └── main/                      # 主程序组件 (17 源文件)
│       ├── CMakeLists.txt         # 组件 CMake (注册全部源文件)
│       ├── Kconfig.projbuild      # 项目配置菜单 (引脚/涂鸦开关)
│       ├── main.c                 # app_main() 入口 (366 行)
│       ├── include/
│       │   └── app_common.h       # 公共头文件
│       ├── bsp/                   # 板级支持包
│       │   ├── bsp_lcd_gc9a01.c/h # GC9A01 SPI LCD 驱动 (240x240)
│       │   ├── bsp_i2s_audio.c/h   # I2S 音频驱动 (INMP441 + MAX98357A)
│       │   ├── bsp_tp4056.c/h      # TP4056 电池管理驱动
│       │   ├── bsp_pogo_pin.c/h    # Pogo Pin 热插拔检测驱动
│       │   └── bsp_ov2640.c/h      # OV2640 摄像头 DVP 驱动
│       ├── ble/                   # BLE NUS 通信栈
│       │   ├── ble_common.h       # 公共定义 (UUID / 接口声明)
│       │   ├── ble_nus_service.c  # NimBLE GATT Server (NUS Service)
│       │   ├── ble_transport.c/h  # 帧解析 / 分包重组 / CRC16
│       │   └── ble_gap_manager.c  # GAP 广播 / 连接管理
│       ├── display/               # 显示管理
│       │   ├── display_manager.c  # 显示管理器 (双缓冲 / 帧提交)
│       │   ├── ui_renderer.c      # LVGL UI 渲染器
│       │   └── ui_screens.c       # UI 页面 (状态/巡检/告警/导航)
│       ├── audio/                 # 音频管线
│       │   ├── audio_pipeline.c   # 录音 / 播放管线管理
│       │   └── audio_codec.c      # G.711 编解码
│       ├── camera/
│       │   └── camera_module.c    # OV2640 磁吸模块管理 (热插拔/采集)
│       ├── power/
│       │   └── power_manager.c    # 电源管理 (低电量/休眠/唤醒)
│       └── tuya/                  # 涂鸦 IoT 集成
│           ├── tuya_ble_adapter.c # 涂鸦 BLE 协议适配层
│           ├── tuya_dp_defs.h     # DP 数据点定义 (14 个)
│           └── tuya_dp_handler.c   # DP 收发处理
│
├── camera-module/                 # 磁吸摄像模块规范
│   ├── pogo_pin_spec.md           # Pogo Pin 引脚定义与磁吸接口规范
│   ├── hardware/
│   │   ├── bom/
│   │   │   └── camera_module_bom.csv  # 物料清单 (BOM)
│   │   └── schematic/
│   │       └── README.md         # 原理图说明
│   └── mechanical/
│       └── README.md              # 机械结构说明
│
├── bag-terminal/                  # 挎包终端 Python 服务 (CM4 Linux)
│   ├── Dockerfile                 # 容器镜像构建
│   ├── docker-compose.yml         # Docker Compose 编排 (host 网络 / privileged)
│   ├── requirements.txt           # Python 依赖
│   ├── .env.example               # 环境变量模板
│   ├── config/
│   │   ├── config.yaml            # 默认配置 (BLE/AI/RAG/存储/充电/网络)
│   │   └── tuya_device_config.json # 涂鸦设备三元组配置
│   ├── systemd/
│   │   └── bag-terminal.service   # systemd 服务单元
│   ├── scripts/
│   │   ├── install.sh             # 一键安装脚本
│   │   ├── download_models.sh     # AI 模型下载脚本
│   │   ├── setup_ble.sh           # BlueZ/HCI 配置脚本
│   │   └── setup_modem.sh         # 5G 模组 AT 命令配置脚本
│   └── app/                       # FastAPI 应用
│       ├── __init__.py
│       ├── config.py              # Pydantic Settings 配置加载
│       ├── main.py                # FastAPI 入口 (lifespan / 路由注册)
│       ├── ai/                    # AI 推理模块
│       │   ├── model_manager.py   # 模型生命周期管理
│       │   ├── yolo_inference.py   # YOLOv8n ONNX 推理 (8 类铁路缺陷)
│       │   ├── image_processor.py # 图像预处理 (letterbox/归一化)
│       │   └── detection_postprocessor.py  # NMS / 坐标映射
│       ├── ble/                   # BLE 网桥模块
│       │   ├── ble_bridge.py      # Bleak Central 多设备管理 (582 行)
│       │   ├── nus_client.py      # 单设备 NUS 客户端
│       │   ├── message_protocol.py # 帧编解码 / 分包重组
│       │   └── __init__.py
│       ├── rag/                   # 本地 RAG 引擎
│       │   ├── local_rag.py       # RAG 主引擎 (检索+生成, 410 行)
│       │   ├── vector_store.py    # Qdrant 向量存储
│       │   ├── embedding_model.py # BGE-small-zh 嵌入模型
│       │   ├── llm_inference.py   # Phi-3-mini LLM 推理 (GGUF)
│       │   └── knowledge_sync.py  # 知识库增量同步
│       ├── storage/               # 存储模块
│       │   ├── local_db.py        # SQLite 本地数据库 (离线缓存)
│       │   ├── schema.sql         # 数据库 Schema (7 张表)
│       │   ├── file_storage.py    # 照片/视频文件存储
│       │   └── cache_manager.py   # 离线缓存管理
│       ├── communication/         # 外部通信模块
│       │   ├── saas_api_client.py # SaaS REST API 客户端
│       │   ├── tuya_gateway.py     # 涂鸦 IoT MQTT 网关
│       │   └── mqtt_client.py     # MQTT 通用客户端
│       ├── network/               # 网络模块
│       │   ├── modem_manager.py   # 5G 模组管理 (AT 命令)
│       │   └── network_monitor.py # 网络状态监控
│       ├── charging/
│       │   └── charge_manager.py   # BQ25895 充电管理 (I2C)
│       ├── ota/
│       │   └── firmware_manager.py # 眼镜固件 OTA 升级管理
│       ├── monitoring/
│       │   ├── health_check.py     # 健康检查端点
│       │   └── metrics_collector.py # Prometheus 指标采集
│       └── models/                # 数据模型
│           ├── ble_message.py     # BLE 消息帧模型
│           ├── device_state.py    # 眼镜设备状态模型
│           └── inspection_record.py # 巡检记录模型
│
├── saas-extensions/               # SaaS 后端扩展
│   ├── 02-extensions.sql          # PostgreSQL Schema 扩展 (391 行)
│   └── endpoints/                 # FastAPI 端点
│       ├── devices.py             # 设备注册 / 绑定 / 状态查询
│       ├── ota.py                 # OTA 固件版本管理 / 分发
│       └── telemetry.py           # 遥测数据接收 / 查询
│
└── .github/workflows/             # CI/CD 流水线
    ├── glasses-firmware.yml       # ESP-IDF 固件编译验证
    ├── bag-terminal.yml            # Python 后端测试 / Docker 构建
    └── protocol-tests.yml          # 协议一致性测试
```

## 模块化设计

### 1. 基础眼镜 (单只 / 镜片显示 / 耳机)

| 组件 | 芯片/方案 | 接口 |
|------|----------|------|
| MCU | ESP32-S3 (双核 240MHz, 4MB Flash) | — |
| 显示 | GC9A01 1.54" 240x240 圆形 LCD | SPI |
| 音频输入 | INMP441 I2S 麦克风 | I2S |
| 音频输出 | MAX98357A I2S 功放 | I2S |
| 电池管理 | TP4056 USB 充电 + DW01 保护 | GPIO + ADC |
| 通信 | BLE 5.0 NimBLE (NUS 从设备) | BLE |
| IoT | 涂鸦 BLE SDK (可选) | BLE |

### 2. 磁吸摄像模块

| 组件 | 规格 |
|------|------|
| 传感器 | OV2640 (2MP, 1600x1200, DVP) |
| 接口 | 5-Pin Pogo Pin (磁吸热插拔) |
| 总线 | 8 位 DVP 并行 + SCCB (I2C) |
| 供电 | 3.3V (从眼镜供电) |
| 检测 | GPIO 中断 (Pogo Pin 连接状态) |

### 3. 挎包终端 (充电 / 对外基站 / 移动算力)

| 组件 | 规格 |
|------|------|
| 计算平台 | Raspberry Pi CM4 (ARM A72, 4GB RAM) |
| BLE 网桥 | Bleak (Python) — NUS Central, 最多 4 副并发 |
| AI 推理 | YOLOv8n (ONNX Runtime) — 8 类铁路缺陷检测 |
| 本地 RAG | Qdrant + BGE-small-zh + Phi-3-mini (GGUF Q4) |
| 5G 基站 | AT 命令拨号, 对外 WiFi 热点 |
| 充电管理 | BQ25895 (I2C) — 4 副眼镜并发充电 |
| 存储 | SQLite (离线缓存) + 照片/视频文件存储 |
| IoT | 涂鸦 Linux SDK (MQTT 网关) |
| 容器化 | Docker Compose (host 网络 / privileged) |

## 技术选型

| 模块 | 硬件平台 | 核心技术 |
|------|----------|---------|
| 基础眼镜 | ESP32-S3 (双核 240MHz, WiFi + BLE) | ESP-IDF v5.1 + NimBLE + LVGL |
| 磁吸摄像 | OV2640 + Pogo Pin | 8 位 DVP 并行总线 + SCCB |
| 挎包终端 | Raspberry Pi CM4 (ARM Linux) | Python + FastAPI + Bleak + ONNX Runtime |
| 云端 SaaS | Docker / K8s | FastAPI + PostgreSQL + MinIO + Milvus + Dify |

## 通信协议

### BLE NUS 帧格式

```
SYNC(0xAA 0x55) | MSG_TYPE(1B) | SEQ(2B BE) | LEN(2B BE) | DATA(NB) | CRC16(2B LE)
```

详见 [`shared/protocols/ble_nus_protocol.md`](shared/protocols/ble_nus_protocol.md)。

### 消息类型

8 个范围段共 35 个枚举值 (0x00-0x8F)，C 与 Python 实现一一对应：

| 范围段 | 类别 | 枚举数 |
|--------|------|--------|
| 0x00-0x0F | 通用控制 (握手/心跳/断开/ACK) | 5 |
| 0x10-0x1F | 设备状态 (状态上报/配置/低电量) | 5 |
| 0x20-0x2F | 摄像头 (接入/断开/图像帧/参数) | 4 |
| 0x30-0x3F | 音频 (录音开始/结束/数据/播放) | 4 |
| 0x40-0x4F | RAG (查询请求/结果/上下文) | 3 |
| 0x50-0x5F | 巡检 (记录开始/照片/结束/位置) | 4 |
| 0x60-0x6F | 告警 (缺陷告警/紧急通知) | 2 |
| 0x70-0x7F | OTA (通知/请求/数据/完成) | 4 |
| 0x80-0x8F | 显示 (页面切换/亮度/导航/清屏) | 4 |

### 涂鸦 DP 数据点

14 个数据点 (10 上报 + 4 下行)，详见 [`shared/protocols/tuya_dp_schema.json`](shared/protocols/tuya_dp_schema.json)。

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

### 运行挎包终端

```bash
cd bag-terminal
cp .env.example .env  # 编辑环境变量
./scripts/download_models.sh  # 下载 AI 模型
docker compose up -d  # 启动服务
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

## 许可证

Apache License 2.0 — 详见 [`LICENSE`](LICENSE)。
