"""workerd 进程入口（S-01c）：意图待发队列的唯一投递者，并在决策时刻运行策略（T-01 移交）。

- 单实例锁 workerd.lock 保证只有一个投递循环（S-01b：并发投递会让回执冲突）。
- 每轮：没有连接就连 tradingd 的意图端点（核验服务端是 tradingd 账户，带超时）；投递全部待发意图。
  连不上、tradingd 未就绪或投递中断都只是本轮放弃，下一轮重连重投（tradingd 按 intent_id 去重）。
- 意图超过 max_age 仍未投出即作废（S-01b，防追单）；日线策略默认几分钟。
- 策略调度（可选，server/app/strategy_runner.py）：每轮先在线程里调用一次调度，再投递。
  调度出错只记日志，不影响投递；outbox 本身出错记 ERROR 并跳过本轮调度。
  调度与投递参数不兼容（运行窗口加投递间隔不短于限龄）时拒绝启动。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from server.app.common import LOCK_WAIT_SECONDS, acquire_lock
from server.app.strategy_runner import RunnerConfigError, StrategyRunner
from server.app.supervisor import EXIT_BUSY, EXIT_OK, EXIT_REFUSED
from server.ipc.outbox import Outbox, OutboxError, deliver_pending
from server.ipc.protocol import IpcClient, Role
from server.ipc.transport import Endpoints, TransportError, connect_role
from server.platforms import LocalChannel, PlatformError
from server.storage.logs import log_event

log = logging.getLogger(__name__)

LOCK_FILE = "workerd.lock"


@dataclass(frozen=True)
class WorkerdSettings:
    runtime_dir: Path
    meta_path: Path
    tradingd_account: str
    max_age: timedelta = timedelta(minutes=5)
    deliver_seconds: float = 2.0
    connect_timeout: float = 5.0
    deliver_timeout: float = 30.0  # 一轮投递的上限：tradingd 卡住时放弃这轮，下一轮重连重投
    instance: str = "qts"
    lock_wait_seconds: float = LOCK_WAIT_SECONDS


async def run_workerd(
    settings: WorkerdSettings,
    channel: LocalChannel,
    stop: asyncio.Event,
    *,
    runner: StrategyRunner | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    if runner is not None and not _runner_compatible(runner, settings):
        return EXIT_REFUSED
    try:
        resolved = settings.meta_path.resolve()  # 锁跟着 meta.db 走：同一个队列只有一个投递者
        path = resolved.with_name(resolved.name + ".lock")
        lock = await acquire_lock(path, wait=settings.lock_wait_seconds)
    except PlatformError as exc:
        log_event(log, logging.ERROR, "workerd 单实例锁不可用，拒绝启动", reason=str(exc))
        return EXIT_REFUSED
    if lock is None:
        return EXIT_BUSY
    try:
        outbox = Outbox(settings.meta_path)
    except OutboxError as exc:
        log_event(log, logging.ERROR, "workerd 打不开 outbox，拒绝启动", reason=str(exc))
        lock.release()
        return EXIT_REFUSED
    endpoints = Endpoints(channel, settings.runtime_dir, settings.instance)
    client: IpcClient | None = None
    try:
        while not stop.is_set():
            if runner is not None:
                await _run_strategies(runner, outbox, clock)
            if client is None:
                client = await _connect(endpoints, settings)
            if client is not None:
                try:
                    report = await asyncio.wait_for(
                        deliver_pending(outbox, client, max_age=settings.max_age),
                        settings.deliver_timeout,
                    )
                    failed = report.remaining > 0  # 中途失败：连接可能已作废
                except TimeoutError:  # 请求被取消时客户端已自我作废（S-01b）
                    log_event(log, logging.WARNING, "workerd 本轮投递超时，下一轮重连")
                    failed = True
                if failed:
                    await client.close()
                    client = None
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), settings.deliver_seconds)
        return EXIT_OK
    finally:
        if client is not None:
            await client.close()
        outbox.close()
        lock.release()


def _runner_compatible(runner: StrategyRunner, settings: WorkerdSettings) -> bool:
    try:
        runner.check_delivery(deliver_seconds=settings.deliver_seconds, max_age=settings.max_age)
    except RunnerConfigError as exc:
        log_event(log, logging.ERROR, "策略调度参数不兼容，拒绝启动", reason=str(exc))
        return False
    return True


async def _run_strategies(
    runner: StrategyRunner, outbox: Outbox, clock: Callable[[], datetime]
) -> None:
    """在线程里跑一次调度（读数据湖、算策略都是阻塞的）；任何错误都不影响投递。"""
    try:
        await asyncio.to_thread(runner.run, clock(), outbox)
    except OutboxError as exc:
        log_event(log, logging.ERROR, "策略调度写 outbox 失败，本轮跳过", reason=str(exc))
    except Exception as exc:
        log_event(log, logging.ERROR, "策略调度意外出错", error=type(exc).__name__)


async def _connect(endpoints: Endpoints, settings: WorkerdSettings) -> IpcClient | None:
    try:
        return await asyncio.wait_for(
            connect_role(endpoints, Role.WORKERD, server_account=settings.tradingd_account),
            settings.connect_timeout,
        )
    except (OSError, TransportError, TimeoutError) as exc:
        log_event(log, logging.WARNING, "workerd 暂时连不上 tradingd", error=type(exc).__name__)
        return None


__all__ = ("LOCK_FILE", "WorkerdSettings", "run_workerd")
