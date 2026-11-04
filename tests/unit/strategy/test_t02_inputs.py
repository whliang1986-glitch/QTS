"""T-02 新增输入的校验（运行器 freeze）与基准策略的订单生命周期。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from server.domain.enums import DataQuality, OrderClass, Side
from server.domain.market import Quote
from server.strategy.base import StrategyContext, StrategyError, evaluate
from tests.unit.strategy.test_benchmark import NAV, STRATEGY, context, quote

# ---------- freeze：前视与输入校验 ----------


def _bad(ctx: StrategyContext, **changes: Any) -> StrategyContext:
    return replace(ctx, **changes)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"sessions": []}, None),  # 空序列由策略自己拒绝（下一条用例）
        ({"nav": Decimal("NaN")}, "nav 必须是有限的 Decimal"),
        ({"nav": Decimal("Infinity")}, "nav 必须是有限的 Decimal"),
        ({"nav": 100000.0}, "nav 必须是有限的 Decimal"),
        ({"settled_cash": 5}, "settled_cash 必须是有限的 Decimal"),
        ({"positions": {"SPY": Decimal(-1)}}, "持仓不能为负"),
        ({"positions": {"SPY": Decimal("NaN")}}, "持仓不合法"),
    ],
)
def test_invalid_amounts_are_refused(changes: dict[str, Any], message: str | None) -> None:
    ctx = _bad(context(), **changes)
    with pytest.raises(StrategyError, match=message or "交易日序列"):
        evaluate(STRATEGY, ctx)


def test_quote_after_decision_time_is_lookahead() -> None:
    ctx = context()
    late = quote("SPY", "100.00", "100.10")
    quotes = dict(ctx.quotes) | {"SPY": replace_ts(late, ctx.decision_at + timedelta(seconds=1))}
    with pytest.raises(StrategyError, match="报价时间晚于决策时刻"):
        evaluate(STRATEGY, _bad(ctx, quotes=quotes))


def replace_ts(q: Any, ts: datetime) -> Any:
    return Quote(**(q.model_dump() | {"ts": ts}))


@pytest.mark.parametrize(
    "sessions",
    [
        lambda s: list(reversed(s)),  # 不是升序
        lambda s: [*s, s[-1]],  # 有重复
        lambda s: s[:-1],  # 不以当日结尾
    ],
)
def test_sessions_must_be_ascending_and_end_today(sessions: Any) -> None:
    ctx = context()
    with pytest.raises(StrategyError, match="交易日序列必须严格升序"):
        evaluate(STRATEGY, _bad(ctx, sessions=sessions(list(ctx.sessions))))


def test_bars_must_be_strictly_ascending() -> None:
    ctx = context()
    spy = list(ctx.bars["SPY"])
    spy[-1], spy[-2] = spy[-2], spy[-1]
    with pytest.raises(StrategyError, match="严格按时间升序"):
        evaluate(STRATEGY, _bad(ctx, bars=dict(ctx.bars) | {"SPY": spy}))


def test_missing_stop_history_refuses_to_rebalance() -> None:
    with pytest.raises(StrategyError, match="缺少止损记录"):
        evaluate(STRATEGY, _bad(context(), stopped_out=None))


def test_flagged_quote_and_fractional_position_are_refused() -> None:
    ctx = context()
    base = quote("TLT", "100.00", "100.10").model_dump()
    flagged = Quote(**(base | {"quality": frozenset({DataQuality.STALE})}))
    with pytest.raises(StrategyError, match="质量标记"):
        evaluate(STRATEGY, _bad(ctx, quotes=dict(ctx.quotes) | {"TLT": flagged}))
    with pytest.raises(StrategyError, match="碎股"):
        evaluate(STRATEGY, _bad(ctx, positions={"EFA": Decimal("10.5")}))


def test_missing_cash_or_non_positive_nav_refuses() -> None:
    with pytest.raises(StrategyError, match="settled_cash"):
        evaluate(STRATEGY, _bad(context(), settled_cash=None))
    with pytest.raises(StrategyError, match="净值不为正"):
        evaluate(STRATEGY, _bad(context(), nav=Decimal(0)))


# ---------- 订单生命周期 ----------


def at(day: date) -> datetime:
    return datetime.combine(day, time(15, 0), UTC)


def test_sell_day_then_buy_day_with_new_keys() -> None:
    """第 0 天只卖（先卖后买）；第 1 天卖单已成交、按新持仓重算：只买，幂等键带新日期。"""
    day0 = evaluate(STRATEGY, context(positions={"EFA": Decimal(50)}))
    assert [(i.symbol, i.side) for i in day0] == [("EFA", Side.SELL)]
    assert day0[0].intent_id == "benchmark:v1:2026-12-01:EFA:SELL:REDUCE"
    day1 = evaluate(STRATEGY, context(day=date(2026, 12, 2)))
    assert {(i.symbol, i.side, i.order_class) for i in day1} == {
        (s, Side.BUY, OrderClass.OPEN) for s in ("QQQ", "TLT", "SPY", "GLD")
    }
    assert all(i.intent_id.startswith("benchmark:v1:2026-12-02:") for i in day1)
    assert all(i.created_at == at(date(2026, 12, 2)) for i in day1)


def test_retry_days_recompute_until_the_window_closes() -> None:
    """没成交的单当日过期不追价：第 1 到 3 个交易日按最新持仓重算，之后不再下单。"""
    for day, expect in ((date(2026, 12, 2), True), (date(2026, 12, 3), True),
                        (date(2026, 12, 4), True), (date(2026, 12, 7), False)):  # fmt: skip
        assert bool(STRATEGY.decide(context(day=day))) is expect, day


def test_same_day_rerun_gives_identical_intents() -> None:
    first = evaluate(STRATEGY, context())
    again = evaluate(STRATEGY, context())
    assert [i.model_dump() for i in first] == [i.model_dump() for i in again]


def test_partial_reduce_when_above_target_by_more_than_the_band() -> None:
    # 持有 QQQ 约 35%（目标 23.75%）：超出 5 个百分点以上，减到目标股数
    held = Decimal(350)
    proposals = STRATEGY.decide(context(positions={"QQQ": held}))
    [sell] = proposals
    assert (sell.symbol, sell.side, sell.order_class) == ("QQQ", Side.SELL, OrderClass.REDUCE)
    keep = (NAV * Decimal("0.2375") / Decimal("100.05")).to_integral_value(rounding="ROUND_DOWN")
    assert sell.quantity == held - keep
