"""S-01c：进程守护（崩溃退避重启、拒绝启动不重启、正常退出不重启、停止）。"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from server.app.supervisor import (
    ALERT_AFTER,
    EXIT_OK,
    EXIT_REFUSED,
    Backoff,
    ChildSpec,
    Supervisor,
)


class FakeChild:
    def __init__(self, code: int | None) -> None:
        self.code = code
        self.done = asyncio.Event()
        self.terminated = False
        if code is not None:
            self.done.set()

    async def wait(self) -> int:
        await self.done.wait()
        assert self.code is not None
        return self.code

    def terminate(self) -> None:
        self.terminated = True
        self.code = 1
        self.done.set()

    def kill(self) -> None:
        self.terminate()


class Script:
    """按顺序给出每次启动的退出码；None 表示一直运行直到被停止。"""

    def __init__(self, codes: list[int | None]) -> None:
        self.codes = codes
        self.spawned: list[FakeChild] = []
        self.sleeps: list[float] = []
        self.now = 0.0

    async def spawn(self, argv: Sequence[str]) -> Any:
        child = FakeChild(self.codes.pop(0) if self.codes else EXIT_OK)
        self.spawned.append(child)
        return child

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay

    def clock(self) -> float:
        return self.now


def supervisor(script: Script, *names: str) -> Supervisor:
    specs = [ChildSpec(n, ("python", "-m", n)) for n in names or ("tradingd",)]
    return Supervisor(specs, spawn=script.spawn, sleep=script.sleep, clock=script.clock)


@pytest.mark.asyncio
async def test_crashes_restart_with_exponential_backoff() -> None:
    script = Script([1, 1, 1, 1, 1, 1, 1, 1, EXIT_OK])
    outcomes = await supervisor(script).run()
    assert outcomes == {"tradingd": "exited"}
    assert script.sleeps == [1, 2, 4, 8, 16, 32, 60, 60]  # 封顶 60 秒
    assert len(script.spawned) == 9


@pytest.mark.asyncio
async def test_refused_start_is_not_restarted() -> None:
    script = Script([EXIT_REFUSED])
    assert await supervisor(script).run() == {"tradingd": "refused"}
    assert len(script.spawned) == 1 and script.sleeps == []


@pytest.mark.asyncio
async def test_clean_exit_is_not_restarted() -> None:
    script = Script([EXIT_OK])
    assert await supervisor(script).run() == {"tradingd": "exited"}
    assert script.sleeps == []


def test_backoff_resets_after_a_stable_run() -> None:
    backoff = Backoff()
    assert [backoff.after_crash(1) for _ in range(3)] == [1, 2, 4]
    assert backoff.after_crash(301) == 1  # 稳定运行 5 分钟以上再崩，从头算
    assert backoff.failures == 1


def test_alert_threshold_is_counted() -> None:
    backoff = Backoff()
    for _ in range(ALERT_AFTER):
        backoff.after_crash(0)
    assert backoff.failures == ALERT_AFTER


@pytest.mark.parametrize(("initial", "maximum"), [(0, 1), (-1, 1), (2, 1)])
def test_bad_backoff_is_refused(initial: float, maximum: float) -> None:
    with pytest.raises(ValueError, match="退避"):
        Backoff(initial, maximum)


@pytest.mark.parametrize("names", [(), ("a", "a")])
def test_bad_specs_are_refused(names: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="子进程清单"):
        Supervisor([ChildSpec(n, ("x",)) for n in names])


@pytest.mark.asyncio
async def test_spawn_failures_are_retried_with_backoff() -> None:
    attempts: list[int] = []

    async def failing(argv: Sequence[str]) -> Any:
        attempts.append(1)
        if len(attempts) < 3:
            raise FileNotFoundError("python")
        return FakeChild(EXIT_OK)

    script = Script([])
    sup = Supervisor(
        [ChildSpec("w", ("x",))], spawn=failing, sleep=script.sleep, clock=script.clock
    )
    assert await sup.run() == {"w": "exited"}
    assert script.sleeps == [1, 2]


@pytest.mark.asyncio
async def test_stop_terminates_running_children_and_stops_restarting() -> None:
    script = Script([None, None])
    sup = supervisor(script, "tradingd", "guardd")
    task = asyncio.create_task(sup.run())
    while len(script.spawned) < 2:
        await asyncio.sleep(0)
    await sup.stop()
    assert await asyncio.wait_for(task, 5) == {"tradingd": "stopped", "guardd": "stopped"}
    assert all(c.terminated for c in script.spawned)
    assert len(script.spawned) == 2  # 停止后没有再拉起


@pytest.mark.asyncio
async def test_children_are_watched_independently() -> None:
    """一个子进程拒绝启动，不影响另一个崩溃后重启。"""
    codes = {"tradingd": [EXIT_REFUSED], "guardd": [1, EXIT_OK]}
    spawned: list[str] = []

    async def spawn(argv: Sequence[str]) -> Any:
        name = argv[-1]
        spawned.append(name)
        return FakeChild(codes[name].pop(0))

    script = Script([])
    sup = Supervisor(
        [ChildSpec(n, ("python", "-m", n)) for n in codes],
        spawn=spawn, sleep=script.sleep, clock=script.clock,
    )  # fmt: skip
    assert await sup.run() == {"tradingd": "refused", "guardd": "exited"}
    assert spawned.count("guardd") == 2


@pytest.mark.asyncio
async def test_real_process_crash_is_restarted(tmp_path: Path) -> None:
    """真实子进程：第一次崩溃（退出码 3），第二次正常退出；守护进程拉起两次。"""
    marker = tmp_path / "runs.txt"
    code = (
        "import sys, pathlib\n"
        "p = pathlib.Path(sys.argv[1])\n"
        "n = len(p.read_text().splitlines()) if p.exists() else 0\n"
        "p.write_text('run' + chr(10) * (n + 1))\n"
        "sys.exit(3 if n == 0 else 0)\n"
    )
    sleeps: list[float] = []

    async def fast_sleep(delay: float) -> None:
        sleeps.append(delay)

    sup = Supervisor(
        [ChildSpec("child", (sys.executable, "-c", code, str(marker)))], sleep=fast_sleep
    )
    assert await asyncio.wait_for(sup.run(), 60) == {"child": "exited"}
    assert sleeps == [1]
    assert len(marker.read_text().splitlines()) == 2
