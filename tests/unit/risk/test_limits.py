"""R-02b：阈值规则。所有阈值都是测试夹具，不是 Dylan 批准的参数。"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Any

import pytest
from server.domain.account import CashBalance, Position
from server.domain.enums import OrderClass, OrderType, RiskVerdict, Side, TimeInForce
from server.domain.instrument import Instrument
from server.domain.orders import PROTECTIVE_STOP_SOURCE, OrderIntent
from server.risk import limits as lm
from server.risk.engine import OpenOrder, RiskContext, RuleResult, StopCooldown, Subject
from server.risk.parameters import SPECS, Kind, RiskParameters
from tests.support.oms import ACCOUNT, SPY, T0

V = RiskVerdict
FIXTURE = {
    "max_order_notional_nav": "0.05", "max_position_etf_nav": "0.25",
    "max_position_stk_nav": "0.10", "max_total_exposure_nav": "1", "min_cash_buffer_nav": "0.02",
    "max_daily_loss_nav": "0.02", "max_strategy_drawdown": "0.10", "max_open_orders": "3",
    "max_orders_per_minute": "2", "max_limit_deviation": "0.01", "stop_cooldown_trading_days": "5",
}  # fmt: skip
PARAMS = RiskParameters(
    version="fixture",
    values=MappingProxyType(
        {
            s.name: Decimal(FIXTURE.get(s.name, "0.5" if s.kind is Kind.RATIO else "10"))
            for s in SPECS
        }
    ),
)
STK = Instrument.model_validate({**SPY.model_dump(), "symbol": "AAPL", "asset_class": "STK"})


def intent(**kw: Any) -> OrderIntent:
    data: dict[str, Any] = {
        "intent_id": "i-1", "created_at": T0, "strategy_id": "s", "strategy_version": "1",
        "account_id": ACCOUNT.account_id, "symbol": "SPY", "side": Side.BUY,
        "quantity": Decimal(10), "order_type": OrderType.LIMIT, "limit_price": Decimal(100),
        "order_class": OrderClass.OPEN, "time_in_force": TimeInForce.DAY,
    }  # fmt: skip
    data.update(kw)
    return OrderIntent.model_validate(data)


def reduce_order(**kw: Any) -> OrderIntent:
    return intent(side=Side.SELL, order_class=OrderClass.REDUCE, **kw)


def stop_order() -> OrderIntent:
    return intent(
        side=Side.SELL, order_class=OrderClass.PROTECTIVE_STOP, order_type=OrderType.STOP,
        limit_price=None, stop_price=Decimal(90), time_in_force=TimeInForce.GTC,
        strategy_id=PROTECTIVE_STOP_SOURCE,
    )  # fmt: skip


def position(qty: str, symbol: str = "SPY") -> Position:
    return Position(
        account_id=ACCOUNT.account_id, symbol=symbol, quantity=Decimal(qty),
        average_cost=Decimal(100), as_of=T0,
    )  # fmt: skip


def buy(qty: str, symbol: str = "SPY", intent_id: str = "o-1") -> OpenOrder:
    return OpenOrder(
        intent_id=intent_id, symbol=symbol, side=Side.BUY, order_class=OrderClass.OPEN,
        remaining_quantity=Decimal(qty), limit_price=Decimal(100),
    )  # fmt: skip


UNSET: Any = object()


def ctx(**kw: Any) -> RiskContext:
    data: dict[str, Any] = {
        "now": T0,
        "positions": (),
        "open_orders": (),
        "cash": CashBalance(
            account_id=ACCOUNT.account_id,
            currency="USD",
            settled=Decimal(100000),
            unsettled=Decimal(0),
            as_of=T0,
        ),
        "instruments": {"SPY": SPY, "AAPL": STK},
        "last_prices": {"SPY": Decimal(100), "AAPL": Decimal(100)},
        "nav": Decimal(100000),
        "start_of_day_nav": Decimal(100000),
        "strategy_drawdown": {"s": Decimal(0)},
        "recent_submissions": (),
        "stop_cooldown": {},
    }
    data.update(kw)
    return RiskContext.model_validate(data)


def run(rule: type[lm._Rule], order: OrderIntent, context: RiskContext) -> RuleResult | None:
    return rule(PARAMS).check(Subject.new_order(order), context)


def reduced_to(result: RuleResult | None) -> Decimal | None:
    assert result is not None and result.verdict is V.REDUCE, result
    return result.max_quantity


def rejected(result: RuleResult | None) -> bool:
    return result is not None and result.verdict is V.REJECT


# ---------- 单笔名义金额 ----------


def test_order_notional() -> None:
    assert run(lm.OrderNotionalRule, intent(quantity=Decimal(50)), ctx()) is None  # 5000 = 上限
    assert reduced_to(run(lm.OrderNotionalRule, intent(quantity=Decimal(60)), ctx())) == 50
    assert rejected(run(lm.OrderNotionalRule, intent(), ctx(nav=None)))
    assert run(lm.OrderNotionalRule, reduce_order(quantity=Decimal(900)), ctx()) is None


# ---------- 单标的 ----------


def test_position_limit_counts_holding_and_pending_buys() -> None:
    held = ctx(positions=(position("200"),), open_orders=(buy("30"),))  # 20000 + 3000，上限 25000
    assert reduced_to(run(lm.PositionLimitRule, intent(quantity=Decimal(30)), held)) == 20
    full = ctx(positions=(position("250"),))
    assert rejected(run(lm.PositionLimitRule, intent(), full))
    assert run(lm.PositionLimitRule, intent(quantity=Decimal(20)), held) is None


def test_position_limit_uses_stock_cap_for_stocks() -> None:
    held = ctx(positions=(position("90", "AAPL"),))  # 9000，个股上限 10000
    order = intent(symbol="AAPL", quantity=Decimal(15))
    assert reduced_to(run(lm.PositionLimitRule, order, held)) == 10


UNPRICED_BUY = OpenOrder(
    intent_id="x", symbol="SPY", side=Side.BUY, order_class=OrderClass.OPEN,
    remaining_quantity=Decimal(1), limit_price=None,
)  # fmt: skip


@pytest.mark.parametrize(
    "missing",
    [
        {"nav": None},
        {"last_prices": {}},
        {"open_orders": None},
        {"positions": None},
        {"instruments": {}},
        {"open_orders": (UNPRICED_BUY,)},
    ],
)
def test_position_limit_rejects_missing_context(missing: dict[str, Any]) -> None:
    assert rejected(run(lm.PositionLimitRule, intent(), ctx(**missing)))


# ---------- 总敞口与现金缓冲 ----------


def test_exposure_cap() -> None:
    context = ctx(positions=(position("900"),))  # 敞口 90000，上限 100000
    assert reduced_to(run(lm.ExposureAndCashRule, intent(quantity=Decimal(150)), context)) == 100


def test_cash_buffer_is_the_stricter_bound() -> None:
    context = ctx(
        cash=CashBalance(
            account_id=ACCOUNT.account_id, currency="USD", settled=Decimal(3000),
            unsettled=Decimal(0), as_of=T0,
        )
    )  # fmt: skip
    # 可用 3000 − 缓冲 2000 = 1000
    assert reduced_to(run(lm.ExposureAndCashRule, intent(quantity=Decimal(20)), context)) == 10


def test_exposure_rejects_unpriced_positions_and_missing_context() -> None:
    assert rejected(run(lm.ExposureAndCashRule, intent(), ctx(positions=(position("1", "QQQ"),))))
    assert rejected(run(lm.ExposureAndCashRule, intent(), ctx(positions=None)))
    assert rejected(run(lm.ExposureAndCashRule, intent(), ctx(cash=None)))


# ---------- 日内亏损、策略回撤 ----------


def test_daily_loss_halts() -> None:
    result = run(lm.DailyLossRule, intent(), ctx(nav=Decimal(98000)))  # 亏 2%
    assert result is not None and result.verdict is V.HALT
    assert run(lm.DailyLossRule, intent(), ctx(nav=Decimal(98001))) is None
    assert rejected(run(lm.DailyLossRule, intent(), ctx(start_of_day_nav=None)))
    assert run(lm.DailyLossRule, reduce_order(), ctx(nav=Decimal(90000))) is None  # 减仓不受限


def test_strategy_drawdown() -> None:
    assert rejected(
        run(lm.StrategyDrawdownRule, intent(), ctx(strategy_drawdown={"s": Decimal("0.1")}))
    )
    assert (
        run(lm.StrategyDrawdownRule, intent(), ctx(strategy_drawdown={"s": Decimal("0.09")}))
        is None
    )
    assert rejected(run(lm.StrategyDrawdownRule, intent(), ctx(strategy_drawdown={})))


# ---------- 未成交订单数、下单频率 ----------


def test_open_orders_excludes_stops_and_self() -> None:
    three = tuple(buy("1", intent_id=f"o-{n}") for n in range(3))
    assert rejected(run(lm.OpenOrdersRule, intent(), ctx(open_orders=three)))
    assert rejected(run(lm.OpenOrdersRule, reduce_order(), ctx(open_orders=three)))
    stop_open = OpenOrder(
        intent_id="st", symbol="SPY", side=Side.SELL, order_class=OrderClass.PROTECTIVE_STOP,
        remaining_quantity=Decimal(1),
    )  # fmt: skip
    two_and_stop = (*three[:2], stop_open)
    assert run(lm.OpenOrdersRule, intent(), ctx(open_orders=two_and_stop)) is None
    with_self = (*three[:2], buy("1", intent_id="i-1"))
    assert run(lm.OpenOrdersRule, intent(), ctx(open_orders=with_self)) is None
    assert rejected(run(lm.OpenOrdersRule, intent(), ctx(open_orders=None)))


def test_order_rate() -> None:
    recent = (T0 - timedelta(seconds=30), T0 - timedelta(seconds=10))
    assert rejected(run(lm.OrderRateRule, intent(), ctx(recent_submissions=recent)))
    older = (T0 - timedelta(seconds=61), T0 - timedelta(seconds=10))
    assert run(lm.OrderRateRule, intent(), ctx(recent_submissions=older)) is None
    assert rejected(run(lm.OrderRateRule, intent(), ctx(recent_submissions=None)))
    amend = Subject(intent=intent(), amendment_id="am", price=Decimal(100), quantity=Decimal(10))
    assert lm.OrderRateRule(PARAMS).check(amend, ctx(recent_submissions=recent)) is None


# ---------- 限价偏离 ----------


def test_limit_deviation() -> None:
    assert run(lm.LimitDeviationRule, intent(limit_price=Decimal(101)), ctx()) is None
    assert rejected(run(lm.LimitDeviationRule, intent(limit_price=Decimal("101.5")), ctx()))
    assert rejected(run(lm.LimitDeviationRule, reduce_order(limit_price=Decimal("98.5")), ctx()))
    assert rejected(run(lm.LimitDeviationRule, intent(), ctx(last_prices={})))


# ---------- 止损后冷却期 ----------


@pytest.mark.parametrize(
    ("days", "rebalanced", "blocked"),
    [(3, True, True), (5, False, True), (5, True, False), (10, True, False)],
)
def test_stop_cooldown(days: int, rebalanced: bool, blocked: bool) -> None:
    cooldown = {"SPY": StopCooldown(trading_days_since_stop=days, rebalanced_since_stop=rebalanced)}
    result = run(lm.StopCooldownRule, intent(), ctx(stop_cooldown=cooldown))
    assert rejected(result) is blocked
    assert run(lm.StopCooldownRule, reduce_order(), ctx(stop_cooldown=cooldown)) is None


def test_stop_cooldown_unknown_rejects() -> None:
    assert run(lm.StopCooldownRule, intent(), ctx()) is None  # 没有止损记录
    assert rejected(run(lm.StopCooldownRule, intent(), ctx(stop_cooldown=None)))


# ---------- 保护性止损豁免全部阈值规则 ----------


def test_protective_stops_are_exempt_from_all_limit_rules() -> None:
    empty = RiskContext(now=T0)  # 全部上下文缺失：止损也不应被阈值规则拦下
    for rule in lm.limit_rules(PARAMS):
        assert rule.check(Subject.new_order(stop_order()), empty) is None, rule.rule_id


def test_every_limit_rule_rejects_open_orders_without_context() -> None:
    empty = RiskContext(now=T0)
    for rule in lm.limit_rules(PARAMS):
        result = rule.check(Subject.new_order(intent()), empty)
        assert result is not None and result.verdict is V.REJECT, rule.rule_id


def test_cash_buffer_deducts_unsettled_purchases() -> None:
    owing = ctx(
        cash=CashBalance(
            account_id=ACCOUNT.account_id, currency="USD", settled=Decimal(3500),
            unsettled=Decimal(-500), as_of=T0,
        )
    )  # fmt: skip
    # 可用 3500 − 500（未结算买入应付）− 缓冲 2000 = 1000
    assert reduced_to(run(lm.ExposureAndCashRule, intent(quantity=Decimal(20)), owing)) == 10


# ---------- R-02b 评审补充 ----------


@pytest.mark.parametrize("bad", [Decimal("-0.12"), Decimal("1.5")])
def test_drawdown_with_wrong_sign_or_scale_is_rejected(bad: Decimal) -> None:
    result = run(lm.StrategyDrawdownRule, intent(), ctx(strategy_drawdown={"s": bad}))
    assert result is not None and result.verdict is V.REJECT and "口径" in result.message


def amend(order: OrderIntent, price: str, qty: str | None = None) -> Subject:
    quantity = Decimal(qty) if qty is not None else order.quantity
    return Subject(intent=order, amendment_id="am", price=Decimal(price), quantity=quantity)


def test_amendment_is_checked_at_its_new_price() -> None:
    own = (buy("10", intent_id="i-1"),)  # 被改的旧单不重复计入
    order = intent()
    assert lm.LimitDeviationRule(PARAMS).check(amend(order, "101"), ctx(open_orders=own)) is None
    over = lm.LimitDeviationRule(PARAMS).check(amend(order, "102"), ctx(open_orders=own))
    assert over is not None and over.verdict is V.REJECT
    big = intent(quantity=Decimal(50))  # 50 × 100 = 上限 5000；改价到 110 超出
    result = lm.OrderNotionalRule(PARAMS).check(amend(big, "110"), ctx(open_orders=own))
    assert result is not None and result.verdict is V.REDUCE  # 引擎对改单的 REDUCE 改判 REJECT


def test_negative_positions_are_rejected_by_limit_rules_themselves() -> None:
    negative = ctx(positions=(position("-5"),))
    assert rejected(run(lm.PositionLimitRule, intent(), negative))
    assert rejected(run(lm.ExposureAndCashRule, intent(), negative))


def test_boundaries() -> None:
    assert rejected(run(lm.OrderNotionalRule, intent(), ctx(nav=Decimal(0))))
    assert rejected(run(lm.DailyLossRule, intent(), ctx(start_of_day_nav=Decimal(0))))
    exactly_a_minute = (T0 - timedelta(seconds=60), T0 - timedelta(seconds=10))
    assert run(lm.OrderRateRule, intent(), ctx(recent_submissions=exactly_a_minute)) is None


def test_hit_messages_carry_the_numbers() -> None:
    result = run(lm.OrderNotionalRule, intent(quantity=Decimal(60)), ctx())
    assert result is not None and "5000" in result.message
