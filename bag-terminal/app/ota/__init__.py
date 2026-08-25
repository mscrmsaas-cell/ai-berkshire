"""挎包终端 OTA 固件管理模块。

负责眼镜固件的空中升级全流程：
- 下载固件 → BLE NUS 分块传输 → 眼镜写入 → 分区切换 → 重启 → 验证
"""

from app.ota.firmware_manager import FirmwareManager, OtaConfig

__all__ = [
    "FirmwareManager",
    "OtaConfig",
]
