"""tradingd 的调度器（S-01d）：例行评估、盘中对账、日终对账、券商每日重启窗口。

全部由交易日历驱动（D-03），不硬编码北京或美东时刻。唯一的固定时刻是券商（IB Gateway）
每日自动重启的窗口：它由 IBC / Gateway 的设置决定，这里按配置里写明的时区换算
（zoneinfo 在 Windows 与 macOS / Linux 上行为一致，时区数据来自 tzdata 包）。

- 例行评估：每 assess 间隔调一次 core.assess()（运行状态机；出错时核心自己持久化 SAFE HALT）。
- 盘中对账：常规交易时段内、不在券商重启窗口时，从开盘起每 intraday 间隔一次 reconcile(INTRADAY)。
- 日终对账：每个交易时段收盘后 end_of_day_delay 做一次 reconcile(END_OF_DAY)；同一天只做一次。
  tradingd 在收盘后、日终对账前重启的，启动后补做。
- 券商重启窗口：broker_window(now) 供运行状态机使用（窗口内拒绝新增风险，不做盘中对账）。
- 日历查不到（超出覆盖范围、数据错误）：当作“不在交易时段”，只做评估、不做对账，
  并记 ERROR（失败即关闭）。
- 对账出错：记日志，下一轮照常；对账发现差异由对账模块自己 SAFE HALT（S-01a / R-07）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from server.data.calendar import Session
from server.risk.reconcile import ReconKind
from server.storage.logs import log_event

log = logging.getLogger(__name__)

TICK_SECONDS = 1.0


class Calendar(Protocol):
    def session(self, day: date) -> Session | None: ...

    def is_open(self, at: datetime) -> bool: ...

    def next_session(self, after: datetime) -> Session: ...


class Core(Protocol):
    def assess(self) -> str: ...

    async def reconcile(self, kind: ReconKind) -> bool: ...


@dataclass(frozen=True)
class BrokerWindow:
    """券商每日重启窗口：zone 时区里每天 start 起持续 length。"""

    zone: ZoneInfo
    start: time
    length: timedelta

    def contains(self, at: datetime) -> bool:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("时间必须带时区")
        local = at.astimezone(self.zone)
        for day in (local.date() - timedelta(days=1), local.date()):  # 窗口可能跨午夜
            begin = datetime.combine(day, self.start, tzinfo=self.zone)
            if begin <= local < begin + self.length:
                return True
        return False


@dataclass(frozen=True)
class ScheduleSettings:
    assess_every: timedelta = timedelta(seconds=15)
    intraday_every: timedelta = timedelta(minutes=5)
    end_of_day_delay: timedelta = timedelta(minutes=10)
    broker_window: BrokerWindow | None = None


@dataclass
class _State:
    next_assess: datetime | None = None
    last_intraday: datetime | None = None
    eod_done: set[date] = field(default_factory=set)


class Scheduler:
    def __init__(
        self,
        core: Core,
        calendar: Calendar,
        settings: ScheduleSettings,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if settings.assess_every <= timedelta(0) or settings.intraday_every <= timedelta(0):
            raise ValueError("assess_every 与 intraday_every 必须为正")
        if settings.end_of_day_delay < timedelta(0):
            raise ValueError("end_of_day_delay 不能为负")
        self._core = core
        self._calendar = calendar
        self._settings = settings
        self._clock = clock
        self._sleep = sleep
        self._state = _State()

    def in_broker_window(self, at: datetime) -> bool:
        window = self._settings.broker_window
        return window is not None and window.contains(at)

    async def run(self) -> None:
        """常驻：每 TICK_SECONDS 检查一次到期的任务。"""
        while True:
            await self.run_due()
            await self._sleep(TICK_SECONDS)

    async def run_due(self) -> list[str]:
        """跑一次到期的任务，返回跑了哪些（测试与日志用）。"""
        now = self._clock()
        done: list[str] = []
        state = self._state
        if state.next_assess is None or now >= state.next_assess:
            self._core.assess()
            state.next_assess = now + self._settings.assess_every
            done.append("assess")
        session = self._today(now)
        if session is None:
            return done
        if self._intraday_due(now, session):
            await self._reconcile(ReconKind.INTRADAY)
            state.last_intraday = now
            done.append("intraday")
        if (
            session.day not in state.eod_done
            and now >= session.close + self._settings.end_of_day_delay
        ):
            await self._reconcile(ReconKind.END_OF_DAY)
            state.eod_done.add(session.day)
            done.append("end_of_day")
        return done

    def _today(self, now: datetime) -> Session | None:
        try:
            return self._calendar.session(now.date())
        except Exception as exc:  # 日历查不到：失败即关闭，只评估不对账
            log_event(log, logging.ERROR, "交易日历查不到，跳过对账", error=type(exc).__name__)
            return None

    def _intraday_due(self, now: datetime, session: Session) -> bool:
        if not session.open <= now < session.close or self.in_broker_window(now):
            return False
        last = self._state.last_intraday
        if last is None or last < session.open:
            return True
        return now - last >= self._settings.intraday_every

    async def _reconcile(self, kind: ReconKind) -> None:
        try:
            clean = await self._core.reconcile(kind)
        except Exception as exc:  # 对账本身出错：下一轮照常
            log_event(
                log, logging.ERROR, "调度的对账出错", kind=kind.value, error=type(exc).__name__
            )
            return
        log_event(log, logging.INFO if clean else logging.WARNING, "调度的对账完成",
                  kind=kind.value, clean=clean)  # fmt: skip


__all__ = ("BrokerWindow", "Calendar", "Core", "ScheduleSettings", "Scheduler")
