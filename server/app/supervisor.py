"""进程守护（S-01c）：拉起 tradingd / apid / workerd / guardd，崩溃后按指数退避自动重启。

- 退出码 0：正常停止，不重启。
- EXIT_REFUSED（78）：子进程自己判定不能启动（启动校验失败、配置错误、持久化 HALT 下的拒绝等），
  重启也没用：不重启并告警，等人处理。
- 其他退出码（含被杀）：崩溃，退避后重启。
- 退避：1 秒起每次翻倍，封顶 60 秒；连续稳定运行满 5 分钟后清零；
  连续崩溃达到 ALERT_AFTER 次记 ERROR 告警。
- 守护进程本身由操作系统的服务机制拉起（Windows 任务计划程序、Linux systemd；见 docs/runbooks/）。
- 只用 asyncio 的跨平台子进程接口，不判断操作系统。停止用 terminate()：Windows 上等同于强制结束，
  tradingd 按崩溃处理（重启后先查券商再对账，S-01a），所以不依赖优雅退出。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from server.storage.logs import log_event

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_REFUSED = 78  # sysexits.h 的 EX_CONFIG：不要重启
INITIAL_DELAY = 1.0
MAX_DELAY = 60.0
STABLE_AFTER = 300.0
ALERT_AFTER = 5
STOP_GRACE = 10.0


class Child(Protocol):
    async def wait(self) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


Spawn = Callable[[Sequence[str]], Awaitable[Child]]


@dataclass(frozen=True)
class ChildSpec:
    name: str
    argv: tuple[str, ...]


class Backoff:
    def __init__(
        self,
        initial: float = INITIAL_DELAY,
        maximum: float = MAX_DELAY,
        stable_after: float = STABLE_AFTER,
    ) -> None:
        if not 0 < initial <= maximum:
            raise ValueError("退避参数不合法")
        self._initial = initial
        self._maximum = maximum
        self._stable_after = stable_after
        self._next = initial
        self.failures = 0

    def after_crash(self, ran_for: float) -> float:
        """一次崩溃之后要等多久；运行够久的崩溃从头算。"""
        if ran_for >= self._stable_after:
            self._next = self._initial
            self.failures = 0
        delay = self._next
        self._next = min(self._next * 2, self._maximum)
        self.failures += 1
        return delay


async def _spawn(argv: Sequence[str]) -> Child:
    return await asyncio.create_subprocess_exec(*argv)


class Supervisor:
    def __init__(
        self,
        specs: Sequence[ChildSpec],
        *,
        spawn: Spawn = _spawn,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        backoff: Callable[[], Backoff] = Backoff,
    ) -> None:
        names = [s.name for s in specs]
        if not specs or len(set(names)) != len(names):
            raise ValueError("子进程清单不能为空，名字不能重复")
        self._specs = tuple(specs)
        self._spawn = spawn
        self._sleep = sleep
        self._clock = clock
        self._backoff = backoff
        self._running: dict[str, Child] = {}
        self._stopping = False
        self.outcomes: dict[str, str] = {}  # 每个子进程最终为什么不再重启

    async def run(self) -> dict[str, str]:
        """守护全部子进程，直到它们都不再需要重启（或 stop() 被调用）。"""
        await asyncio.gather(*(self._watch(spec) for spec in self._specs))
        return dict(self.outcomes)

    async def _watch(self, spec: ChildSpec) -> None:
        backoff = self._backoff()
        while not self.stopping():
            started = self._clock()
            try:
                child = await self._spawn(spec.argv)
            except OSError as exc:  # 可执行文件不在等：按崩溃处理，退避后再试
                code: int | None = None
                log_event(log, logging.ERROR, "子进程启动失败", child=spec.name,
                          error=type(exc).__name__)  # fmt: skip
            else:
                self._running[spec.name] = child
                code = await child.wait()
                self._running.pop(spec.name, None)
            if self.stopping():
                self.outcomes[spec.name] = "stopped"
                return
            if code == EXIT_OK:
                log_event(log, logging.WARNING, "子进程正常退出，不再重启", child=spec.name)
                self.outcomes[spec.name] = "exited"
                return
            if code == EXIT_REFUSED:
                log_event(log, logging.ERROR, "子进程拒绝启动，不再重启，需要人工处理",
                          child=spec.name)  # fmt: skip
                self.outcomes[spec.name] = "refused"
                return
            delay = backoff.after_crash(self._clock() - started)
            level = logging.ERROR if backoff.failures >= ALERT_AFTER else logging.WARNING
            log_event(log, level, "子进程崩溃，退避后重启", child=spec.name, code=code,
                      delay=delay, failures=backoff.failures)  # fmt: skip
            await self._sleep(delay)
        self.outcomes[spec.name] = "stopped"

    def stopping(self) -> bool:
        return self._stopping

    async def stop(self) -> None:
        """停止守护：先 terminate，宽限期后仍未退出的 kill。"""
        self._stopping = True
        children = list(self._running.values())
        for child in children:
            with contextlib.suppress(ProcessLookupError):
                child.terminate()
        for child in children:
            try:
                await asyncio.wait_for(child.wait(), STOP_GRACE)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    child.kill()
                await child.wait()


__all__ = (
    "ALERT_AFTER",
    "EXIT_OK",
    "EXIT_REFUSED",
    "Backoff",
    "ChildSpec",
    "Supervisor",
)
