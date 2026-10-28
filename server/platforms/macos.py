"""macOS 实现：尚未实现（计划后段任务 P-MAC，见 docs/tasks/P-MAC.md）。

计划做法：Unix 域套接字 + LOCAL_PEERCRED / getpeereid 读对端 uid，launchd 安装服务。
在实现之前，任何调用都明确抛 PlatformNotImplementedError，不退化为不核验身份的通道。
"""

from __future__ import annotations

from server.platforms.base import PlatformError, PlatformNotImplementedError, UnavailableChannel


class MacChannel(UnavailableChannel):
    reason = "macOS 的本机通道与对端身份核验尚未实现（P-MAC）"
    error: type[PlatformError] = PlatformNotImplementedError


__all__ = ("MacChannel",)
