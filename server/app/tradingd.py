"""tradingd 进程入口（S-01c）：唯一下单、唯一写账本的进程。

启动顺序（失败即关闭）：
1. 单实例锁（运行目录下 tradingd.lock）：已有实例在跑 → 拒绝启动（EXIT_REFUSED）；
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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from server.app.supervisor import EXIT_OK, EXIT_REFUSED
from server.guard.heartbeat import HeartbeatFile
from server.ipc.protocol import IpcServer, Role
from server.ipc.transport import Endpoints, TransportError, serve_role
from server.platforms import Listener, LocalChannel, PlatformError
from server.platforms.files import InstanceLock
from server.risk.assembly import Assembled, CoreInputs, build_trading_core
from server.risk.mode_manager import ModeError
from server.risk.run_state import RunStateMachine
from server.storage.errors import StorageError
from server.storage.ledger import LedgerRepository, open_ledger
from server.storage.logs import log_event

log = logging.getLogger(__name__)

EXIT_CRASH = 1
HEARTBEAT_FILE = "tradingd.heartbeat"
LOCK_FILE = "tradingd.lock"


class BrokerUnavailableError(Exception):
    """券商适配器不能构造（例如 IBKR 适配器尚未实现）：拒绝启动。"""


def production_broker() -> Any:
    raise BrokerUnavailableError("IBKR 券商适配器尚未实现（B-03），tradingd 拒绝启动")


@dataclass(frozen=True)
class TradingdSettings:
    runtime_dir: Path
    ledger_path: Path
    allowed: Mapping[Role, frozenset[str]]  # 每个端点允许连接的系统账户
    heartbeat_seconds: float = 15.0
    instance: str = "qts"


@dataclass(frozen=True)
class TradingdDeps:
    channel: LocalChannel
    inputs: Callable[[], CoreInputs]
    broker: Callable[[], Any] = production_broker
    report_health: Callable[[RunStateMachine, Any], None] = lambda _rs, _b: None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


async def run_tradingd(settings: TradingdSettings, deps: TradingdDeps, stop: asyncio.Event) -> int:
    """跑到 stop 被置位（返回 EXIT_OK）或出错（EXIT_REFUSED / EXIT_CRASH）。"""
    if set(settings.allowed) != set(Role):
        log_event(log, logging.ERROR, "tradingd 配置缺少端点的允许账户")
        return EXIT_REFUSED
    lock = InstanceLock(settings.runtime_dir / LOCK_FILE)
    try:
        lock.acquire()
    except PlatformError as exc:
        log_event(log, logging.ERROR, "tradingd 单实例锁拿不到，拒绝启动", reason=str(exc))
        return EXIT_REFUSED
    try:
        return await _run_locked(settings, deps, stop)
    finally:
        lock.release()


async def _run_locked(settings: TradingdSettings, deps: TradingdDeps, stop: asyncio.Event) -> int:
    try:
        repo = LedgerRepository(open_ledger(settings.ledger_path))
        broker = deps.broker()
        assembled = build_trading_core(repo, broker, deps.inputs())
    except (StorageError, BrokerUnavailableError, PlatformError, ValueError) as exc:
        log_event(log, logging.ERROR, "tradingd 装配失败，拒绝启动", error=type(exc).__name__,
                  reason=str(exc))  # fmt: skip
        return EXIT_REFUSED
    try:
        await broker.connect()
    except Exception as exc:  # 连不上券商：可能是暂时的，按崩溃退出由守护进程退避重试
        log_event(log, logging.ERROR, "tradingd 连接券商失败", error=type(exc).__name__)
        return EXIT_CRASH
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


async def _serve(
    settings: TradingdSettings,
    deps: TradingdDeps,
    assembled: Assembled,
    broker: Any,
    stop: asyncio.Event,
) -> int:
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
        while True:
            heartbeat.beat(deps.clock())
            deps.report_health(assembled.run_state, broker)
            await asyncio.sleep(settings.heartbeat_seconds)

    async def events() -> None:
        await assembled.core.handle_events(broker.events())
        raise ConnectionError("券商回报流结束")

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
                      error=type(failure).__name__ if failure else "结束")  # fmt: skip
        return EXIT_CRASH
    finally:
        for task in (*tasks, stopper):
            task.cancel()
        for task in (*tasks, stopper):
            with contextlib.suppress(BaseException):
                await task
        _close(listeners)
        with contextlib.suppress(Exception):
            await broker.disconnect()


def _close(listeners: list[Listener]) -> None:
    for listener in listeners:
        with contextlib.suppress(Exception):
            listener.close()


__all__ = (
    "EXIT_CRASH",
    "HEARTBEAT_FILE",
    "LOCK_FILE",
    "BrokerUnavailableError",
    "TradingdDeps",
    "TradingdSettings",
    "production_broker",
    "run_tradingd",
)
