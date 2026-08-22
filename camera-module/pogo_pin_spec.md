# 磁吸摄像模块 Pogo Pin 接口规范

> 文件路径: `camera-module/pogo_pin_spec.md`
> 版本: 1.0.0
> 最后更新: 2024-01-01
> 关联文件: `glasses-firmware/main/bsp/bsp_pogo_pin.c`, `glasses-firmware/main/bsp/bsp_ov2640.c`

---

## 1. 概述

磁吸摄像模块通过 Pogo Pin (弹簧顶针) 连接器与眼镜主体实现热插拔。
连接器分为两部分:

| 连接器 | 引脚数 | 用途 | 信号速率 |
|--------|--------|------|----------|
| Pogo Pin Block A | 10 | 电源 + SCCB 配置 + 控制 + 热插拔检测 | ≤ 400 kHz (SCCB) |
| FPC 连接器 B | 14 | DVP 8 位并行数据总线 + 同步信号 | ≤ 48 MHz (PCLK) |

**设计理由**: Pogo Pin 适合低引脚数、频繁插拔场景；DVP 高速并行总线
(8 数据 + 3 同步 + 1 时钟) 需要阻抗匹配的 FPC 排线传输，不适合 Pogo Pin
弹簧接触的较高接触电阻。

---

## 2. Pogo Pin Block A — 10 脚定义

### 2.1 机械参数

| 参数 | 值 |
|------|-----|
| 连接器类型 | 磁吸式 Pogo Pin (弹簧顶针) |
| 引脚数 | 10 |
| 间距 (pitch) | 2.0 mm |
| 额定电流 | 2 A per pin |
| 接触电阻 | ≤ 30 mΩ |
| 插拔寿命 | ≥ 10,000 次 |
| 磁铁 | N35 钕铁硼, 4 颗 (对角分布) |
| 对位方式 | 磁吸 + 机械导柱 (防呆设计) |

### 2.2 引脚定义

```
          ┌──────────────────────────────────┐
          │        眼镜侧 (母座)              │
          │                                  │
   GND  ──┤ 1    2  ├── VCC_3V3
   GND  ──┤ 3    4  ├── VCC_3V3
 SCL   ──┤ 5    6  ├── SDA
 DETECT ──┤ 7    8  ├── RST_N
 XCLK  ──┤ 9   10  ├── PWDN
          │                                  │
          └──────────────────────────────────┘
                    摄像模块侧 (公座)
```

| 引脚 | 信号名 | 方向 | 电压 | 功能 | ESP32-S3 GPIO |
|------|--------|------|------|------|---------------|
| 1 | GND | — | 0V | 信号地 | — |
| 2 | VCC_3V3 | 眼镜→摄像 | 3.3V | 摄像模块主电源 | — |
| 3 | GND | — | 0V | 电源地 (大电流返回) | — |
| 4 | VCC_3V3 | 眼镜→摄像 | 3.3V | 摄像模块辅助电源 (OV2640 模组) | — |
| 5 | SCCB_SCL | 眼镜→摄像 | 3.3V | SCCB 时钟 (I2C-like, 400 kHz) | CONFIG_BSP_CAM_PIN_SIOC (默认 GPIO9) |
| 6 | SCCB_SDA | 双向 | 3.3V | SCCB 数据 | CONFIG_BSP_CAM_PIN_SIOD (默认 GPIO8) |
| 7 | DETECT | 摄像→眼镜 | 3.3V | 热插拔检测 (连接时拉低) | CONFIG_BSP_POGO_DETECT_GPIO (默认 GPIO21) |
| 8 | RST_N | 眼镜→摄像 | 3.3V | 摄像头硬件复位 (低有效) | CONFIG_BSP_CAM_PIN_RESET (默认 GPIO48) |
| 9 | XCLK | 眼镜→摄像 | 3.3V | 外部时钟输入 (20 MHz, LEDC 驱动) | CONFIG_BSP_CAM_PIN_XCLK (默认 GPIO10) |
| 10 | PWDN | 眼镜→摄像 | 3.3V | 断电控制 (高=断电) | CONFIG_BSP_CAM_PIN_PWDN (默认 GPIO47) |

### 2.3 信号说明

#### VCC_3V3 (引脚 2, 4)

摄像模块电源由眼镜主体 LDO (RT9013-3.3) 提供，最大输出 500 mA。
OV2640 典型工作电流: 60-90 mA (捕获时峰值可达 200 mA)。
两路 VCC 分别为 OV2640 模拟核 (AVDD) 和数字 I/O (DVDD/DOVDD) 供电，
避免数字噪声耦合到模拟端。

**滤波**: 每个 VCC 引脚旁并联 0.1μF + 10μF MLCC 陶瓷电容。

#### GND (引脚 1, 3)

双地线设计: 引脚 1 为信号地 (SCCB/XCLK 回流)，引脚 3 为电源地
(OV2640 大电流回流)。在 PCB 布局中两者在眼镜主板单点汇接。

#### SCCB_SCL / SCCB_SDA (引脚 5, 6)

OV2640 SCCB (Serial Camera Control Bus) 配置总线，I2C-like 协议。
- 频率: 400 kHz (Fast Mode)
- 上拉: 4.7kΩ × 2 (在眼镜侧)
- 地址: 0x30 (OV2640 默认 7-bit SCCB 地址)

#### DETECT (引脚 7)

热插拔检测引脚。连接器对准时磁铁吸合，该引脚物理导通至 GND
(低有效)。断开时由 ESP32-S3 内部上拉电阻拉高。

- 上拉: ESP32-S3 内部上拉 (约 45kΩ) + 外部 10kΩ
- 消抖: 软件 50 ms 消抖定时器 (bsp_pogo_pin.c)
- 中断: 双边沿 (下降沿=接入, 上升沿=断开)

#### RST_N (引脚 8)

OV2640 硬件复位引脚 (低有效)。眼镜在 Pogo Pin 连接后拉低 10 ms
再释放，确保 OV2640 从已知状态启动。

#### XCLK (引脚 9)

OV2640 外部主时钟输入。由 ESP32-S3 的 LEDC 通道输出 20 MHz 方波。

- 驱动: LEDC_TIMER_1 / LEDC_CHANNEL_1 (与 LCD 背光分开)
- 频率: 20 MHz
- 占空比: 50%

#### PWDN (引脚 10)

OV2640 Power-Down 控制 (高=断电)。正常工作时保持低电平。
深度睡眠时拉高以降低摄像模块功耗 (< 10 μA)。

---

## 3. FPC 连接器 B — 14 脚定义

### 3.1 机械参数

| 参数 | 值 |
|------|-----|
| 连接器类型 | FPC (柔性印制电路板) 排线连接器 |
| 引脚数 | 14 |
| 间距 (pitch) | 0.5 mm |
| 排线长度 | 30 mm (眼镜侧) + 20 mm (摄像侧) |
| 阻抗 | 50Ω 差分 / 75Ω 单端 |
| 锁定方式 | 磁吸预对位 + FPC 插拔锁定 |

### 3.2 引脚定义

```
  ┌──────────────────────────────────────────────────────┐
  │  FPC 14-pin (0.5mm pitch, same-side contact)         │
  │                                                      │
  │  1  2  3  4  5  6  7  8  9  10 11 12 13 14           │
  │  D0 D1 D2 D3 D4 D5 D6 D7 VS HR PC GN VC GN GN        │
  └──────────────────────────────────────────────────────┘
```

| 引脚 | 信号名 | 方向 | 功能 | ESP32-S3 GPIO |
|------|--------|------|------|---------------|
| 1 | DVP_D0 | 摄像→眼镜 | 数据位 0 (LSB) | CONFIG_BSP_CAM_PIN_D0 (默认 GPIO4) |
| 2 | DVP_D1 | 摄像→眼镜 | 数据位 1 | CONFIG_BSP_CAM_PIN_D1 (默认 GPIO5) |
| 3 | DVP_D2 | 摄像→眼镜 | 数据位 2 | CONFIG_BSP_CAM_PIN_D2 (默认 GPIO6) |
| 4 | DVP_D3 | 摄像→眼镜 | 数据位 3 | CONFIG_BSP_CAM_PIN_D3 (默认 GPIO7) |
| 5 | DVP_D4 | 摄像→眼镜 | 数据位 4 | CONFIG_BSP_CAM_PIN_D4 (默认 GPIO14) |
| 6 | DVP_D5 | 摄像→眼镜 | 数据位 5 | CONFIG_BSP_CAM_PIN_D5 (默认 GPIO15) |
| 7 | DVP_D6 | 摄像→眼镜 | 数据位 6 | CONFIG_BSP_CAM_PIN_D6 (默认 GPIO16) |
| 8 | DVP_D7 | 摄像→眼镜 | 数据位 7 (MSB) | CONFIG_BSP_CAM_PIN_D7 (默认 GPIO17) |
| 9 | VSYNC | 摄像→眼镜 | 帧同步 (垂直) | CONFIG_BSP_CAM_PIN_VSYNC (默认 GPIO18) |
| 10 | HREF | 摄像→眼镜 | 行有效 (水平) | CONFIG_BSP_CAM_PIN_HREF (默认 GPIO12) |
| 11 | PCLK | 摄像→眼镜 | 像素时钟 | CONFIG_BSP_CAM_PIN_PCLK (默认 GPIO11) |
| 12 | GND | — | 数字地 | — |
| 13 | VCC_3V3 | 眼镜→摄像 | DVP 驱动器电源 | — |
| 14 | GND | — | 屏蔽地 | — |

### 3.3 DVP 时序参数

| 参数 | 值 | 说明 |
|------|-----|------|
| PCLK 频率 | 20-48 MHz | 取决于 XCLK 和 OV2640 内部分频 |
| VSYNC 周期 | 33-100 ms | 一帧时间 (10-30 fps) |
| HREF 宽度 | 320 pixels × PCLK | QVGA 有效行 |
| 数据建立时间 | 5 ns (min) | D[7:0] 在 PCLK 上升沿前 |
| 数据保持时间 | 5 ns (min) | D[7:0] 在 PCLK 上升沿后 |

---

## 4. 热插拔时序

```
         ┌──────────────────────────────────────────────────────┐
         │              Pogo Pin 插入时序                         │
         │                                                      │
 DETECT──┤  hi                              ┌──lo──────────────┤
         │                                  │  (磁铁吸合)       │
 VCC─────┤        ┌──3.3V──────────────────┤                   │
         │  0V ───┘  (VCC 上电)                                  │
         │                                                      │
 RST_N───┤                      ┌──10ms──┐    ┌──hi───────────┤
         │  hi───────────────────┘  复位   └──                │
         │                                                      │
 SCCB────┤                                       ┌──SCL/SDA─── ─┤
         │  idle                                │  SCCB 配置   │
         │                                      │              │
 XCLK────┤                                       ┌──20MHz──────┤
         │  0V                                   │  XCLK 启动  │
         │                                                      │
 DVP─────┤                                                   ┌──┤ JPEG 数据
         │                                                   │  │
         └──────────────────────────────────────────────────────┘
              t0     t1        t2        t3        t4        t5
```

| 时间点 | 事件 | 软件动作 |
|--------|------|----------|
| t0 | Pogo Pin 物理连接, DETECT 拉低 | GPIO 中断触发, 启动 50ms 消抖 |
| t1 (t0 + 50ms) | 消抖完成, VCC 已上电 | bsp_pogo_pin 回调 BSP_POGO_EVENT_ATTACHED |
| t2 (t1 + 10ms) | RST_N 拉低 10ms 复位 OV2640 | bsp_camera_init() 内部处理 |
| t3 (t2 + 10ms) | RST_N 释放, XCLK 启动 | 等待 OV2640 稳定 |
| t4 (t3 + 100ms) | SCCB 配置 OV2640 寄存器 | bsp_camera_init() 完成 |
| t5 (t4 + 33ms) | 首帧 JPEG 数据就绪 | bsp_camera_capture() 可用 |

---

## 5. 断开时序

| 时间点 | 事件 | 软件动作 |
|--------|------|----------|
| t0 | Pogo Pin 物理分离, DETECT 拉高 | GPIO 上升沿中断触发 |
| t1 (t0 + 50ms) | 消抖完成 | bsp_pogo_pin 回调 BSP_POGO_EVENT_DETACHED |
| t2 | 归还帧缓冲 | bsp_camera_return_frame() |
| t3 | 释放摄像头资源 | bsp_camera_deinit() |
| t4 | 通知 BLE 对端 | MSG_CAMERA_DETACHED |

---

## 6. GPIO 引脚分配汇总

### 6.1 Pogo Pin Block A (10 脚)

| 信号 | ESP32-S3 GPIO | Kconfig 变量 | 备注 |
|------|---------------|-------------|------|
| SCCB_SCL | GPIO9 | CONFIG_BSP_CAM_PIN_SIOC | I2C-like |
| SCCB_SDA | GPIO8 | CONFIG_BSP_CAM_PIN_SIOD | I2C-like |
| DETECT | GPIO21 | CONFIG_BSP_POGO_DETECT_GPIO | 内部上拉 + 外部 10kΩ |
| RST_N | GPIO48 | CONFIG_BSP_CAM_PIN_RESET | 低有效 |
| XCLK | GPIO10 | CONFIG_BSP_CAM_PIN_XCLK | LEDC 输出 |
| PWDN | GPIO47 | CONFIG_BSP_CAM_PIN_PWDN | 高=断电 |

### 6.2 FPC 连接器 B (14 脚)

| 信号 | ESP32-S3 GPIO | Kconfig 变量 |
|------|---------------|-------------|
| DVP_D0 | GPIO4 | CONFIG_BSP_CAM_PIN_D0 |
| DVP_D1 | GPIO5 | CONFIG_BSP_CAM_PIN_D1 |
| DVP_D2 | GPIO6 | CONFIG_BSP_CAM_PIN_D2 |
| DVP_D3 | GPIO7 | CONFIG_BSP_CAM_PIN_D3 |
| DVP_D4 | GPIO14 | CONFIG_BSP_CAM_PIN_D4 |
| DVP_D5 | GPIO15 | CONFIG_BSP_CAM_PIN_D5 |
| DVP_D6 | GPIO16 | CONFIG_BSP_CAM_PIN_D6 |
| DVP_D7 | GPIO17 | CONFIG_BSP_CAM_PIN_D7 |
| VSYNC | GPIO18 | CONFIG_BSP_CAM_PIN_VSYNC |
| HREF | GPIO12 | CONFIG_BSP_CAM_PIN_HREF |
| PCLK | GPIO11 | CONFIG_BSP_CAM_PIN_PCLK |

> **注意**: GPIO 引脚可根据实际 PCB 布局在 `menuconfig` 中修改。
> 上述默认值匹配 ESP32-S3-WROOM-1 模组的可用 GPIO 范围
> (避开 Flash/PSRAM 专用引脚 GPIO26-32, GPIO33-37)。

---

## 7. ESD 保护

每个信号引脚在眼镜侧增加 TVS 二极管阵列 (如 ESDA6V8-1U2):
- 工作电压: 3.3V
- 钳位电压: ≤ 9.2V @ 8A (IEC 61000-4-2 Level 4)
- 结电容: ≤ 0.5 pF (不影响高速 DVP)

---

## 8. 引用

- OV2640 数据手册: `docs/datasheets/OV2640_DS.pdf`
- ESP32-S3 GPIO 矩阵: ESP32-S3 技术参考手册, Chapter 4
- SCCB 协议: OmniVision SCCB Specification v2.1c
- esp32-camera 组件: `.gitmodules` (git submodule)
