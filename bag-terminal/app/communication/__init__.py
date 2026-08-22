"""挎包终端通信模块。

本模块负责挎包终端与外部系统的所有通信：
- 涂鸦云平台 MQTT 通信 (mqtt_client)
- 涂鸦 Linux SDK 网关 (tuya_gateway)
- SaaS 后端 REST API 客户端 (saas_api_client)
"""

from app.communication.mqtt_client import TuyaMqttClient
from app.communication.tuya_gateway import TuyaGatewayManager
from app.communication.saas_api_client import SaasApiClient

__all__ = [
    "TuyaMqttClient",
    "TuyaGatewayManager",
    "SaasApiClient",
]
