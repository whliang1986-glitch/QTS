"""数据质量门禁与报告（D-05，V4.1 8.1）。

六类检查，各自的失败行为固定（不由调用方改）：

| 检查 | 判定 | 失败时行为 |
|---|---|---|
| 新鲜度 | 最新日线早于“已收盘并已过发布延迟”的最近交易日 | 禁止新增风险 |
| 完整性 | OHLC 逻辑不成立、日线没有恰好覆盖一个交易时段、规格缺失 | 隔离记录，不进入策略或回测 |
| 连续性 | 缺交易日、重复、时间逆序 | 标记并出报告，禁止静默填充 |
| 来源一致性 | 主备数据源收盘价差超阈值、备源缺失 | 报警，不盲目下单 |
| 前视 | 决策使用了决策时刻之后才发布的数据 | 回测失败 |
| 复权一致性 | 同一序列混用复权版本、拆股前后价格比与复权口径不符 | 停止该标的的回测与自动交易 |

- 只检查、出报告与打标记，不修改、不填充任何数据。
- 失败即关闭：无法核对（备源缺失、拆股前后缺 K 线）按不通过处理。
- 阈值没有默认值，由调用方显式给出；全部用 Decimal 与带时区的 UTC 时间。
- 实时报价的陈旧、价差等在下单时由风控数据门禁（R-03）检查，它按这里打的质量标记拒绝。
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from server.data.calendar import CalendarRangeError, Session, TradingCalendar
from server.domain.corporate_actions import CorporateActionKind, CorporateActionRecord
from server.domain.enums import DataQuality
from server.domain.errors import InstrumentSpecMissingError
from server.domain.instrument import Instrument
from server.domain.market import MarketBar, Quote
from server.domain.values import to_utc

UNADJUSTED = "none"  # 未复权序列的 adjustment_version（复权版本由 D-04 定义）
BPS = Decimal(10000)
_FRESHNESS_LOOKBACK_DAYS = 14  # 找“最近应有数据的交易日”时最多往回看的天数


class QualityCheck(StrEnum):
    FRESHNESS = "FRESHNESS"
    COMPLETENESS = "COMPLETENESS"
    CONTINUITY = "CONTINUITY"
    SOURCE_CONSISTENCY = "SOURCE_CONSISTENCY"
    LOOKAHEAD = "LOOKAHEAD"
    ADJUSTMENT = "ADJUSTMENT"


class QualityAction(StrEnum):
    BLOCK_NEW_RISK = "BLOCK_NEW_RISK"  # 禁止新增风险
    QUARANTINE = "QUARANTINE"  # 隔离记录
    REPORT = "REPORT"  # 标记并出报告
    ALERT = "ALERT"  # 报警，不盲目下单
    FAIL_BACKTEST = "FAIL_BACKTEST"  # 回测失败
    HALT_SYMBOL = "HALT_SYMBOL"  # 停止该标的的回测与自动交易


ACTIONS: Mapping[QualityCheck, QualityAction] = {
    QualityCheck.FRESHNESS: QualityAction.BLOCK_NEW_RISK,
    QualityCheck.COMPLETENESS: QualityAction.QUARANTINE,
    QualityCheck.CONTINUITY: QualityAction.REPORT,
    QualityCheck.SOURCE_CONSISTENCY: QualityAction.ALERT,
    QualityCheck.LOOKAHEAD: QualityAction.FAIL_BACKTEST,
    QualityCheck.ADJUSTMENT: QualityAction.HALT_SYMBOL,
}
FLAGS: Mapping[QualityCheck, DataQuality] = {
    QualityCheck.FRESHNESS: DataQuality.STALE,
    QualityCheck.COMPLETENESS: DataQuality.INCOMPLETE,
    QualityCheck.CONTINUITY: DataQuality.GAP,
    QualityCheck.SOURCE_CONSISTENCY: DataQuality.SOURCE_MISMATCH,
    QualityCheck.ADJUSTMENT: DataQuality.ADJUSTMENT_MISMATCH,
}
# 这些检查不通过时，该标的不得新增风险（策略不跑、风控拒单）
_BLOCKING = frozenset(
    {QualityAction.BLOCK_NEW_RISK, QualityAction.ALERT, QualityAction.HALT_SYMBOL}
)


class LookaheadError(Exception):
    """回测使用了决策时刻之后才发布的数据：回测失败。"""


@dataclass(frozen=True)
class QualityThresholds:
    publish_lag: timedelta  # 日线在收盘后多久才算“已发布、可用”
    max_source_diff_bps: Decimal  # 主备数据源收盘价差上限（相对主源）
    split_tolerance: Decimal  # 拆股前后价格比相对口径的允许偏差（例如 0.25 = 25%）

    def __post_init__(self) -> None:
        if self.publish_lag < timedelta(0):
            raise ValueError("发布延迟不能为负")
        for name, value in (
            ("max_source_diff_bps", self.max_source_diff_bps),
            ("split_tolerance", self.split_tolerance),
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} 必须是正的 Decimal")


@dataclass(frozen=True)
class QualityIssue:
    check: QualityCheck
    symbol: str
    day: date | None
    detail: str

    @property
    def action(self) -> QualityAction:
        return ACTIONS[self.check]

    def to_dict(self) -> dict[str, str | None]:
        return {
            "check": self.check.value,
            "action": self.action.value,
            "symbol": self.symbol,
            "day": None if self.day is None else self.day.isoformat(),
            "detail": self.detail,
        }


def _issue(check: QualityCheck, symbol: str, day: date | None, detail: str) -> QualityIssue:
    return QualityIssue(check, symbol, day, detail)


def _day(bar: MarketBar) -> date:
    return bar.start.astimezone(UTC).date()


# ---------- 完整性 ----------


def _bar_problem(
    bar: MarketBar, calendar: TradingCalendar, as_of: datetime, publish_lag: timedelta
) -> str | None:
    if not (bar.low <= min(bar.open, bar.close) and max(bar.open, bar.close) <= bar.high):
        return f"OHLC 不成立：O={bar.open} H={bar.high} L={bar.low} C={bar.close}"
    session = calendar.session(_day(bar))
    if session is None:
        return f"{_day(bar)} 不是交易日"
    if (bar.start, bar.end) != (session.open, session.close):
        return (
            f"日线必须恰好覆盖交易时段 {session.open.isoformat()}–{session.close.isoformat()}，"
            f"实际 {bar.start.isoformat()}–{bar.end.isoformat()}"
        )
    # 盘中拉到的当日 K 线与完整日线形状相同，只能按时间判断：没收盘、没发布就不完整
    if bar_available_at(bar, publish_lag) > as_of:
        return f"{_day(bar)} 的日线在 {as_of.isoformat()} 还没有收盘或发布，是未完成的 K 线"
    return None


def _spec_problem(bar: MarketBar, instruments: Mapping[str, Instrument]) -> str | None:
    spec = instruments.get(bar.symbol)
    try:
        if spec is None or spec.symbol != bar.symbol:
            raise InstrumentSpecMissingError(f"{bar.symbol} 没有标的规格")
        spec.require_tradable()
    except InstrumentSpecMissingError as exc:
        return str(exc)
    return None


def check_completeness(
    bars: Iterable[MarketBar],
    *,
    calendar: TradingCalendar,
    as_of: datetime,
    publish_lag: timedelta,
    instruments: Mapping[str, Instrument] | None = None,
) -> tuple[list[MarketBar], list[MarketBar], list[QualityIssue]]:
    """把日线分成（通过、隔离、问题）。

    as_of 是拉取或检查的时刻：收盘加发布延迟晚于它的 K 线是未完成的，隔离。
    给出 instruments 时，规格缺失的标的整体隔离；下单前的规格检查仍由风控负责（require_tradable）。
    """
    moment = to_utc(as_of)
    accepted: list[MarketBar] = []
    quarantined: list[MarketBar] = []
    issues: list[QualityIssue] = []
    for bar in bars:
        problem = _bar_problem(bar, calendar, moment, publish_lag)
        if problem is None and instruments is not None:
            problem = _spec_problem(bar, instruments)
        if problem is None:
            accepted.append(bar)
        else:
            quarantined.append(bar)
            issues.append(_issue(QualityCheck.COMPLETENESS, bar.symbol, _day(bar), problem))
    return accepted, quarantined, issues


# ---------- 连续性 ----------


def check_continuity(
    symbol: str,
    bars: Sequence[MarketBar],
    *,
    calendar: TradingCalendar,
    start: date,
    end: date,
) -> list[QualityIssue]:
    """按给出的顺序检查一个标的的日线：逆序、重复，以及 [start, end] 内缺少的交易日。"""
    issues: list[QualityIssue] = []

    def add(day: date, detail: str) -> None:
        issues.append(_issue(QualityCheck.CONTINUITY, symbol, day, detail))

    seen: set[date] = set()
    previous: MarketBar | None = None
    for bar in bars:
        day = _day(bar)
        if bar.symbol != symbol:
            # 记在混入的标的名下：不能让本标的当天的好日线被打上标记
            issues.append(
                _issue(QualityCheck.CONTINUITY, bar.symbol, day, f"混入了 {symbol} 的序列")
            )
            continue
        if day in seen:
            add(day, "重复的日线")
        elif previous is not None and bar.start < previous.start:
            add(day, f"时间逆序：排在 {_day(previous)} 之后")
        seen.add(day)
        if previous is None or bar.start > previous.start:
            previous = bar
    for session in calendar.sessions_between(start, end):
        if session.day not in seen:
            add(session.day, "缺少该交易日的日线")
    return issues


# ---------- 新鲜度 ----------


def expected_latest_session(
    calendar: TradingCalendar, now: datetime, publish_lag: timedelta
) -> Session:
    """收盘并已过发布延迟的最近一个交易时段（日线到这一天为止都应当已有）。"""
    moment = to_utc(now)
    day = moment.date()
    for _ in range(_FRESHNESS_LOOKBACK_DAYS):
        session = calendar.session(day)
        if session is not None and session.close + publish_lag <= moment:
            return session
        day -= timedelta(days=1)
    raise ValueError(f"{moment.isoformat()} 之前 {_FRESHNESS_LOOKBACK_DAYS} 天内没有已收盘的交易日")


def check_freshness(
    symbol: str,
    latest: MarketBar | None,
    *,
    calendar: TradingCalendar,
    now: datetime,
    publish_lag: timedelta,
) -> list[QualityIssue]:
    expected = expected_latest_session(calendar, now, publish_lag).day
    if latest is None:
        return [_issue(QualityCheck.FRESHNESS, symbol, expected, "没有任何日线")]
    if latest.symbol != symbol:
        return [_issue(QualityCheck.FRESHNESS, symbol, expected, f"最新日线是 {latest.symbol} 的")]
    if _day(latest) < expected:
        return [_issue(QualityCheck.FRESHNESS, symbol, expected,
                       f"最新日线是 {_day(latest)}，应当已有 {expected}")]  # fmt: skip
    return []


# ---------- 来源一致性 ----------


def _diff_bps(primary: Decimal, secondary: Decimal) -> Decimal:
    return abs(primary - secondary) / primary * BPS


def check_source_consistency(
    symbol: str,
    primary: Sequence[MarketBar],
    secondary: Sequence[MarketBar],
    *,
    max_diff_bps: Decimal,
) -> list[QualityIssue]:
    """逐日比对主备数据源的收盘价；备源缺这一天、与主源是同一来源、复权口径不同，都算无法核对（不通过）。"""
    backup = {_day(b): b for b in secondary if b.symbol == symbol}
    issues: list[QualityIssue] = []

    def add(day: date, detail: str) -> None:
        issues.append(_issue(QualityCheck.SOURCE_CONSISTENCY, symbol, day, detail))

    for bar in primary:
        day = _day(bar)
        other = backup.get(day)
        if other is None:
            add(day, "备源缺少该日，无法核对")
        elif other.source == bar.source:
            add(day, f"备源与主源是同一来源 {bar.source}，无法核对")
        elif other.adjustment_version != bar.adjustment_version:
            add(day, f"主备复权口径不同：{bar.adjustment_version} / {other.adjustment_version}")
        elif (diff := _diff_bps(bar.close, other.close)) > max_diff_bps:
            add(
                day,
                f"收盘价差 {diff:.2f} bps 超过上限 {max_diff_bps}："
                f"{bar.source}={bar.close} {other.source}={other.close}",
            )
    return issues


def flag_quote(
    quote: Quote, secondary: Quote | None, *, max_diff_bps: Decimal, max_skew: timedelta
) -> Quote:
    """有备源报价时比对最新价，以下任一情况打上 SOURCE_MISMATCH 标记：
    价差超上限、任一方没有最新价、标的不同、同一来源、两份报价时间相差超过 max_skew（备源陈旧）。

    首期只有一个实时数据源（IBKR）：secondary 为 None 时不比对、不打标记。
    """
    if max_skew < timedelta(0):
        raise ValueError("max_skew 不能为负")
    if not isinstance(max_diff_bps, Decimal) or not max_diff_bps.is_finite() or max_diff_bps <= 0:
        raise ValueError("max_diff_bps 必须是正的 Decimal")
    if secondary is None:
        return quote
    ok = (
        secondary.symbol == quote.symbol
        and secondary.source != quote.source
        and abs(secondary.ts - quote.ts) <= max_skew
        and quote.last is not None
        and secondary.last is not None
        and _diff_bps(quote.last, secondary.last) <= max_diff_bps
    )
    if ok:
        return quote
    return Quote.model_validate(
        {**quote.model_dump(), "quality": quote.quality | {DataQuality.SOURCE_MISMATCH}}
    )


# ---------- 前视 ----------


def bar_available_at(bar: MarketBar, publish_lag: timedelta) -> datetime:
    """日线在收盘并过了发布延迟之后才可用。"""
    return bar.end + publish_lag


def check_lookahead(
    decision_at: datetime, inputs: Iterable[tuple[str, datetime]]
) -> list[QualityIssue]:
    """inputs 是（标签, 可用时刻）；可用时刻晚于决策时刻即为前视。标签形如 “SPY” 或 “SPY:close”。"""
    moment = to_utc(decision_at)
    issues: list[QualityIssue] = []
    for label, available_at in inputs:
        available = to_utc(available_at)
        if available > moment:
            issues.append(_issue(QualityCheck.LOOKAHEAD, label.split(":", 1)[0], available.date(),
                                 f"{label} 在 {available.isoformat()} 才可用，"
                                 f"决策时刻是 {moment.isoformat()}"))  # fmt: skip
    return issues


def require_no_lookahead(
    decision_at: datetime, bars: Iterable[MarketBar], *, publish_lag: timedelta
) -> None:
    """回测在每个决策点调用：有前视即抛 LookaheadError（回测失败）。"""
    issues = check_lookahead(
        decision_at, ((f"{b.symbol}:{_day(b)}", bar_available_at(b, publish_lag)) for b in bars)
    )
    if issues:
        raise LookaheadError("；".join(i.detail for i in issues))


# ---------- 复权一致性 ----------


def _split_problem(
    action: CorporateActionRecord,
    series: Sequence[MarketBar],
    *,
    unadjusted: bool,
    calendar: TradingCalendar,
    tolerance: Decimal,
) -> str | None:
    assert action.numerator is not None  # noqa: S101 - 模型校验保证拆股有比例
    assert action.denominator is not None  # noqa: S101
    split = Decimal(action.numerator) / Decimal(action.denominator)
    # 未复权的接受区间 [split(1-t), split(1+t)] 与已复权的 [1-t, 1+t] 相交时，
    # 两种口径分不开：无法核对
    distinguishable = (
        split * (1 - tolerance) > 1 + tolerance or split * (1 + tolerance) < 1 - tolerance
    )
    if not distinguishable:
        return (
            f"拆股 {action.event_id} 的比例 {split} 离 1 太近，"
            f"在容差 {tolerance} 内无法区分复权口径"
        )
    before = [b for b in series if _day(b) < action.ex_date]
    after = [b for b in series if _day(b) >= action.ex_date]
    if not before or not after or _day(after[0]) != action.ex_date:
        return f"拆股 {action.event_id} 的除权日或前一交易日缺日线，无法核对"
    gap_start, gap_end = _day(before[-1]) + timedelta(days=1), action.ex_date - timedelta(days=1)
    try:
        between = gap_start <= gap_end and any(calendar.sessions_between(gap_start, gap_end))
    except CalendarRangeError:
        return f"拆股 {action.event_id} 超出交易日历范围，无法核对"
    if between:
        return f"拆股 {action.event_id} 的除权日前一交易日缺日线，无法核对"
    ratio = before[-1].close / after[0].open
    expected = split if unadjusted else Decimal(1)
    if abs(ratio / expected - 1) > tolerance:
        basis = "未复权" if unadjusted else "已复权"
        return (
            f"拆股 {action.event_id}：前收 / 除权开盘 = {ratio:.4f}，按{basis}口径应约为 {expected}"
        )
    return None


def check_adjustment(
    symbol: str,
    bars: Sequence[MarketBar],
    actions: Collection[CorporateActionRecord],
    *,
    calendar: TradingCalendar,
    tolerance: Decimal,
) -> list[QualityIssue]:
    """同一序列只能有一种复权版本；每次拆股前后的价格比必须与复权口径一致。

    未复权序列：前一交易日收盘 / 除权日开盘 ≈ numerator / denominator；已复权序列：≈ 1。
    除权日或紧挨着的前一交易日缺 K 线、拆股比例离 1 太近而分不清口径：无法核对，按不通过处理。
    现金分红的影响小于常见的日内波动，它的复权口径由 D-04 校验。
    """
    series = sorted((b for b in bars if b.symbol == symbol), key=lambda b: b.start)
    if not series:
        return []
    versions = sorted({b.adjustment_version for b in series})
    if len(versions) > 1:
        return [_issue(QualityCheck.ADJUSTMENT, symbol, None, f"同一序列混用复权版本：{versions}")]
    first, last = _day(series[0]), _day(series[-1])
    issues: list[QualityIssue] = []
    for action in sorted(actions, key=lambda a: a.ex_date):
        if action.symbol != symbol or action.kind is not CorporateActionKind.SPLIT:
            continue
        # 除权日正好是序列第一天：没有前一交易日可比，按无法核对处理（调用方应往前多取一个交易日）
        if not first <= action.ex_date <= last:
            continue
        problem = _split_problem(
            action, series, unadjusted=versions[0] == UNADJUSTED, calendar=calendar,
            tolerance=tolerance,
        )  # fmt: skip
        if problem is not None:
            issues.append(_issue(QualityCheck.ADJUSTMENT, symbol, action.ex_date, problem))
    return issues


# ---------- 报告 ----------


@dataclass(frozen=True)
class QualityReport:
    checked_at: datetime
    issues: tuple[QualityIssue, ...]
    accepted: tuple[MarketBar, ...]
    quarantined: tuple[MarketBar, ...]

    @property
    def passed(self) -> bool:
        return not self.issues

    def blocked_symbols(self) -> frozenset[str]:
        """不得新增风险的标的（新鲜度、来源一致性、复权一致性不通过）。"""
        return frozenset(i.symbol for i in self.issues if i.action in _BLOCKING)

    def flagged(self) -> tuple[MarketBar, ...]:
        """通过完整性检查的日线，按当日问题打上质量标记（不改数值）。"""
        flags: dict[tuple[str, date | None], set[DataQuality]] = {}
        for issue in self.issues:
            flag = FLAGS.get(issue.check)
            if flag is not None:
                flags.setdefault((issue.symbol, issue.day), set()).add(flag)
        out = []
        for bar in self.accepted:
            extra = flags.get((bar.symbol, _day(bar)), set()) | flags.get((bar.symbol, None), set())
            if extra:
                quality = bar.quality | frozenset(extra)
                out.append(MarketBar.model_validate({**bar.model_dump(), "quality": quality}))
            else:
                out.append(bar)
        return tuple(out)

    def to_json(self) -> str:
        data = {
            "checked_at": self.checked_at.isoformat(),
            "passed": self.passed,
            "blocked_symbols": sorted(self.blocked_symbols()),
            "accepted": len(self.accepted),
            "quarantined": len(self.quarantined),
            "issues": [i.to_dict() for i in self.issues],
        }
        return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def assess_daily_bars(  # noqa: PLR0913 - 每项检查的输入都显式给出
    symbol: str,
    bars: Sequence[MarketBar],
    *,
    calendar: TradingCalendar,
    start: date,
    end: date,
    now: datetime,
    thresholds: QualityThresholds,
    actions: Collection[CorporateActionRecord] = (),
    secondary: Sequence[MarketBar] | None = None,
    instruments: Mapping[str, Instrument] | None = None,
) -> QualityReport:
    """对一个标的的 [start, end] 日线跑完整性、连续性、新鲜度、复权与（有备源时）来源一致性检查。

    - 别的标的的 K 线、同一交易日的多根 K 线（不知道哪根对）一律隔离，不进 accepted；
    - now 超出交易日历范围时抛 CalendarRangeError：调用方（D-02）必须把它当作数据不健康；
    - 前视检查在回测的每个决策点单独调用（require_no_lookahead）。
    """
    lag = thresholds.publish_lag
    accepted, quarantined, issues = check_completeness(
        bars, calendar=calendar, as_of=now, publish_lag=lag, instruments=instruments
    )
    days: dict[date, int] = {}
    for bar in bars:  # 按全部本标的 K 线计数：同一天一好一坏也说明不知道哪根对
        if bar.symbol == symbol:
            days[_day(bar)] = days.get(_day(bar), 0) + 1
    kept: list[MarketBar] = []
    for bar in accepted:
        if bar.symbol != symbol:
            problem = f"不是 {symbol} 的日线（{bar.symbol}）"
        elif days[_day(bar)] > 1:
            problem = "同一交易日有多根日线，无法判断哪根正确"
        else:
            kept.append(bar)
            continue
        quarantined.append(bar)
        # 问题记在这根 K 线自己的标的名下，不误标本标的当天的其他日线
        issues.append(_issue(QualityCheck.COMPLETENESS, bar.symbol, _day(bar), problem))
    accepted = kept
    issues += check_continuity(symbol, bars, calendar=calendar, start=start, end=end)
    latest = max(accepted, key=lambda b: b.start, default=None)
    issues += check_freshness(symbol, latest, calendar=calendar, now=now, publish_lag=lag)
    issues += check_adjustment(
        symbol, accepted, actions, calendar=calendar, tolerance=thresholds.split_tolerance
    )
    if secondary is not None:
        issues += check_source_consistency(
            symbol, accepted, secondary, max_diff_bps=thresholds.max_source_diff_bps
        )
    return QualityReport(
        checked_at=to_utc(now),
        issues=tuple(issues),
        accepted=tuple(accepted),
        quarantined=tuple(quarantined),
    )
