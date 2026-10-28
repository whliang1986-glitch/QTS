"""平台适配层（S-01b 起）：唯一允许按操作系统分支的地方。

业务代码只通过这里拿平台实现，不得自己判断操作系统（Dylan 2026-10-28：Windows 与 macOS 都要支持，
Linux 用于 CI）。不支持或尚未实现的平台明确报错，不静默降级。
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from server.platforms.base import (
    Listener,
    LocalChannel,
    PeerIdentity,
    PlatformError,
    PlatformNotImplementedError,
)
from server.platforms.linux import LinuxChannel
from server.platforms.macos import MacChannel
from server.platforms.windows import WindowsChannel

_CHANNELS: dict[str, Callable[[], LocalChannel]] = {
    "win32": WindowsChannel,
    "darwin": MacChannel,
    "linux": LinuxChannel,
}


def local_channel() -> LocalChannel:
    """当前操作系统的本机进程间通道实现。"""
    factory = _CHANNELS.get(sys.platform)
    if factory is None:
        raise PlatformNotImplementedError(f"不支持的操作系统：{sys.platform}")
    return factory()


__all__ = (
    "Listener",
    "LocalChannel",
    "PeerIdentity",
    "PlatformError",
    "PlatformNotImplementedError",
    "local_channel",
)
