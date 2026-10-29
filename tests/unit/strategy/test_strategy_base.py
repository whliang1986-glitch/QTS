"""策略接口与运行器（T-01）：只提意图、逐条校验、整批拒绝、确定性幂等键。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from server.domain.enums import OrderClass, OrderType, Side, TimeInForce
from server.domain.market import MarketBar
from server.strategy.base import (
    Proposal,
    StrategyContext,
    StrategyError,
    build_intents,
    evaluate,
    freeze,
    identity_of,
)

DAY = date(2026, 10, 23)
DECISION = datetime(2026, 10, 23, 13, 45, tzinfo=UTC)


def bar(symbol: str, day: date, close: str = "100") -> MarketBar:
    start = datetime(day.year, day.month, day.day, 13, 30, tzinfo=UTC)
    c = Decimal(close)
    return MarketBar(
        symbol=symbol, start=start, end=start + timedelta(hours=6, minutes=30), open=c,
        high=c + 1, low=c - 1, close=c, volume=Decimal(1000), source="fake",
        adjustment_version="none",
    )  # fmt: skip


@dataclass
class Fixed:
    """按给定提议原样返回的策略（测试用）。"""

    proposals: Sequence[Proposal] = ()
    strategy_id: str = "demo"
    version: str = "v1"
    universe: frozenset[str] = field(default_factory=lambda: frozenset({"SPY", "QQQ"}))
    error: Exception | None = None

    def decide(self, context: StrategyContext) -> Sequence[Proposal]:
        if self.error is not None:
            raise self.error
        return self.proposals


def ctx(**kw: object) -> StrategyContext:
    data: dict[str, object] = {
        "decision_at": DECISION, "session_day": DAY, "account_id": "DU0000001",
        "bars": {"SPY": [bar("SPY", date(2026, 10, 22))], "QQQ": [bar("QQQ", date(2026, 10, 22))]},
        "positions": {"SPY": Decimal(50)},
    }  # fmt: skip
    data.update(kw)
    return StrategyContext(**data)  # type: ignore[arg-type]


def prop(
    side: Side, cls: OrderClass, symbol: str = "SPY", qty: int = 1, price: str = "1"
) -> Proposal:
    return Proposal(
        symbol=symbol, side=side, quantity=Decimal(qty), limit_price=Decimal(price),
        order_class=cls, reason="测试",
    )  # fmt: skip


def buy(symbol: str = "SPY", qty: int = 10, price: str = "100.00") -> Proposal:
    return prop(Side.BUY, OrderClass.OPEN, symbol, qty, price)


def sell(symbol: str = "SPY", qty: int = 10) -> Proposal:
    return prop(Side.SELL, OrderClass.REDUCE, symbol, qty, "99.00")


def test_proposals_become_day_limit_intents_with_deterministic_ids() -> None:
    intents = evaluate(Fixed([buy("QQQ"), sell("SPY", 50)]), ctx())
    assert [i.intent_id for i in intents] == [
        "demo:v1:2026-10-23:QQQ:BUY:OPEN", "demo:v1:2026-10-23:SPY:SELL:REDUCE",
    ]  # fmt: skip
    first = intents[0]
    assert (first.order_type, first.time_in_force) == (OrderType.LIMIT, TimeInForce.DAY)
    assert (first.strategy_id, first.strategy_version) == ("demo", "v1")
    assert first.created_at == DECISION
    assert first.account_id == "DU0000001"
    again = evaluate(Fixed([buy("QQQ"), sell("SPY", 50)]), ctx())
    assert [i.intent_id for i in again] == [i.intent_id for i in intents]  # 重跑同键


@pytest.mark.parametrize(
    ("proposals", "match"),
    [
        ([buy("AAPL")], "标的池以外"),
        ([prop(Side.SELL, OrderClass.OPEN)], "只做多"),
        ([sell("SPY", 51)], "不超过持仓"),
        ([sell("QQQ", 1)], "不超过持仓"),  # 没有持仓的减仓
        ([prop(Side.BUY, OrderClass.REDUCE)], "减仓只能卖出"),
        ([prop(Side.SELL, OrderClass.PROTECTIVE_STOP, price="90")], "保护性止损"),
        ([buy(), buy(qty=5)], "提议了两次"),
        ([buy(), sell("SPY", 5)], "提议了两次"),  # 同一次运行不同时买卖同一标的
    ],
)
def test_bad_proposals_refuse_the_whole_batch(proposals: list[Proposal], match: str) -> None:
    with pytest.raises(StrategyError, match=match):
        evaluate(Fixed([buy("QQQ"), *proposals]), ctx())


def test_proposal_for_a_symbol_without_data_is_refused() -> None:
    with pytest.raises(StrategyError, match="没有可用数据"):
        evaluate(
            Fixed([buy("QQQ")]), ctx(bars={"SPY": [bar("SPY", date(2026, 10, 22))], "QQQ": []})
        )


@pytest.mark.parametrize(
    ("bars", "match"),
    [
        ({"SPY": [bar("SPY", date(2026, 10, 23))]}, "前视"),  # 当天还没收盘
        ({"SPY": [bar("QQQ", date(2026, 10, 22))]}, "混入了"),
        ({"AAPL": [bar("AAPL", date(2026, 10, 22))]}, "标的池以外"),
    ],
)
def test_bad_inputs_are_refused(bars: dict[str, list[MarketBar]], match: str) -> None:
    with pytest.raises(StrategyError, match=match):
        evaluate(Fixed([]), ctx(bars=bars))


def test_naive_decision_time_is_refused() -> None:
    with pytest.raises(StrategyError, match="决策时刻"):
        evaluate(Fixed([]), ctx(decision_at=datetime(2026, 10, 23, 13, 45)))  # noqa: DTZ001


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"strategy_id": "Demo"}, "策略标识"),
        ({"strategy_id": "flatten-all"}, "保留"),
        ({"strategy_id": "manual"}, "保留"),
        ({"strategy_id": "oms.protective"}, "保留"),  # 系统来源
        ({"strategy_id": "risk.emergency_flatten"}, "保留"),
        ({"version": "v 1"}, "版本"),
        ({"universe": frozenset()}, "标的池"),
    ],
)
def test_strategy_identity_is_checked(changes: dict[str, object], match: str) -> None:
    strategy = Fixed([])
    for key, value in changes.items():
        setattr(strategy, key, value)
    with pytest.raises(StrategyError, match=match):
        evaluate(strategy, ctx())


def test_strategy_crash_is_contained() -> None:
    with pytest.raises(StrategyError, match="运行出错：ZeroDivisionError"):
        evaluate(Fixed(error=ZeroDivisionError("bug")), ctx())


def test_non_proposal_output_is_refused() -> None:
    with pytest.raises(StrategyError, match="不是 Proposal"):
        identity = identity_of(Fixed())
        build_intents(identity, freeze(identity, ctx()), [object()])


def test_proposal_model_rejects_fractional_or_float_quantities() -> None:
    from pydantic import ValidationError  # noqa: PLC0415

    with pytest.raises(ValidationError):
        Proposal(symbol="SPY", side=Side.BUY, quantity=Decimal("1.5"), limit_price=Decimal(1),
                 order_class=OrderClass.OPEN, reason="零股")  # fmt: skip
    with pytest.raises(ValidationError):
        Proposal(symbol="SPY", side=Side.BUY, quantity=Decimal(1), limit_price=100.0,  # type: ignore[arg-type]
                 order_class=OrderClass.OPEN, reason="float")  # fmt: skip


# ---------- 评审补充 ----------


@dataclass
class Tamper:
    """在 decide 里改写输入与自身属性的有缺陷策略。"""

    strategy_id: str = "tamper"
    version: str = "v1"
    universe: frozenset[str] = field(default_factory=lambda: frozenset({"SPY"}))
    mode: str = "positions"

    def decide(self, context: StrategyContext) -> Sequence[Proposal]:
        if self.mode == "positions":
            context.positions["SPY"] = Decimal(10_000)  # type: ignore[index]
        elif self.mode == "universe":
            self.universe = frozenset({"SPY", "AAPL"})
            return [buy("AAPL")]
        elif self.mode == "bars":
            context.bars["SPY"].append(bar("SPY", date(2026, 10, 21)))  # type: ignore[attr-defined]
        return [sell("SPY", 500)]


@pytest.mark.parametrize("mode", ["positions", "bars"])
def test_strategies_cannot_mutate_their_inputs(mode: str) -> None:
    with pytest.raises(StrategyError, match=r"运行出错：(TypeError|AttributeError)"):
        evaluate(Tamper(mode=mode), ctx(bars={"SPY": [bar("SPY", date(2026, 10, 22))]}))


def test_changing_the_universe_during_decide_does_not_widen_it() -> None:
    with pytest.raises(StrategyError, match="标的池以外"):
        evaluate(Tamper(mode="universe"), ctx(bars={"SPY": [bar("SPY", date(2026, 10, 22))]}))


def test_strategy_whose_properties_raise_is_contained() -> None:
    class _Broken:
        strategy_id = "broken"
        version = "v1"

        @property
        def universe(self) -> frozenset[str]:
            raise RuntimeError("属性缺陷")

        def decide(self, context: StrategyContext) -> Sequence[Proposal]:
            return []

    with pytest.raises(StrategyError, match="读不到策略标识"):
        evaluate(_Broken(), ctx())


def test_universe_and_positions_types_are_strict() -> None:
    with pytest.raises(StrategyError, match="标的池必须是"):
        evaluate(Fixed(universe="SPY"), ctx(bars={}))  # type: ignore[arg-type]
    with pytest.raises(StrategyError, match="持仓必须是"):
        evaluate(Fixed([]), ctx(positions={"SPY": 1.5}))
