"""
设备状态模型

定义:
    - DeviceMode: 设备模式枚举
    - GlassesState: 眼镜设备运行状态
    - BagTerminalState: 挎包终端运行状态
    - TelemetryData: 遥测数据 (上报 SaaS)
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import IntEnum
from typing import Any

from pydantic import BaseModel, Field


class DeviceMode(IntEnum):
    """设备工作模式"""
    STANDBY = 0       # 待机
    INSPECTING = 1    # 巡检中
    ALERTING = 2      # 告警中
    CHARGING = 3      # 充电中
    OTA = 4           # OTA 升级中
    OFFLINE = 5       # 离线


class GlassesState(BaseModel):
    """
    眼镜设备运行状态

    跟踪单副眼镜的实时状态, 由 BLE 心跳/状态上报更新。
    """
    # --- 标识 ---
    ble_address: str = Field(description="BLE MAC 地址")
    device_name: str = Field(default="", description="设备名称")
    device_id: str = Field(default="", description="设备 ID (涂鸦/序列号)")

    # --- 连接状态 ---
    is_connected: bool = Field(default=False, description="BLE 是否已连接")
    connected_at: datetime | None = Field(default=None, description="连接时间")
    last_seen: datetime | None = Field(default=None, description="最后心跳时间")

    # --- 电池 ---
    battery_level: int = Field(default=0, ge=0, le=100, description="电量百分比")
    is_charging: bool = Field(default=False, description="充电状态")
    low_battery_alerted: bool = Field(default=False, description="低电量告警已发送")

    # --- 摄像头 ---
    camera_connected: bool = Field(default=False, description="摄像头连接状态")
    camera_attached_at: datetime | None = Field(default=None, description="摄像头接入时间")
    camera_detached_at: datetime | None = Field(default=None, description="摄像头断开时间")
    frame_count: int = Field(default=0, description="接收帧数")

    # --- 工作模式 ---
    mode: DeviceMode = Field(default=DeviceMode.STANDBY, description="当前工作模式")
    firmware_version: str = Field(default="", description="固件版本")

    # --- BLE 信号 ---
    rssi: int = Field(default=-100, ge=-100, le=0, description="BLE 信号强度 dBm")
    mtu: int = Field(default=23, description="协商的 MTU")

    # --- 巡检 ---
    current_task_id: str | None = Field(default=None, description="当前巡检任务 ID")

    # --- 统计 ---
    total_bytes_received: int = Field(default=0, description="接收总字节数")
    total_bytes_sent: int = Field(default=0, description="发送总字节数")
    messages_received: int = Field(default=0, description="接收消息数")
    messages_sent: int = Field(default=0, description="发送消息数")
    reconnect_count: int = Field(default=0, description="重连次数")

    def touch(self) -> None:
        """更新最后心跳时间"""
        self.last_seen = datetime.now(timezone.utc)

    @property
    def is_heartbeat_timeout(self, timeout_seconds: int = 30) -> bool:
        """心跳是否超时"""
        if not self.last_seen:
            return True
        elapsed = (datetime.now(timezone.utc) - self.last_seen).total_seconds()
        return elapsed > timeout_seconds

    @property
    def uptime_seconds(self) -> float:
        """已连接时长 (秒)"""
        if not self.connected_at:
            return 0.0
        return (datetime.now(timezone.utc) - self.connected_at).total_seconds()

    def to_telemetry_dict(self) -> dict[str, Any]:
        """转换为遥测数据字典 (上报 SaaS)"""
        return {
            "device_id": self.device_id,
            "ble_address": self.ble_address,
            "battery_level": self.battery_level,
            "is_charging": self.is_charging,
            "camera_connected": self.camera_connected,
            "mode": self.mode.name,
            "firmware_version": self.firmware_version,
            "rssi": self.rssi,
            "mtu": self.mtu,
            "is_connected": self.is_connected,
            "uptime_seconds": round(self.uptime_seconds, 1),
            "reconnect_count": self.reconnect_count,
            "total_bytes_received": self.total_bytes_received,
            "total_bytes_sent": self.total_bytes_sent,
            "messages_received": self.messages_received,
            "messages_sent": self.messages_sent,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


class BagTerminalState(BaseModel):
    """
    挎包终端运行状态

    监控 CM4 平台资源使用情况, 定期上报 SaaS。
    """
    # --- 标识 ---
    device_id: str = Field(default="", description="挎包终端设备 ID")
    serial_number: str = Field(default="", description="序列号")
    firmware_version: str = Field(default="1.0.0", description="固件版本")

    # --- 资源 ---
    cpu_usage_percent: float = Field(default=0.0, ge=0.0, description="CPU 使用率 (%)")
    cpu_temperature: float = Field(default=0.0, description="CPU 温度 (°C)")
    memory_usage_percent: float = Field(default=0.0, ge=0.0, description="内存使用率 (%)")
    memory_total_mb: int = Field(default=0, description="总内存 (MB)")
    memory_used_mb: int = Field(default=0, description="已用内存 (MB)")
    disk_usage_percent: float = Field(default=0.0, ge=0.0, description="磁盘使用率 (%)")
    disk_total_gb: float = Field(default=0.0, description="磁盘总量 (GB)")
    disk_free_gb: float = Field(default=0.0, description="磁盘可用 (GB)")

    # --- 网络 ---
    network_online: bool = Field(default=False, description="网络是否在线")
    network_type: str = Field(default="unknown", description="网络类型 (5G/4G/WiFi/none)")
    signal_strength: int = Field(default=-100, description="信号强度 dBm")
    sim_iccid: str = Field(default="", description="SIM ICCID")

    # --- BLE 网桥 ---
    ble_active: bool = Field(default=False, description="BLE 网桥是否运行")
    connected_glasses_count: int = Field(default=0, ge=0, description="已连接眼镜数")

    # --- AI ---
    ai_model_loaded: bool = Field(default=False, description="AI 模型是否加载")
    ai_model_version: str = Field(default="", description="AI 模型版本")
    inference_count: int = Field(default=0, description="累计推理次数")
    avg_inference_time_ms: float = Field(default=0.0, description="平均推理耗时 (ms)")

    # --- RAG ---
    rag_ready: bool = Field(default=False, description="RAG 引擎是否就绪")
    rag_query_count: int = Field(default=0, description="RAG 查询次数")
    knowledge_doc_count: int = Field(default=0, description="知识库文档数")

    # --- 充电 ---
    is_charging: bool = Field(default=False, description="充电状态")
    charge_current_ma: int = Field(default=0, description="充电电流 (mA)")
    charge_voltage_mv: int = Field(default=0, description="充电电压 (mV)")
    battery_temperature: float = Field(default=0.0, description="电池温度 (°C)")

    # --- 运行 ---
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def update_resource_stats(
        self,
        cpu_usage: float,
        cpu_temp: float,
        mem_used: int,
        mem_total: int,
        disk_used: float,
        disk_total: float,
    ) -> None:
        """更新资源统计"""
        self.cpu_usage_percent = cpu_usage
        self.cpu_temperature = cpu_temp
        self.memory_total_mb = mem_total
        self.memory_used_mb = mem_used
        self.memory_usage_percent = (mem_used / mem_total * 100) if mem_total > 0 else 0.0
        self.disk_total_gb = disk_total
        self.disk_free_gb = disk_total - disk_used
        self.disk_usage_percent = (disk_used / disk_total * 100) if disk_total > 0 else 0.0
        self.last_updated = datetime.now(timezone.utc)

    def update_network_stats(
        self,
        online: bool,
        net_type: str,
        signal: int,
    ) -> None:
        """更新网络统计"""
        self.network_online = online
        self.network_type = net_type
        self.signal_strength = signal
        self.last_updated = datetime.now(timezone.utc)

    def update_inference_stats(self, inference_time_ms: float) -> None:
        """更新推理统计 (滑动平均)"""
        self.inference_count += 1
        alpha = 0.1  # EMA 平滑因子
        self.avg_inference_time_ms = (
            self.avg_inference_time_ms * (1 - alpha) + inference_time_ms * alpha
            if self.inference_count > 1
            else inference_time_ms
        )

    @property
    def uptime_seconds(self) -> float:
        """运行时长"""
        return (datetime.now(timezone.utc) - self.started_at).total_seconds()

    def to_telemetry_dict(self) -> dict[str, Any]:
        """转换为遥测数据字典"""
        return {
            "device_id": self.device_id,
            "cpu_usage_percent": round(self.cpu_usage_percent, 1),
            "cpu_temperature": round(self.cpu_temperature, 1),
            "memory_usage_percent": round(self.memory_usage_percent, 1),
            "memory_total_mb": self.memory_total_mb,
            "memory_used_mb": self.memory_used_mb,
            "disk_usage_percent": round(self.disk_usage_percent, 1),
            "disk_free_gb": round(self.disk_free_gb, 2),
            "network_online": self.network_online,
            "network_type": self.network_type,
            "signal_strength": self.signal_strength,
            "sim_iccid": self.sim_iccid,
            "connected_glasses_count": self.connected_glasses_count,
            "ai_model_loaded": self.ai_model_loaded,
            "ai_model_version": self.ai_model_version,
            "inference_count": self.inference_count,
            "avg_inference_time_ms": round(self.avg_inference_time_ms, 1),
            "rag_ready": self.rag_ready,
            "rag_query_count": self.rag_query_count,
            "knowledge_doc_count": self.knowledge_doc_count,
            "is_charging": self.is_charging,
            "charge_current_ma": self.charge_current_ma,
            "charge_voltage_mv": self.charge_voltage_mv,
            "battery_temperature": round(self.battery_temperature, 1),
            "uptime_seconds": round(self.uptime_seconds, 1),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


class TelemetryData(BaseModel):
    """遥测数据上报模型 (批量上报 SaaS)"""
    device_type: str = Field(description="设备类型 (glasses/bag_terminal)")
    device_id: str = Field(description="设备 ID")
    telemetry: dict[str, Any] = Field(description="遥测数据")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
