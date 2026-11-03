"""apid 进程入口（S-02）：认证、REST + WebSocket；命令经本机通道交给 tradingd（ADR-14）。

- 不连券商，不写账本：账本只用只读连接（mode=ro + query_only）。
- 令牌认证在 S-03 实现之前，生产入口拒绝启动（78），不提供“无认证”模式（失败即关闭）。
- S-03 加 TLS 之前只允许监听本机回环地址。
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from server.api.app import ApiDeps, create_app
from server.api.auth import Authenticator
from server.api.commands import CommandClient, IpcCommandGateway
from server.app.common import AdapterNotAvailableError
from server.app.supervisor import EXIT_OK, EXIT_REFUSED
from server.ipc.protocol import Role
from server.ipc.transport import Endpoints, connect_role
from server.platforms import LocalChannel
from server.storage.ledger import LedgerRepository, open_ledger
from server.storage.logs import log_event

log = logging.getLogger(__name__)


def production_authenticator() -> Authenticator:
    raise AdapterNotAvailableError("令牌认证尚未实现（S-03），apid 拒绝启动")


@dataclass(frozen=True)
class ApidSettings:
    runtime_dir: Path
    ledger_path: Path
    tradingd_account: str
    host: str = "127.0.0.1"
    port: int = 8730
    instance: str = "qts"


class ReadOnlyLedger:
    """只读账本：首次成功打开后复用；打开失败不缓存，下次请求再试（期间一律 503）。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._repo: LedgerRepository | None = None
        self._lock = threading.Lock()  # 同步路由在线程池里跑

    def __call__(self) -> LedgerRepository:
        with self._lock:
            if self._repo is None:
                self._repo = LedgerRepository(open_ledger(self._path, read_only=True))
            return self._repo


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def build_apid_app(
    settings: ApidSettings, channel: LocalChannel, authenticator: Authenticator
) -> FastAPI:
    endpoints = Endpoints(channel, settings.runtime_dir, settings.instance)

    async def connect() -> CommandClient:
        return await connect_role(endpoints, Role.APID, server_account=settings.tradingd_account)

    deps = ApiDeps(
        authenticator=authenticator,
        ledger=ReadOnlyLedger(settings.ledger_path),
        gateway=IpcCommandGateway(connect),
    )
    return create_app(deps)


async def run_apid(
    settings: ApidSettings,
    channel: LocalChannel,
    stop: asyncio.Event,
    *,
    authenticator: Callable[[], Authenticator] = production_authenticator,
) -> int:
    try:
        auth = authenticator()
    except AdapterNotAvailableError as exc:
        log_event(log, logging.ERROR, "apid 拒绝启动", reason=str(exc))
        return EXIT_REFUSED
    if not _loopback(settings.host):
        log_event(log, logging.ERROR, "S-03（TLS）之前 apid 只能监听本机回环地址，拒绝启动",
                  host=settings.host)  # fmt: skip
        return EXIT_REFUSED
    app = build_apid_app(settings, channel, auth)
    config = uvicorn.Config(
        app, host=settings.host, port=settings.port, lifespan="off", access_log=False,
        log_config=None, server_header=False,
    )  # fmt: skip
    server = uvicorn.Server(config)
    serving = asyncio.ensure_future(server.serve())
    stopping = asyncio.ensure_future(stop.wait())
    await asyncio.wait({serving, stopping}, return_when=asyncio.FIRST_COMPLETED)
    server.should_exit = True
    stopping.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await stopping
    await serving
    return EXIT_OK if stop.is_set() else EXIT_REFUSED


__all__ = (
    "ApidSettings",
    "ReadOnlyLedger",
    "build_apid_app",
    "production_authenticator",
    "run_apid",
)
