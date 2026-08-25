"""
BLE 网桥包 — Bleak Central 角色

子模块:
    message_protocol  — BLE NUS 二进制帧协议 (解析/CRC16/重组/ACK)
    nus_client         — NUS GATT 客户端 (订阅 TX 通知, RX 写入)
    ble_bridge         — BLE 网桥管理 (扫描/连接/重连, 最多 4 副并发)
"""

from app.ble.message_protocol import (
    MessageProtocol,
    FrameAssembler,
    AckTracker,
    ProtocolError,
    CRCError,
)
from app.ble.nus_client import NusClient
from app.ble.ble_bridge import BleBridge

__all__ = [
    "MessageProtocol",
    "FrameAssembler",
    "AckTracker",
    "ProtocolError",
    "CRCError",
    "NusClient",
    "BleBridge",
]
