"""tradingd 进程入口（S-01c）：唯一下单、唯一写账本的进程。

启动顺序（失败即关闭）：
1. 单写者锁（账本旁的 ledger.db.lock）：等几秒仍被占 → EXIT_BUSY，守护进程退避后再试；
2. 打开账本：未迁移或结构不完整 → 拒绝启动；
3. 构造券商适配器（IBKR 适配器在 B-03 才有，之前生产入口一律拒绝启动，不拿模拟券商顶替）；
4. 装配交易核心（server.risk.assembly），连接券商，先报告各组件健康，再 core.start()；
   启动校验失败（账户不符、持久化 HALT 下的拒绝等）→ 拒绝启动，不被守护进程反复拉起；
5. 开两个进程间端点：意图（只接 workerd 账户）、命令（只接 apid 账户）；
6. 常驻：处理券商回报流、定时写心跳（给 guardd）。
   回报流断开或任何常驻任务出错 → 以崩溃退出（退出码 1），由守护进程退避重启；
   重启后照样先查券商、再对账（S-01a）。
例行评估、盘中 / 日终对账的调度在 S-01d。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from server.app.common import LOCK_WAIT_SECONDS, AdapterNotAvailableError, acquire_lock
from server.app.supervisor import EXIT_BUSY, EXIT_OK, EXIT_REFUSED
from server.broker.base import BrokerUnavailableError
from server.guard.heartbeat import HeartbeatFile
from server.ipc.protocol import IpcServer, Role
from server.ipc.transport import Endpoints, TransportError, serve_role
from server.platforms import Listener, LocalChannel, PlatformError
from server.platforms.files import ensure_private_dir
from server.risk.assembly import Assembled, CoreInputs, build_trading_core
from server.risk.mode_manager import ModeError
from server.risk.run_state import RunStateMachine
from server.storage.errors import StorageError
from server.storage.ledger import LedgerRepository, open_ledger
from server.storage.logs import log_event

log = logging.getLogger(__name__)

EXIT_CRASH = 1
HEARTBEAT_FILE = "tradingd.heartbeat"
PROBE_TIMEOUT = 5.0


def ledger_lock(ledger_path: Path) -> Path:
    """单写者锁放在账本旁边：不管配置里的运行目录是什么，同一个账本只能有一个 tradingd。"""
    resolved = ledger_path.resolve()  # 经符号链接访问也落到同一把锁
    return resolved.with_name(resolved.name + ".lock")


def production_broker() -> Any:
    raise AdapterNotAvailableError("IBKR 券商适配器尚未实现（B-03），tradingd 拒绝启动")


@dataclass(frozen=True)
class TradingdSettings:
    runtime_dir: Path
    ledger_path: Path
    allowed: Mapping[Role, frozenset[str]]  # 每个端点允许连接的系统账户
    heartbeat_seconds: float = 15.0
    instance: str = "qts"
    lock_wait_seconds: float = LOCK_WAIT_SECONDS


@dataclass(frozen=True)
class TradingdDeps:
    channel: LocalChannel
    inputs: Callable[[], CoreInputs]
    broker: Callable[[], Any] = production_broker
    report_health: Callable[[RunStateMachine, Any], None] = lambda _rs, _b: None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


async def run_tradingd(settings: TradingdSettings, deps: TradingdDeps, stop: asyncio.Event) -> int:
    """跑到 stop 被置位（EXIT_OK）或出错（EXIT_REFUSED / EXIT_BUSY / EXIT_CRASH）。"""
    if set(settings.allowed) != set(Role):
        log_event(log, logging.ERROR, "tradingd 配置缺少端点的允许账户")
        return EXIT_REFUSED
    try:
        lock = await acquire_lock(
            ledger_lock(settings.ledger_path), wait=settings.lock_wait_seconds
        )
    except PlatformError as exc:
        log_event(log, logging.ERROR, "tradingd 单实例锁不可用，拒绝启动", reason=str(exc))
        return EXIT_REFUSED
    if lock is None:  # 另一个 tradingd 还在（或刚崩溃、锁还没释放）：退避后再试
        return EXIT_BUSY
    try:
        return await _run_locked(settings, deps, stop)
    finally:
        lock.release()


async def _run_locked(settings: TradingdSettings, deps: TradingdDeps, stop: asyncio.Event) -> int:
    try:
        repo = LedgerRepository(open_ledger(settings.ledger_path))
        broker = deps.broker()
        assembled = build_trading_core(repo, broker, deps.inputs())
    except (StorageError, AdapterNotAvailableError, PlatformError, ValueError) as exc:
        log_event(log, logging.ERROR, "tradingd 装配失败，拒绝启动", error=type(exc).__name__,
                  reason=str(exc))  # fmt: skip
        return EXIT_REFUSED
    try:
        await broker.connect()
    except Exception as exc:  # 连不上券商：可能是暂时的，按崩溃退出由守护进程退避重试
        log_event(log, logging.ERROR, "tradingd 连接券商失败", error=type(exc).__name__)
        return EXIT_CRASH
    try:
        try:
            deps.report_health(assembled.run_state, broker)
            await assembled.core.start()
        except ModeError as exc:  # 启动校验不通过：重启也没用
            log_event(log, logging.ERROR, "tradingd 启动校验失败，拒绝启动", reason=str(exc))
            return EXIT_REFUSED
        except Exception as exc:
            log_event(log, logging.ERROR, "tradingd 启动出错", error=type(exc).__name__)
            return EXIT_CRASH
        return await _serve(settings, deps, assembled, broker, stop)
    finally:
        with contextlib.suppress(Exception):
            await broker.disconnect()


async def _serve(
    settings: TradingdSettings,
    deps: TradingdDeps,
    assembled: Assembled,
    broker: Any,
    stop: asyncio.Event,
) -> int:
    try:  # 套接字（Linux）与心跳文件都放在运行目录：先建好并收紧权限（父目录可能不存在）
        ensure_private_dir(settings.runtime_dir)
    except PlatformError as exc:
        log_event(log, logging.ERROR, "tradingd 运行目录不可用", reason=str(exc))
        return EXIT_CRASH
    endpoints = Endpoints(deps.channel, settings.runtime_dir, settings.instance)
    server = IpcServer(assembled.core)
    listeners: list[Listener] = []
    try:
        for role in Role:
            listeners.append(await serve_role(endpoints, role, settings.allowed[role], server))
    except TransportError as exc:
        log_event(log, logging.ERROR, "tradingd 端点开不起来", reason=str(exc))
        _close(listeners)
        return EXIT_CRASH
    heartbeat = HeartbeatFile(settings.runtime_dir / HEARTBEAT_FILE)

    async def beat() -> None:
        """心跳只在“券商连着、两个端点都能接受连接”时写：否则按崩溃退出，让 guardd 能介入。"""
        while True:
            if not broker.connected:
                raise BrokerUnavailableError("券商连接已断开")
            for role in Role:
                await _probe(deps.channel, endpoints.address(role))
            if not broker.connected:  # 自检期间断了线：不写这次心跳
                raise BrokerUnavailableError("券商连接已断开")
            try:
                heartbeat.beat(deps.clock())
            except PermissionError:  # Windows 上 guardd 恰好在读：跳过这一次，下次再写
                log_event(log, logging.WARNING, "心跳文件暂时写不进去，跳过一次")
            deps.report_health(assembled.run_state, broker)
            await asyncio.sleep(settings.heartbeat_seconds)

    async def events() -> None:
        await assembled.core.handle_events(broker.events())
        raise BrokerUnavailableError("券商回报流结束")

    tasks = [asyncio.create_task(beat()), asyncio.create_task(events())]
    stopper = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
        if stopper in done:
            log_event(log, logging.WARNING, "tradingd 收到停止请求")
            return EXIT_OK
        for task in done:
            failure = task.exception()
            log_event(log, logging.ERROR, "tradingd 常驻任务出错，按崩溃退出",
                      error=type(failure).__name__ if failure else "结束",
                      reason=str(failure) if failure else "")  # fmt: skip
        return EXIT_CRASH
    finally:
        for task in (*tasks, stopper):
            task.cancel()
        for task in (*tasks, stopper):
            with contextlib.suppress(BaseException):
                await task
        _close(listeners)


async def _probe(channel: LocalChannel, address: str) -> None:
    """自检端点还能接受连接（Windows 上 accept 循环出错会悄悄停止，S-01b 已知限制）。

    只建立连接、不发任何请求就关闭；对端会按账户策略处理（同账户放行，异账户直接断开），都不影响自检结论。
    """
    try:
        await asyncio.wait_for(_probe_once(channel, address), PROBE_TIMEOUT)  # 连接与关闭一起限时
    except (OSError, PlatformError, TimeoutError) as exc:
        raise ConnectionError(f"端点 {address} 不再接受连接") from exc


async def _probe_once(channel: LocalChannel, address: str) -> None:
    _reader, writer, identity = await channel.connect(address)
    try:
        if identity.pid != os.getpid():  # 端点被别的进程占用（冒名）：同样按失效处理
            raise ConnectionError(f"端点 {address} 的监听方不是本进程（{identity.describe()}）")
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


def _close(listeners: list[Listener]) -> None:
    for listener in listeners:
        with contextlib.suppress(Exception):
            listener.close()


__all__ = (
    "EXIT_CRASH",
    "HEARTBEAT_FILE",
    "TradingdDeps",
    "TradingdSettings",
    "ledger_lock",
    "production_broker",
    "run_tradingd",
)
