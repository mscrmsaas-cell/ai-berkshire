"""挎包终端充电管理模块。

负责通过 I2C 与 BQ25895 充电 IC 通信：
- 充电电流/电压监控
- 电池温度保护
- 充电状态管理
"""

from app.charging.charge_manager import ChargeManager, BatteryStatus, ChargeConfig

__all__ = [
    "ChargeManager",
    "BatteryStatus",
    "ChargeConfig",
]
