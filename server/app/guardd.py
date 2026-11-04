"""guardd 进程入口（S-01c，V4.1 7.6）：看门狗，tradingd 心跳丢失时逐单撤销未成交的开仓单。

- 单实例锁 guardd.lock；账本只读打开（guardd 不写账本）；
  独立的券商连接（独立 clientId，跨 clientId 撤单的可行性在 B-03 实测）。
- 券商适配器不可用（B-03 之前）→ 拒绝启动；连不上券商、连接中断（含契约里的
  BrokerUnavailableError 与 connected 变为 False）→ 以崩溃退出，由守护进程退避重启。
- 单实例锁等几秒仍被占 → EXIT_BUSY，守护进程退避后再试（不会永久放弃）。
- 每轮检查的判断与撤单规则见 server.guard.watchdog；单轮出错只记日志，下一轮再查。
- 告警（S-07）：心跳丢失、撤单失败按 P0，券商连接中断按 P1，交给 AlertHub；每轮驱动推送与重复提醒。
  手机渠道（Telegram）配置之前 AlertHub 只写日志（channel_ok=False）；外部心跳配置后每轮 tick。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from server.app.common import LOCK_WAIT_SECONDS, AdapterNotAvailableError, acquire_lock
from server.app.supervisor import EXIT_BUSY, EXIT_OK, EXIT_REFUSED
from server.app.tradingd import EXIT_CRASH, HEARTBEAT_FILE
from server.broker.base import BrokerUnavailableError
from server.guard.alerts import Alert, AlertHub, Level
from server.guard.heartbeat import HeartbeatFile
from server.guard.heartbeat_ping import HeartbeatPinger
from server.guard.watchdog import HEARTBEAT_TIMEOUT, GuardReport, GuardWatchdog
from server.platforms import PlatformError
from server.storage.errors import StorageError
from server.storage.ledger import LedgerRepository, open_ledger
from server.storage.logs import log_event

log = logging.getLogger(__name__)

LOCK_FILE = "guardd.lock"
HEARTBEAT_LOST = "tradingd_heartbeat_lost"
CANCEL_FAILED = "guardd_cancel_failed"
CHECK_FAILING = "guardd_check_failing"
CHECK_FAILURES_ALERT = 3  # 连续这么多轮检查出错即报 P0
DRIVE_SECONDS = 1.0
DRIVE_STEP_TIMEOUT = 30.0


def production_guard_broker() -> Any:
    raise AdapterNotAvailableError("IBKR 券商适配器尚未实现（B-03），guardd 拒绝启动")


@dataclass(frozen=True)
class GuarddSettings:
    runtime_dir: Path
    ledger_path: Path
    check_seconds: float = 15.0
    timeout: timedelta = HEARTBEAT_TIMEOUT
    lock_wait_seconds: float = LOCK_WAIT_SECONDS


@dataclass(frozen=True)
class GuarddDeps:
    broker: Callable[[], Any] = production_guard_broker
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    alerts: AlertHub | None = None  # None：只写日志的告警（手机渠道未配置）
    pinger: HeartbeatPinger | None = None


async def run_guardd(settings: GuarddSettings, deps: GuarddDeps, stop: asyncio.Event) -> int:
    try:
        path = settings.runtime_dir / LOCK_FILE
        lock = await acquire_lock(path, wait=settings.lock_wait_seconds)
    except PlatformError as exc:
        log_event(log, logging.ERROR, "guardd 单实例锁不可用，拒绝启动", reason=str(exc))
        return EXIT_REFUSED
    if lock is None:
        return EXIT_BUSY
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
    except (StorageError, AdapterNotAvailableError, ValueError) as exc:
        log_event(log, logging.ERROR, "guardd 装配失败，拒绝启动", error=type(exc).__name__,
                  reason=str(exc))  # fmt: skip
        return EXIT_REFUSED
    alerts = deps.alerts or AlertHub(None, clock=deps.clock)
    if deps.pinger is not None:  # 先报一次平安再连券商：断线崩溃循环里外部服务也能收到 ping
        with contextlib.suppress(Exception):
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(loop.run_in_executor(_IO, deps.pinger.tick), DRIVE_STEP_TIMEOUT)
    driver = asyncio.ensure_future(_drive(alerts, deps.pinger, stop))  # 连券商之前就开始外部心跳
    try:
        return await _watch(settings, broker, watchdog, alerts, stop)
    finally:
        driver.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await driver


async def _watch(
    settings: GuarddSettings,
    broker: Any,
    watchdog: GuardWatchdog,
    alerts: AlertHub,
    stop: asyncio.Event,
) -> int:
    try:
        await broker.connect()
    except Exception as exc:
        log_event(log, logging.ERROR, "guardd 连接券商失败", error=type(exc).__name__)
        _broker_alert(alerts, f"guardd 连接券商失败：{type(exc).__name__}")
        await _flush(alerts)
        return EXIT_CRASH
    errors = 0
    try:
        while not stop.is_set():
            try:
                if not broker.connected:  # 断线不会自己好：按崩溃退出，由守护进程重连
                    raise BrokerUnavailableError("guardd 的券商连接已断开")
                report = await watchdog.check()
                errors = 0
                alerts.resolve(CHECK_FAILING)
                _report_alerts(alerts, report, settings.timeout)
            except (BrokerUnavailableError, ConnectionError) as exc:
                log_event(log, logging.ERROR, "guardd 券商连接中断，按崩溃退出", reason=str(exc))
                _broker_alert(alerts, "guardd 的券商连接中断")
                await _flush(alerts)
                return EXIT_CRASH
            # 单轮出错：记日志，下一轮再查；连续出错报 P0（看门狗可能失效）
            except Exception as exc:
                errors += 1
                log_event(log, logging.ERROR, "guardd 本轮检查出错", error=type(exc).__name__)
                if errors >= CHECK_FAILURES_ALERT:
                    with contextlib.suppress(Exception):  # 告警出错不能冲出主循环
                        alerts.raise_alert(Alert(
                            Level.P0, "guard_check_failing",
                            {"count": str(errors), "error": type(exc).__name__},
                            key=CHECK_FAILING,
                        ))  # fmt: skip
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), settings.check_seconds)
        return EXIT_OK
    finally:
        with contextlib.suppress(Exception):
            await broker.disconnect()


def _report_alerts(alerts: AlertHub, report: GuardReport, timeout: timedelta) -> None:
    """心跳恢复即解除（确认只停止提醒，风险消失才关闭）；丢失期间每轮重复上报不会重复推送。"""
    if report.alive:
        alerts.resolve(HEARTBEAT_LOST)
        alerts.resolve(CANCEL_FAILED)
        return
    seconds = str(int(timeout.total_seconds()))
    alerts.raise_alert(Alert(Level.P0, "tradingd_heartbeat_lost", {"seconds": seconds},
                             key=HEARTBEAT_LOST))  # fmt: skip
    if report.failed:
        detail = f"guardd 撤单失败 {len(report.failed)} 笔，下一轮重试"
        alerts.raise_alert(Alert(Level.P0, "emergency_failed", {"detail": detail},
                                 key=CANCEL_FAILED))  # fmt: skip


def _broker_alert(alerts: AlertHub, detail: str) -> None:
    with contextlib.suppress(Exception):  # 告警本身出错不能掩盖原来的退出路径
        alerts.raise_alert(Alert(Level.P1, "broker_disconnected", {"detail": detail},
                                 key="guardd_broker_disconnected"))  # fmt: skip


# 告警与外部心跳专用线程（不占用 asyncio 默认线程池：那个池还负责券商重连时的 DNS 解析）
_IO = ThreadPoolExecutor(max_workers=2, thread_name_prefix="guardd-alerts")


async def _drive(alerts: AlertHub, pinger: HeartbeatPinger | None, stop: asyncio.Event) -> None:
    """后台每秒驱动外部心跳与推送。网络 I/O 放在专用线程里并有总时限：慢网络不拖住看门狗。

    - 心跳先于推送：即使进程很快因断线退出，也尽量先报一次平安；
    - 两步各自独立：一边出错不影响另一边；
    - 上一次还没结束（线程卡在网络上）就跳过这一轮，不会越积越多。
    """
    loop = asyncio.get_running_loop()
    running: dict[str, asyncio.Future[None]] = {}
    steps = (("ping", None if pinger is None else pinger.tick), ("pump", alerts.pump))
    while not stop.is_set():
        for name, step in steps:
            if step is None or (name in running and not running[name].done()):
                continue
            running[name] = loop.run_in_executor(_IO, step)
            try:
                await asyncio.wait_for(asyncio.shield(running[name]), DRIVE_STEP_TIMEOUT)
            except Exception as exc:
                log_event(log, logging.ERROR, "告警推送或外部心跳出错", step=name,
                          error=type(exc).__name__)  # fmt: skip
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), DRIVE_SECONDS)


async def _flush(alerts: AlertHub) -> None:
    """崩溃退出前尽量把告警推出去（有总时限，不会卡住退出）。"""
    with contextlib.suppress(Exception):
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(loop.run_in_executor(_IO, alerts.pump), DRIVE_STEP_TIMEOUT)


__all__ = ("LOCK_FILE", "GuarddDeps", "GuarddSettings", "production_guard_broker", "run_guardd")
