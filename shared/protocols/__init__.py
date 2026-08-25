"""
shared.protocols package initialization.

导出 MessageType 枚举和相关常量，供挎包终端和服务端代码直接使用。
"""

from __future__ import annotations

from .message_types import (
    EXPECTED_COUNT,
    MESSAGE_RANGES,
    MessageType,
)

__all__ = [
    "MessageType",
    "EXPECTED_COUNT",
    "MESSAGE_RANGES",
]

__version__ = "1.0.0"
