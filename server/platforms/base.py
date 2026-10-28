"""平台适配层接口（S-01b 起）：所有与操作系统相关的能力都经这里，业务代码不得直接判断操作系统。

目前包含本机进程间通道（Unix 域套接字 / Windows 命名管道）与对端账户核验；
系统服务安装、文件权限、路径约定在 S-01c 起按同一方式加入。

身份用字符串表示，各平台自己的格式：Linux 为 "uid:<n>"，Windows 为账户 SID（"S-1-5-…"）。
上层只做“是否在允许集合里”的比较，不解析格式。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class PlatformError(Exception):
    """平台能力无法安全提供（未实现、调用失败、核验不了）。一律按失败即关闭处理。"""


class PlatformNotImplementedError(PlatformError):
    """这个平台的实现还没做（例如 macOS）：明确报错，不退化为不核验身份的通道。"""


@dataclass(frozen=True)
class PeerIdentity:
    """内核 / 系统给出的对端账户与进程号。"""

    account: str
    pid: int

    def describe(self) -> str:
        return f"pid={self.pid} account={self.account}"


PeerHandler = Callable[[asyncio.StreamReader, asyncio.StreamWriter, PeerIdentity], Awaitable[None]]


class Listener(Protocol):
    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...


class LocalChannel(Protocol):
    """本机进程间通道：只在本机可达，连接时由系统给出对端账户。"""

    def current_account(self) -> str:
        """本进程运行所用的账户（与 PeerIdentity.account 同一格式）。"""
        ...

    def address(self, name: str, runtime_dir: Path) -> str:
        """端点地址：runtime_dir 是 tradingd 专属运行目录（Unix 用来放套接字文件）。"""
        ...

    async def serve(self, address: str, on_peer: PeerHandler) -> Listener:
        """监听端点；每个连接先取得对端身份再交给 on_peer。端点已被占用时抛 PlatformError。"""
        ...

    async def connect(
        self, address: str
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, PeerIdentity]:
        """连接端点，并返回服务端（监听方）的身份，供调用方核验。"""
        ...


_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")


def check_endpoint_name(name: str) -> str:
    """端点名各平台同一规则：1–64 个小写字母、数字或连字符（不含路径分隔符、点、空白）。"""
    if not isinstance(name, str) or not 0 < len(name) <= 64 or not set(name) <= _NAME_CHARS:
        raise PlatformError(f"端点名不合法：{name!r}")
    return name


class UnavailableChannel:
    """不可用的通道：每个方法都明确报错（失败即关闭，不静默降级）。"""

    reason = "本平台没有这个通道实现"
    error: type[PlatformError] = PlatformError

    def current_account(self) -> str:
        raise self.error(self.reason)

    def address(self, name: str, runtime_dir: Path) -> str:
        raise self.error(self.reason)

    async def serve(self, address: str, on_peer: PeerHandler) -> Listener:
        raise self.error(self.reason)

    async def connect(
        self, address: str
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, PeerIdentity]:
        raise self.error(self.reason)


__all__ = (
    "Listener",
    "LocalChannel",
    "PeerHandler",
    "PeerIdentity",
    "PlatformError",
    "PlatformNotImplementedError",
    "UnavailableChannel",
    "check_endpoint_name",
)
