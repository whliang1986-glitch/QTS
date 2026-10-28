"""本机进程间通道（S-01b，ADR-14）：每个角色一个端点，连接时双向核验账户。与操作系统无关。

- 平台细节（Unix 域套接字 / Windows 命名管道、怎样取得对端账户）都在 server.platforms，
  这里不判断操作系统。
- 每个角色一个端点：意图端点只接 workerd，命令端点只接 apid；
  角色由连上的端点决定，不信任消息里的自报身份。
- 服务端：对端账户必须在该端点的允许集合里，否则立即断开。
- 客户端：服务端账户必须是配置的 tradingd 账户，否则断开（防止有人抢先占用端点冒充 tradingd）。
- 已知限制：同一个系统账户运行的进程彼此可读写内存，账户相同时的角色区分只能防程序错误，
  **不是安全边界**；真正隔离需要 apid / workerd 各用单独账户（允许集合本就按账户配置），
  上实盘前由 Dylan 决定。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from server.ipc.protocol import IpcClient, IpcServer, Peer, Role
from server.platforms import Listener, LocalChannel, PeerIdentity, PlatformError
from server.storage.logs import log_event

log = logging.getLogger(__name__)

ENDPOINTS = {Role.WORKERD: "intents", Role.APID: "commands"}
DEFAULT_INSTANCE = "qts"


class TransportError(Exception):
    """通道不能安全建立（允许集合不合法、服务端身份不符、平台核验失败）。"""


def _accounts(allowed: Iterable[str]) -> frozenset[str]:
    accounts = frozenset(allowed)
    if not accounts or not all(isinstance(a, str) and a.strip() for a in accounts):
        raise TransportError("允许连接的账户集合必须非空，且每项是非空字符串")
    return accounts


@dataclass(frozen=True)
class Endpoints:
    """一组端点：平台通道、tradingd 专属运行目录、实例名（测试里用不同实例名避免冲突）。"""

    channel: LocalChannel
    runtime_dir: Path
    instance: str = DEFAULT_INSTANCE

    def address(self, role: Role) -> str:
        """角色 → 端点地址（地址只由角色推出，调用方不能把命令端点错配给 workerd）。"""
        if not isinstance(role, Role):
            raise TransportError(f"未知角色：{role!r}")
        try:
            return self.channel.address(f"{self.instance}-{ENDPOINTS[role]}", self.runtime_dir)
        except PlatformError as exc:
            raise TransportError(str(exc)) from exc


async def serve_role(
    endpoints: Endpoints, role: Role, allowed: Iterable[str], server: IpcServer
) -> Listener:
    """tradingd 侧：为一个角色开它自己的端点。"""
    accounts = _accounts(allowed)
    address = endpoints.address(role)

    async def on_peer(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter, identity: PeerIdentity
    ) -> None:
        own = identity.pid == os.getpid()  # tradingd 自己的端点自检：只记 DEBUG，不刷告警
        if identity.account not in accounts:
            level = logging.DEBUG if own else logging.WARNING
            log_event(log, level, "未登记的账户尝试连接 tradingd，断开",
                      role=role.value, peer=identity.describe())  # fmt: skip
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            return
        log_event(
            log,
            logging.DEBUG if own else logging.INFO,
            "进程间连接已建立",
            role=role.value,
            peer=identity.describe(),
        )
        await server.serve(reader, writer, Peer(role, identity.describe()))

    try:
        return await endpoints.channel.serve(address, on_peer)
    except PlatformError as exc:
        raise TransportError(str(exc)) from exc


async def connect_role(endpoints: Endpoints, role: Role, *, server_account: str) -> IpcClient:
    """apid / workerd 侧：连接本角色的端点，并核验对端是 server_account 账户运行的进程。"""
    address = endpoints.address(role)
    try:
        reader, writer, identity = await endpoints.channel.connect(address)
    except PlatformError as exc:
        raise TransportError(str(exc)) from exc
    if identity.account != server_account:
        writer.close()
        raise TransportError(f"对端不是 tradingd 账户（{identity.describe()}），拒绝连接")
    return IpcClient(reader, writer)


__all__ = (
    "DEFAULT_INSTANCE",
    "ENDPOINTS",
    "Endpoints",
    "TransportError",
    "connect_role",
    "serve_role",
)
