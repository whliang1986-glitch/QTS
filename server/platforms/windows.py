"""Windows 实现：命名管道 + 按进程令牌核验对端账户 SID。

- 端点：\\\\.\\pipe\\<name>。asyncio 的管道服务端对首个实例使用 FILE_FLAG_FIRST_PIPE_INSTANCE：
  同名管道已被别的进程创建时启动失败，防止抢先占用名字冒充 tradingd。
- 服务端：GetNamedPipeClientComputerNameW 必须以 ERROR_PIPE_LOCAL 失败（只接受本机客户端，
  远程经 SMB 连进来的一律拒绝）；GetNamedPipeClientProcessId 取客户端进程号，打开其令牌读账户 SID。
- 客户端：GetNamedPipeServerProcessId 取服务端进程号，同样读账户 SID，
  交调用方核验是否为 tradingd 账户。
- 任何一步失败都按身份不明断开（失败即关闭）。
- 已知限制：按进程号查令牌，进程号在极短时间内被复用的情形无法完全排除；同一 Windows 用户内的
  进程彼此可以读写内存，所以同账户内的角色区分只能防程序错误，不是安全边界（见 S-01b 任务单）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
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

PIPE_PREFIX = "\\\\.\\pipe\\"
ERROR_PIPE_LOCAL = 229

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _adv = ctypes.WinDLL("advapi32", use_last_error=True)

    _k32.GetNamedPipeClientProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    _k32.GetNamedPipeClientProcessId.restype = wintypes.BOOL
    _k32.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    _k32.GetNamedPipeServerProcessId.restype = wintypes.BOOL
    _k32.GetNamedPipeClientComputerNameW.argtypes = [
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.ULONG,
    ]  # fmt: skip
    _k32.GetNamedPipeClientComputerNameW.restype = wintypes.BOOL
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.LocalFree.argtypes = [wintypes.HLOCAL]
    _k32.LocalFree.restype = wintypes.HLOCAL
    _k32.GetCurrentProcessId.argtypes = []
    _k32.GetCurrentProcessId.restype = wintypes.DWORD
    _adv.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    ]  # fmt: skip
    _adv.OpenProcessToken.restype = wintypes.BOOL
    _adv.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]  # fmt: skip
    _adv.GetTokenInformation.restype = wintypes.BOOL
    _adv.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    _adv.ConvertSidToStringSidW.restype = wintypes.BOOL

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1

    def _fail(what: str) -> PlatformError:
        return PlatformError(f"{what} 失败（错误码 {ctypes.get_last_error()}）")

    def _account_of(pid: int) -> str:
        process = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not process:
            raise _fail("OpenProcess")
        try:
            token = wintypes.HANDLE()
            if not _adv.OpenProcessToken(process, _TOKEN_QUERY, ctypes.byref(token)):
                raise _fail("OpenProcessToken")
            try:
                size = wintypes.DWORD()
                _adv.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
                if size.value == 0:
                    raise _fail("GetTokenInformation(size)")
                buffer = ctypes.create_string_buffer(size.value)
                if not _adv.GetTokenInformation(
                    token, _TOKEN_USER, buffer, size, ctypes.byref(size)
                ):
                    raise _fail("GetTokenInformation")
                sid = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0]  # TOKEN_USER.User.Sid
                text = wintypes.LPWSTR()
                if not _adv.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                    raise _fail("ConvertSidToStringSidW")
                try:
                    value = text.value
                finally:
                    _k32.LocalFree(ctypes.cast(text, wintypes.HLOCAL))
                if not value:
                    raise PlatformError("账户 SID 为空")
                return value
            finally:
                _k32.CloseHandle(token)
        finally:
            _k32.CloseHandle(process)

    def _pipe_handle(writer: asyncio.StreamWriter) -> int:
        pipe = writer.get_extra_info("pipe")
        handle = getattr(pipe, "handle", None)
        if not isinstance(handle, int):
            raise PlatformError("取不到命名管道句柄")
        return handle

    def _require_local_client(handle: int) -> None:
        name = ctypes.create_unicode_buffer(256)
        if _k32.GetNamedPipeClientComputerNameW(handle, name, ctypes.sizeof(name)):
            raise PlatformError("拒绝远程计算机经命名管道连接")
        if ctypes.get_last_error() != ERROR_PIPE_LOCAL:
            raise _fail("GetNamedPipeClientComputerNameW")

    def _client_identity(handle: int) -> PeerIdentity:
        _require_local_client(handle)
        pid = wintypes.ULONG()
        if not _k32.GetNamedPipeClientProcessId(handle, ctypes.byref(pid)):
            raise _fail("GetNamedPipeClientProcessId")
        return PeerIdentity(_account_of(pid.value), pid.value)

    def _server_identity(handle: int) -> PeerIdentity:
        pid = wintypes.ULONG()
        if not _k32.GetNamedPipeServerProcessId(handle, ctypes.byref(pid)):
            raise _fail("GetNamedPipeServerProcessId")
        return PeerIdentity(_account_of(pid.value), pid.value)

    class _PipeListener:
        def __init__(self, servers: list[object]) -> None:
            self._servers = servers

        def close(self) -> None:
            for server in self._servers:
                server.close()  # type: ignore[attr-defined]

        async def wait_closed(self) -> None:
            await asyncio.sleep(0)  # 管道服务端关闭是同步的；让出一次以便回调收尾

    def _proactor() -> asyncio.ProactorEventLoop:
        loop = asyncio.get_running_loop()
        if not isinstance(loop, asyncio.ProactorEventLoop):
            raise PlatformError("命名管道需要 ProactorEventLoop")
        return loop

    class WindowsChannel:
        def current_account(self) -> str:
            return _account_of(_k32.GetCurrentProcessId())

        def address(self, name: str, runtime_dir: Path) -> str:
            del runtime_dir  # 命名管道不在文件系统里
            return PIPE_PREFIX + check_endpoint_name(name)

        async def serve(self, address: str, on_peer: PeerHandler) -> Listener:
            loop = _proactor()

            async def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                try:
                    peer = _client_identity(_pipe_handle(writer))
                except Exception as exc:  # 核验不了：按身份不明断开
                    log.warning("无法核验管道对端身份，断开：%s", exc)
                    writer.close()
                    with contextlib.suppress(OSError):
                        await writer.wait_closed()
                    return
                await on_peer(reader, writer, peer)

            def factory() -> asyncio.StreamReaderProtocol:
                return asyncio.StreamReaderProtocol(asyncio.StreamReader(), connected)

            try:
                servers = await loop.start_serving_pipe(factory, address)
            except OSError as exc:  # FIRST_PIPE_INSTANCE：同名管道已存在
                raise PlatformError(f"{address} 已被占用，拒绝启动") from exc
            return _PipeListener(list(servers))

        async def connect(
            self, address: str
        ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, PeerIdentity]:
            loop = _proactor()
            reader = asyncio.StreamReader()
            protocol = asyncio.StreamReaderProtocol(reader)
            transport, _ = await loop.create_pipe_connection(lambda: protocol, address)
            writer = asyncio.StreamWriter(transport, protocol, reader, loop)
            try:
                peer = _server_identity(_pipe_handle(writer))
            except Exception as exc:
                writer.close()
                raise PlatformError("无法核验管道服务端身份") from exc
            return reader, writer, peer

else:

    class WindowsChannel(UnavailableChannel):  # type: ignore[no-redef,unused-ignore]
        """非 Windows 平台上不可用（平台选择在 server.platforms.local_channel）。"""

        reason = f"WindowsChannel 只能在 Windows 上使用（当前 {sys.platform}）"

        def __init__(self) -> None:
            raise PlatformError(self.reason)


__all__ = ("PIPE_PREFIX", "WindowsChannel")
