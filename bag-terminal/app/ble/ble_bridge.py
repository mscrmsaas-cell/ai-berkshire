"""
BLE 网桥 — Bleak Central 角色, 1:1 严格配对模式 (铁路等保安全要求)

功能:
    - 持续扫描 NUS Service 设备
    - 1:1 严格配对 (仅允许一副预绑定眼镜连接)
    - MAC 地址过滤 (仅允许预绑定的设备)
    - RSSI 阈值监控 (低于 -85 dBm 主动断连, 防止远距离窃听)
    - LE Secure Connections 加密配对 (AES-128)
    - 自动连接/重连 (指数退避)
    - 统一消息分发 (路由到 AI 推理 / RAG / 巡检记录)
    - 设备状态管理 (GlassesState)

架构:
    BleBridge
      └── NusClient #1 (唯一眼镜) ──→ 消息分发
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Awaitable, Callable

import structlog
from bleak import BleakScanner
from bleak.exc import BleakError

from app.ble.message_protocol import MessageProtocol
from app.ble.nus_client import NusClient
from app.config import Settings
from app.models.ble_message import BleFrame, MessageType
from app.models.device_state import DeviceMode, GlassesState

logger = structlog.get_logger(__name__)

# 全局消息处理器类型: (nus_client, frame, glasses_state) -> None
MessageHandler = Callable[[NusClient, BleFrame, GlassesState], Awaitable[None]]


class BleBridge:
    """
    BLE Central 网桥 — 1:1 严格配对模式

    管理扫描、连接、重连、消息分发。
    仅允许一副预绑定眼镜连接 (max_connections=1)。

    安全特性:
        - MAC 地址过滤: 仅允许 bonded_device_address 匹配的设备
        - RSSI 阈值: 低于 rssi_disconnect_threshold 主动断连
        - 设备名称前缀: 匹配 device_name_prefix
        - 心跳超时: heartbeat_timeout 秒无心跳则断开
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.ble_config = settings.ble

        # 协议层
        self.protocol = MessageProtocol(
            max_payload_size=self.ble_config.max_payload_size,
            mtu=self.ble_config.mtu,
        )

        # 已连接设备: {address: NusClient}
        self.connected_devices: dict[str, NusClient] = {}
        # 设备状态: {address: GlassesState}
        self.device_states: dict[str, GlassesState] = {}
        # 重连计数: {address: retry_count}
        self._reconnect_counts: dict[str, int] = {}
        # 重连锁 (防止同一设备并发重连)
        self._reconnect_locks: dict[str, asyncio.Lock] = {}

        # 消息处理器
        self._message_handler: MessageHandler | None = None

        # 运行状态
        self.is_running: bool = False
        self._scan_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None

        # 扫描结果缓存 (用于日志/调试)
        self._last_scan_results: list[dict] = []

    # -------------------------------------------------------------------
    # 消息处理器注册
    # -------------------------------------------------------------------

    def set_message_handler(self, handler: MessageHandler) -> None:
        """
        注册全局消息处理器

        每收到一个完整帧 (非 ACK/心跳), 调用此处理器。
        """
        self._message_handler = handler

    # -------------------------------------------------------------------
    # 启动/停止
    # -------------------------------------------------------------------

    async def start(self) -> None:
        """启动 BLE 网桥 (扫描 + 连接循环)"""
        if self.is_running:
            logger.warning("ble_bridge.already_running")
            return

        self.is_running = True
        logger.info(
            "ble_bridge.starting",
            scan_duration=self.ble_config.scan_duration,
            max_connections=self.ble_config.max_connections,
            mode="1:1_pairing",
            tx_power_dbm=self.ble_config.tx_power_dbm,
            max_range_m=self.ble_config.max_range_meters,
            bonded_addr=self.ble_config.bonded_device_address or "any_name_match",
            rssi_threshold=self.ble_config.rssi_disconnect_threshold,
        )

        # 启动扫描循环
        self._scan_task = asyncio.create_task(self._scan_loop())

        # 启动重连检查循环
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def stop(self) -> None:
        """停止 BLE 网桥, 断开所有连接"""
        logger.info("ble_bridge.stopping")
        self.is_running = False

        # 取消后台任务
        for task in [self._scan_task, self._reconnect_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._scan_task = None
        self._reconnect_task = None

        # 断开所有设备
        disconnect_tasks = [
            client.disconnect() for client in self.connected_devices.values()
        ]
        if disconnect_tasks:
            await asyncio.gather(*disconnect_tasks, return_exceptions=True)

        self.connected_devices.clear()
        self.device_states.clear()
        logger.info("ble_bridge.stopped")

    # -------------------------------------------------------------------
    # 扫描循环
    # -------------------------------------------------------------------

    async def _scan_loop(self) -> None:
        """持续扫描 NUS 设备并自动连接"""
        while self.is_running:
            try:
                # 检查是否有空闲连接位
                available_slots = (
                    self.ble_config.max_connections - len(self.connected_devices)
                )
                if available_slots <= 0:
                    # 已达最大连接数, 等待
                    await asyncio.sleep(self.ble_config.scan_interval)
                    continue

                # 扫描
                logger.debug(
                    "ble_bridge.scanning",
                    available_slots=available_slots,
                )

                devices = await BleakScanner.discover(
                    timeout=self.ble_config.scan_duration,
                    return_adv=True,
                )

                # 过滤 NUS 设备
                nus_devices = []
                for device, adv_data in devices.items() if isinstance(devices, dict) else []:
                    if self._is_nus_device(device, adv_data):
                        nus_devices.append((device, adv_data))

                # 兼容 BleakScanner.discover 返回 list 的情况
                if not isinstance(devices, dict):
                    for device in devices:
                        adv_data = getattr(device, "details", None)
                        if self._is_nus_device(device, adv_data):
                            nus_devices.append((device, adv_data))

                self._last_scan_results = [
                    {
                        "address": d.address,
                        "name": d.name or "",
                        "rssi": getattr(adv, "rssi", -100) if adv else -100,
                    }
                    for d, adv in nus_devices
                ]

                if nus_devices:
                    logger.info(
                        "ble_bridge.devices_found",
                        count=len(nus_devices),
                        devices=[d.address for d, _ in nus_devices],
                    )

                # 连接未连接的设备
                for device, adv_data in nus_devices:
                    if len(self.connected_devices) >= self.ble_config.max_connections:
                        break
                    if device.address not in self.connected_devices:
                        asyncio.ensure_future(self._connect_device(device))

                # 等待下一轮扫描
                await asyncio.sleep(self.ble_config.scan_interval)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("ble_bridge.scan_error", error=str(exc))
                await asyncio.sleep(self.ble_config.scan_interval)

    def _is_nus_device(self, device, adv_data) -> bool:
        """
        判断是否为目标 NUS 设备 — 1:1 严格配对模式

        匹配条件 (全部满足):
            1. 设备地址匹配预绑定 MAC (如果配置了 bonded_device_address)
            2. UUID 包含 NUS Service UUID, 或设备名称匹配前缀
            3. RSSI 不低于阈值 (防止远距离连接)
        """
        # 1. MAC 地址过滤 — 如果配置了预绑定地址, 严格匹配
        bonded_addr = self.ble_config.bonded_device_address
        if bonded_addr:
            if device.address.upper() != bonded_addr.upper():
                logger.debug(
                    "ble_bridge.device_rejected_mac_mismatch",
                    address=device.address,
                    expected=bonded_addr,
                )
                return False

        # 2. RSSI 阈值检查 — 防止远距离连接 (≤10m 安全距离)
        rssi = getattr(adv_data, "rssi", None) if adv_data else None
        if rssi is not None and rssi < self.ble_config.rssi_disconnect_threshold:
            logger.debug(
                "ble_bridge.device_rejected_rssi",
                address=device.address,
                rssi=rssi,
                threshold=self.ble_config.rssi_disconnect_threshold,
            )
            return False

        # 3. 检查 UUID
        if adv_data:
            service_uuids = getattr(adv_data, "service_uuids", None) or []
            for uuid in service_uuids:
                if uuid and self.ble_config.service_uuid.lower() in uuid.lower():
                    # 检查名称前缀
                    if self.ble_config.device_name_prefix:
                        name = device.name or ""
                        if not name.startswith(self.ble_config.device_name_prefix):
                            continue
                    return True

        # 4. 检查设备名称
        if self.ble_config.device_name_prefix:
            name = device.name or ""
            if name.startswith(self.ble_config.device_name_prefix):
                return True

        return False

    # -------------------------------------------------------------------
    # 连接管理
    # -------------------------------------------------------------------

    async def _connect_device(self, device) -> None:
        """连接单个 BLE 设备"""
        address = device.address

        # 已连接则跳过
        if address in self.connected_devices:
            return

        # 创建 NusClient
        client = NusClient(
            address=address,
            config=self.ble_config,
            protocol=self.protocol,
        )
        client.device_name = device.name or address
        client.rssi = getattr(device, "rssi", -100)

        # 设置回调
        client.set_message_callback(self._on_message)
        client.set_disconnect_callback(self._on_disconnect)

        # 连接
        success = await client.connect()
        if success:
            self.connected_devices[address] = client
            self._reconnect_counts[address] = 0

            # 创建设备状态
            state = GlassesState(
                ble_address=address,
                device_name=client.device_name,
                is_connected=True,
                connected_at=datetime.now(timezone.utc),
                last_seen=datetime.now(timezone.utc),
                rssi=client.rssi,
                mtu=client.protocol.mtu,
            )
            self.device_states[address] = state

            logger.info(
                "ble_bridge.device_connected",
                address=address,
                name=client.device_name,
                total=len(self.connected_devices),
            )
        else:
            logger.warning(
                "ble_bridge.connect_failed",
                address=address,
                name=client.device_name,
            )

    async def _on_disconnect(self, address: str) -> None:
        """设备断开回调"""
        client = self.connected_devices.get(address)
        state = self.device_states.get(address)

        if state:
            state.is_connected = False
            state.mode = DeviceMode.OFFLINE

        if client:
            client.is_connected = False

        logger.warning(
            "ble_bridge.device_disconnected",
            address=address,
            total=len(self.connected_devices),
        )

        # 不立即移除, 交给重连循环处理
        # 重置流解析器
        if client:
            client._stream_parser.reset()

    # -------------------------------------------------------------------
    # 重连循环
    # -------------------------------------------------------------------

    async def _reconnect_loop(self) -> None:
        """定期检查并重连已断开的设备"""
        while self.is_running:
            try:
                await asyncio.sleep(self.ble_config.reconnect_interval)

                # 查找需要重连的设备
                to_reconnect = []
                for address, client in list(self.connected_devices.items()):
                    if not client.is_connected and address not in to_reconnect:
                        retries = self._reconnect_counts.get(address, 0)
                        if retries < self.ble_config.reconnect_max_retries:
                            to_reconnect.append(address)
                        else:
                            # 超过最大重试, 移除设备
                            logger.error(
                                "ble_bridge.reconnect_exhausted",
                                address=address,
                                retries=retries,
                            )
                            self.connected_devices.pop(address, None)
                            self.device_states.pop(address, None)
                            self._reconnect_counts.pop(address, None)

                for address in to_reconnect:
                    if len(self.connected_devices) >= self.ble_config.max_connections:
                        break

                    # 获取或创建重连锁
                    if address not in self._reconnect_locks:
                        self._reconnect_locks[address] = asyncio.Lock()

                    asyncio.ensure_future(self._reconnect_device(address))

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("ble_bridge.reconnect_loop_error", error=str(exc))

    async def _reconnect_device(self, address: str) -> None:
        """重连单个设备 (带指数退避)"""
        lock = self._reconnect_locks.get(address)
        if lock and lock.locked():
            return  # 已在重连中

        if lock is None:
            lock = asyncio.Lock()
            self._reconnect_locks[address] = lock

        async with lock:
            retries = self._reconnect_counts.get(address, 0)
            client = self.connected_devices.get(address)

            if not client:
                return

            # 指数退避
            backoff = min(
                self.ble_config.reconnect_interval * (2 ** retries),
                60,  # 最大 60 秒
            )

            logger.info(
                "ble_bridge.reconnecting",
                address=address,
                attempt=retries + 1,
                backoff=backoff,
            )

            await asyncio.sleep(backoff)

            # 尝试重新连接
            success = await client.connect()
            if success:
                self._reconnect_counts[address] = 0
                state = self.device_states.get(address)
                if state:
                    state.is_connected = True
                    state.connected_at = datetime.now(timezone.utc)
                    state.last_seen = datetime.now(timezone.utc)
                    state.reconnect_count += 1
                    state.mode = DeviceMode.STANDBY

                logger.info(
                    "ble_bridge.reconnected",
                    address=address,
                    total_reconnects=state.reconnect_count if state else 0,
                )
            else:
                self._reconnect_counts[address] = retries + 1
                logger.warning(
                    "ble_bridge.reconnect_failed",
                    address=address,
                    attempt=retries + 1,
                )

    # -------------------------------------------------------------------
    # 消息分发
    # -------------------------------------------------------------------

    async def _on_message(self, client: NusClient, frame: BleFrame) -> None:
        """
        消息回调 — 统一分发入口

        更新设备状态后, 调用注册的消息处理器。
        """
        address = client.address
        state = self.device_states.get(address)

        # 更新设备状态
        if state:
            state.touch()
            state.messages_received += 1
            state.total_bytes_received += len(frame.payload)

            # 根据消息类型更新状态
            self._update_state_from_frame(state, frame)

        # 调用消息处理器
        if self._message_handler:
            try:
                await self._message_handler(client, frame, state)
            except Exception as exc:
                logger.error(
                    "ble_bridge.message_handler_error",
                    address=address,
                    msg_type=frame.msg_type.name,
                    error=str(exc),
                )
        else:
            logger.debug(
                "ble_bridge.no_handler",
                address=address,
                msg_type=frame.msg_type.name,
            )

    def _update_state_from_frame(self, state: GlassesState, frame: BleFrame) -> None:
        """根据消息类型更新设备状态"""
        msg_type = frame.msg_type

        if msg_type == MessageType.HEARTBEAT:
            if len(frame.payload) >= 9:
                state.battery_level = frame.payload[8]

        elif msg_type == MessageType.DEVICE_STATUS_REPORT:
            if len(frame.payload) >= 4:
                state.battery_level = frame.payload[0]
                state.is_charging = bool(frame.payload[1])
                state.camera_connected = bool(frame.payload[2])
                mode_val = frame.payload[3]
                try:
                    state.mode = DeviceMode(mode_val)
                except ValueError:
                    pass

        elif msg_type == MessageType.CAMERA_ATTACHED:
            state.camera_connected = True
            state.camera_attached_at = datetime.now(timezone.utc)

        elif msg_type == MessageType.CAMERA_DETACHED:
            state.camera_connected = False
            state.camera_detached_at = datetime.now(timezone.utc)

        elif msg_type == MessageType.CAMERA_FRAME_DATA:
            state.frame_count += 1

        elif msg_type == MessageType.LOW_BATTERY:
            state.low_battery_alerted = True

        elif msg_type == MessageType.INSPECTION_START:
            state.mode = DeviceMode.INSPECTING

        elif msg_type == MessageType.INSPECTION_END:
            state.mode = DeviceMode.STANDBY
            state.current_task_id = None

        elif msg_type == MessageType.ALERT:
            state.mode = DeviceMode.ALERTING

    # -------------------------------------------------------------------
    # 主动发送
    # -------------------------------------------------------------------

    async def send_to_device(
        self,
        address: str,
        msg_type: MessageType,
        payload: bytes = b"",
        wait_ack: bool | None = None,
    ) -> bool:
        """向指定设备发送消息"""
        client = self.connected_devices.get(address)
        if not client or not client.is_connected:
            logger.warning("ble_bridge.device_not_connected", address=address)
            return False

        success = await client.send_frame(msg_type, payload, wait_ack=wait_ack)
        if success:
            state = self.device_states.get(address)
            if state:
                state.messages_sent += 1
                state.total_bytes_sent += len(payload)
        return success

    async def broadcast(
        self,
        msg_type: MessageType,
        payload: bytes = b"",
    ) -> dict[str, bool]:
        """向所有已连接设备广播消息"""
        results = {}
        tasks = []
        addresses = []

        for address, client in self.connected_devices.items():
            if client.is_connected:
                addresses.append(address)
                tasks.append(client.send_frame(msg_type, payload))

        if tasks:
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            for addr, outcome in zip(addresses, outcomes):
                results[addr] = isinstance(outcome, bool) and outcome

        return results

    # -------------------------------------------------------------------
    # 状态查询
    # -------------------------------------------------------------------

    @property
    def connected_count(self) -> int:
        """已连接设备数"""
        return sum(1 for c in self.connected_devices.values() if c.is_connected)

    def get_device_state(self, address: str) -> GlassesState | None:
        """获取设备状态"""
        return self.device_states.get(address)

    def get_all_states(self) -> list[GlassesState]:
        """获取所有设备状态"""
        return list(self.device_states.values())

    def get_scan_results(self) -> list[dict]:
        """获取最近扫描结果"""
        return self._last_scan_results

    @property
    def stats(self) -> dict:
        """网桥统计"""
        return {
            "is_running": self.is_running,
            "connected_count": self.connected_count,
            "max_connections": self.ble_config.max_connections,
            "devices": {
                addr: client.stats for addr, client in self.connected_devices.items()
            },
        }
