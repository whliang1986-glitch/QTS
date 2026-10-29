"""S-01d：调度器（真实 NYSE 交易日历；交易核心用替身记录调用）。"""

from __future__ import annotations

import asyncio
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


# ---------- S-01e：评估与对账解耦、对账超时告警、日历到期告警 ----------


class Counter:
    """可控时钟：每调用一次前进 15 秒（两个循环共用）。"""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=15)
        return self.now


async def _fast_sleep(_seconds: float) -> None:
    await asyncio.sleep(0.001)


async def _until(check: Any, seconds: float = 5) -> None:
    for _ in range(int(seconds / 0.01)):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("条件一直没有满足")


def _logged(caplog: pytest.LogCaptureFixture, text: str) -> int:
    return sum(text in r.getMessage() for r in caplog.records)


class Slow(Core):
    """对账卡住直到放行；记录内层是否被取消。"""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.cancelled = False

    async def reconcile(self, kind: ReconKind) -> bool:
        self.calls.append(kind.value)
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return True


def slow_scheduler(core: Core, timeout: timedelta) -> Scheduler:
    return Scheduler(core, CAL, ScheduleSettings(reconcile_timeout=timeout),
                     clock=Counter(OPEN), sleep=_fast_sleep)  # fmt: skip


@pytest.mark.asyncio
async def test_a_stuck_reconcile_does_not_stop_assess_and_is_not_cancelled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """对账卡住：评估照常；超时后反复记 ERROR；不取消、不重发；放行后正常完成。"""
    caplog.set_level("INFO")
    core = Slow()
    runner = asyncio.create_task(slow_scheduler(core, timedelta(milliseconds=30)).run())
    await _until(
        lambda: core.calls.count("assess") >= 5 and _logged(caplog, "对账超时仍未返回") >= 2
    )
    assert not core.cancelled
    first = core.calls.count("INTRADAY") + core.calls.count("END_OF_DAY")
    assert first == 1  # 卡住期间没有另起一次
    core.release.set()
    await _until(lambda: _logged(caplog, "调度的对账完成") >= 1)
    assert not core.cancelled  # 放行后是正常完成，不是被取消
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner


@pytest.mark.asyncio
async def test_stopping_cancels_the_in_flight_reconcile_without_orphans() -> None:
    """tradingd 停止（外部取消调度器）：正在进行的对账一并取消并等它结束，不留孤儿（评审 P1-2）。"""
    core = Slow()
    runner = asyncio.create_task(slow_scheduler(core, timedelta(seconds=30)).run())
    await _until(lambda: "INTRADAY" in core.calls or "END_OF_DAY" in core.calls)
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    assert core.cancelled  # 超时之前停止：同样取消了内层，没有留下继续运行的任务


@pytest.mark.asyncio
async def test_internal_cancellation_crashes_the_scheduler() -> None:
    """对账内部冒出 CancelledError：调度器整体抛出（而不是对账循环悄悄停掉，评审 P1-1）。"""

    class Cancelling(Core):
        async def reconcile(self, kind: ReconKind) -> bool:
            raise asyncio.CancelledError

    sched = Scheduler(Cancelling(), CAL, ScheduleSettings(), clock=Counter(OPEN), sleep=_fast_sleep)
    with pytest.raises(RuntimeError, match="意外取消"):
        await asyncio.wait_for(sched.run(), 5)


@pytest.mark.asyncio
async def test_a_failing_assess_crashes_the_scheduler_with_its_own_error() -> None:
    class Broken(Core):
        def assess(self) -> str:
            raise ValueError("评估坏了")

    sched = Scheduler(Broken(), CAL, ScheduleSettings(), clock=Counter(OPEN), sleep=_fast_sleep)
    with pytest.raises(ValueError, match="评估坏了"):  # 解包后的根因，不是 ExceptionGroup
        await asyncio.wait_for(sched.run(), 5)


@pytest.mark.asyncio
async def test_calendar_expiry_is_warned_once_a_day(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("WARNING")
    near_end = datetime(2026, 12, 10, 15, 0, tzinfo=UTC)  # 日历只到 12-31：剩 21 天
    sched, _core, clock = make(near_end)
    await sched.run_assess_due()
    clock.now += timedelta(minutes=1)
    await sched.run_assess_due()
    warnings = [r for r in caplog.records if "交易日历即将到期" in r.getMessage()]
    assert len(warnings) == 1
    clock.now += timedelta(days=1)
    await sched.run_assess_due()
    assert len([r for r in caplog.records if "交易日历即将到期" in r.getMessage()]) == 2


@pytest.mark.asyncio
async def test_no_expiry_warning_with_plenty_of_coverage(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("WARNING")
    sched, _core, _ = make(OPEN)  # 10-28，离 12-31 还有两个月
    await sched.run_assess_due()
    assert not [r for r in caplog.records if "交易日历即将到期" in r.getMessage()]


def test_reconcile_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="reconcile_timeout"):
        Scheduler(Core(), CAL, ScheduleSettings(reconcile_timeout=timedelta(0)))


def test_calendar_exposes_its_coverage_end() -> None:
    assert CAL.end == date(2026, 12, 31)


@pytest.mark.asyncio
async def test_expired_calendar_is_an_error_not_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("WARNING")

    class Short:
        end = date(2026, 10, 1)

        def session(self, day: date) -> Any:
            return CAL.session(day)

        def is_open(self, at: datetime) -> bool:
            return CAL.is_open(at)

        def next_session(self, after: datetime) -> Any:
            return CAL.next_session(after)

    sched = Scheduler(Core(), Short(), ScheduleSettings(), clock=lambda: OPEN)
    await sched.run_assess_due()
    assert _logged(caplog, "交易日历已到期") == 1
