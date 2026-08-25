"""挎包终端网络模块。

负责：
- 5G 模块管理（Quectel RM500Q-GL AT 命令） (modem_manager)
- 网络连通性监控与自动重连 (network_monitor)
"""

from app.network.modem_manager import ModemManager
from app.network.network_monitor import NetworkMonitor

__all__ = [
    "ModemManager",
    "NetworkMonitor",
]
