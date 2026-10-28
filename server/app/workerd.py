"""workerd 进程入口（S-01c）：意图待发队列的唯一投递者（策略运行器在 T 组任务接入）。

- 单实例锁 workerd.lock 保证只有一个投递循环（S-01b：并发投递会让回执冲突）。
- 每轮：没有连接就连 tradingd 的意图端点（核验服务端是 tradingd 账户，带超时）；投递全部待发意图。
  连不上、tradingd 未就绪或投递中断都只是本轮放弃，下一轮重连重投（tradingd 按 intent_id 去重）。
- 意图超过 max_age 仍未投出即作废（S-01b，防追单）；日线策略默认几分钟。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from server.app.supervisor import EXIT_OK, EXIT_REFUSED
from server.ipc.outbox import Outbox, OutboxError, deliver_pending
from server.ipc.protocol import IpcClient, Role
from server.ipc.transport import Endpoints, TransportError, connect_role
from server.platforms import LocalChannel, PlatformError
from server.platforms.files import InstanceLock
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
    instance: str = "qts"


async def run_workerd(settings: WorkerdSettings, channel: LocalChannel, stop: asyncio.Event) -> int:
    lock = InstanceLock(settings.runtime_dir / LOCK_FILE)
    try:
        lock.acquire()
    except PlatformError as exc:
        log_event(log, logging.ERROR, "workerd 单实例锁拿不到，拒绝启动", reason=str(exc))
        return EXIT_REFUSED
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
            if client is None:
                client = await _connect(endpoints, settings)
            if client is not None:
                report = await deliver_pending(outbox, client, max_age=settings.max_age)
                if report.remaining:  # 中途失败：连接可能已作废，下一轮重连
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
