"""进程入口共用的小工具（S-01c）。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from server.platforms.files import InstanceLock, InstanceLockedError
from server.storage.logs import log_event

log = logging.getLogger(__name__)

LOCK_WAIT_SECONDS = 5.0


class AdapterNotAvailableError(Exception):
    """券商适配器或数据源还不能构造（例如 IBKR 适配器尚未实现）：拒绝启动。

    与 server.broker.base.BrokerUnavailableError（运行中断线）不是一回事，名字刻意区分。
    """


async def acquire_lock(path: Path, *, wait: float = LOCK_WAIT_SECONDS) -> InstanceLock | None:
    """拿单实例锁；被占时最多等 wait 秒（上一个实例刚崩溃、操作系统还没释放句柄的情形）。

    等不到返回 None：调用方以 EXIT_BUSY 退出，由守护进程退避后再试，而不是永久放弃。
    其他平台错误（例如 macOS 尚未实现）照常抛出。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    while True:
        try:
            return InstanceLock(path).acquire()
        except InstanceLockedError:
            if loop.time() >= deadline:
                log_event(log, logging.ERROR, "单实例锁一直被占用", lock=str(path))
                return None
            await asyncio.sleep(0.2)


__all__ = ("LOCK_WAIT_SECONDS", "AdapterNotAvailableError", "acquire_lock")
