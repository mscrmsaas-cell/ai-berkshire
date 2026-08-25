"""充电管理器 — BQ25895 I2C 充电 IC。

通过 I2C 总线与 TI BQ25895 充电管理芯片通信：
- 充电电流/电压设置与监控
- 电池电压、温度、电量读取
- 温度保护（NTC 热敏电阻）
- 充电状态（预充/恒流/恒压/充满/故障）
- 输入电流限制（USB/适配器）

BQ25895 I2C 地址：0x6B（7-bit）
关键寄存器：
  0x00  — INPUT_SRC     输入源/输入电流限制
  0x03  — CHRG_CUR      充电电流
  0x04  — PRE_CHRG_TERM 预充电流/终止电流
  0x05  — CHRG_VOLT     充电电压限制
  0x06  — MISC          杂项控制
  0x07  — SYSTEM_VSYS   最小系统电压
  0x0A  — CHRG_STAT     充电状态
  0x0B  — FAULT         故障
  0x0C  — VINDPM_STAT   输入电压状态
  0x0E  — BAT_VOLT      电池电压 ADC（28-bit，每 LSB 1.648mV）

Linux I2C 访问：/dev/i2c-{bus}
  使用 smbus2 或 os.open + ioctl(I2C_SLAVE)
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import structlog

logger = structlog.get_logger(__name__)

# I2C 常量
I2C_SLAVE = 0x0703
BQ25895_I2C_ADDR = 0x6B  # 7-bit address

# BQ25895 寄存器地址
REG_INPUT_SRC = 0x00
REG_CHRG_CUR = 0x03
REG_PRE_TERM = 0x04
REG_CHRG_VOLT = 0x05
REG_MISC = 0x06
REG_SYSTEM_VSYS = 0x07
REG_CHRG_STAT = 0x0A
REG_FAULT = 0x0B
REG_VINDPM_STAT = 0x0C
REG_BAT_VOLT = 0x0E
REG_TEMP = 0x10       # 温度（通过 ADC）
REG_CHRG_TIMER = 0x12

# 电压/电流换算常数
BAT_VOLT_LSB_MV = 1.648       # 电池电压 ADC 每位 1.648 mV
BAT_VOLT_BASE_MV = 2304        # 基准偏移 2304 mV
CHRG_CUR_LSB_MA = 64          # 充电电流每位 64 mA
CHRG_CUR_BASE_MA = 0
CHRG_VOLT_LSB_MV = 16          # 充电电压每位 16 mV
CHRG_VOLT_BASE_MV = 3840       # 基准 3840 mV
INPUT_CUR_LSB_MA = 50          # 输入电流每位 50 mA
INPUT_CUR_BASE_MA = 100         # 基准 100 mA


class ChargeStatus(Enum):
    """充电状态。"""

    NOT_CHARGING = "not_charging"
    PRE_CHARGE = "pre_charge"
    FAST_CHARGE = "fast_charge"
    CHARGE_DONE = "charge_done"
    FAULT = "fault"
    UNKNOWN = "unknown"


class FaultType(Enum):
    """故障类型。"""

    NONE = "none"
    INPUT_OVP = "input_ovp"
    INPUT_UVP = "input_uvp"
    THERMAL_SHUTDOWN = "thermal_shutdown"
    THERMAL_REGULATION = "thermal_regulation"
    TIMER_EXPIRED = "timer_expired"
    BAT_OVP = "battery_ovp"
    BAT_MISSING = "battery_missing"


@dataclass
class BatteryStatus:
    """电池状态快照。"""

    voltage_mv: int = 0          # 电池电压 mV
    charge_current_ma: int = 0   # 充电电流 mA
    input_current_ma: int = 0    # 输入电流 mA
    temperature_c: float = 0.0  # 电池温度 ℃
    charge_status: ChargeStatus = ChargeStatus.UNKNOWN
    fault: FaultType = FaultType.NONE
    is_charging: bool = False
    is_power_good: bool = False
    battery_level: int = 0      # 电量百分比 0-100
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "voltage_mv": self.voltage_mv,
            "voltage_v": round(self.voltage_mv / 1000, 3),
            "charge_current_ma": self.charge_current_ma,
            "input_current_ma": self.input_current_ma,
            "temperature_c": round(self.temperature_c, 1),
            "charge_status": self.charge_status.value,
            "fault": self.fault.value,
            "is_charging": self.is_charging,
            "is_power_good": self.is_power_good,
            "battery_level": self.battery_level,
        }


@dataclass
class ChargeConfig:
    """充电管理配置。"""

    i2c_bus: int = 1                     # I2C 总线号
    i2c_device: str = "/dev/i2c-1"
    # 充电参数
    charge_voltage_mv: int = 4208        # 充电截止电压（4.208V 锂电池标准）
    charge_current_ma: int = 2000       # 充电电流 2A
    input_current_ma: int = 3000        # 输入电流限制 3A
    pre_charge_current_ma: int = 256    # 预充电电流
    termination_current_ma: int = 128   # 终止电流
    # 温度保护
    temp_min_c: float = 0.0             # 最低温度 ℃
    temp_max_c: float = 45.0            # 最高温度 ℃
    temp_derate_c: float = 40.0         # 降额温度
    temp_shutdown_c: float = 60.0       # 关断温度
    # 监控
    monitor_interval: float = 10.0      # 监控间隔（秒）


class ChargeManager:
    """BQ25895 充电管理器。

    使用示例::

        manager = ChargeManager(config)
        await manager.init()
        status = await manager.get_battery_status()
        print(f"电量={status.battery_level}% 充电中={status.is_charging}")
        await manager.start_monitoring()  # 后台监控
        await manager.stop_monitoring()
    """

    def __init__(self, config: ChargeConfig | None = None) -> None:
        self._config = config or ChargeConfig()
        self._fd: int | None = None
        self._monitor_task: asyncio.Task | None = None
        self._running = False
        self._last_status: BatteryStatus | None = None

        # 外部回调
        self.on_low_battery: Callable[[BatteryStatus], None] | None = None
        self.on_over_temperature: Callable[[BatteryStatus], None] | None = None
        self.on_fault: Callable[[BatteryStatus], None] | None = None

    # ── I2C 底层 ──────────────────────────────────────────────────

    async def init(self) -> bool:
        """初始化 I2C 连接并配置 BQ25895。"""
        try:
            self._fd = os.open(self._config.i2c_device, os.O_RDWR)
            fcntl.ioctl(self._fd, I2C_SLAVE, BQ25895_I2C_ADDR)
            logger.info(
                "charge_ic_initialized",
                bus=self._config.i2c_bus,
                addr=hex(BQ25895_I2C_ADDR),
            )
            # 配置充电参数
            await self._configure_registers()
            return True
        except PermissionError:
            logger.error("i2c_permission_denied", device=self._config.i2c_device)
            return False
        except FileNotFoundError:
            logger.error("i2c_device_not_found", device=self._config.i2c_device)
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("charge_ic_init_error", error=str(exc))
            return False

    async def close(self) -> None:
        """关闭 I2C 连接。"""
        await self.stop_monitoring()
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        logger.info("charge_ic_closed")

    def _read_register(self, reg: int) -> int:
        """读取单个寄存器。"""
        if self._fd is None:
            raise RuntimeError("I2C not initialized")
        os.write(self._fd, bytes([reg]))
        # 小延迟确保数据就绪
        data = os.read(self._fd, 1)
        return data[0] if data else 0

    def _write_register(self, reg: int, value: int) -> None:
        """写入单个寄存器。"""
        if self._fd is None:
            raise RuntimeError("I2C not initialized")
        os.write(self._fd, bytes([reg, value & 0xFF]))

    async def _read_register_async(self, reg: int) -> int:
        """异步读取寄存器（线程池执行）。"""
        return await asyncio.get_event_loop().run_in_executor(
            None, self._read_register, reg
        )

    async def _write_register_async(self, reg: int, value: int) -> None:
        """异步写入寄存器。"""
        await asyncio.get_event_loop().run_in_executor(
            None, self._write_register, reg, value
        )

    # ── 寄存器配置 ────────────────────────────────────────────────

    async def _configure_registers(self) -> None:
        """配置 BQ25895 充电参数寄存器。"""
        # REG_INPUT_SRC (0x00): 输入电流限制
        # bit[5:3] = ILIM, bit[2:0] = input voltage DPM
        input_limit_reg = (self._config.input_current_ma - INPUT_CUR_BASE_MA) // INPUT_CUR_LSB_MA
        input_limit_reg = max(0, min(0x3F, input_limit_reg))
        # EN_HIZ=0, EN_ILIM=1
        reg_val = 0x80 | (input_limit_reg & 0x3F)  # bit7=EN_HIZ(0), bit6=EN_ILIM(1)... actually
        # BQ25895 REG00: bit7=EN_HIZ, bit6=EN_ILIM, bit[5:3]=IILIM
        input_ilim = ((self._config.input_current_ma - INPUT_CUR_BASE_MA) // INPUT_CUR_LSB_MA) & 0x1F
        reg_val = (1 << 6) | (input_ilim << 3) | 0x07  # EN_ILIM=1, VINDPM=3.5V offset
        await self._write_register_async(REG_INPUT_SRC, reg_val)

        # REG_CHRG_CUR (0x03): 充电电流
        cur_reg = (self._config.charge_current_ma - CHRG_CUR_BASE_MA) // CHRG_CUR_LSB_MA
        cur_reg = max(0, min(0x7F, cur_reg))
        # bit7=EN_PFM, bit[6:2]=ICHG
        reg_val = (cur_reg & 0x7F)
        await self._write_register_async(REG_CHRG_CUR, reg_val)

        # REG_CHRG_VOLT (0x05): 充电电压限制
        volt_reg = (self._config.charge_voltage_mv - CHRG_VOLT_BASE_MV) // CHRG_VOLT_LSB_MV
        volt_reg = max(0, min(0x1F, volt_reg))
        # bit7=EN_VDPM, bit[6:2]=VREG, bit1=TREG(45C)=1, bit0=TOPOFF_TIMER
        reg_val = (1 << 7) | ((volt_reg & 0x1F) << 2) | 0x02
        await self._write_register_async(REG_CHRG_VOLT, reg_val)

        # REG_PRE_TERM (0x04): 预充电流 + 终止电流
        # bit[7:4]=IPRECHG, bit[3:0]=ITERM
        prechg = (self._config.pre_charge_current_ma - 128) // 64
        prechg = max(0, min(0x0F, prechg))
        term = (self._config.termination_current_ma - 128) // 64
        term = max(0, min(0x0F, term))
        reg_val = ((prechg & 0x0F) << 4) | (term & 0x0F)
        await self._write_register_async(REG_PRE_TERM, reg_val)

        # REG_MISC (0x06): 杂项 - 使能 BATFET, 看门狗
        # bit5=WATCHDOG, bit4=EN_VDPM_BAT_TRACK
        reg_val = 0x8B  # 安全默认值
        await self._write_register_async(REG_MISC, reg_val)

        # REG_SYSTEM_VSYS (0x07): 最小系统电压
        # bit[2:1] = VSYS_MIN (default 3.5V=01)
        reg_val = 0x0B  # 默认值
        await self._write_register_async(REG_SYSTEM_VSYS, reg_val)

        logger.info("charge_registers_configured")

    # ── 状态读取 ──────────────────────────────────────────────────

    async def get_battery_status(self) -> BatteryStatus:
        """读取当前电池/充电状态。"""
        status = BatteryStatus()

        try:
            # 电池电压
            bat_reg = await self._read_register_async(REG_BAT_VOLT)
            status.voltage_mv = BAT_VOLT_BASE_MV + (bat_reg & 0x7F) * int(BAT_VOLT_LSB_MV)

            # 充电状态 (0x0A)
            chrg_stat = await self._read_register_async(REG_CHRG_STAT)
            # bit[7:6]=CHRG_STAT, bit4=PG_STAT, bit3=SD, bit2=VSYS_STAT, bit[1:0]=THERM_STAT
            chrg_bits = (chrg_stat >> 6) & 0x03
            status.is_power_good = bool((chrg_stat >> 4) & 0x01)
            status.is_charging = chrg_bits != 0 and chrg_bits != 3
            status.charge_status = {
                0: ChargeStatus.NOT_CHARGING,
                1: ChargeStatus.PRE_CHARGE,
                2: ChargeStatus.FAST_CHARGE,
                3: ChargeStatus.CHARGE_DONE,
            }.get(chrg_bits, ChargeStatus.UNKNOWN)

            # 充电电流（通过 REG01-02 的 ADC）
            cur_reg = await self._read_register_async(0x01)
            status.charge_current_ma = cur_reg * 50  # 简化估算

            # 输入电流
            in_cur_reg = await self._read_register_async(0x02)
            status.input_current_ma = (in_cur_reg & 0x3F) * INPUT_CUR_LSB_MA

            # 温度（通过 ADC 寄存器，简化处理）
            temp_reg = await self._read_register_async(REG_TEMP)
            status.temperature_c = self._raw_to_temp(temp_reg)

            # 故障状态
            fault_reg = await self._read_register_async(REG_FAULT)
            status.fault = self._parse_fault(fault_reg)

            # 电量百分比估算
            status.battery_level = self._voltage_to_percent(status.voltage_mv)

            # 温度保护检查
            if status.temperature_c >= self._config.temp_shutdown_c:
                logger.error("thermal_shutdown_triggered", temp=status.temperature_c)
                if self.on_over_temperature:
                    self.on_over_temperature(status)
            elif status.temperature_c < self._config.temp_min_c:
                logger.error("battery_too_cold", temp=status.temperature_c)

            if status.fault != FaultType.NONE:
                logger.warning("charge_fault", fault=status.fault.value)
                if self.on_fault:
                    self.on_fault(status)

            if status.battery_level <= 20 and not status.is_charging:
                logger.warning("low_battery", level=status.battery_level)
                if self.on_low_battery:
                    self.on_low_battery(status)

        except Exception as exc:  # noqa: BLE001
            logger.error("battery_status_read_error", error=str(exc))

        self._last_status = status
        return status

    @staticmethod
    def _raw_to_temp(raw: int) -> float:
        """ADC 原始值转温度（简化估算）。

        BQ25895 温度 ADC 通过 TS 引脚外部 NTC 测量。
        简化映射：25℃ → 128, 每位约 0.46℃
        """
        # 实际温度换算依赖 NTC 分压网络，这里使用简化线性映射
        if raw == 0:
            return 25.0
        return round(25.0 + (raw - 128) * 0.46, 1)

    @staticmethod
    def _voltage_to_percent(voltage_mv: int) -> int:
        """电池电压 → 电量百分比（锂离子电池放电曲线近似）。"""
        # 4.2V = 100%, 3.0V = 0%
        if voltage_mv >= 4200:
            return 100
        if voltage_mv <= 3000:
            return 0
        # 分段近似（非线性）
        if voltage_mv >= 4100:
            return 90 + (voltage_mv - 4100) // 10
        if voltage_mv >= 3800:
            return 50 + (voltage_mv - 3800) // 6
        if voltage_mv >= 3500:
            return 15 + (voltage_mv - 3500) // 6
        return (voltage_mv - 3000) // 16

    @staticmethod
    def _parse_fault(fault_reg: int) -> FaultType:
        """解析故障寄存器。"""
        # bit7=WATCHDOG_FAULT, bit6=BOOST_FAULT, bit5=CHRG_FAULT, bit4=BAT_FAULT
        # bit3=NTC_FAULT[1], bit[2:0]=NTC_FAULT
        if fault_reg & 0x80:
            return FaultType.TIMER_EXPIRED
        if fault_reg & 0x20:
            # CHRG_FAULT bits
            chrg_fault = (fault_reg >> 4) & 0x03
            if chrg_fault == 1:
                return FaultType.TIMER_EXPIRED
            if chrg_fault == 2:
                return FaultType.THERMAL_SHUTDOWN
            if chrg_fault == 3:
                return FaultType.THERMAL_REGULATION
        if fault_reg & 0x08:
            return FaultType.BAT_OVP
        ntc = fault_reg & 0x07
        if ntc != 0:
            return FaultType.THERMAL_REGULATION
        return FaultType.NONE

    # ── 后台监控 ──────────────────────────────────────────────────

    async def start_monitoring(self) -> None:
        """启动后台监控循环。"""
        if self._running:
            return
        self._running = True
        self._monitor_task = asyncio.create_task(
            self._monitor_loop(), name="charge-monitor"
        )
        logger.info("charge_monitoring_started", interval=self._config.monitor_interval)

    async def stop_monitoring(self) -> None:
        """停止监控。"""
        self._running = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

    async def _monitor_loop(self) -> None:
        """充电状态监控循环。"""
        while self._running:
            try:
                status = await self.get_battery_status()
                # 温度保护
                if status.temperature_c >= self._config.temp_shutdown_c:
                    logger.critical(
                        "thermal_shutdown",
                        temp=status.temperature_c,
                        action="disabling_charge"
                    )
                    # 通过 I2C 禁用充电
                    await self._disable_charging()
                elif (
                    status.temperature_c >= self._config.temp_derate_c
                    and status.temperature_c < self._config.temp_max_c
                ):
                    logger.warning("thermal_derating", temp=status.temperature_c)
                    # 降低充电电流
                    await self._derate_charging()

                logger.debug(
                    "battery_monitor",
                    voltage=status.voltage_mv,
                    level=status.battery_level,
                    charging=status.is_charging,
                    temp=status.temperature_c,
                )
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.error("charge_monitor_error", error=str(exc))
            await asyncio.sleep(self._config.monitor_interval)

    async def _disable_charging(self) -> None:
        """紧急禁用充电（温度过高）。"""
        # REG_CHRG_VOLT bit7=EN_VDPM(0=disable charging)... 
        # 实际通过 REG01 CTRL bit4=EN_CHRG
        reg_val = await self._read_register_async(0x01)
        await self._write_register_async(0x01, reg_val & ~0x10)  # 清除 EN_CHRG

    async def _derate_charging(self) -> None:
        """降额充电电流。"""
        cur_reg = await self._read_register_async(REG_CHRG_CUR)
        # 降低到一半
        new_cur = max(0, (cur_reg & 0x7F) // 2)
        await self._write_register_async(REG_CHRG_CUR, new_cur)

    # ── 手动控制 ──────────────────────────────────────────────────

    async def set_charge_current(self, current_ma: int) -> bool:
        """手动设置充电电流。"""
        cur_reg = (current_ma - CHRG_CUR_BASE_MA) // CHRG_CUR_LSB_MA
        cur_reg = max(0, min(0x7F, cur_reg))
        await self._write_register_async(REG_CHRG_CUR, cur_reg)
        logger.info("charge_current_set", current_ma=current_ma)
        return True

    async def set_charge_voltage(self, voltage_mv: int) -> bool:
        """手动设置充电截止电压。"""
        volt_reg = (voltage_mv - CHRG_VOLT_BASE_MV) // CHRG_VOLT_LSB_MV
        volt_reg = max(0, min(0x1F, volt_reg))
        old = await self._read_register_async(REG_CHRG_VOLT)
        new_val = (old & 0x83) | ((volt_reg & 0x1F) << 2)
        await self._write_register_async(REG_CHRG_VOLT, new_val)
        logger.info("charge_voltage_set", voltage_mv=voltage_mv)
        return True

    async def enable_charging(self) -> bool:
        """启用充电。"""
        reg_val = await self._read_register_async(0x01)
        await self._write_register_async(0x01, reg_val | 0x10)
        logger.info("charging_enabled")
        return True

    async def disable_charging(self) -> bool:
        """禁用充电。"""
        reg_val = await self._read_register_async(0x01)
        await self._write_register_async(0x01, reg_val & ~0x10)
        logger.info("charging_disabled")
        return True

    # ── 状态查询 ──────────────────────────────────────────────────

    @property
    def last_status(self) -> BatteryStatus | None:
        return self._last_status

    def is_initialized(self) -> bool:
        return self._fd is not None
