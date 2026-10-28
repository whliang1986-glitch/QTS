"""guardd 进程入口（S-01c，V4.1 7.6）：看门狗，tradingd 心跳丢失时逐单撤销未成交的开仓单。

- 单实例锁 guardd.lock；账本只读打开（guardd 不写账本）；
  独立的券商连接（独立 clientId，跨 clientId 撤单的可行性在 B-03 实测）。
- 券商适配器不可用（B-03 之前）→ 拒绝启动；连不上券商或连接中断 → 以崩溃退出，由守护进程退避重启。
- 每轮检查的判断与撤单规则见 server.guard.watchdog；单轮出错只记日志，下一轮再查。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from server.app.supervisor import EXIT_OK, EXIT_REFUSED
from server.app.tradingd import EXIT_CRASH, HEARTBEAT_FILE, BrokerUnavailableError
from server.guard.heartbeat import HeartbeatFile
from server.guard.watchdog import HEARTBEAT_TIMEOUT, GuardWatchdog
from server.platforms import PlatformError
from server.platforms.files import InstanceLock
from server.storage.errors import StorageError
from server.storage.ledger import LedgerRepository, open_ledger
from server.storage.logs import log_event

log = logging.getLogger(__name__)

LOCK_FILE = "guardd.lock"


def production_guard_broker() -> Any:
    raise BrokerUnavailableError("IBKR 券商适配器尚未实现（B-03），guardd 拒绝启动")


@dataclass(frozen=True)
class GuarddSettings:
    runtime_dir: Path
    ledger_path: Path
    check_seconds: float = 15.0
    timeout: timedelta = HEARTBEAT_TIMEOUT


@dataclass(frozen=True)
class GuarddDeps:
    broker: Callable[[], Any] = production_guard_broker
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


async def run_guardd(settings: GuarddSettings, deps: GuarddDeps, stop: asyncio.Event) -> int:
    lock = InstanceLock(settings.runtime_dir / LOCK_FILE)
    try:
        lock.acquire()
    except PlatformError as exc:
        log_event(log, logging.ERROR, "guardd 单实例锁拿不到，拒绝启动", reason=str(exc))
        return EXIT_REFUSED
    try:
        return await _run(settings, deps, stop)
    finally:
        lock.release()


async def _run(settings: GuarddSettings, deps: GuarddDeps, stop: asyncio.Event) -> int:
    try:
        repo = LedgerRepository(open_ledger(settings.ledger_path, read_only=True))
        broker = deps.broker()
        watchdog = GuardWatchdog(
            broker, repo, HeartbeatFile(settings.runtime_dir / HEARTBEAT_FILE),
            timeout=settings.timeout, clock=deps.clock,
        )  # fmt: skip
    except (StorageError, BrokerUnavailableError, ValueError) as exc:
        log_event(log, logging.ERROR, "guardd 装配失败，拒绝启动", error=type(exc).__name__,
                  reason=str(exc))  # fmt: skip
        return EXIT_REFUSED
    try:
        await broker.connect()
    except Exception as exc:
        log_event(log, logging.ERROR, "guardd 连接券商失败", error=type(exc).__name__)
        return EXIT_CRASH
    try:
        while not stop.is_set():
            try:
                await watchdog.check()
            except ConnectionError:
                log_event(log, logging.ERROR, "guardd 券商连接中断，按崩溃退出")
                return EXIT_CRASH
            except Exception as exc:  # 单轮出错：记日志，下一轮再查
                log_event(log, logging.ERROR, "guardd 本轮检查出错", error=type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), settings.check_seconds)
        return EXIT_OK
    finally:
        with contextlib.suppress(Exception):
            await broker.disconnect()


__all__ = ("LOCK_FILE", "GuarddDeps", "GuarddSettings", "production_guard_broker", "run_guardd")
