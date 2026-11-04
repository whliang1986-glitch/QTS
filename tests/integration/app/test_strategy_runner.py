"""workerd 的策略调度（T-01 移交）：只在决策窗口内跑、每个交易日只跑一次、输入出错整轮跳过。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from server.app.strategies import StrategyInputs
from server.app.strategy_runner import RunnerConfigError, RunnerSources, StrategyRunner
from server.app.supervisor import EXIT_REFUSED
from server.app.workerd import WorkerdSettings, run_workerd
from server.ipc.outbox import Outbox

from tests.integration.app.test_strategies import (
    ACCOUNT_ID,
    CALENDAR,
    NOW,
    SCHEDULE,
    SellSome,
    _lake,
)


class Sources:
    def __init__(self) -> None:
        self.calls = 0
        self.fail: str | None = None

    def positions(self) -> dict[str, Decimal]:
        self.calls += 1
        if self.fail == "positions":
            raise OSError("账本读不了")
        return {"SPY": Decimal(1000)}

    def account(self) -> str:
        if self.fail == "account":
            raise OSError("账户未同步")
        return ACCOUNT_ID

    def bundle(self) -> RunnerSources:
        return RunnerSources(positions=self.positions, inputs=StrategyInputs,
                             blocked=frozenset, account_id=self.account)  # fmt: skip


def runner(lake: Any, sources: Sources) -> StrategyRunner:
    return StrategyRunner([SellSome()], lake=lake, calendar=CALENDAR, sources=sources.bundle(),
                          schedule=SCHEDULE)  # fmt: skip


@pytest.mark.asyncio
async def test_runs_once_per_session_inside_the_window(tmp_path: Path) -> None:
    lake = await _lake(tmp_path)
    outbox = Outbox(tmp_path / "meta.db", clock=lambda: NOW)
    sources = Sources()
    r = runner(lake, sources)
    assert r.run(NOW - timedelta(minutes=10), outbox) is None  # 还没到决策时刻
    assert sources.calls == 0  # 窗口外不读任何数据
    report = r.run(NOW, outbox)
    assert report is not None and report.enqueued == ("demo:v1:2026-10-23:SPY:SELL:REDUCE",)
    assert r.run(NOW + timedelta(minutes=1), outbox) is None  # 同一天不再跑
    assert sources.calls == 1
    assert r.run(datetime(2026, 10, 24, 15, 0, tzinfo=UTC), outbox) is None  # 周六
    outbox.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["account", "positions"])
async def test_source_failure_skips_the_day(tmp_path: Path, failure: str) -> None:
    lake = await _lake(tmp_path)
    outbox = Outbox(tmp_path / "meta.db", clock=lambda: NOW)
    sources = Sources()
    sources.fail = failure
    r = runner(lake, sources)
    report = r.run(NOW, outbox)
    assert report is not None and report.enqueued == ()
    assert "*" in report.skipped
    assert outbox.pending() == []
    sources.fail = None
    assert r.run(NOW + timedelta(minutes=1), outbox) is None  # 当天不再补跑：等人处理
    outbox.close()


def test_window_plus_delivery_must_fit_in_intent_age(tmp_path: Path) -> None:
    r = StrategyRunner([], lake=None, calendar=CALENDAR, sources=Sources().bundle(),  # type: ignore[arg-type]
                       schedule=SCHEDULE)  # fmt: skip
    r.check_delivery(deliver_seconds=2.0, max_age=timedelta(minutes=5))
    with pytest.raises(RunnerConfigError, match="意图限龄"):
        r.check_delivery(deliver_seconds=2.0, max_age=timedelta(minutes=4))


@pytest.mark.asyncio
async def test_workerd_refuses_incompatible_runner(tmp_path: Path) -> None:
    from server.platforms import local_channel  # noqa: PLC0415

    settings = WorkerdSettings(runtime_dir=tmp_path / "run", meta_path=tmp_path / "meta.db",
                               tradingd_account="x", max_age=timedelta(minutes=3))  # fmt: skip
    r = StrategyRunner([], lake=None, calendar=CALENDAR, sources=Sources().bundle(),  # type: ignore[arg-type]
                       schedule=SCHEDULE)  # fmt: skip
    code = await run_workerd(settings, local_channel(), asyncio.Event(), runner=r)
    assert code == EXIT_REFUSED


@pytest.mark.asyncio
async def test_workerd_calls_the_runner_each_round_and_survives_errors(tmp_path: Path) -> None:
    from server.platforms import local_channel  # noqa: PLC0415

    stop = asyncio.Event()
    seen: list[datetime] = []

    class Spy(StrategyRunner):
        def run(self, now: datetime, outbox: Outbox) -> Any:
            seen.append(now)
            if len(seen) == 1:
                raise RuntimeError("策略缺陷")  # 出错不影响后面的轮次
            stop.set()
            return None

    settings = WorkerdSettings(runtime_dir=tmp_path / "run", meta_path=tmp_path / "meta.db",
                               tradingd_account="x", deliver_seconds=0.05,
                               connect_timeout=0.2)  # fmt: skip
    spy = Spy([], lake=None, calendar=CALENDAR, sources=Sources().bundle(),  # type: ignore[arg-type]
              schedule=SCHEDULE)  # fmt: skip
    code = await asyncio.wait_for(
        run_workerd(settings, local_channel(), stop, runner=spy, clock=lambda: NOW), 20
    )
    assert code == 0
    assert seen == [NOW, NOW]
