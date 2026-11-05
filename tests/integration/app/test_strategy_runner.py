"""workerd 的策略调度（T-01 移交）：只在决策窗口内跑、每个交易日只跑一次（落盘，重启也不重跑）、
线程里只计算不碰 outbox、数据源出错或计算超时当天放弃。"""

from __future__ import annotations

import asyncio
import time as clock_time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from server.app.strategies import StrategyInputs
from server.app.strategy_runner import RunnerConfigError, RunnerSources, StrategyRunner
from server.app.supervisor import EXIT_OK, EXIT_REFUSED
from server.app.workerd import WorkerdSettings, run_workerd
from server.ipc.outbox import Outbox
from server.platforms import local_channel

from tests.integration.app.test_strategies import (
    ACCOUNT_ID,
    CALENDAR,
    NOW,
    SCHEDULE,
    SellSome,
    _lake,
)

KEY = "demo:v1:2026-10-23:SPY:SELL:REDUCE"


class Sources:
    def __init__(self) -> None:
        self.calls = 0
        self.fail: str | None = None
        self.delay = 0.0

    def positions(self) -> dict[str, Decimal]:
        self.calls += 1
        if self.delay:
            clock_time.sleep(self.delay)
        if self.fail == "positions":
            raise OSError("账本读不了")
        return {"SPY": Decimal(1000)}

    def account(self) -> str:
        if self.fail == "account":
            raise OSError("账户未同步")
        return "" if self.fail == "empty" else ACCOUNT_ID

    def bundle(self) -> RunnerSources:
        return RunnerSources(positions=self.positions, inputs=StrategyInputs,
                             blocked=frozenset, account_id=self.account)  # fmt: skip


def runner(lake: Any, sources: Sources, state: Path, **kw: Any) -> StrategyRunner:
    return StrategyRunner([SellSome()], lake=lake, calendar=CALENDAR, sources=sources.bundle(),
                          state_path=state, schedule=SCHEDULE, **kw)  # fmt: skip


@pytest.mark.asyncio
async def test_runs_once_per_session_and_survives_restart(tmp_path: Path) -> None:
    lake = await _lake(tmp_path)
    outbox = Outbox(tmp_path / "meta.db", clock=lambda: NOW)
    sources = Sources()
    state = tmp_path / "runner.json"
    r = runner(lake, sources, state)
    assert r.prepare(NOW - timedelta(minutes=10)) is None  # 还没到决策时刻
    assert sources.calls == 0 and not state.exists()  # 窗口外不读任何数据
    prepared = r.prepare(NOW)
    assert prepared is not None
    assert [i.intent_id for i in prepared.intents] == [KEY]
    assert outbox.pending() == []  # 第一步只收集，不碰 outbox
    report = r.commit(prepared, outbox)
    assert report.enqueued == (KEY,)
    assert r.prepare(NOW + timedelta(minutes=1)) is None  # 同一天不再跑
    restarted = runner(lake, sources, state)  # 进程重启：新实例、同一个状态文件
    assert restarted.prepare(NOW + timedelta(minutes=2)) is None
    assert sources.calls == 1
    outbox.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["account", "positions", "empty"])
async def test_source_failure_skips_the_day(tmp_path: Path, failure: str) -> None:
    lake = await _lake(tmp_path)
    sources = Sources()
    sources.fail = failure
    r = runner(lake, sources, tmp_path / "runner.json")
    prepared = r.prepare(NOW)
    assert prepared is not None and prepared.intents == [] and "*" in prepared.skipped
    sources.fail = None
    assert r.prepare(NOW + timedelta(minutes=1)) is None  # 当天不再补跑：等人处理


@pytest.mark.asyncio
async def test_unwritable_or_corrupt_state_refuses_to_run(tmp_path: Path) -> None:
    lake = await _lake(tmp_path)
    blocker = tmp_path / "file"
    blocker.write_text("x")
    sources = Sources()
    with pytest.raises(OSError):
        runner(lake, sources, blocker / "runner.json").prepare(NOW)  # 写不进标记：不运行
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    with pytest.raises(ValueError):
        runner(lake, sources, corrupt).prepare(NOW)  # 记录损坏：不当作“没跑过”
    assert sources.calls == 0


def test_time_budget_must_fit_in_intent_age(tmp_path: Path) -> None:
    r = runner(None, Sources(), tmp_path / "s.json")
    r.check_delivery(deliver_seconds=2.0, max_age=timedelta(minutes=5))  # 4 分 + 30 秒 + 2 秒
    with pytest.raises(RunnerConfigError, match="意图限龄"):
        r.check_delivery(deliver_seconds=2.0, max_age=timedelta(minutes=4, seconds=30))


def settings(tmp_path: Path, **kw: Any) -> WorkerdSettings:
    base: dict[str, Any] = {"runtime_dir": tmp_path / "run", "meta_path": tmp_path / "meta.db",
                            "tradingd_account": "x", "deliver_seconds": 0.05,
                            "connect_timeout": 0.2}  # fmt: skip
    return WorkerdSettings(**(base | kw))


@pytest.mark.asyncio
async def test_workerd_refuses_incompatible_runner(tmp_path: Path) -> None:
    r = runner(None, Sources(), tmp_path / "s.json")
    code = await run_workerd(settings(tmp_path, max_age=timedelta(minutes=3)), local_channel(),
                             asyncio.Event(), runner=r)  # fmt: skip
    assert code == EXIT_REFUSED


@pytest.mark.asyncio
async def test_workerd_runs_strategies_into_the_real_outbox(tmp_path: Path) -> None:
    """端到端：workerd + runner + 真实 outbox。tradingd 没启动，意图留在待发队列里。"""
    lake = await _lake(tmp_path)
    stop = asyncio.Event()
    r = runner(lake, Sources(), tmp_path / "runner.json")
    task = asyncio.create_task(run_workerd(settings(tmp_path), local_channel(), stop, runner=r,
                                           clock=lambda: NOW))  # fmt: skip
    probe = Outbox(tmp_path / "meta.db", clock=lambda: NOW)
    for _ in range(100):
        if probe.pending():
            break
        await asyncio.sleep(0.05)
    assert [i.intent_id for i in probe.pending()] == [KEY]
    stop.set()
    assert await asyncio.wait_for(task, 20) == EXIT_OK
    probe.close()


@pytest.mark.asyncio
async def test_workerd_gives_up_on_slow_compute_without_touching_outbox(tmp_path: Path) -> None:
    lake = await _lake(tmp_path)
    sources = Sources()
    sources.delay = 1.5
    r = runner(lake, sources, tmp_path / "runner.json", compute_timeout=timedelta(seconds=0.3))
    stop = asyncio.Event()
    task = asyncio.create_task(run_workerd(settings(tmp_path), local_channel(), stop, runner=r,
                                           clock=lambda: NOW))  # fmt: skip
    await asyncio.sleep(2.5)  # 慢线程结束后也不会写入 outbox
    stop.set()
    assert await asyncio.wait_for(task, 20) == EXIT_OK
    probe = Outbox(tmp_path / "meta.db", clock=lambda: NOW)
    assert probe.pending() == []
    assert sources.calls == 1  # 当天已标记，没有重跑
    probe.close()


@pytest.mark.asyncio
async def test_workerd_survives_runner_errors(tmp_path: Path) -> None:
    stop = asyncio.Event()
    seen: list[datetime] = []

    class Spy(StrategyRunner):
        def prepare(self, now: datetime) -> Any:
            seen.append(now)
            if len(seen) == 1:
                raise RuntimeError("策略缺陷")  # 出错不影响后面的轮次
            stop.set()
            return None

    spy = Spy([], lake=None, calendar=CALENDAR, sources=Sources().bundle(),  # type: ignore[arg-type]
              state_path=tmp_path / "s.json", schedule=SCHEDULE)  # fmt: skip
    code = await asyncio.wait_for(
        run_workerd(settings(tmp_path), local_channel(), stop, runner=spy,
                    clock=lambda: datetime(2026, 10, 23, 14, 0, tzinfo=UTC)), 20,
    )  # fmt: skip
    assert code == EXIT_OK
    assert len(seen) == 2
