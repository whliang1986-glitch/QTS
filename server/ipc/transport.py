"""本机进程间通道（S-01b，ADR-14）：Unix 域套接字 + 连接时核验对端进程身份。

- 只走本机套接字，不走 TCP，不对局域网暴露。
- 对端身份取自内核（Linux 的 SO_PEERCRED：pid、uid、gid），不信任消息里的自报身份；
  按 RolePolicy 把操作系统账户映射到角色（workerd / apid）。没登记的账户直接断开（失败即关闭）。
- 同一个操作系统账户不能同时登记为两个角色：否则 workerd 能冒充 apid 发命令。
  因此部署时 apid、workerd 必须以不同的服务账户运行（S-01c 的系统服务配置）。
- 双向核验：客户端连上后也读对端凭据，要求是配置的 tradingd 账户，
  防止有人抢先绑定路径冒充 tradingd。
- 套接字应放在 tradingd 专属目录（属主 tradingd、0750），不放 /tmp（S-01c 部署）。
- Windows 命名管道（按客户端进程号查令牌 SID）待服务器操作系统确定后实现；
  在此之前 Windows 上启动通道直接报错，不退化为不核验身份的通道。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import stat
import struct
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from server.ipc.protocol import IpcClient, IpcServer, Peer, Role
from server.storage.logs import log_event

log = logging.getLogger(__name__)

SOCKET_MODE = 0o660  # 属主与同组可连；组成员关系由部署配置（S-01c），身份仍按 uid 逐个核验
_UCRED = struct.Struct("iII")  # struct ucred { pid_t pid; uid_t uid; gid_t gid; }（uid 无符号）
# 目前只有 Linux 能从内核拿到对端凭据；其他平台（含 Windows）启动通道直接报错
SUPPORTED = hasattr(socket, "SO_PEERCRED") and hasattr(asyncio, "start_unix_server")


class TransportError(Exception):
    """通道无法安全建立（平台不支持、路径被占用、身份策略不合法）。"""


@dataclass(frozen=True)
class PeerCredentials:
    """内核给出的对端进程凭据。"""

    pid: int
    uid: int
    gid: int


class RolePolicy:
    """操作系统账户（uid）→ 角色。一个账户只能对应一个角色。"""

    def __init__(self, uids: Mapping[Role, int]) -> None:
        if not uids:
            raise TransportError("身份策略为空：没有任何进程能连接")
        seen: dict[int, Role] = {}
        for role, uid in uids.items():
            if not isinstance(role, Role) or type(uid) is not int or uid < 0:
                raise TransportError(f"身份策略不合法：{role!r} → {uid!r}")
            if uid in seen:
                raise TransportError(
                    f"{seen[uid].value} 与 {role.value} 不能用同一个操作系统账户运行（uid {uid}）"
                )
            seen[uid] = role
        self._by_uid = seen

    def identify(self, creds: PeerCredentials) -> Peer | None:
        role = self._by_uid.get(creds.uid)
        if role is None:
            return None
        return Peer(role, f"pid={creds.pid} uid={creds.uid}")


def peer_credentials(sock: socket.socket) -> PeerCredentials:
    """读内核记录的对端凭据；平台不支持时抛 TransportError。"""
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        raise TransportError(f"{sys.platform} 不支持 SO_PEERCRED，无法核验对端身份")
    raw = sock.getsockopt(socket.SOL_SOCKET, option, _UCRED.size)
    pid, uid, gid = _UCRED.unpack(raw)
    return PeerCredentials(pid, uid, gid)


def _prepare_path(path: Path) -> None:
    """绑定前清理：只删除遗留的套接字文件，其他类型的文件一律拒绝（防止误删或被抢占）。"""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode):
        raise TransportError(f"{path} 已存在且不是套接字，拒绝覆盖")
    if info.st_uid != getattr(os, "getuid", lambda: -1)():
        raise TransportError(f"{path} 属于其他账户，拒绝覆盖")
    if _listening(path):  # 另一个 tradingd 实例正在监听：不能抢占
        raise TransportError(f"{path} 上已有进程在监听，拒绝启动第二个实例")
    path.unlink()


def _listening(path: Path) -> bool:
    family = getattr(socket, "AF_UNIX", None)
    if family is None:
        return False
    probe = socket.socket(family, socket.SOCK_STREAM)
    probe.settimeout(1)
    try:
        probe.connect(str(path))
    except OSError:
        return False
    else:
        return True
    finally:
        probe.close()


async def serve_unix(path: Path, server: IpcServer, policy: RolePolicy) -> asyncio.Server:
    """tradingd 侧：在 path 上监听；每个连接先核验身份，再交给 IpcServer。"""
    if not SUPPORTED:
        raise TransportError(f"{sys.platform} 上的进程身份核验尚未实现，拒绝启动通道")
    _prepare_path(path)

    async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            creds = peer_credentials(writer.get_extra_info("socket"))
            peer = policy.identify(creds)
        except Exception as exc:  # 任何核验失败都按未登记处理
            log_event(log, logging.WARNING, "无法核验对端身份，断开", error=type(exc).__name__)
            peer = None
        if peer is None:
            log_event(log, logging.WARNING, "未登记的进程尝试连接 tradingd，断开")
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            return
        log_event(log, logging.INFO, "进程间连接已建立", role=peer.role.value, peer=peer.detail)
        await server.serve(reader, writer, peer)

    if sys.platform == "win32":  # 上面已拒绝；这里只为类型检查按平台分支
        raise TransportError("Windows 命名管道尚未实现")
    else:
        listener = await asyncio.start_unix_server(on_connect, path=str(path))
        os.chmod(path, SOCKET_MODE)
        return listener


async def connect_unix(path: Path, *, server_uid: int) -> IpcClient:
    """apid / workerd 侧：连接 tradingd，并核验对端是 server_uid 账户运行的进程。"""
    if not SUPPORTED:
        raise TransportError(f"{sys.platform} 上的进程间通道尚未实现")
    if sys.platform == "win32":
        raise TransportError("Windows 命名管道尚未实现")
    else:
        reader, writer = await asyncio.open_unix_connection(str(path))
        try:
            creds = peer_credentials(writer.get_extra_info("socket"))
        except Exception as exc:
            writer.close()
            raise TransportError("无法核验 tradingd 的身份") from exc
        if creds.uid != server_uid:
            writer.close()
            raise TransportError(f"对端不是 tradingd 账户（uid {creds.uid}），拒绝连接")
        return IpcClient(reader, writer)


__all__ = (
    "SOCKET_MODE",
    "SUPPORTED",
    "PeerCredentials",
    "RolePolicy",
    "TransportError",
    "connect_unix",
    "peer_credentials",
    "serve_unix",
)
