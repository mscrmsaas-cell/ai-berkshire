"""
挎包终端配置加载模块

使用 Pydantic Settings 从环境变量和 config.yaml 加载配置。
环境变量优先级最高, 覆盖 YAML 默认值。

加载顺序:
    1. 环境变量 (大写 + 下划线, 如 SAAS_API_URL)
    2. config/config.yaml
    3. Pydantic Settings 默认值
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# 配置子模型
# ---------------------------------------------------------------------------

class ServerConfig(BaseModel):
    """Web 服务配置"""
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 1
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    log_level: str = "INFO"


class BleConfig(BaseModel):
    """BLE 网桥配置 — 1:1 配对模式 (铁路等保安全要求)

    安全特性:
        - 严格 1:1 配对 (max_connections=1, 仅允许一副眼镜)
        - LE Secure Connections (AES-128 加密配对)
        - TX 功率限制 (-12 dBm ~ 0 dBm, 控制通信距离 ≤10m)
        - 绑定设备地址过滤 (仅允许预绑定的眼镜 MAC)
        - 跳频扩频 (BLE 自适应跳频, 抗铁路电磁干扰)
        - 心跳超时断连 (防止异常连接保持)
    """
    service_uuid: str = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
    tx_char_uuid: str = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
    rx_char_uuid: str = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
    # 1:1 严格配对模式 — 仅允许一副眼镜连接
    max_connections: int = 1
    # 扫描参数
    scan_duration: int = 5            # 缩短扫描时间 (1:1 模式无需长时间扫描)
    scan_interval: int = 10           # 缩短扫描间隔 (快速重连)
    # 连接参数
    connection_timeout: int = 10     # 连接超时 (秒)
    mtu: int = 247                   # 最大传输单元
    # 重连参数
    reconnect_interval: int = 3      # 重连间隔 (秒, 1:1 模式需快速恢复)
    reconnect_max_retries: int = 20  # 最大重试次数 (增加到 20 次保证可靠性)
    # 心跳
    heartbeat_interval: int = 5      # 心跳间隔 (缩短到 5 秒, 快速检测断连)
    heartbeat_timeout: int = 15      # 心跳超时 (15 秒, 超过则断开重连)
    # 帧协议
    max_payload_size: int = 4096
    max_reassembly_packets: int = 64
    ack_timeout: int = 3
    ack_max_retries: int = 3
    # 设备过滤 — 仅允许预绑定的设备名称前缀
    device_name_prefix: str = "RailGlasses"
    # ── 铁路安全增强参数 ──
    # 绑定设备 MAC 地址 (仅允许此地址的眼镜连接, 空则匹配名称前缀)
    bonded_device_address: str = ""  # 预绑定 MAC (如 "AA:BB:CC:DD:EE:FF")
    # TX 功率控制 (dBm, 控制通信距离 ≤10m)
    # BLE 5.0 TX 功率范围: -20 ~ +8 dBm
    # -12 dBm ≈ 10 米视距 (开阔环境)
    # 0 dBm ≈ 15-20 米 (铁路环境中衰减更快, 适合 ≤10m)
    tx_power_dbm: int = -6           # 保守设置, 确保 ≤10m
    # 加密配对方式 (LE Secure Connections)
    pairing_mode: str = "le_secure"  # le_secure / legacy / none
    # 配对加密密钥位数
    encryption_key_size: int = 16    # AES-128
    # 通信距离上限 (米, 用于日志和告警)
    max_range_meters: int = 10
    # RSSI 阈值 (低于此值认为超出安全距离, 主动断连)
    rssi_disconnect_threshold: int = -85  # dBm


class PcBridgeConfig(BaseModel):
    """指定 PC/服务器对接配置 (替代原 SaaS/5G 配置)

    数据仅通过 USB 有线连接到指定 PC/服务器, 不经任何外部网络。
    """
    # PC 对接 API 端口 (仅绑定 USB 网卡 10.0.0.1)
    api_port: int = 9090
    # 数据导出目录
    export_dir: str = "/mnt/sdcard/bag-terminal/exports"
    # .dat 包最大大小 (MB)
    max_package_size_mb: int = 4096
    # HMAC 签名密钥 (从环境变量读取)
    hmac_key_env: str = "BAG_EXPORT_HMAC_KEY"
    # Token 认证
    token_ttl_seconds: int = 1800
    token_length: int = 8
    # 串口调试
    serial_port: str = "/dev/ttyACM0"
    serial_baudrate: int = 115200
    # 调试命令白名单
    debug_allowed_commands: list[str] = Field(
        default_factory=lambda: [
            "ls", "df", "free", "uptime", "dmesg",
            "ip", "ps", "cat", "grep", "sqlite3", "python3",
        ]
    )
    debug_command_timeout: int = 10
    debug_rate_limit: int = 10


class TuyaConfig(BaseModel):
    """涂鸦 IoT 配置 — 已禁用 (铁路等保要求外网隔离)"""
    enabled: bool = False  # 强制禁用, 不可开启
    mqtt_broker: str = ""
    mqtt_port: int = 1883
    mqtt_client_id: str = ""
    mqtt_username: str = ""
    mqtt_password: str = ""
    device_config_path: str = ""


class YoloModelConfig(BaseModel):
    """YOLO 模型配置"""
    model_path: str = "models/yolov8n_railway.onnx"
    input_size: int = 320
    confidence_threshold: float = 0.5
    nms_threshold: float = 0.45
    classes: list[str] = Field(
        default_factory=lambda: [
            "rail_crack",
            "rail_wear",
            "screw_loose",
            "screw_missing",
            "plate_damage",
            "ballast_abnormal",
            "vegetation_intrusion",
            "foreign_object",
        ]
    )
    hot_update_interval: int = 0  # 0 = 禁用云端热更新, 仅 USB 导入
    model_registry_url: str = ""  # 已清空, 不连接外部服务器


class EmbeddingModelConfig(BaseModel):
    """嵌入模型配置"""
    model_path: str = "models/bge-small-zh.onnx"
    max_seq_length: int = 512
    embedding_dim: int = 768
    batch_size: int = 16


class LlmModelConfig(BaseModel):
    """LLM 模型配置"""
    model_path: str = "models/phi-3-mini-4k-instruct-q4.gguf"
    n_ctx: int = 4096
    n_gpu_layers: int = 0
    n_threads: int = 4
    max_tokens: int = 512
    temperature: float = 0.3
    top_p: float = 0.9
    repeat_penalty: float = 1.1
    stop_tokens: list[str] = Field(
        default_factory=lambda: ["<|end|>", "Human:", "Assistant:"]
    )


class ModelsConfig(BaseModel):
    """模型配置聚合"""
    model_dir: str = "models"
    yolo: YoloModelConfig = Field(default_factory=YoloModelConfig)
    embedding: EmbeddingModelConfig = Field(default_factory=EmbeddingModelConfig)
    llm: LlmModelConfig = Field(default_factory=LlmModelConfig)


class RagConfig(BaseModel):
    """RAG 引擎配置 — 本地离线模式 (无云端同步)"""
    qdrant_path: str = "data/qdrant"
    collection_name: str = "railway_knowledge"
    embedding_dim: int = 768
    top_k: int = 5
    score_threshold: float = 0.5
    # 知识库同步已移除云端, 改为 USB 有线从 PC 导入
    sync_interval: int = 0  # 0 = 禁用自动同步, 仅 USB 触发
    # PC 端知识库导入目录 (USB 挂载时可访问)
    pc_import_dir: str = "/mnt/sdcard/bag-terminal/knowledge_import"
    max_context_chars: int = 2000


class StorageConfig(BaseModel):
    """存储配置"""
    db_path: str = "data/bag_terminal.db"
    photo_dir: str = "storage/photos"
    video_dir: str = "storage/videos"
    max_disk_usage_gb: int = 32
    cache_max_retries: int = 5
    cache_retry_backoff: float = 2.0


class ChargingConfig(BaseModel):
    """充电管理配置"""
    i2c_bus: int = 1
    i2c_address: int = 0x6B
    poll_interval: int = 5
    temp_threshold: float = 60.0
    low_battery_threshold: int = 20


# ---------------------------------------------------------------------------
# 顶层 Settings
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    """
    挎包终端全局配置

    优先级: 环境变量 > YAML > 代码默认值
    环境变量名使用大写, 嵌套字段用双下划分隔, 如 MODELS__YOLO__INPUT_SIZE
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    # 基础环境
    env: str = "production"
    tz: str = "Asia/Shanghai"

    # 子配置
    server: ServerConfig = Field(default_factory=ServerConfig)
    ble: BleConfig = Field(default_factory=BleConfig)
    # PC/服务器对接配置 (替代原 SaaS 配置, 仅 USB 有线通道)
    pc_bridge: PcBridgeConfig = Field(default_factory=PcBridgeConfig)
    tuya: TuyaConfig = Field(default_factory=TuyaConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    rag: RagConfig = Field(default_factory=RagConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    charging: ChargingConfig = Field(default_factory=ChargingConfig)

    # 配置文件路径
    config_file: str = "config/config.yaml"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并字典, override 覆盖 base"""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_yaml(path: str | Path) -> dict[str, Any]:
    """加载 YAML 配置文件"""
    yaml_path = Path(path)
    if not yaml_path.exists():
        return {}
    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    获取全局配置 (单例)

    加载流程:
        1. 读取 config/config.yaml
        2. 用 YAML 值初始化 Settings
        3. Pydantic Settings 自动用环境变量覆盖
    """
    config_file = os.environ.get("CONFIG_FILE", "config/config.yaml")
    yaml_data = _load_yaml(config_file)

    # 用 YAML 数据构建 Settings (环境变量会自动覆盖)
    settings = Settings(**yaml_data)
    return settings


def reload_settings() -> Settings:
    """强制重新加载配置 (热更新场景)"""
    get_settings.cache_clear()
    return get_settings()
