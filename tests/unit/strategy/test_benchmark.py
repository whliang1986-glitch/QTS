"""基准策略（T-02）：按规则手工复算动量、过滤、冷却期、波动率缩放、先卖后买与资金约束。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_DOWN, Decimal

import pytest
from server.domain.enums import OrderClass, Side
from server.domain.market import MarketBar, Quote
from server.strategy.base import StrategyContext, StrategyError, evaluate
from server.strategy.benchmark import BenchmarkParams, BenchmarkStrategy

TODAY = date(2026, 12, 1)  # 12 月第一个交易日（周二）
NAV = Decimal(100000)


def weekdays(end: date, count: int) -> list[date]:
    days: list[date] = []
    d = end
    while len(days) < count:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return sorted(days)


SESSIONS = weekdays(TODAY, 260)  # 截至今天的交易日（简化：周一到周五）
HISTORY = [d for d in SESSIONS if d < TODAY][-230:]  # 今天之前的 230 根日线


def bars(symbol: str, closes: Sequence[Decimal]) -> list[MarketBar]:
    out = []
    for day, close in zip(HISTORY[-len(closes) :], closes, strict=True):
        start = datetime.combine(day, time(14, 30), UTC)
        out.append(MarketBar(
            symbol=symbol, start=start, end=start + timedelta(hours=6, minutes=30),
            open=close, high=close, low=close, close=close, volume=Decimal(1000000),
            source="test", adjustment_version="v1",
        ))  # fmt: skip
    return out


def growth(rate: str, n: int = 230) -> list[Decimal]:
    """每天固定涨 rate：波动率为 0（不缩放）。"""
    price, out = Decimal(100), []
    for _ in range(n):
        out.append(price)
        price = (price * (1 + Decimal(rate))).quantize(Decimal("0.0001"))
    return out


def alternating(up: str, down: str, n: int = 230) -> list[Decimal]:
    """涨跌交替：用来手工复算波动率。"""
    price, out = Decimal(100), []
    for i in range(n):
        out.append(price)
        price = (price * (1 + Decimal(up if i % 2 == 0 else down))).quantize(Decimal("0.0001"))
    return out


def quote(symbol: str, bid: str, ask: str, *, delayed: bool = False) -> Quote:
    return Quote(symbol=symbol, ts=datetime.combine(TODAY, time(15, 0), UTC), bid=Decimal(bid),
                 ask=Decimal(ask), last=None, source="test", delayed=delayed)  # fmt: skip


DEFAULT_RATES = {"SPY": "0.001", "QQQ": "0.002", "IWM": "0.0005", "EFA": "-0.0005",
                 "TLT": "0.0015", "GLD": "0.0008"}  # fmt: skip


def context(
    *,
    day: date = TODAY,
    series: dict[str, list[Decimal]] | None = None,
    positions: dict[str, Decimal] | None = None,
    settled: Decimal = NAV,
    stopped: dict[str, date] | None = None,
    quotes: dict[str, Quote] | None = None,
) -> StrategyContext:
    data = series or {s: growth(r) for s, r in DEFAULT_RATES.items()}
    sessions = [d for d in SESSIONS if d <= day] + [d for d in weekdays(day, 10) if d > TODAY]
    sessions = sorted(set(d for d in sessions if d <= day))
    return StrategyContext(
        decision_at=datetime.combine(day, time(15, 0), UTC), session_day=day, account_id="DU1",
        bars={s: bars(s, c) for s, c in data.items()}, positions=positions or {},
        quotes=quotes if quotes is not None else {s: quote(s, "100.00", "100.10") for s in data},
        nav=NAV, settled_cash=settled, sessions=sessions, stopped_out=stopped or {}, mode="PAPER",
    )  # fmt: skip


STRATEGY = BenchmarkStrategy(BenchmarkParams(cooldown_days=5))


def test_selection_follows_momentum_and_absolute_filter() -> None:
    plan = STRATEGY.plan(context())
    assert plan.window_day == 0
    assert plan.selected == ("QQQ", "TLT", "SPY", "GLD")  # 按日涨幅排序
    assert plan.excluded["EFA"] == "6 个月涨幅不大于 0"
    assert plan.excluded["IWM"] == "动量排名不在前列"
    for symbol in plan.selected:  # 波动率为 0：不缩放，每只 95% ÷ 4
        assert plan.targets[symbol] == Decimal("0.2375")


def test_momentum_score_is_hand_computed() -> None:
    closes = growth("0.002")
    plan = STRATEGY.plan(context())
    expected = sum(closes[-1] / closes[-1 - d] - 1 for d in (21, 63, 126)) / 3
    assert plan.scores["QQQ"] == expected


def test_volatility_scaling_hand_computed() -> None:
    # 涨 2.2%、跌 1.8% 交替：日收益偏离均值 ±2%，60 个收益的样本方差 = 60 × 0.02² ÷ 59
    series = {s: growth(r) for s, r in DEFAULT_RATES.items()}
    series["TLT"] = alternating("0.022", "-0.018")
    plan = STRATEGY.plan(context(series=series))
    closes = series["TLT"][-61:]  # 手工复算：60 个日收益 → 样本方差 → 年化
    returns = [closes[i + 1] / closes[i] - 1 for i in range(60)]
    mean = sum(returns, Decimal(0)) / 60
    variance = sum(((r - mean) ** 2 for r in returns), Decimal(0)) / 59
    vol = (variance * 252).sqrt()
    assert abs(vol - (Decimal(60) * Decimal("0.0004") / 59 * 252).sqrt()) < Decimal("0.001")
    assert "TLT" in plan.selected
    assert plan.targets["TLT"] == Decimal("0.2375") * (Decimal("0.15") / vol)
    assert plan.targets["TLT"] < Decimal("0.12")  # 年化约 32%：只配一半左右


def test_spy_below_200_day_average_blocks_equities() -> None:
    series = {s: growth(r) for s, r in DEFAULT_RATES.items()}
    spy = growth("0.002", 200) + [Decimal(150) * (1 - Decimal("0.01") * i) for i in range(30)]
    series["SPY"] = spy  # 先涨后跌：6 个月仍上涨，但已跌破 200 日均线
    plan = STRATEGY.plan(context(series=series))
    for symbol in ("SPY", "QQQ", "IWM", "EFA"):
        assert symbol in plan.excluded
    assert set(plan.selected) <= {"TLT", "GLD"}
    assert "均线" in plan.excluded["QQQ"]


def test_cooldown_after_protective_stop() -> None:
    recent = STRATEGY.plan(context(stopped={"QQQ": date(2026, 11, 27)}))  # 2 个交易日前
    assert recent.excluded["QQQ"] == "止损冷却期"
    old = STRATEGY.plan(context(stopped={"QQQ": date(2026, 11, 2)}))  # 很久以前，且早于本月调仓日
    assert "QQQ" in old.selected
    # 调仓窗口第 2 天：止损发生在本月调仓日当天，之后才过 2 天：
    # 不足 5 天，也没到下一个调仓日，仍在冷却期
    later = STRATEGY.plan(context(day=date(2026, 12, 3), stopped={"QQQ": TODAY}))
    assert later.excluded["QQQ"] == "止损冷却期"


def test_outside_rebalance_window_does_nothing() -> None:
    day = date(2026, 12, 7)  # 调仓日之后第 4 个交易日：超过 3 天重试
    ctx = context(day=day, quotes={})  # 没有报价也不出错：根本不需要下单
    assert STRATEGY.plan(ctx).window_day is None
    assert STRATEGY.decide(ctx) == []


def test_fresh_account_buys_four_names_with_settled_cash() -> None:
    proposals = STRATEGY.decide(context())
    assert [p.symbol for p in proposals] == ["QQQ", "TLT", "SPY", "GLD"]
    for p in proposals:
        assert p.side is Side.BUY and p.order_class is OrderClass.OPEN
        assert p.limit_price == Decimal("100.30")  # 卖一价 100.10 × 1.002 = 100.3002 → 100.30
        assert p.quantity == (NAV * Decimal("0.2375") / Decimal("100.30")).to_integral_value(
            rounding=ROUND_DOWN
        )


def test_buys_are_limited_by_settled_cash_and_reserve() -> None:
    # 已结算现金只有 3 万：留出 5% NAV（5000）后只能花 2.5 万，按动量排名分配
    proposals = STRATEGY.decide(context(settled=Decimal(30000)))
    assert [p.symbol for p in proposals] == ["QQQ", "TLT"]  # 第一名买足，第二名用剩下的
    assert proposals[0].quantity == Decimal(236)  # 23750 ÷ 100.30 向下取整
    assert proposals[1].quantity == Decimal(13)  # (25000 − 236 × 100.30) ÷ 100.30 = 13.25 → 13
    assert sum(p.quantity * p.limit_price for p in proposals) <= Decimal(25000)


def test_sells_first_and_no_buys_on_the_same_day() -> None:
    proposals = STRATEGY.decide(context(positions={"EFA": Decimal(50)}, settled=NAV))
    assert len(proposals) == 1
    sell = proposals[0]
    assert (sell.symbol, sell.side, sell.order_class) == ("EFA", Side.SELL, OrderClass.REDUCE)
    assert sell.quantity == Decimal(50)  # 不在名单：清仓，不受最小金额限制
    assert sell.limit_price == Decimal("99.80")  # 买一价 100.00 × 0.998


def test_band_keeps_close_positions_untouched() -> None:
    near = (NAV * Decimal("0.22") / Decimal("100.05")).to_integral_value(rounding=ROUND_DOWN)
    proposals = STRATEGY.decide(context(positions={"QQQ": near}, settled=Decimal(5000)))
    assert all(p.symbol != "QQQ" for p in proposals)  # 差不到 5 个百分点：不动


def test_missing_or_delayed_quote_refuses_the_run() -> None:
    quotes = {s: quote(s, "100.00", "100.10") for s in DEFAULT_RATES}
    del quotes["TLT"]
    with pytest.raises(StrategyError, match="TLT 没有买卖报价"):
        STRATEGY.decide(context(quotes=quotes))
    quotes["TLT"] = quote("TLT", "100.00", "100.10", delayed=True)
    with pytest.raises(StrategyError, match="延迟行情"):
        STRATEGY.decide(context(quotes=quotes))


def test_short_history_or_sessions_refuses() -> None:
    short = {s: growth(r, 150) for s, r in DEFAULT_RATES.items()}
    with pytest.raises(StrategyError, match="至少需要"):
        STRATEGY.plan(context(series=short))
    ctx = context()
    bad = StrategyContext(
        decision_at=ctx.decision_at, session_day=ctx.session_day, account_id="DU1", bars=ctx.bars,
        sessions=[d for d in ctx.sessions if d >= TODAY], nav=NAV, settled_cash=NAV,
    )  # fmt: skip
    with pytest.raises(StrategyError, match="交易日序列不够长"):
        STRATEGY.plan(bad)


def test_runner_accepts_the_proposals() -> None:
    """经过运行器（evaluate）：意图都是当日有效限价单，幂等键按策略、版本、日期、标的。"""
    intents = evaluate(STRATEGY, context())
    assert {i.intent_id for i in intents} == {
        f"benchmark:v1:2026-12-01:{s}:BUY:OPEN" for s in ("QQQ", "TLT", "SPY", "GLD")
    }


def test_params_validation() -> None:
    with pytest.raises(ValueError, match="现金保留"):
        BenchmarkParams(cooldown_days=5, cash_reserve=Decimal(1))
    with pytest.raises(ValueError, match="趋势标的"):
        BenchmarkParams(cooldown_days=5, trend_symbol="XYZ")
