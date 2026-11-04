"""基准策略：ETF 动量轮动（T-02，规则见 docs/tasks/T-02.md）。

纯计算：只依赖 domain 与 strategy.base。全部金额、数量、比率用 Decimal，不用 float。

每月第一个交易日调仓，之后最多重试 retry_days 个交易日（没成交的单当日过期，不追价，
第二天按最新数据重算）：
1. 动量分 = 1、3、6 个月（21、63、126 个交易日）涨幅的平均；6 个月涨幅不大于 0 的不买；
2. SPY 收盘低于 200 日均线时，股票类 ETF 都不买；
3. 保护性止损成交后进入冷却期：至少 cooldown_days 个交易日，并且持续到下一个调仓日；
4. 取动量分最高的 hold_count 只。每只目标 = (1 − 现金保留) ÷ hold_count
   × min(1, 波动率目标 ÷ 60 日年化波动率)；
5. 目标与当前相差不到调仓门槛就不动（清仓不受门槛与最小金额限制）；名义金额小于最小订单金额的不下；
6. 先卖后买：当天有要卖的就只卖，买入留到之后的交易日；
   买入只用已结算现金（ADR-15），并留出现金保留。
限价：买入 = 卖一价 × (1 + 让价)，卖出 = 买一价 × (1 − 让价)，按美分取整。
报价缺失、延迟或不合理即拒绝本次运行。
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from server.domain.enums import OrderClass, Side
from server.domain.market import MarketBar, Quote
from server.strategy.base import Proposal, StrategyContext, StrategyError

UNIVERSE = ("SPY", "QQQ", "IWM", "EFA", "TLT", "GLD")
EQUITY = frozenset({"SPY", "QQQ", "IWM", "EFA"})
TRADING_DAYS_PER_YEAR = 252
CENT = Decimal("0.01")
ZERO = Decimal(0)
ONE = Decimal(1)


@dataclass(frozen=True)
class BenchmarkParams:
    cooldown_days: int  # 止损冷却期（交易日）：来自受保护的风控参数，由装配层只读传入
    universe: tuple[str, ...] = UNIVERSE
    equity: frozenset[str] = EQUITY
    trend_symbol: str = "SPY"
    momentum_days: tuple[int, ...] = (21, 63, 126)
    absolute_days: int = 126
    trend_days: int = 200
    vol_days: int = 60
    vol_target: Decimal = Decimal("0.15")
    hold_count: int = 4
    cash_reserve: Decimal = Decimal("0.05")
    rebalance_band: Decimal = Decimal("0.05")
    min_order: Decimal = Decimal(500)
    price_concession: Decimal = Decimal("0.002")
    retry_days: int = 3

    def __post_init__(self) -> None:
        if self.cooldown_days < 0 or self.hold_count <= 0 or self.retry_days < 0:
            raise ValueError("冷却期、持有数量、重试天数不合法")
        if not ZERO <= self.cash_reserve < ONE or self.vol_target <= ZERO:
            raise ValueError("现金保留比例或波动率目标不合法")
        if not set(self.equity) <= set(self.universe) or self.trend_symbol not in self.universe:
            raise ValueError("股票类标的与趋势标的必须在标的池里")
        if min(self.momentum_days) <= 0 or self.trend_days <= 0 or self.vol_days <= 1:
            raise ValueError("回看窗口必须为正")

    @property
    def history_needed(self) -> int:
        """需要的日线根数（含起点）。"""
        return max(*self.momentum_days, self.absolute_days, self.trend_days, self.vol_days) + 1


@dataclass(frozen=True)
class Plan:
    """一次运行的中间结果（便于测试与日志）。"""

    window_day: int | None  # 调仓窗口第几天（0 = 调仓日）；None = 不在窗口内
    scores: Mapping[str, Decimal] = field(default_factory=dict)
    selected: tuple[str, ...] = ()
    targets: Mapping[str, Decimal] = field(default_factory=dict)  # 目标权重（占 NAV）
    excluded: Mapping[str, str] = field(default_factory=dict)  # 标的 → 没选它的原因


class BenchmarkStrategy:
    def __init__(self, params: BenchmarkParams, *, strategy_id: str = "benchmark") -> None:
        self._params = params
        self._id = strategy_id

    @property
    def strategy_id(self) -> str:
        return self._id

    @property
    def version(self) -> str:
        return "v1"

    @property
    def universe(self) -> frozenset[str]:
        return frozenset(self._params.universe)

    # ---------- 规则 ----------

    def plan(self, ctx: StrategyContext) -> Plan:
        p = self._params
        window = _window_day(ctx.sessions, ctx.session_day)
        if window is None or window > p.retry_days:
            return Plan(window_day=None)
        if ctx.stopped_out is None:  # 没提供止损记录：分不清“没止损”还是“漏传”，不调仓
            raise StrategyError("缺少止损记录，判断不了冷却期，不调仓")
        closes = {s: _closes(ctx.bars.get(s, ()), p.history_needed, s) for s in p.universe}
        trend = closes[p.trend_symbol]
        bear = trend[-1] < _mean(trend[-p.trend_days :])
        scores: dict[str, Decimal] = {}
        excluded: dict[str, str] = {}
        for symbol in p.universe:
            series = closes[symbol]
            scores[symbol] = _mean([_change(series, d) for d in p.momentum_days])
            if _change(series, p.absolute_days) <= ZERO:
                excluded[symbol] = "6 个月涨幅不大于 0"
            elif bear and symbol in p.equity:
                excluded[symbol] = f"{p.trend_symbol} 低于 {p.trend_days} 日均线"
            elif self._cooling(symbol, ctx):
                excluded[symbol] = "止损冷却期"
        eligible = sorted((s for s in p.universe if s not in excluded),
                          key=lambda s: (-scores[s], s))  # fmt: skip
        selected = tuple(eligible[: p.hold_count])
        slot = (ONE - p.cash_reserve) / p.hold_count
        targets = {}
        for symbol in selected:
            vol = _annual_vol(closes[symbol][-(p.vol_days + 1) :])
            scale = ONE if vol <= ZERO else min(ONE, p.vol_target / vol)
            targets[symbol] = slot * scale
        for symbol in eligible[p.hold_count :]:
            excluded[symbol] = "动量排名不在前列"
        return Plan(window, scores, selected, targets, excluded)

    def _cooling(self, symbol: str, ctx: StrategyContext) -> bool:
        if ctx.stopped_out is None:  # plan() 开头已经拒绝；这里只为类型收窄
            raise StrategyError("缺少止损记录，判断不了冷却期，不调仓")
        stop = ctx.stopped_out.get(symbol)
        if stop is None:
            return False
        sessions = list(ctx.sessions)
        since = sum(1 for d in sessions if stop < d <= ctx.session_day)
        if since < self._params.cooldown_days:
            return True
        current = _window_start(sessions, ctx.session_day)
        return current is None or current <= stop  # 止损之后还没到过新的调仓日

    def decide(self, ctx: StrategyContext) -> Sequence[Proposal]:
        plan = self.plan(ctx)
        if plan.window_day is None:
            return []
        nav, settled = _required(ctx.nav, "nav"), _required(ctx.settled_cash, "settled_cash")
        if nav <= ZERO:
            raise StrategyError("净值不为正，不调仓")
        p = self._params
        mids = {s: _mid(self._quote(ctx, s)) for s in p.universe if self._needs_quote(ctx, s, plan)}
        sells = self._sells(ctx, plan, nav, mids)
        if sells:
            return sells  # 先卖后买：买入留到卖出结算之后
        return self._buys(ctx, plan, nav, settled, mids)

    def _needs_quote(self, ctx: StrategyContext, symbol: str, plan: Plan) -> bool:
        return symbol in plan.targets or ctx.positions.get(symbol, ZERO) > ZERO

    def _quote(self, ctx: StrategyContext, symbol: str) -> Quote:
        quote = ctx.quotes.get(symbol)
        if quote is None or quote.bid is None or quote.ask is None:
            raise StrategyError(f"{symbol} 没有买卖报价")
        if quote.delayed:
            raise StrategyError(f"{symbol} 的报价是延迟行情，不能用于下单")
        if quote.quality:
            raise StrategyError(
                f"{symbol} 的报价带质量标记 {sorted(q.value for q in quote.quality)}"
            )
        if not ZERO < quote.bid <= quote.ask:
            raise StrategyError(f"{symbol} 的报价不合理（买 {quote.bid}，卖 {quote.ask}）")
        return quote

    def _sells(
        self, ctx: StrategyContext, plan: Plan, nav: Decimal, mids: Mapping[str, Decimal]
    ) -> list[Proposal]:
        p = self._params
        out = []
        for symbol in sorted(s for s, q in ctx.positions.items() if q > ZERO):
            held = ctx.positions[symbol]
            if held != held.to_integral_value():
                raise StrategyError(f"{symbol} 有碎股持仓 {held}：首期只交易整股，需人工处理")
            quote = self._quote(ctx, symbol)
            limit = _cents(quote.bid * (ONE - p.price_concession))  # type: ignore[operator]
            target = plan.targets.get(symbol, ZERO)
            if target == ZERO:
                qty, why = held, "不在本月选中名单：清仓"
            else:
                current = held * mids[symbol] / nav
                if current - target < p.rebalance_band:
                    continue
                keep = (nav * target / mids[symbol]).to_integral_value(rounding=ROUND_DOWN)
                qty, why = held - keep, f"超出目标 {_pct(current - target)}：减仓"
                if qty * limit < p.min_order:
                    continue
            if qty > ZERO:
                out.append(Proposal(symbol=symbol, side=Side.SELL, quantity=qty, limit_price=limit,
                                    order_class=OrderClass.REDUCE, reason=why))  # fmt: skip
        return out

    def _buys(
        self,
        ctx: StrategyContext,
        plan: Plan,
        nav: Decimal,
        settled: Decimal,
        mids: Mapping[str, Decimal],
    ) -> list[Proposal]:
        p = self._params
        budget = settled - nav * p.cash_reserve  # 只用已结算现金，并留出现金保留
        out = []
        for symbol in plan.selected:  # 按动量排名分配
            held = ctx.positions.get(symbol, ZERO)
            current = held * mids[symbol] / nav
            gap = plan.targets[symbol] - current
            if gap < p.rebalance_band:
                continue
            quote = self._quote(ctx, symbol)
            limit = _cents(quote.ask * (ONE + p.price_concession))  # type: ignore[operator]
            want = (nav * gap / limit).to_integral_value(rounding=ROUND_DOWN)
            afford = (max(budget, ZERO) / limit).to_integral_value(rounding=ROUND_DOWN)
            qty = min(want, afford)
            if qty <= ZERO or qty * limit < p.min_order:
                continue
            budget -= qty * limit
            out.append(Proposal(
                symbol=symbol, side=Side.BUY, quantity=qty, limit_price=limit,
                order_class=OrderClass.OPEN,
                reason=f"动量第 {plan.selected.index(symbol) + 1}，"
                       f"目标 {_pct(plan.targets[symbol])}",
            ))  # fmt: skip
        return out


# ---------- 计算工具（Decimal） ----------


def _required(value: Decimal | None, name: str) -> Decimal:
    if value is None:
        raise StrategyError(f"缺少 {name}，不调仓")
    return value


def _closes(series: Sequence[MarketBar], needed: int, symbol: str) -> list[Decimal]:
    if len(series) < needed:
        raise StrategyError(f"{symbol} 只有 {len(series)} 根日线，至少需要 {needed} 根")
    return [bar.close for bar in series[-needed:]]


def _change(series: Sequence[Decimal], days: int) -> Decimal:
    return series[-1] / series[-1 - days] - ONE


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, ZERO) / len(values)


def _annual_vol(closes: Sequence[Decimal]) -> Decimal:
    returns = [b / a - ONE for a, b in itertools.pairwise(closes)]
    mean = _mean(returns)
    variance = sum(((r - mean) ** 2 for r in returns), ZERO) / (len(returns) - 1)
    return (variance * TRADING_DAYS_PER_YEAR).sqrt()


def _mid(quote: Quote) -> Decimal:
    assert quote.bid is not None and quote.ask is not None  # noqa: S101 - _quote 已检查
    return (quote.bid + quote.ask) / 2


def _cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _pct(value: Decimal) -> str:
    return f"{(value * 100).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}%"


def _window_start(sessions: Sequence[date], today: date) -> date | None:
    """当前调仓窗口的起点：本月第一个交易日。交易日序列必须包含上个月的最后一个交易日才能确认。"""
    month = [d for d in sessions if (d.year, d.month) == (today.year, today.month) and d <= today]
    earlier = [d for d in sessions if d < date(today.year, today.month, 1)]
    if not month or not earlier:
        if sessions:
            raise StrategyError("交易日序列不够长，确认不了本月第一个交易日")
        raise StrategyError("缺少交易日序列")
    return month[0]


def _window_day(sessions: Sequence[date], today: date) -> int | None:
    start = _window_start(sessions, today)
    if start is None:
        return None
    return sum(1 for d in sessions if start < d <= today)


__all__ = ("EQUITY", "UNIVERSE", "BenchmarkParams", "BenchmarkStrategy", "Plan")
