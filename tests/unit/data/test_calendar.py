"""D-03：交易日历——期望值按 NYSE 公布的日程独立写死，不从被测库推导。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from server.data.calendar import CalendarDataError, CalendarRangeError, Session, TradingCalendar


@pytest.fixture(scope="module")
def cal() -> TradingCalendar:
    return TradingCalendar.nyse(start=date(2025, 1, 1), end=date(2028, 12, 31))


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# (日期, 开盘 UTC, 收盘 UTC, 是否提前收市)
SESSIONS = [
    # 夏令时（EDT, UTC-4）：09:30–16:00 = 13:30–20:00 UTC
    (date(2026, 10, 30), utc(2026, 10, 30, 13, 30), utc(2026, 10, 30, 20, 0), False),
    # 2026-11-01 夏令时结束（EST, UTC-5）：14:30–21:00 UTC
    (date(2026, 11, 2), utc(2026, 11, 2, 14, 30), utc(2026, 11, 2, 21, 0), False),
    # 感恩节次日、平安夜提前到 13:00 ET 收市
    (date(2026, 11, 27), utc(2026, 11, 27, 14, 30), utc(2026, 11, 27, 18, 0), True),
    (date(2026, 12, 24), utc(2026, 12, 24, 14, 30), utc(2026, 12, 24, 18, 0), True),
    # 2027-03-14 夏令时开始，3-15 恢复 13:30 UTC 开盘
    (date(2027, 3, 15), utc(2027, 3, 15, 13, 30), utc(2027, 3, 15, 20, 0), False),
    # 感恩节前的周三是完整交易日
    (date(2026, 11, 25), utc(2026, 11, 25, 14, 30), utc(2026, 11, 25, 21, 0), False),
    # 独立日补休在周五（7-3），周四 7-2 不提前收市
    (date(2026, 7, 2), utc(2026, 7, 2, 13, 30), utc(2026, 7, 2, 20, 0), False),
    # 2028-01-01 逢周六：NYSE 不在前一个周五补休，2027-12-31 照常交易
    (date(2027, 12, 31), utc(2027, 12, 31, 14, 30), utc(2027, 12, 31, 21, 0), False),
]

HOLIDAYS = [
    date(2025, 1, 9),  # 卡特总统国丧日（临时休市，锁住库版本）
    date(2026, 1, 1),  # 元旦
    date(2026, 1, 19),  # 马丁·路德·金纪念日
    date(2026, 2, 16),  # 总统日
    date(2026, 4, 3),  # 耶稣受难日
    date(2026, 5, 25),  # 阵亡将士纪念日
    date(2027, 6, 18),  # 六月节逢周六，周五补休
    date(2027, 7, 5),  # 独立日逢周日，周一补休
    date(2027, 12, 24),  # 圣诞节逢周六，周五补休
    date(2026, 6, 19),  # 六月节
    date(2026, 7, 3),  # 独立日（7-4 是周六，提前到周五休市）
    date(2026, 9, 7),  # 劳动节
    date(2026, 11, 26),  # 感恩节
    date(2026, 12, 25),  # 圣诞节
    date(2027, 1, 1),  # 元旦
    date(2027, 1, 18),  # 马丁·路德·金纪念日
    date(2027, 2, 15),  # 总统日
    date(2027, 3, 26),  # 耶稣受难日
]


@pytest.mark.parametrize(("day", "open_utc", "close_utc", "early"), SESSIONS)
def test_known_sessions(
    cal: TradingCalendar, day: date, open_utc: datetime, close_utc: datetime, early: bool
) -> None:
    session = cal.session(day)
    assert session == Session(day=day, open=open_utc, close=close_utc, early_close=early)
    assert session.open.tzinfo is UTC and session.close.tzinfo is UTC


@pytest.mark.parametrize("day", HOLIDAYS)
def test_known_holidays(cal: TradingCalendar, day: date) -> None:
    assert not cal.is_trading_day(day)
    assert cal.session(day) is None


@pytest.mark.parametrize("day", [date(2026, 10, 24), date(2026, 10, 25)])
def test_weekends_are_closed(cal: TradingCalendar, day: date) -> None:
    assert not cal.is_trading_day(day)


def test_next_session_skips_holiday_and_weekend(cal: TradingCalendar) -> None:
    # 感恩节（周四）收盘后：下一交易日是周五提前收市
    after_wed_close = utc(2026, 11, 25, 22, 0)
    nxt = cal.next_session(after_wed_close)
    assert nxt.day == date(2026, 11, 27) and nxt.early_close
    # 周五提前收市之后：下一交易日是周一
    assert cal.next_session(utc(2026, 11, 27, 18, 0)).day == date(2026, 11, 30)


def test_is_open_respects_early_close(cal: TradingCalendar) -> None:
    assert cal.is_open(utc(2026, 12, 24, 17, 59))
    assert not cal.is_open(utc(2026, 12, 24, 18, 0))  # 收盘时刻本身不算开市
    assert not cal.is_open(utc(2026, 12, 25, 15, 0))
    assert cal.is_open(utc(2026, 11, 2, 14, 30))
    assert not cal.is_open(utc(2026, 11, 2, 13, 30))  # 冬令时 13:30 UTC 还没开盘


def test_sessions_between(cal: TradingCalendar) -> None:
    days = [s.day for s in cal.sessions_between(date(2026, 12, 23), date(2026, 12, 29))]
    assert days == [date(2026, 12, 23), date(2026, 12, 24), date(2026, 12, 28), date(2026, 12, 29)]


def test_adhoc_closure_overrides_library() -> None:
    # 临时休市（例如国丧日）可由受保护配置登记，优先于库的规则
    cal = TradingCalendar.nyse(
        start=date(2026, 1, 1),
        end=date(2027, 12, 31),
        adhoc_closures=frozenset({date(2026, 10, 30)}),
    )
    assert not cal.is_trading_day(date(2026, 10, 30))
    assert cal.next_session(utc(2026, 10, 29, 21, 0)).day == date(2026, 11, 2)


def test_out_of_range_fails_closed(cal: TradingCalendar) -> None:
    with pytest.raises(CalendarRangeError):
        cal.is_trading_day(date(2030, 1, 2))
    with pytest.raises(CalendarRangeError):
        cal.next_session(utc(2028, 12, 31, 12, 0))


def test_naive_time_rejected(cal: TradingCalendar) -> None:
    with pytest.raises(ValueError, match="时区"):
        cal.is_open(datetime(2026, 12, 24, 15, 0))  # noqa: DTZ001  故意构造无时区时间


# ---------- 评审 D-03 补充：next_session 与 is_open 的边界 ----------


def test_next_session_same_day_before_open(cal: TradingCalendar) -> None:
    assert cal.next_session(utc(2026, 10, 30, 12, 0)).day == date(2026, 10, 30)


def test_next_session_at_open_and_intraday(cal: TradingCalendar) -> None:
    assert cal.next_session(utc(2026, 10, 30, 13, 30)).day == date(2026, 11, 2)
    assert cal.next_session(utc(2026, 10, 30, 15, 0)).day == date(2026, 11, 2)


def test_is_open_boundaries_at_open(cal: TradingCalendar) -> None:
    assert cal.is_open(utc(2026, 10, 30, 13, 30))
    assert not cal.is_open(utc(2026, 10, 30, 13, 29))


def test_non_utc_input_is_converted(cal: TradingCalendar) -> None:
    new_york = timezone(timedelta(hours=-4))
    # 纽约 10-30 晚上 21:00 = UTC 10-31 01:00（周六）
    evening = datetime(2026, 10, 30, 21, 0, tzinfo=new_york)
    assert not cal.is_open(evening)
    assert cal.next_session(evening).day == date(2026, 11, 2)
    assert cal.is_open(datetime(2026, 10, 30, 10, 0, tzinfo=new_york))


def test_naive_time_rejected_by_next_session(cal: TradingCalendar) -> None:
    with pytest.raises(ValueError, match="时区"):
        cal.next_session(datetime(2026, 10, 30, 12, 0))  # noqa: DTZ001  故意构造无时区时间


def test_before_start_and_bad_ranges(cal: TradingCalendar) -> None:
    with pytest.raises(CalendarRangeError):
        cal.is_trading_day(date(2024, 12, 31))
    with pytest.raises(ValueError):
        list(cal.sessions_between(date(2026, 12, 29), date(2026, 12, 23)))
    with pytest.raises(CalendarRangeError):
        list(cal.sessions_between(date(2028, 12, 1), date(2029, 1, 5)))


# ---------- 评审 D-03 补充：登记错误与异常数据失败即关闭 ----------

S0, S1 = date(2026, 1, 1), date(2027, 12, 31)


@pytest.mark.parametrize(
    "closures",
    [
        frozenset({date(2029, 1, 2)}),  # 超出范围
        frozenset({date(2026, 10, 24)}),  # 周六：本就休市，多半是写错
        frozenset({date(2026, 12, 25)}),  # 已知休市日
        frozenset({datetime(2026, 10, 30, tzinfo=UTC)}),  # datetime 不是 date
        frozenset({"2026-10-30"}),  # 字符串
    ],
)
def test_bad_adhoc_closures_are_rejected(closures: frozenset[object]) -> None:
    with pytest.raises(CalendarDataError):
        TradingCalendar.nyse(adhoc_closures=closures, start=S0, end=S1)  # type: ignore[arg-type]


def test_bad_range_is_rejected() -> None:
    with pytest.raises(CalendarDataError):
        TradingCalendar.nyse(start=date(2027, 1, 1), end=date(2026, 1, 1))
    with pytest.raises(CalendarDataError):
        TradingCalendar.nyse(start=datetime(2026, 1, 1, tzinfo=UTC), end=date(2027, 1, 1))


def test_adhoc_early_close_only_shortens_the_session() -> None:
    cal = TradingCalendar.nyse(
        adhoc_early_closes={date(2026, 10, 30): utc(2026, 10, 30, 17, 0)}, start=S0, end=S1
    )
    session = cal.session(date(2026, 10, 30))
    assert session is not None and session.close == utc(2026, 10, 30, 17, 0) and session.early_close
    assert not cal.is_open(utc(2026, 10, 30, 17, 0))
    for bad_close in (utc(2026, 10, 30, 21, 0), utc(2026, 10, 30, 13, 0)):  # 推后、早于开盘
        with pytest.raises(CalendarDataError):
            TradingCalendar.nyse(
                adhoc_early_closes={date(2026, 10, 30): bad_close}, start=S0, end=S1
            )
    with pytest.raises(CalendarDataError):  # 不是交易日
        TradingCalendar.nyse(
            adhoc_early_closes={date(2026, 10, 31): utc(2026, 10, 31, 17, 0)}, start=S0, end=S1
        )


def _session(day: date, open_: datetime, close: datetime) -> Session:
    return Session(day=day, open=open_, close=close, early_close=False)


@pytest.mark.parametrize(
    "sessions",
    [
        {date(2026, 1, 5): _session(date(2026, 1, 5), utc(2026, 1, 5, 21), utc(2026, 1, 5, 14))},
        {date(2026, 1, 5): _session(date(2026, 1, 5), utc(2026, 1, 5, 23), utc(2026, 1, 6, 1))},
        {date(2026, 1, 5): _session(date(2026, 1, 6), utc(2026, 1, 6, 14), utc(2026, 1, 6, 21))},
        {date(2030, 1, 7): _session(date(2030, 1, 7), utc(2030, 1, 7, 14), utc(2030, 1, 7, 21))},
    ],
)
def test_invalid_session_data_is_rejected(sessions: dict[date, Session]) -> None:
    with pytest.raises(CalendarDataError):
        TradingCalendar(sessions, start=date(2026, 1, 1), end=date(2027, 12, 31))


def test_sessions_are_copied_defensively() -> None:
    day = date(2026, 1, 5)
    source = {day: _session(day, utc(2026, 1, 5, 14, 30), utc(2026, 1, 5, 21))}
    cal = TradingCalendar(source, start=date(2026, 1, 1), end=date(2026, 12, 31))
    source.clear()
    assert cal.is_trading_day(day)


def test_more_bad_override_registrations() -> None:
    with pytest.raises(CalendarDataError):  # 提前收市的 key 是 datetime
        TradingCalendar.nyse(
            adhoc_early_closes={datetime(2026, 10, 30, tzinfo=UTC): utc(2026, 10, 30, 17)},
            start=S0,
            end=S1,
        )
    with pytest.raises(CalendarDataError):  # 提前收市日期超出范围
        TradingCalendar.nyse(
            adhoc_early_closes={date(2029, 1, 2): utc(2029, 1, 2, 17)}, start=S0, end=S1
        )
    with pytest.raises(CalendarDataError):  # 同一天既休市又提前收市
        TradingCalendar.nyse(
            adhoc_closures=frozenset({date(2026, 10, 30)}),
            adhoc_early_closes={date(2026, 10, 30): utc(2026, 10, 30, 17)},
            start=S0,
            end=S1,
        )
    with pytest.raises(CalendarDataError):  # 收盘时间不是 datetime
        TradingCalendar.nyse(
            adhoc_early_closes={date(2026, 10, 30): "17:00"},  # type: ignore[dict-item]
            start=S0,
            end=S1,
        )


def test_non_utc_session_is_rejected() -> None:
    est = timezone(timedelta(hours=-5))
    day = date(2026, 1, 5)
    bad = Session(
        day=day,
        open=datetime(2026, 1, 5, 9, 30, tzinfo=est),
        close=datetime(2026, 1, 5, 16, 0, tzinfo=est),
        early_close=False,
    )
    with pytest.raises(CalendarDataError):
        TradingCalendar({day: bad}, start=date(2026, 1, 1), end=date(2026, 12, 31))
