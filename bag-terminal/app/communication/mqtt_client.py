"""涂鸦云 MQTT 客户端。

负责挎包终端与涂鸦 IoT 云平台之间的 MQTT 通信：
- 基于 paho-mqtt 异步循环
- QoS 1 保证消息至少一次送达
- 自动断线重连（指数退避 + 心跳保活）
- 主题订阅 / 发布封装

涂鸦 MQTT 主题格式：
  - 下行（指令）: tyink/{device_id}/commands
  - 上行（数据点）: tyink/{device_id}/dp
  - OTA 通知:   tyink/{device_id}/ota
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import paho.mqtt.client as mqtt
import structlog

logger = structlog.get_logger(__name__)

# ── 涂鸦 MQTT 主题模板 ──────────────────────────────────────────
TOPIC_DOWN_COMMANDS = "tyink/{device_id}/commands"
TOPIC_UP_DP = "tyink/{device_id}/dp"
TOPIC_OTA = "tyink/{device_id}/ota"
TOPIC_LWT = "tyink/{device_id}/status"  # 遗嘱主题


class MqttConnectionState(Enum):
    """MQTT 连接状态机。"""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"


@dataclass
class TuyaMqttConfig:
    """涂鸦 MQTT 客户端配置。"""

    broker_host: str = "m1.tuyacn.com"
    broker_port: int = 8883
    client_id: str = ""
    username: str = ""
    password: str = ""
    device_id: str = ""
    keepalive: int = 60          # 心跳间隔（秒）
    qos: int = 1                 # 默认 QoS 1
    use_tls: bool = True         # 涂鸦云要求 TLS
    ca_cert_path: str = ""       # CA 证书路径
    reconnect_interval: float = 5.0   # 初始重连间隔
    reconnect_max_interval: float = 300.0  # 最大重连间隔
    reconnect_backoff_factor: float = 2.0  # 指数退避因子


# ── 回调签名 ─────────────────────────────────────────────────────
CommandHandler = Callable[[dict[str, Any]], None]
DpHandler = Callable[[dict[str, Any]], None]
OtaHandler = Callable[[dict[str, Any]], None]


class TuyaMqttClient:
    """涂鸦云 MQTT 客户端。

    封装 paho-mqtt 客户端，提供：
    - 异步连接与自动重连
    - 主题订阅与回调分发
    - 数据点（DP）上报
    - 命令下发接收
    - OTA 通知接收

    使用示例::

        client = TuyaMqttClient(config)
        client.on_command = handle_command
        client.on_ota = handle_ota
        client.start()            # 启动后台线程
        client.publish_dp({1: True, 2: 85})
        client.stop()
    """

    def __init__(self, config: TuyaMqttConfig) -> None:
        self._config = config
        self._state = MqttConnectionState.DISCONNECTED
        self._state_lock = threading.Lock()
        self._client: mqtt.Client | None = None
        self._reconnect_delay = config.reconnect_interval
        self._should_run = False
        self._reconnect_thread: threading.Thread | None = None

        # 外部可注册的回调
        self.on_command: CommandHandler | None = None
        self.on_dp_response: DpHandler | None = None
        self.on_ota: OtaHandler | None = None
        self.on_connect_cb: Callable[[], None] | None = None
        self.on_disconnect_cb: Callable[[str], None] | None = None

    # ── 生命周期 ──────────────────────────────────────────────────

    def start(self) -> None:
        """启动 MQTT 客户端（异步连接）。"""
        if self._should_run:
            logger.warning("mqtt_client_already_running")
            return
        self._should_run = True
        self._init_client()
        self._connect_async()

    def stop(self) -> None:
        """停止客户端并清理资源。"""
        self._should_run = False
        self._set_state(MqttConnectionState.DISCONNECTED)
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception as exc:  # noqa: BLE001
                logger.warning("mqtt_stop_error", error=str(exc))
        logger.info("mqtt_client_stopped", device_id=self._config.device_id)

    # ── 内部初始化 ────────────────────────────────────────────────

    def _init_client(self) -> None:
        """创建 paho Client 并注册回调。"""
        client_id = self._config.client_id or f"bag-terminal-{self._config.device_id}"
        client = mqtt.Client(
            client_id=client_id,
            clean_session=True,
            protocol=mqtt.MQTTv311,
            transport="tcp",
        )
        client.username_pw_set(self._config.username, self._config.password)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.on_log = self._on_log

        # 遗嘱消息：设备离线时通知云平台
        lwt_topic = TOPIC_LWT.format(device_id=self._config.device_id)
        lwt_payload = json.dumps({"device_id": self._config.device_id, "online": False})
        client.will_set(lwt_topic, lwt_payload, qos=self._config.qos, retain=True)

        # TLS 配置
        if self._config.use_tls:
            if self._config.ca_cert_path:
                client.tls_set(ca_certs=self._config.ca_cert_path)
            else:
                client.tls_set()  # 使用系统默认 CA
            client.tls_insecure_set(False)

        self._client = client

    def _connect_async(self) -> None:
        """异步发起连接。"""
        self._set_state(MqttConnectionState.CONNECTING)
        assert self._client is not None
        try:
            self._client.connect_async(
                host=self._config.broker_host,
                port=self._config.broker_port,
                keepalive=self._config.keepalive,
            )
            self._client.loop_start()
        except Exception as exc:  # noqa: BLE001
            logger.error("mqtt_connect_failed", error=str(exc))
            self._set_state(MqttConnectionState.ERROR)
            self._schedule_reconnect()

    # ── paho 回调 ────────────────────────────────────────────────

    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: dict, rc: int) -> None:
        if rc == 0:
            self._set_state(MqttConnectionState.CONNECTED)
            self._reconnect_delay = self._config.reconnect_interval  # 重置退避
            logger.info("mqtt_connected", device_id=self._config.device_id)

            # 订阅主题
            self._subscribe_topics()

            # 发布在线状态
            self._publish_online_status()

            if self.on_connect_cb:
                try:
                    self.on_connect_cb()
                except Exception as exc:  # noqa: BLE001
                    logger.error("on_connect_cb_error", error=str(exc))
        else:
            logger.error("mqtt_connect_refused", rc=rc)
            self._set_state(MqttConnectionState.ERROR)
            self._schedule_reconnect()

    def _on_disconnect(self, client: mqtt.Client, userdata: Any, rc: int) -> None:
        reason = "graceful" if rc == 0 else f"unexpected rc={rc}"
        logger.warning("mqtt_disconnected", reason=reason)
        self._set_state(MqttConnectionState.RECONNECTING)
        if self.on_disconnect_cb:
            try:
                self.on_disconnect_cb(reason)
            except Exception as exc:  # noqa: BLE001
                logger.error("on_disconnect_cb_error", error=str(exc))
        if self._should_run and rc != 0:
            self._schedule_reconnect()

    def _on_message(self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
        """消息分发到对应的回调处理器。"""
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.error("mqtt_message_parse_error", topic=msg.topic, error=str(exc))
            return

        device_id = self._config.device_id
        if msg.topic == TOPIC_DOWN_COMMANDS.format(device_id=device_id):
            logger.debug("mqtt_command_received", payload=payload)
            if self.on_command:
                self.on_command(payload)
        elif msg.topic == TOPIC_OTA.format(device_id=device_id):
            logger.info("mqtt_ota_received", payload=payload)
            if self.on_ota:
                self.on_ota(payload)
        elif msg.topic == TOPIC_UP_DP.format(device_id=device_id):
            # DP 上行响应（ACK）
            if self.on_dp_response:
                self.on_dp_response(payload)
        else:
            logger.debug("mqtt_unhandled_topic", topic=msg.topic)

    def _on_log(self, client: mqtt.Client, userdata: Any, level: int, string: str) -> None:
        if level == mqtt.Logging.LOG_ERROR:
            logger.error("mqtt_log_error", message=string)

    # ── 订阅 ──────────────────────────────────────────────────────

    def _subscribe_topics(self) -> None:
        """订阅下行主题。"""
        assert self._client is not None
        device_id = self._config.device_id
        topics = [
            (TOPIC_DOWN_COMMANDS.format(device_id=device_id), self._config.qos),
            (TOPIC_OTA.format(device_id=device_id), self._config.qos),
            (TOPIC_UP_DP.format(device_id=device_id), self._config.qos),
        ]
        for topic, qos in topics:
            result = self._client.subscribe(topic, qos=qos)
            if result[0] == mqtt.MQTT_ERR_SUCCESS:
                logger.info("mqtt_subscribed", topic=topic, qos=qos)
            else:
                logger.error("mqtt_subscribe_failed", topic=topic, result=result)

    # ── 发布 ──────────────────────────────────────────────────────

    def publish_dp(self, dp_data: dict[int | str, Any]) -> bool:
        """上报涂鸦数据点。

        :param dp_data: DP ID → 值的映射，如 ``{1: True, 2: 85, 6: "inspection"}`
        :return: 是否成功入队
        """
        if not self.is_connected():
            logger.warning("mqtt_publish_not_connected, dp_data_queued_locally")
            return False
        topic = TOPIC_UP_DP.format(device_id=self._config.device_id)
        payload = json.dumps(
            {
                "device_id": self._config.device_id,
                "dp": {str(k): v for k, v in dp_data.items()},
                "timestamp": int(time.time()),
            },
            ensure_ascii=False,
        )
        assert self._client is not None
        info = self._client.publish(topic, payload, qos=self._config.qos)
        if info.rc == mqtt.MQTT_ERR_SUCCESS:
            logger.debug("mqtt_dp_published", dp_keys=list(dp_data.keys()), mid=info.mid)
            return True
        logger.error("mqtt_publish_failed", rc=info.rc)
        return False

    def publish_alert(self, alert_type: str, severity: str, description: str) -> bool:
        """通过 MQTT 上报告警事件。"""
        return self.publish_dp(
            {
                60: {
                    "type": alert_type,
                    "severity": severity,
                    "description": description,
                }
            }
        )

    def _publish_online_status(self) -> None:
        """发布在线状态（retain）。"""
        assert self._client is not None
        topic = TOPIC_LWT.format(device_id=self._config.device_id)
        payload = json.dumps(
            {"device_id": self._config.device_id, "online": True, "timestamp": int(time.time())}
        )
        self._client.publish(topic, payload, qos=self._config.qos, retain=True)

    # ── 重连 ──────────────────────────────────────────────────────

    def _schedule_reconnect(self) -> None:
        """在后台线程中安排重连（指数退避）。"""
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            return
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, daemon=True, name="mqtt-reconnect"
        )
        self._reconnect_thread.start()

    def _reconnect_loop(self) -> None:
        """指数退避重连循环。"""
        while self._should_run:
            time.sleep(self._reconnect_delay)
            if not self._should_run:
                break
            logger.info(
                "mqtt_reconnect_attempt",
                delay=self._reconnect_delay,
                device_id=self._config.device_id,
            )
            self._set_state(MqttConnectionState.RECONNECTING)
            assert self._client is not None
            try:
                self._client.reconnect()
                self._reconnect_delay = self._config.reconnect_interval  # 成功则重置
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("mqtt_reconnect_failed", error=str(exc))
                # 指数退避
                self._reconnect_delay = min(
                    self._reconnect_delay * self._config.reconnect_backoff_factor,
                    self._config.reconnect_max_interval,
                )
                self._set_state(MqttConnectionState.ERROR)

    # ── 状态查询 ──────────────────────────────────────────────────

    def is_connected(self) -> bool:
        """是否已连接。"""
        return self._state == MqttConnectionState.CONNECTED

    @property
    def state(self) -> MqttConnectionState:
        return self._state

    def _set_state(self, state: MqttConnectionState) -> None:
        with self._state_lock:
            self._state = state
