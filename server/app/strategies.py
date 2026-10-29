"""策略调度（T-01）：由交易日历驱动，在 workerd 里运行策略并把意图放进 outbox。

- 决策时刻 = 交易时段开盘 + offset（必须早于收盘）；不在时段内或还没到决策时刻都不运行。
  意图是当日有效的限价单：tradingd 只接受 5 分钟内生成的意图（S-01d），
  所以在开盘后运行，而不是前一晚。
- 输入：从数据湖读每个标的的日线，只保留在决策时刻之前收盘并过了发布延迟的；
  质量门禁阻断的标的（D-05 / D-02 的 blocked）或缺数据的标的所在的策略整个跳过（失败即关闭）。
- 输出：意图逐条放进 outbox（幂等键见 server/strategy/base.py）；同键同内容重复入队无副作用，
  同键不同内容拒绝。策略出错只影响它自己，不影响其他策略。
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from server.data.calendar import CalendarRangeError, TradingCalendar
from server.data.lake import DataLake, LakeError
from server.data.quality import bar_available_at
from server.domain.market import MarketBar
from server.domain.values import to_utc
from server.ipc.outbox import Outbox, OutboxConflictError, OutboxError
from server.strategy.base import Strategy, StrategyContext, StrategyError, evaluate


class ScheduleError(Exception):
    """调度参数或交易日历不合法：拒绝运行。"""


# tradingd 只接受 5 分钟内生成的意图（S-01d）：运行窗口必须明显短于它，留出投递时间
MAX_RUN_WINDOW = timedelta(minutes=4)


@dataclass(frozen=True)
class StrategySchedule:
    decision_offset: timedelta  # 开盘后多久做决策
    run_window: timedelta  # 决策时刻之后多久内还可以运行（意图的 created_at 固定为决策时刻）
    lookback_days: int  # 读多少个日历日的历史日线
    publish_lag: timedelta  # 日线收盘后多久才算可用（与数据质量门禁一致）

    def __post_init__(self) -> None:
        if self.decision_offset <= timedelta(0):
            raise ScheduleError("决策时刻必须在开盘之后")
        if not timedelta(0) < self.run_window <= MAX_RUN_WINDOW:
            raise ScheduleError(f"运行窗口必须为正且不超过 {MAX_RUN_WINDOW}（tradingd 的意图限龄）")
        if self.lookback_days <= 0 or self.publish_lag < timedelta(0):
            raise ScheduleError("回看天数必须为正，发布延迟不能为负")


def decision_time(
    calendar: TradingCalendar, day: date, schedule: StrategySchedule
) -> datetime | None:
    """这一天的决策时刻；非交易日返回 None；决策时刻加运行窗口不早于收盘时拒绝（含提前收市）。"""
    try:
        session = calendar.session(day)
    except CalendarRangeError as exc:
        raise ScheduleError(f"交易日历不覆盖 {day}：{exc}") from exc
    if session is None:
        return None
    at = session.open + schedule.decision_offset
    if at + schedule.run_window >= session.close:
        raise ScheduleError(f"{day} 的决策时刻 {at.isoformat()} 加运行窗口不早于收盘")
    return at


@dataclass(frozen=True)
class RunReport:
    enqueued: tuple[str, ...] = ()  # 新入队的意图
    duplicates: tuple[str, ...] = ()  # 同键同内容、已在 outbox 的意图（同日重跑）
    skipped: dict[str, str] = field(default_factory=dict)  # 策略 → 跳过原因
    failed: dict[str, str] = field(default_factory=dict)  # 策略 → 失败原因


def _history(  # noqa: PLR0913 - 读取窗口与可用时刻都显式给出
    lake: DataLake, symbol: str, start: date, end: date, *, at: datetime, lag: timedelta
) -> list[MarketBar]:
    bars = lake.read_bars(symbol, start, end)
    return [b for b in bars if bar_available_at(b, lag) <= at]


def _previous_session(calendar: TradingCalendar, day: date) -> date:
    probe = day - timedelta(days=1)
    for _ in range(14):
        if calendar.is_trading_day(probe):
            return probe
        probe -= timedelta(days=1)
    raise ScheduleError(f"{day} 之前 14 天内没有交易日")


def _label(strategy: Strategy, index: int) -> str:
    try:
        return str(strategy.strategy_id)
    except Exception:  # 属性本身出错：用序号标识
        return f"#{index}"


def run_strategies(  # noqa: PLR0912, PLR0913 - 调度的全部输入与跳过原因逐项列出
    strategies: Sequence[Strategy],
    *,
    lake: DataLake,
    calendar: TradingCalendar,
    outbox: Outbox,
    schedule: StrategySchedule,
    now: datetime,
    account_id: str,
    positions: Callable[[], Mapping[str, Decimal]],
    blocked: Collection[str],
) -> RunReport:
    """在决策时刻之后的运行窗口内运行一次全部策略。同一天重复调用是安全的：
    意图的生成时间与行情输入都按决策时刻固定，重跑得到相同的意图（outbox 去重）；
    持仓是运行时读取的：第一轮的单成交后持仓变化，重跑可能同键不同内容（被拒）或产生新的键（由风控兜底）。
    outbox 本身出错（非内容冲突）时上抛，由 workerd 告警，而不是记成某个策略的失败。

    注意：同一策略多条意图逐条入队，遇到冲突即停止，已入队的保留（入队不是整批原子的）。
    """
    moment = to_utc(now)
    labels = [_label(s, i) for i, s in enumerate(strategies)]
    if len(set(labels)) != len(labels):
        raise ScheduleError(f"同一轮里有重复的策略标识：{labels}")
    day = calendar.session_day(moment) if calendar.is_open(moment) else None
    if day is None:
        return RunReport(skipped=dict.fromkeys(labels, "不在交易时段内"))
    at = decision_time(calendar, day, schedule)
    if at is None or moment < at:
        return RunReport(skipped=dict.fromkeys(labels, "还没到决策时刻"))
    if moment > at + schedule.run_window:
        return RunReport(skipped=dict.fromkeys(labels, "已过运行窗口"))
    held = dict(positions())
    expected = _previous_session(calendar, day)  # 最新日线必须是上一交易日（数据新鲜）
    start = day - timedelta(days=schedule.lookback_days)
    enqueued: list[str] = []
    duplicates: list[str] = []
    skipped: dict[str, str] = {}
    failed: dict[str, str] = {}
    for strategy, sid in zip(strategies, labels, strict=True):
        try:
            universe = frozenset(strategy.universe)
            bad = sorted(universe & set(blocked))
            if bad:
                skipped[sid] = f"数据质量门禁阻断：{bad}"
                continue
            try:
                bars = {
                    symbol: _history(lake, symbol, start, day, at=at, lag=schedule.publish_lag)
                    for symbol in sorted(universe)
                }
            except LakeError as exc:
                skipped[sid] = f"读不到数据：{exc}"
                continue
            stale = sorted(
                s for s, series in bars.items() if not series or series[-1].start.date() != expected
            )
            if stale:
                skipped[sid] = f"缺数据或数据不新鲜（应有 {expected}）：{stale}"
                continue
            context = StrategyContext(
                decision_at=at, session_day=day, account_id=account_id, bars=bars,
                positions={s: q for s, q in held.items() if s in universe},
            )  # fmt: skip
            try:
                intents = evaluate(strategy, context)
            except StrategyError as exc:
                failed[sid] = str(exc)
                continue
            for intent in intents:
                try:
                    fresh = outbox.enqueue(intent)
                except OutboxConflictError as exc:  # 同键不同内容：绝不多下一单
                    failed[sid] = f"{intent.intent_id} 入队被拒：{exc}"
                    break
                (enqueued if fresh else duplicates).append(intent.intent_id)
        except OutboxError:  # outbox 本身出错（数据库损坏、磁盘满）：不是单个策略的问题，上抛
            raise
        except Exception as exc:  # 一个策略的任何意外错误只影响它自己
            failed[sid] = f"意外错误 {type(exc).__name__}：{exc}"
    return RunReport(tuple(enqueued), tuple(duplicates), skipped, failed)
