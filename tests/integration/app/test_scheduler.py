"""S-01d：调度器（真实 NYSE 交易日历；交易核心用替身记录调用）。"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from server.app.scheduler import BrokerWindow, Scheduler, ScheduleSettings
from server.data.calendar import TradingCalendar
from server.risk.reconcile import ReconKind

ET = ZoneInfo("America/New_York")
CAL = TradingCalendar.nyse(start=date(2026, 10, 1), end=date(2026, 12, 31))
# 2026-10-28 周三：常规时段 09:30–16:00 ET = 13:30–20:00 UTC（夏令时）
OPEN = datetime(2026, 10, 28, 13, 30, tzinfo=UTC)
CLOSE = datetime(2026, 10, 28, 20, 0, tzinfo=UTC)


class Core:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = False

    def assess(self) -> str:
        self.calls.append("assess")
        return "NORMAL"

    async def reconcile(self, kind: ReconKind) -> bool:
        self.calls.append(kind.value)
        if self.fail:
            raise RuntimeError("对账出错")
        return True


class Clock:
    def __init__(self, at: datetime) -> None:
        self.now = at

    def __call__(self) -> datetime:
        return self.now


def make(at: datetime, **kw: Any) -> tuple[Scheduler, Core, Clock]:
    core, clock = Core(), Clock(at)
    return Scheduler(core, CAL, ScheduleSettings(**kw), clock=clock), core, clock


@pytest.mark.asyncio
async def test_assess_runs_on_its_own_interval() -> None:
    sched, _core, clock = make(OPEN - timedelta(hours=3))
    # 刚启动：评估，并补做最近一个已收盘时段（10-27）的日终对账
    assert await sched.run_due() == ["assess", "end_of_day"]
    clock.now += timedelta(seconds=10)
    assert await sched.run_due() == []
    clock.now += timedelta(seconds=5)
    assert await sched.run_due() == ["assess"]


@pytest.mark.asyncio
async def test_intraday_reconcile_every_five_minutes_only_in_session() -> None:
    sched, core, clock = make(OPEN - timedelta(minutes=1))
    assert "intraday" not in await sched.run_due()  # 开盘前不做
    clock.now = OPEN
    assert "intraday" in await sched.run_due()  # 开盘即做一次
    clock.now = OPEN + timedelta(minutes=4)
    assert "intraday" not in await sched.run_due()
    clock.now = OPEN + timedelta(minutes=5)
    assert "intraday" in await sched.run_due()
    clock.now = CLOSE
    assert "intraday" not in await sched.run_due()  # 收盘时刻不算盘中
    assert core.calls.count("INTRADAY") == 2


@pytest.mark.asyncio
async def test_end_of_day_once_after_close() -> None:
    sched, core, clock = make(CLOSE + timedelta(minutes=5))
    await sched.run_due()  # 启动时补做 10-27 的日终对账
    assert core.calls.count("END_OF_DAY") == 1
    clock.now = CLOSE + timedelta(minutes=9)
    assert "end_of_day" not in await sched.run_due()  # 收盘后 10 分钟才做当天的
    clock.now = CLOSE + timedelta(minutes=10)
    assert "end_of_day" in await sched.run_due()
    clock.now += timedelta(hours=1)
    assert "end_of_day" not in await sched.run_due()  # 同一天只做一次
    assert core.calls.count("END_OF_DAY") == 2


@pytest.mark.asyncio
async def test_end_of_day_failure_is_retried_not_marked_done() -> None:
    """日终对账出错：不记为已做，按盘中间隔重试，成功后才算（评审 P1-2）。"""
    sched, core, clock = make(CLOSE + timedelta(minutes=10))
    core.fail = True
    assert "end_of_day" in await sched.run_due()
    clock.now += timedelta(minutes=1)
    assert "end_of_day" not in await sched.run_due()  # 还没到重试时刻
    clock.now += timedelta(minutes=4)
    core.fail = False
    assert "end_of_day" in await sched.run_due()  # 重试成功
    clock.now += timedelta(minutes=10)
    assert "end_of_day" not in await sched.run_due()
    assert core.calls.count("END_OF_DAY") == 2


@pytest.mark.asyncio
async def test_restart_after_utc_midnight_still_catches_up() -> None:
    """10-28 收盘后、UTC 午夜之后（美东 21:00）才重启：仍补做 10-28 的日终对账（评审 P1-2）。"""
    sched, core, _ = make(datetime(2026, 10, 29, 1, 0, tzinfo=UTC))
    assert "end_of_day" in await sched.run_due()
    assert core.calls == ["assess", "END_OF_DAY"]


@pytest.mark.asyncio
async def test_restart_after_close_catches_up_end_of_day() -> None:
    """tradingd 在收盘后、日终对账前重启：启动后补做。"""
    sched, _core, _ = make(CLOSE + timedelta(hours=2))
    assert "end_of_day" in await sched.run_due()


@pytest.mark.asyncio
async def test_early_close_is_driven_by_the_calendar() -> None:
    """感恩节次日 2026-11-27 13:00 ET 提前收市：盘中对账与日终对账跟着日历走，不硬编码 16:00。"""
    early_close = datetime(2026, 11, 27, 18, 0, tzinfo=UTC)  # 13:00 ET（冬令时）
    session = CAL.session(date(2026, 11, 27))
    assert session is not None and session.close == early_close and session.early_close
    before, _core, _ = make(early_close - timedelta(minutes=1))
    assert "intraday" in await before.run_due()
    after, _core, _ = make(early_close + timedelta(hours=2))  # 15:00 ET：平常还在盘中
    done = await after.run_due()
    assert "intraday" not in done and "end_of_day" in done


@pytest.mark.asyncio
async def test_holiday_and_weekend_have_no_intraday_reconcile() -> None:
    for moment in (
        datetime(2026, 11, 26, 16, 0, tzinfo=UTC),  # 感恩节
        datetime(2026, 10, 31, 16, 0, tzinfo=UTC),  # 周六
    ):
        sched, core, clock = make(moment)
        await sched.run_due()  # 只会补做前一个交易日的日终对账
        assert "INTRADAY" not in core.calls
        clock.now += timedelta(minutes=30)
        assert await sched.run_due() == ["assess"]


@pytest.mark.asyncio
async def test_calendar_out_of_range_fails_closed() -> None:
    """日历查不到：只评估，不做对账。"""
    sched, core, _ = make(datetime(2027, 3, 1, 15, 0, tzinfo=UTC))
    assert await sched.run_due() == ["assess"]
    assert core.calls == ["assess"]


@pytest.mark.asyncio
async def test_reconcile_errors_do_not_stop_the_schedule() -> None:
    sched, core, clock = make(OPEN)
    core.fail = True
    assert "intraday" in await sched.run_due()
    clock.now += timedelta(minutes=5)
    core.fail = False
    assert "intraday" in await sched.run_due()


def test_broker_window_crosses_midnight_in_its_own_time_zone() -> None:
    window = BrokerWindow(ET, time(23, 45), timedelta(minutes=30))  # 23:45–00:15 ET
    inside = datetime(2026, 10, 29, 0, 5, tzinfo=ET)
    assert window.contains(inside)
    assert window.contains(datetime(2026, 10, 28, 23, 45, tzinfo=ET))
    assert not window.contains(datetime(2026, 10, 29, 0, 15, tzinfo=ET))
    assert not window.contains(datetime(2026, 10, 28, 23, 44, tzinfo=ET))
    # 换算到 UTC 后同样成立（夏令时与冬令时由 zoneinfo 处理）
    assert window.contains(datetime(2026, 12, 2, 5, 0, tzinfo=UTC))  # 00:00 EST
    with pytest.raises(ValueError, match="时区"):
        window.contains(datetime(2026, 10, 29, 0, 5))  # noqa: DTZ001


@pytest.mark.asyncio
async def test_no_intraday_reconcile_inside_the_broker_window() -> None:
    noon = datetime(2026, 10, 28, 16, 0, tzinfo=UTC)  # 12:00 ET
    window = BrokerWindow(ET, time(11, 55), timedelta(minutes=15))
    sched, _core, _ = make(noon, broker_window=window)
    assert sched.in_broker_window(noon)
    assert "intraday" not in await sched.run_due()


@pytest.mark.parametrize(
    "kw",
    [
        {"assess_every": timedelta(0)},
        {"intraday_every": timedelta(seconds=-1)},
        {"end_of_day_delay": timedelta(seconds=-1)},
    ],
)
def test_bad_schedule_settings_are_refused(kw: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Scheduler(Core(), CAL, ScheduleSettings(**kw))


# ---------- 交易日历：DAY 意图的归属时段 ----------


def test_session_day_assigns_pre_and_post_market_moments() -> None:
    assert CAL.session_day(OPEN + timedelta(hours=1)) == date(2026, 10, 28)  # 盘中：当天
    assert CAL.session_day(OPEN - timedelta(hours=2)) == date(2026, 10, 28)  # 盘前：当天
    assert CAL.session_day(CLOSE + timedelta(minutes=1)) == date(2026, 10, 29)  # 盘后：下一个
    assert CAL.session_day(datetime(2026, 10, 31, 15, 0, tzinfo=UTC)) == date(2026, 11, 2)  # 周六
