"""交易日历与时区（D-03）。

- 调度一律由交易日历驱动，不硬编码北京时间或美东时间；夏令时、提前收市、节假日都由日历决定。
- 只表示**常规交易时段**（不含盘前盘后），与首期“日线、限价、常规时段”一致。
- 底层用 exchange_calendars 的 XNYS 日历；对外只暴露 UTC datetime 与本模块的 Session，
  不把 pandas 类型泄漏到其他层。
- 临时休市与临时提前收市（国丧日、突发停市等库来不及更新的情况）由受保护配置登记（C-05 加载），
  优先于库的规则。只能“减少交易时间”：不支持临时开市或推后收盘。
- 失败即关闭：登记错误（类型不对、超出范围、登记在本就休市的日子）、库返回异常数据、
  查询超出覆盖范围，一律抛异常，不猜测。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType

import exchange_calendars as xcals
import pandas as pd

from server.domain.values import to_utc


class CalendarRangeError(Exception):
    """查询超出日历覆盖范围。"""


class CalendarDataError(ValueError):
    """日历数据或登记的覆盖不合法。"""


@dataclass(frozen=True)
class Session:
    day: date
    open: datetime
    close: datetime
    early_close: bool


def _require_date(value: object, name: str) -> date:
    # datetime 是 date 的子类：datetime 与 date 比较恒不相等，
    # 登记成 datetime 会静默失效，所以严格拒绝
    if type(value) is not date:
        raise CalendarDataError(f"{name} 必须是 date（不能是 datetime 或字符串）：{value!r}")
    return value


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须带时区")
    return value.astimezone(UTC)


class TradingCalendar:
    """请用 `TradingCalendar.nyse(...)` 构造；直接构造时同样会校验全部不变量。"""

    def __init__(self, sessions: Mapping[date, Session], *, start: date, end: date) -> None:
        start, end = _require_date(start, "start"), _require_date(end, "end")
        if start > end:
            raise CalendarDataError("start 不能晚于 end")
        ordered = sorted(sessions.items())
        previous: Session | None = None
        for day, s in ordered:
            if s.day != day or not start <= day <= end:
                raise CalendarDataError(f"{day} 的时段日期不一致或超出范围")
            if s.open.tzinfo is not UTC or s.close.tzinfo is not UTC:
                raise CalendarDataError(f"{day} 的开收盘时间必须是 UTC")
            if not s.open < s.close:
                raise CalendarDataError(f"{day} 开盘不早于收盘")
            # is_open 按 UTC 日期查时段：开收盘必须落在同一个 UTC 日期
            # （XNYS 最早 13:30、最晚 21:00 UTC）
            if not s.open.date() == s.close.date() == day:
                raise CalendarDataError(f"{day} 的时段跨越了 UTC 日期")
            if previous is not None and not previous.close < s.open:
                raise CalendarDataError(f"{previous.day} 与 {day} 的时段重叠或乱序")
            previous = s
        self._sessions: Mapping[date, Session] = MappingProxyType(dict(ordered))
        self._days = tuple(day for day, _ in ordered)
        self._start = start
        self._end = end

    @classmethod
    def nyse(
        cls,
        *,
        start: date,
        end: date,
        adhoc_closures: frozenset[date] = frozenset(),
        adhoc_early_closes: Mapping[date, datetime] | None = None,
    ) -> TradingCalendar:
        start, end = _require_date(start, "start"), _require_date(end, "end")
        if start > end:
            raise CalendarDataError("start 不能晚于 end")
        early_overrides = dict(adhoc_early_closes or {})
        for day in [*adhoc_closures, *early_overrides]:
            _require_date(day, "临时覆盖的日期")
            if not start <= day <= end:
                raise CalendarDataError(f"临时覆盖的日期 {day} 超出日历范围 {start}–{end}")

        raw = xcals.get_calendar("XNYS", start=start.isoformat(), end=end.isoformat())
        library_early = {ts.date() for ts in raw.early_closes}
        sessions: dict[date, Session] = {}
        for label, row in raw.schedule.iterrows():
            day = pd.Timestamp(label).date()
            sessions[day] = Session(
                day=day,
                open=to_utc(row["open"].to_pydatetime()),
                close=to_utc(row["close"].to_pydatetime()),
                early_close=day in library_early,
            )

        for day in adhoc_closures:
            if day not in sessions:
                raise CalendarDataError(f"临时休市 {day} 本就不是交易日：请检查登记")
            del sessions[day]
        for day, close in early_overrides.items():
            if day in adhoc_closures:
                raise CalendarDataError(f"{day} 同时登记了临时休市与临时提前收市")
            session = sessions.get(day)
            if session is None:
                raise CalendarDataError(f"临时提前收市 {day} 不是交易日：请检查登记")
            try:
                new_close = to_utc(close)
            except ValueError as exc:
                raise CalendarDataError(f"{day} 的临时收盘时间不合法：{exc}") from exc
            if not session.open < new_close < session.close:
                raise CalendarDataError(f"{day} 的临时收盘时间只能提前，且必须晚于开盘")
            sessions[day] = Session(day=day, open=session.open, close=new_close, early_close=True)
        return cls(sessions, start=start, end=end)

    # ---------- 查询 ----------

    def _check_day(self, day: date) -> None:
        if not self._start <= day <= self._end:
            raise CalendarRangeError(f"{day} 超出日历范围 {self._start}–{self._end}")

    def is_trading_day(self, day: date) -> bool:
        self._check_day(day)
        return day in self._sessions

    def session(self, day: date) -> Session | None:
        self._check_day(day)
        return self._sessions.get(day)

    def is_open(self, at: datetime) -> bool:
        """是否处于常规交易时段（开盘时刻算开市，收盘时刻不算）。"""
        moment = _require_aware(at)
        # 开收盘落在同一 UTC 日期已在构造时校验，按 UTC 日期查即可
        session = self.session(moment.date())
        return session is not None and session.open <= moment < session.close

    def next_session(self, after: datetime) -> Session:
        """开盘时间严格晚于 after 的下一个时段。

        当天还没开盘则返回当天；开盘时刻或盘中返回下一个交易日。
        """
        moment = _require_aware(after)
        day = moment.date()
        self._check_day(day)
        while day <= self._end:
            session = self._sessions.get(day)
            if session is not None and session.open > moment:
                return session
            day += timedelta(days=1)
        raise CalendarRangeError(f"{after} 之后在日历范围内没有交易时段")

    def sessions_between(self, first: date, last: date) -> Iterator[Session]:
        self._check_day(first)
        self._check_day(last)
        if first > last:
            raise ValueError("first 不能晚于 last")
        for day in self._days:
            if first <= day <= last:
                yield self._sessions[day]
