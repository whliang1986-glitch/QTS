"""Linux 实现：Unix 域套接字 + SO_PEERCRED（内核给出对端 pid、uid）。

- 套接字文件权限 0660；应放在 tradingd 专属运行目录（属主 tradingd、0750），
  不放 /tmp（S-01c 部署）。
- 绑定前只清理本账户遗留、且没有进程在监听的套接字文件；其他文件或正在使用的套接字一律拒绝。
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
from pathlib import Path

from server.platforms.base import (
    Listener,
    PeerHandler,
    PeerIdentity,
    PlatformError,
    UnavailableChannel,
    check_endpoint_name,
)

log = logging.getLogger(__name__)

SOCKET_MODE = 0o660
MAX_SOCKET_PATH = 108  # sockaddr_un.sun_path（含结尾 NUL）
_UCRED = struct.Struct("iII")  # struct ucred { pid_t pid; uid_t uid; gid_t gid; }，uid 无符号


def _account(uid: int) -> str:
    return f"uid:{uid}"


if sys.platform == "linux":

    def _peer(sock: socket.socket) -> PeerIdentity:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
        pid, uid, _gid = _UCRED.unpack(raw)
        return PeerIdentity(_account(uid), pid)

    def _client_identity(sock: socket.socket) -> PeerIdentity:
        """服务端看连进来的客户端。"""
        return _peer(sock)

    def _server_identity(sock: socket.socket) -> PeerIdentity:
        """客户端看它连上的服务端。"""
        return _peer(sock)

    def _listening(path: Path) -> bool:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(1)
        try:
            probe.connect(str(path))
        except OSError:
            return False
        else:
            return True
        finally:
            probe.close()

    def _prepare(path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode):
            raise PlatformError(f"{path} 已存在且不是套接字，拒绝覆盖")
        if info.st_uid != os.getuid():
            raise PlatformError(f"{path} 属于其他账户，拒绝覆盖")
        if _listening(path):
            raise PlatformError(f"{path} 上已有进程在监听，拒绝启动第二个实例")
        path.unlink()

    class LinuxChannel:
        def current_account(self) -> str:
            return _account(os.getuid())

        def address(self, name: str, runtime_dir: Path) -> str:
            path = str(runtime_dir / f"{check_endpoint_name(name)}.sock")
            if len(os.fsencode(path)) >= MAX_SOCKET_PATH:
                raise PlatformError(f"套接字路径过长（上限 {MAX_SOCKET_PATH - 1} 字节）：{path}")
            return path

        async def serve(self, address: str, on_peer: PeerHandler) -> Listener:
            path = Path(address)
            _prepare(path)

            async def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                try:
                    peer = _client_identity(writer.get_extra_info("socket"))
                except Exception as exc:  # 取不到凭据：按身份不明断开
                    log.warning("无法读取对端凭据，断开：%s", type(exc).__name__)
                    writer.close()
                    with contextlib.suppress(OSError):
                        await writer.wait_closed()
                    return
                await on_peer(reader, writer, peer)

            try:
                server = await asyncio.start_unix_server(connected, path=address)
            except (
                OSError
            ) as exc:  # 目录不存在、无权限、路径被占：失败即关闭，交给上层按平台错误处理
                raise PlatformError(f"无法在 {address} 上监听：{exc}") from exc
            try:
                os.chmod(path, SOCKET_MODE)
            except OSError as exc:  # 权限设不上：不留一个权限不明的套接字在监听
                server.close()
                raise PlatformError(f"无法设置 {path} 的权限") from exc
            return server

        async def connect(
            self, address: str
        ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, PeerIdentity]:
            reader, writer = await asyncio.open_unix_connection(address)
            try:
                peer = _server_identity(writer.get_extra_info("socket"))
            except Exception as exc:
                writer.close()
                raise PlatformError("无法读取服务端凭据") from exc
            return reader, writer, peer

else:

    class LinuxChannel(UnavailableChannel):  # type: ignore[no-redef,unused-ignore]
        """非 Linux 平台上不可用（平台选择在 server.platforms.local_channel）。"""

        reason = f"LinuxChannel 只能在 Linux 上使用（当前 {sys.platform}）"

        def __init__(self) -> None:
            raise PlatformError(self.reason)


__all__ = ("MAX_SOCKET_PATH", "SOCKET_MODE", "LinuxChannel")
