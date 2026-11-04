"""apid 进程入口（S-02 / S-03）：认证、REST + WebSocket；命令经本机通道交给 tradingd（ADR-14）。

- 不连券商，不写账本：账本只用只读连接（mode=ro + query_only）。
- api.db 单写者：apid 运行期间持有 api.db 旁的锁；令牌管理命令（token 子命令）拿同一把锁，
  apid 在运行时它们拒绝执行。
- 认证用 api.db 的令牌（只存哈希、可吊销、有效期有上限）；没有任何“无认证”模式。
- 监听非回环地址必须配 TLS 证书与私钥（文件不存在即拒绝启动）；回环地址可以不用 TLS。
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import ssl
import threading
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from server.api.app import ApiDeps, create_app
from server.api.auth import Authenticator
from server.api.commands import CommandClient, IpcCommandGateway
from server.api.security import Audit, ConfirmationBook, TokenAdmin, TokenAuthenticator
from server.app.common import LOCK_WAIT_SECONDS, acquire_lock
from server.app.supervisor import EXIT_BUSY, EXIT_OK, EXIT_REFUSED
from server.ipc.protocol import Role
from server.ipc.transport import Endpoints, connect_role
from server.platforms import LocalChannel, PlatformError
from server.storage.alertsdb import AlertsReader
from server.storage.apidb import ApiDb, ApiDbError
from server.storage.ledger import LedgerRepository, open_ledger
from server.storage.logs import log_event

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApidSettings:
    runtime_dir: Path
    ledger_path: Path
    api_db: Path
    tradingd_account: str
    host: str = "127.0.0.1"
    port: int = 8730
    tls_cert: Path | None = None
    tls_key: Path | None = None
    confirmation_ttl: timedelta = timedelta(seconds=60)
    instance: str = "qts"
    lock_wait_seconds: float = LOCK_WAIT_SECONDS
    alerts_db: Path | None = None  # guardd 写的告警库，apid 只读；None：告警页显示无法获取


def api_db_lock(api_db: Path) -> Path:
    """api.db 的单写者锁（apid 与 token 子命令共用）。"""
    resolved = api_db.resolve()
    return resolved.with_name(resolved.name + ".lock")


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


def loopback(host: str) -> bool:
    """只认字面的回环 IP：主机名（含 localhost）可能被 hosts 文件改指向局域网地址。"""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_listen(settings: ApidSettings) -> str | None:
    """监听配置不安全时返回原因（拒绝启动）。"""
    tls = (settings.tls_cert, settings.tls_key)
    if (tls[0] is None) != (tls[1] is None):
        return "TLS 证书与私钥必须同时配置"
    if tls[0] is None:
        return None if loopback(settings.host) else "监听非回环地址必须配置 TLS 证书与私钥"
    missing = [str(p) for p in tls if p is not None and not p.is_file()]
    if missing:
        return f"TLS 文件不存在：{missing}"
    try:  # 先试加载一次：证书坏了在这里拒绝（78），而不是启动后崩溃、被守护进程反复拉起
        ssl.create_default_context(ssl.Purpose.CLIENT_AUTH).load_cert_chain(
            str(settings.tls_cert), str(settings.tls_key)
        )
    except (ssl.SSLError, OSError) as exc:
        return f"TLS 证书或私钥无法加载：{type(exc).__name__}"
    return None


def build_apid_app(  # noqa: PLR0913 - 装配入口，依赖显式传入
    settings: ApidSettings,
    channel: LocalChannel,
    authenticator: Authenticator,
    audit: Audit,
    tokens: TokenAdmin | None = None,
    *,
    db: ApiDb | None = None,  # 告警确认请求与审计查询（S-05a）
) -> FastAPI:
    endpoints = Endpoints(channel, settings.runtime_dir, settings.instance)

    async def connect() -> CommandClient:
        return await connect_role(endpoints, Role.APID, server_account=settings.tradingd_account)

    deps = ApiDeps(
        authenticator=authenticator,
        ledger=ReadOnlyLedger(settings.ledger_path),
        gateway=IpcCommandGateway(connect),
        audit=audit,
        tokens=tokens,
        alerts=None if settings.alerts_db is None else AlertsReader(settings.alerts_db),
        acks=db,
        audit_log=db,
        confirmations=ConfirmationBook(ttl=settings.confirmation_ttl),
    )
    return create_app(deps)


async def run_apid(settings: ApidSettings, channel: LocalChannel, stop: asyncio.Event) -> int:
    problem = check_listen(settings)
    if problem is not None:
        log_event(log, logging.ERROR, "apid 监听配置不安全，拒绝启动", reason=problem)
        return EXIT_REFUSED
    try:
        lock = await acquire_lock(api_db_lock(settings.api_db), wait=settings.lock_wait_seconds)
    except PlatformError as exc:
        log_event(log, logging.ERROR, "apid 单写者锁不可用，拒绝启动", reason=str(exc))
        return EXIT_REFUSED
    if lock is None:
        return EXIT_BUSY
    try:
        try:
            db = ApiDb(settings.api_db)
        except ApiDbError as exc:
            log_event(log, logging.ERROR, "api.db 不可用，拒绝启动", reason=str(exc))
            return EXIT_REFUSED
        try:
            return await _serve(settings, channel, stop, db)
        finally:
            db.close()
    finally:
        lock.release()


def uvicorn_config(app: FastAPI, settings: ApidSettings) -> uvicorn.Config:
    """不信任任何代理转发头：限流与审计的来源只取 TCP 对端地址（不支持反向代理）。"""
    cert, key = settings.tls_cert, settings.tls_key
    return uvicorn.Config(
        app, host=settings.host, port=settings.port, lifespan="off", access_log=False,
        log_config=None, server_header=False, proxy_headers=False, forwarded_allow_ips="",
        ssl_certfile=None if cert is None else str(cert),
        ssl_keyfile=None if key is None else str(key),
    )  # fmt: skip


async def _serve(
    settings: ApidSettings, channel: LocalChannel, stop: asyncio.Event, db: ApiDb
) -> int:
    app = build_apid_app(settings, channel, TokenAuthenticator(db), db, db, db=db)
    server = uvicorn.Server(uvicorn_config(app, settings))
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
    "api_db_lock",
    "build_apid_app",
    "check_listen",
    "loopback",
    "run_apid",
    "uvicorn_config",
)
