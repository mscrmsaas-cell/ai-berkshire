# 磁吸摄像模块 — 原理图设计说明

> 文件路径: `camera-module/hardware/schematic/README.md`
> 版本: 1.0.0
> 关联文件: `camera-module/pogo_pin_spec.md`, `glasses-firmware/main/bsp/bsp_ov2640.c`

---

## 1. 设计概述

磁吸摄像模块原理图涵盖以下功能模块:

1. **OV2640 传感器接口** — DVP 并行总线 + SCCB 配置 + XCLK 时钟
2. **Pogo Pin 连接器** — 10 脚控制信号接口
3. **FPC 连接器** — 14 脚高速 DVP 数据接口
4. **电源系统** — 3.3V LDO + 滤波网络
5. **ESD 保护** — TVS 二极管阵列

原理图设计工具: KiCad 7.0+ (开源 EDA)
原理图文件: `camera-module/hardware/schematic/camera_module.sch` (待创建)

---

## 2. 电源子系统

### 2.1 电源拓扑

```
                  ┌──────────────────────────────────────────┐
                  │           眼镜主板                        │
                  │                                          │
  VBUS(5V) ───────┤──┬── 100nF ── GND                        │
                  │  │                                       │
                  │  └── RT9013-3.3 ──┬── 10μF ── GND         │
                  │       (LDO)       │                       │
                  │         │         └── 100nF ── GND        │
                  │       3V3_CAM                            │
                  │         │                                 │
                  └─────────┼─────────────────────────────────┘
                            │ (Pogo Pin 引脚 2 & 4)
                  ┌─────────┼─────────────────────────────────┐
                  │         │     摄像模块 PCB                   │
                  │         │                                 │
                  │    ┌────┴────┐                            │
                  │    │ 0.1μF   │                            │
                  │    └────┬────┘                            │
                  │   AVDD DVDD DOVDD                        │
                  │    ┌────┴────┐                            │
                  │    │ OV2640  │                            │
                  │    │ Sensor  │                            │
                  │    └────┬────┘                            │
                  │       GND                                │
                  └──────────────────────────────────────────┘
```

### 2.2 电源要求

| 电源轨 | 电压 | 电流 | 纹波 | 用途 |
|--------|------|------|------|------|
| VCC_3V3 | 3.3V ±3% | 500 mA (峰值) | ≤ 50 mV p-p | OV2640 AVDD/DVDD/DOVDD |

### 2.3 滤波网络

每个 VCC 引脚处放置:
- 0.1μF X7R 0603 (高频去耦)
- 10μF X7R 0805 (大容量储能)
- 铁氧体磁珠 600Ω@100MHz (隔离数字噪声)

OV2640 AVDD (模拟核) 独立走线，串联 10μH 电感 + 0.1μF 陶瓷电容组成
低通滤波器，截止频率约 160 kHz。

---

## 3. DVP 并行总线

### 3.1 信号连接

```
  OV2640                    ESP32-S3
  ┌────────┐                ┌──────────┐
  │ D0  ───┤───────────────┤── GPIO4   │
  │ D1  ───┤───────────────┤── GPIO5   │
  │ D2  ───┤───────────────┤── GPIO6   │
  │ D3  ───┤───────────────┤── GPIO7   │
  │ D4  ───┤───────────────┤── GPIO14  │
  │ D5  ───┤───────────────┤── GPIO15  │
  │ D6  ───┤───────────────┤── GPIO16  │
  │ D7  ───┤───────────────┤── GPIO17  │
  │ VSYNC──┤───────────────┤── GPIO18  │
  │ HREF ──┤───────────────┤── GPIO12  │
  │ PCLK ──┤───────────────┤── GPIO11  │
  │        │               │           │
  │ XCLK ──┤───────────────┤── GPIO10  │ (LEDC 输出 20MHz)
  └────────┘               └──────────┘
       ↑
   FPC 0.5mm pitch 14-pin 排线
```

### 3.2 PCB 布线要求

| 参数 | 要求 |
|------|------|
| D0-D7 走线长度匹配 | 偏差 ≤ 5 mm |
| 走线阻抗 | 50Ω 单端 |
| D0-D7 间距 | ≥ 3W (3倍线宽) |
| PCLK 与 D[7:0] 间距 | ≥ 5W (减少串扰) |
| 过孔数量 | 每条信号线 ≤ 2 个 |

### 3.3 终端匹配

DVP 并行总线在 ESP32-S3 侧不需要外部终端电阻 (ESP32-S3 输入电容
约 5 pF, OV2640 驱动能力足够)。如果信号质量不佳 (振铃/过冲),
可在 PCLK 线上串联 22Ω 电阻做源端匹配。

---

## 4. SCCB 配置总线

### 4.1 电路

```
  VCC_3V3
    │
    ├── 4.7kΩ ── SCL ── Pogo Pin 5 ── OV2640 SIOC
    │
    ├── 4.7kΩ ── SDA ── Pogo Pin 6 ── OV2640 SIOD
    │
    └── GND
```

### 4.2 设计要点

- **上拉电阻**: 4.7kΩ × 2 (在眼镜侧 Pogo Pin 附近放置)
- **总线电容**: ≤ 400 pF (含 Pogo Pin 接触电容 + FPC 寄生)
- **工作频率**: 400 kHz (I2C Fast Mode)
- **ESD 保护**: SCL/SDA 各加一个 TVS (ESDA6V8-1U2)
- **OV2640 地址**: 0x30 (7-bit), 写命令 0x60, 读命令 0x61

---

## 5. 热插拔检测电路

```
  VCC_3V3
    │
    ├── 10kΩ ── DETECT ── Pogo Pin 7 ── GND (连接时)
    │
    └── ESP32-S3 GPIO21 (内部上拉 + 外部 10kΩ)
```

### 5.1 工作原理

- **未连接**: DETECT 通过 10kΩ 上拉电阻为高电平 (3.3V)
- **连接时**: Pogo Pin 引脚 7 导通至 GND, DETECT 拉低 (0V)
- **ESP32-S3 中断**: 配置 GPIO21 为双边沿中断
  - 下降沿 → BSP_POGO_EVENT_ATTACHED
  - 上升沿 → BSP_POGO_EVENT_DETACHED
- **软件消抖**: 50 ms 延迟后确认状态变化

### 5.2 ESD 保护

DETECT 信号在眼镜侧增加 TVS 二极管 (ESDA6V8-1U2) 防止热插拔时
的静电放电损坏 ESP32-S3 GPIO。

---

## 6. 复位与断电控制

### 6.1 RST_N (引脚 8)

```
  ESP32-S3 GPIO48 ── Pogo Pin 8 ── OV2640 RESET (低有效)
  (GPIO48 默认高电平输出; 初始化时拉低 10ms 再释放)
```

### 6.2 PWDN (引脚 10)

```
  ESP32-S3 GPIO47 ── Pogo Pin 10 ── OV2640 PWDN (高=断电)
  (GPIO47 默认低电平; 深度睡眠时拉高)
```

---

## 7. XCLK 时钟电路

```
  ESP32-S3 LEDC Channel 1 ── GPIO10 ── Pogo Pin 9 ── OV2640 XCLK
```

### 7.1 时钟参数

| 参数 | 值 |
|------|-----|
| 频率 | 20 MHz (可通过 menuconfig 调整 10-20 MHz) |
| 占空比 | 50% |
| 驱动方式 | ESP32-S3 LEDC (LEDC_TIMER_1 / LEDC_CHANNEL_1) |
| 信号质量 | 在 OV2640 侧测量: 上升/下降时间 ≤ 5 ns |

### 7.2 注意事项

XCLK 与 LCD 背光 PWM 使用不同的 LEDC 定时器和通道, 避免频率冲突:
- LCD 背光: LEDC_TIMER_0 / LEDC_CHANNEL_0 (1 kHz)
- 摄像头 XCLK: LEDC_TIMER_1 / LEDC_CHANNEL_1 (20 MHz)

---

## 8. ESD 保护布局

| 信号 | TVS 型号 | 位置 |
|------|----------|------|
| SCCB_SCL | ESDA6V8-1U2 | 眼镜侧 Pogo Pin 附近 |
| SCCB_SDA | ESDA6V8-1U2 | 眼镜侧 Pogo Pin 附近 |
| DETECT | ESDA6V8-1U2 | 眼镜侧 Pogo Pin 附近 |
| XCLK | ESDA6V8-1U2 | 眼镜侧 Pogo Pin 附近 |
| RST_N | ESDA6V8-1U2 | 眼镜侧 Pogo Pin 附近 |
| DVP_D0-D7 | 不需要 | FPC 短距离无需 ESD 保护 |
| VSYNC/HREF/PCLK | 不需要 | 同上 |

---

## 9. 电源滤波布局

```
  VCC_3V3 (Pogo Pin 2)                VCC_3V3 (Pogo Pin 4)
    │                                    │
    ├── 10μF ── GND                      ├── 10μF ── GND
    ├── 0.1μF ── GND                     ├── 0.1μF ── GND
    │                                    │
    └── 铁氧体磁珠 ── AVDD (OV2640)      └── 直接连 DVDD/DOVDD
         ├── 1μH 电感                         ├── 0.1μF ── GND
         ├── 0.1μF ── GND                     └── OV2640 DVDD
         └── OV2640 AVDD
```

---

## 10. 设计检查清单

- [ ] VCC 轨: 3.3V LDO 输出纹波 ≤ 50 mV p-p
- [ ] AVDD: 独立走线 + LC 低通滤波 (截止 ≤ 200 kHz)
- [ ] SCCB: 4.7kΩ 上拉, 总线电容 ≤ 400 pF
- [ ] DVP: D0-D7 等长 (偏差 ≤ 5 mm), 阻抗 50Ω
- [ ] PCLK: 与数据线间距 ≥ 5W
- [ ] XCLK: LEDC_TIMER_1 (不与 LCD 背光冲突)
- [ ] DETECT: 10kΩ 上拉 + ESP32-S3 内部上拉 + 50ms 软件消抖
- [ ] ESD: SCCB/DETECT/XCLK 各加 TVS
- [ ] GND: 信号地与电源地单点汇接
- [ ] PCB: 4 层板 (Top / GND / VCC / Bottom), DVP 区域完整地平面
