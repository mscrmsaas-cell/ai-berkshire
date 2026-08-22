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
    """BLE 网桥配置"""
    service_uuid: str = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
    tx_char_uuid: str = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
    rx_char_uuid: str = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
    scan_duration: int = 10
    scan_interval: int = 30
    max_connections: int = 4
    connection_timeout: int = 15
    mtu: int = 247
    reconnect_interval: int = 5
    reconnect_max_retries: int = 10
    heartbeat_interval: int = 10
    heartbeat_timeout: int = 30
    max_payload_size: int = 4096
    max_reassembly_packets: int = 64
    ack_timeout: int = 3
    ack_max_retries: int = 3
    device_name_prefix: str = "RailGlasses"


class SaasConfig(BaseModel):
    """SaaS API 对接配置"""
    api_url: str = "http://saas.local:8000/api/v1"
    api_timeout: int = 30
    auth_username: str = "bag-terminal"
    auth_password: str = ""
    max_retries: int = 3
    retry_backoff: float = 2.0
    sync_priority: list[str] = Field(
        default_factory=lambda: ["alerts", "photos", "detections", "rag", "telemetry"]
    )


class TuyaConfig(BaseModel):
    """涂鸦 IoT 配置"""
    enabled: bool = False
    mqtt_broker: str = "m1.tuyacn.com"
    mqtt_port: int = 1883
    mqtt_client_id: str = ""
    mqtt_username: str = ""
    mqtt_password: str = ""
    device_config_path: str = "config/tuya_device_config.json"


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
    hot_update_interval: int = 300
    model_registry_url: str = ""


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
    """RAG 引擎配置"""
    qdrant_path: str = "data/qdrant"
    collection_name: str = "railway_knowledge"
    embedding_dim: int = 768
    top_k: int = 5
    score_threshold: float = 0.5
    sync_interval: int = 3600
    saas_dify_url: str = "http://saas.local:8000/api/v1/rag/datasets"
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


class NetworkConfig(BaseModel):
    """网络/5G 配置"""
    modem_port: str = "/dev/ttyUSB2"
    modem_baud: int = 115200
    apn: str = "cmnet"
    check_interval: int = 30


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
    saas: SaasConfig = Field(default_factory=SaasConfig)
    tuya: TuyaConfig = Field(default_factory=TuyaConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    rag: RagConfig = Field(default_factory=RagConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    charging: ChargingConfig = Field(default_factory=ChargingConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)

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
