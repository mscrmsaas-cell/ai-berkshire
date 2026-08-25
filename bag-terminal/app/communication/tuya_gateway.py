"""涂鸦 Linux SDK 网关管理器。

通过 ctypes 封装涂鸦 Linux C SDK 共享库（libtuya_iot_gateway.so），
实现：
- 网关初始化与登录
- 子设备（眼镜）注册与解绑
- DP 数据点代理（上行转发 / 下行分发）
- OTA 升级通知分发

涂鸦 Linux SDK 典型流程::

    1. tuya_gateway_init()      → 初始化 SDK
    2. tuya_gateway_login()     → 网关注册登录涂鸦云
    3. tuya_subdev_register()   → 注册子设备（每副眼镜）
    4. tuya_dp_report()         → 上行 DP 上报
    5. dp_cmd_callback          → 下行 DP 回调分发
    6. tuya_ota_notify()        → OTA 通知回调

注意：本模块设计为与 mqtt_client.py 互为补充。涂鸦 SDK 内部封装了
MQTT 通信，但 DP 的业务逻辑（代理转发到 BLE 眼镜）由本模块处理。
"""
from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import structlog

logger = structlog.get_logger(__name__)

# ── SDK 函数签名 C 声明 ─────────────────────────────────────────
# 以下 CAPI 签名基于涂鸦 Linux Gateway SDK 3.x 文档。
# 实际库名可能因版本而异，通过环境变量 TUYA_SDK_PATH 覆盖。

# 回调函数类型
DPCmdCallback = ctypes.CFUNCTYPE(
    ctypes.c_int,              # 返回值
    ctypes.c_char_p,           # device_id
    ctypes.c_uint,             # dp_id
    ctypes.c_uint,             # dp_type
    ctypes.c_char_p,           # dp_value (JSON string)
    ctypes.c_void_p,           # user_data
)

OtaNotifyCallback = ctypes.CFUNCTYPE(
    ctypes.c_int,              # 返回值
    ctypes.c_char_p,           # device_id
    ctypes.c_char_p,           # firmware_url
    ctypes.c_char_p,           # firmware_version
    ctypes.c_void_p,           # user_data
)

SubdevStatusCallback = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_char_p,           # device_id
    ctypes.c_bool,             # online
    ctypes.c_void_p,           # user_data
)


class GatewayState(Enum):
    """网关运行状态。"""

    STOPPED = "stopped"
    INITIALIZING = "initializing"
    LOGGING_IN = "logging_in"
    ONLINE = "online"
    ERROR = "error"


class TuyaDpType(Enum):
    """涂鸦 DP 数据类型。"""

    BOOL = 0      # dpType 0: bool
    VALUE = 1     # dpType 1: int
    STRING = 2    # dpType 2: string
    ENUM = 3      # dpType 3: enum
    RAW = 4       # dpType 4: raw bytes


@dataclass
class SubDeviceInfo:
    """子设备（眼镜）涂鸦注册信息。"""

    device_id: str          # 涂鸦分配的子设备 ID
    device_uuid: str        # 涂鸦设备 UUID
    device_mac: str         # BLE MAC 地址
    pid: str                # 产品 ID（涂鸦平台创建产品获得）
    version: str = "1.0.0"  # 固件版本
    name: str = ""          # 设备名称
    icon_url: str = ""      # 设备图标 URL


@dataclass
class TuyaGatewayConfig:
    """涂鸦网关配置。"""

    gateway_id: str = ""           # 网关设备 ID
    gateway_uuid: str = ""         # 网关 UUID
    gateway_key: str = ""          # 网关密钥（AuthKey）
    sdk_lib_path: str = ""         # SDK .so 路径，空则自动搜索
    storage_path: str = "/var/lib/bag-terminal/tuya"  # SDK 本地存储
    log_level: int = 4             # 0=debug,1=info,2=warn,3=error,4=fatal
    enable_mqtt: bool = True       # SDK 内部 MQTT（与 mqtt_client.py 互补）
    region: str = "cn"             # cn / us / eu


# ── DP 路由策略 ──────────────────────────────────────────────────
# 下行 DP 命令 → BLE 消息类型映射，由本模块分发到 BLE 网桥。
DP_COMMAND_ROUTING: dict[int, str] = {
    101: "remote_reboot",      # 远程重启
    102: "mode_switch",        # 模式切换
    103: "brightness_adjust",  # 亮度调节
    104: "volume_adjust",      # 音量调节
}


class TuyaGatewayManager:
    """涂鸦 Linux SDK 网关管理器。

    封装 ctypes 调用，提供 Python 层 API：

    - :meth:`start` — 初始化 SDK 并登录
    - :meth:`register_sub_device` — 注册子设备（眼镜）
    - :meth:`report_dp` — 上行 DP 上报
    - :meth:`dispatch_ota` — OTA 升级分发到子设备
    - :meth:`stop` — 停止并释放 SDK 资源

    线程安全：内部加锁保护 SDK 调用序列。
    """

    def __init__(self, config: TuyaGatewayConfig) -> None:
        self._config = config
        self._state = GatewayState.STOPPED
        self._lock = threading.RLock()
        self._lib: ctypes.CDLL | None = None
        self._user_data = ctypes.c_void_p(0)

        # 回调保持引用（ctypes 要求回调对象不被 GC 回收）
        self._dp_cmd_cb = DPCmdCallback(self._dp_cmd_callback_wrapper)
        self._ota_cb = OtaNotifyCallback(self._ota_notify_callback_wrapper)
        self._subdev_status_cb = SubdevStatusCallback(
            self._subdev_status_callback_wrapper
        )

        # 已注册子设备缓存: device_id -> SubDeviceInfo
        self._sub_devices: dict[str, SubDeviceInfo] = {}

        # 外部业务回调
        self.on_dp_downlink: Callable[[str, int, int, Any], None] | None = None
        self.on_ota_notify: Callable[[str, str, str], None] | None = None
        self.on_subdev_online: Callable[[str, bool], None] | None = None

    # ── 生命周期 ──────────────────────────────────────────────────

    def start(self) -> bool:
        """初始化 SDK 并登录涂鸦云。"""
        with self._lock:
            if self._state in (GatewayState.ONLINE, GatewayState.LOGGING_IN):
                return True
            self._set_state(GatewayState.INITIALIZING)
            try:
                self._load_library()
                self._configure_sdk()
                self._register_callbacks()
                self._set_state(GatewayState.LOGGING_IN)
                login_ret = self._lib.tuya_gateway_login(
                    self._config.gateway_id.encode("utf-8"),
                    self._config.gateway_uuid.encode("utf-8"),
                    self._config.gateway_key.encode("utf-8"),
                )
                if login_ret != 0:
                    logger.error(
                        "tuya_gateway_login_failed",
                        ret=login_ret,
                        gateway_id=self._config.gateway_id,
                    )
                    self._set_state(GatewayState.ERROR)
                    return False
                self._set_state(GatewayState.ONLINE)
                logger.info(
                    "tuya_gateway_online",
                    gateway_id=self._config.gateway_id,
                )
                return True
            except Exception as exc:  # noqa: BLE001
                logger.error("tuya_gateway_start_error", error=str(exc))
                self._set_state(GatewayState.ERROR)
                return False

    def stop(self) -> None:
        """停止 SDK 并释放资源。"""
        with self._lock:
            if self._lib is None:
                return
            try:
                self._lib.tuya_gateway_exit()
            except Exception as exc:  # noqa: BLE001
                logger.warning("tuya_gateway_exit_error", error=str(exc))
            self._set_state(GatewayState.STOPPED)
            self._lib = None
            self._sub_devices.clear()
            logger.info("tuya_gateway_stopped")

    # ── 子设备管理 ────────────────────────────────────────────────

    def register_sub_device(self, dev_info: SubDeviceInfo) -> bool:
        """注册子设备（眼镜）到涂鸦云。

        :param dev_info: 子设备信息
        :return: 是否注册成功
        """
        with self._lock:
            if self._state != GatewayState.ONLINE:
                logger.error("tuya_gateway_not_online, cannot register sub-device")
                return False
            if dev_info.device_id in self._sub_devices:
                logger.debug("sub_device_already_registered", device_id=dev_info.device_id)
                return True

            c_device_id = ctypes.c_char_p(dev_info.device_id.encode("utf-8"))
            c_uuid = ctypes.c_char_p(dev_info.device_uuid.encode("utf-8"))
            c_mac = ctypes.c_char_p(dev_info.device_mac.encode("utf-8"))
            c_pid = ctypes.c_char_p(dev_info.pid.encode("utf-8"))
            c_version = ctypes.c_char_p(dev_info.version.encode("utf-8"))

            assert self._lib is not None
            ret = self._lib.tuya_subdev_register(
                self._config.gateway_id.encode("utf-8"),
                c_device_id,
                c_uuid,
                c_mac,
                c_pid,
                c_version,
            )
            if ret == 0:
                self._sub_devices[dev_info.device_id] = dev_info
                logger.info(
                    "sub_device_registered",
                    device_id=dev_info.device_id,
                    mac=dev_info.device_mac,
                    pid=dev_info.pid,
                )
                return True
            logger.error("sub_device_register_failed", device_id=dev_info.device_id, ret=ret)
            return False

    def unregister_sub_device(self, device_id: str) -> bool:
        """解绑子设备。"""
        with self._lock:
            if self._lib is None or self._state != GatewayState.ONLINE:
                return False
            ret = self._lib.tuya_subdev_unregister(
                self._config.gateway_id.encode("utf-8"),
                device_id.encode("utf-8"),
            )
            if ret == 0:
                self._sub_devices.pop(device_id, None)
                logger.info("sub_device_unregistered", device_id=device_id)
                return True
            return False

    def list_sub_devices(self) -> list[SubDeviceInfo]:
        """返回已注册的子设备列表。"""
        with self._lock:
            return list(self._sub_devices.values())

    # ── DP 代理 ───────────────────────────────────────────────────

    def report_dp(
        self,
        device_id: str,
        dp_id: int,
        dp_type: TuyaDpType,
        dp_value: Any,
    ) -> bool:
        """上行 DP 上报（代理子设备）。

        :param device_id: 子设备 ID
        :param dp_id: 数据点 ID（见 tuya_dp_schema.json）
        :param dp_type: 数据类型
        :param dp_value: 值（bool/int/str/JSON）
        """
        with self._lock:
            if self._lib is None or self._state != GatewayState.ONLINE:
                logger.warning("tuya_gateway_offline, dp_report_skipped")
                return False
            value_str = json.dumps(dp_value, ensure_ascii=False) if not isinstance(
                dp_value, str
            ) else dp_value
            ret = self._lib.tuya_dp_report(
                device_id.encode("utf-8"),
                ctypes.c_uint(dp_id),
                ctypes.c_uint(dp_type.value),
                value_str.encode("utf-8"),
            )
            if ret == 0:
                logger.debug(
                    "dp_reported",
                    device_id=device_id,
                    dp_id=dp_id,
                    dp_type=dp_type.name,
                )
                return True
            logger.error("dp_report_failed", device_id=device_id, dp_id=dp_id, ret=ret)
            return False

    def report_dp_batch(self, device_id: str, dp_map: dict[int, Any]) -> int:
        """批量上报 DP。"""
        success_count = 0
        for dp_id, value in dp_map.items():
            dp_type = self._infer_dp_type(value)
            if self.report_dp(device_id, dp_id, dp_type, value):
                success_count += 1
        return success_count

    # ── OTA 分发 ──────────────────────────────────────────────────

    def dispatch_ota(
        self,
        device_id: str,
        firmware_url: str,
        target_version: str,
    ) -> bool:
        """通知子设备进行 OTA 升级。

        :param device_id: 子设备 ID
        :param firmware_url: 固件下载 URL
        :param target_version: 目标版本号
        """
        with self._lock:
            if self._lib is None or self._state != GatewayState.ONLINE:
                return False
            ret = self._lib.tuya_ota_notify(
                device_id.encode("utf-8"),
                firmware_url.encode("utf-8"),
                target_version.encode("utf-8"),
            )
            if ret == 0:
                logger.info(
                    "ota_dispatched",
                    device_id=device_id,
                    target_version=target_version,
                )
                return True
            logger.error("ota_dispatch_failed", device_id=device_id, ret=ret)
            return False

    # ── ctypes 库加载 ─────────────────────────────────────────────

    def _load_library(self) -> None:
        """加载涂鸦 SDK 共享库。"""
        lib_path = self._config.sdk_lib_path or self._find_sdk_library()
        if not lib_path:
            raise RuntimeError(
                "Tuya SDK library not found. Set TUYA_SDK_PATH or sdk_lib_path."
            )
        self._lib = ctypes.CDLL(lib_path)
        self._declare_function_signatures()
        logger.info("tuya_sdk_loaded", path=lib_path)

    def _find_sdk_library(self) -> str:
        """在常见路径搜索涂鸦 SDK 库。"""
        candidates = [
            os.environ.get("TUYA_SDK_PATH", ""),
            "/usr/local/lib/libtuya_iot_gateway.so",
            "/usr/lib/libtuya_iot_gateway.so",
            "/opt/tuya/lib/libtuya_iot_gateway.so",
            ctypes.util.find_library("tuya_iot_gateway") or "",
            ctypes.util.find_library("tuya_gateway") or "",
        ]
        for path in candidates:
            if path and os.path.exists(path):
                return path
        # 开发/测试环境：返回占位路径（实际不存在时 ctypes.CDLL 会抛异常）
        return candidates[0] if candidates else ""

    def _declare_function_signatures(self) -> None:
        """声明 C 函数签名（argtypes / restype）。"""
        assert self._lib is not None
        lib = self._lib

        lib.tuya_gateway_init.restype = ctypes.c_int
        lib.tuya_gateway_init.argtypes = [
            ctypes.c_char_p,   # storage_path
            ctypes.c_int,      # log_level
            ctypes.c_char_p,   # region
        ]

        lib.tuya_gateway_login.restype = ctypes.c_int
        lib.tuya_gateway_login.argtypes = [
            ctypes.c_char_p,   # gateway_id
            ctypes.c_char_p,   # uuid
            ctypes.c_char_p,   # key
        ]

        lib.tuya_gateway_exit.restype = ctypes.c_int
        lib.tuya_gateway_exit.argtypes = []

        lib.tuya_subdev_register.restype = ctypes.c_int
        lib.tuya_subdev_register.argtypes = [
            ctypes.c_char_p,   # gateway_id
            ctypes.c_char_p,   # device_id
            ctypes.c_char_p,   # uuid
            ctypes.c_char_p,   # mac
            ctypes.c_char_p,   # pid
            ctypes.c_char_p,   # version
        ]

        lib.tuya_subdev_unregister.restype = ctypes.c_int
        lib.tuya_subdev_unregister.argtypes = [
            ctypes.c_char_p,   # gateway_id
            ctypes.c_char_p,   # device_id
        ]

        lib.tuya_dp_report.restype = ctypes.c_int
        lib.tuya_dp_report.argtypes = [
            ctypes.c_char_p,   # device_id
            ctypes.c_uint,     # dp_id
            ctypes.c_uint,      # dp_type
            ctypes.c_char_p,   # dp_value_json
        ]

        lib.tuya_ota_notify.restype = ctypes.c_int
        lib.tuya_ota_notify.argtypes = [
            ctypes.c_char_p,   # device_id
            ctypes.c_char_p,   # firmware_url
            ctypes.c_char_p,   # target_version
        ]

        lib.tuya_register_dp_cmd_callback.restype = ctypes.c_int
        lib.tuya_register_dp_cmd_callback.argtypes = [DPCmdCallback, ctypes.c_void_p]

        lib.tuya_register_ota_callback.restype = ctypes.c_int
        lib.tuya_register_ota_callback.argtypes = [OtaNotifyCallback, ctypes.c_void_p]

        lib.tuya_register_subdev_status_callback.restype = ctypes.c_int
        lib.tuya_register_subdev_status_callback.argtypes = [
            SubdevStatusCallback, ctypes.c_void_p
        ]

    def _configure_sdk(self) -> None:
        """初始化 SDK。"""
        assert self._lib is not None
        os.makedirs(self._config.storage_path, exist_ok=True)
        self._lib.tuya_gateway_init(
            self._config.storage_path.encode("utf-8"),
            ctypes.c_int(self._config.log_level),
            self._config.region.encode("utf-8"),
        )

    def _register_callbacks(self) -> None:
        """注册 C 回调函数。"""
        assert self._lib is not None
        self._lib.tuya_register_dp_cmd_callback(self._dp_cmd_cb, self._user_data)
        self._lib.tuya_register_ota_callback(self._ota_cb, self._user_data)
        self._lib.tuya_register_subdev_status_callback(
            self._subdev_status_cb, self._user_data
        )

    # ── C 回调包装器 ──────────────────────────────────────────────

    def _dp_cmd_callback_wrapper(
        self,
        device_id: bytes,
        dp_id: int,
        dp_type: int,
        dp_value_json: bytes,
        user_data: ctypes.c_void_p,
    ) -> int:
        """下行 DP 命令回调（C → Python）。"""
        try:
            dev_id = device_id.decode("utf-8") if device_id else ""
            value_str = dp_value_json.decode("utf-8") if dp_value_json else ""
            try:
                value: Any = json.loads(value_str)
            except json.JSONDecodeError:
                value = value_str

            logger.info(
                "dp_downlink_received",
                device_id=dev_id,
                dp_id=dp_id,
                dp_type=dp_type,
                value=value,
            )

            # 路由到 BLE 命令
            command = DP_COMMAND_ROUTING.get(dp_id)
            if command and dev_id in self._sub_devices:
                logger.debug("dp_routing", command=command, device_id=dev_id)

            if self.on_dp_downlink:
                self.on_dp_downlink(dev_id, dp_id, dp_type, value)
            return 0
        except Exception as exc:  # noqa: BLE001
            logger.error("dp_cmd_callback_error", error=str(exc))
            return -1

    def _ota_notify_callback_wrapper(
        self,
        device_id: bytes,
        firmware_url: bytes,
        firmware_version: bytes,
        user_data: ctypes.c_void_p,
    ) -> int:
        """OTA 通知回调。"""
        try:
            dev_id = device_id.decode("utf-8") if device_id else ""
            url = firmware_url.decode("utf-8") if firmware_url else ""
            version = firmware_version.decode("utf-8") if firmware_version else ""
            logger.info(
                "ota_notification",
                device_id=dev_id,
                target_version=version,
                url=url,
            )
            if self.on_ota_notify:
                self.on_ota_notify(dev_id, url, version)
            return 0
        except Exception as exc:  # noqa: BLE001
            logger.error("ota_callback_error", error=str(exc))
            return -1

    def _subdev_status_callback_wrapper(
        self,
        device_id: bytes,
        online: bool,
        user_data: ctypes.c_void_p,
    ) -> int:
        """子设备在线状态回调。"""
        try:
            dev_id = device_id.decode("utf-8") if device_id else ""
            logger.info("subdev_status", device_id=dev_id, online=online)
            if self.on_subdev_online:
                self.on_subdev_online(dev_id, online)
            return 0
        except Exception as exc:  # noqa: BLE001
            logger.error("subdev_status_callback_error", error=str(exc))
            return -1

    # ── 辅助方法 ──────────────────────────────────────────────────

    @staticmethod
    def _infer_dp_type(value: Any) -> TuyaDpType:
        """根据值类型推断 DP 类型。"""
        if isinstance(value, bool):
            return TuyaDpType.BOOL
        if isinstance(value, int):
            return TuyaDpType.VALUE
        if isinstance(value, str):
            return TuyaDpType.STRING
        if isinstance(value, (list, dict)):
            return TuyaDpType.RAW
        return TuyaDpType.STRING

    @property
    def state(self) -> GatewayState:
        return self._state

    def is_online(self) -> bool:
        return self._state == GatewayState.ONLINE

    def _set_state(self, state: GatewayState) -> None:
        self._state = state
