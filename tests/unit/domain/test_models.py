"""C-01：标的、账户、订单主链路模型的不变量。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError
from server.domain.account import Account, CashBalance, Position
from server.domain.enums import (
    AccountType,
    AssetClass,
    Mode,
    OrderClass,
    OrderType,
    RiskVerdict,
    Side,
    TimeInForce,
)
from server.domain.errors import InstrumentSpecMissingError
from server.domain.instrument import Instrument
from server.domain.market import MarketBar, Quote
from server.domain.modes import HaltState
from server.domain.orders import BrokerOrder, Execution, OrderIntent, RiskDecision, RuleHit

T0 = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)


def spy(**overrides: Any) -> Instrument:
    data: dict[str, Any] = {
        "symbol": "SPY",
        "asset_class": AssetClass.ETF,
        "exchange": "ARCA",
        "currency": "USD",
        "tick_size": "0.01",
        "trading_calendar": "XNYS",
    }
    data.update(overrides)
    return Instrument.model_validate(data)


def intent(**overrides: Any) -> OrderIntent:
    data: dict[str, Any] = {
        "intent_id": "int-0001",
        "created_at": T0,
        "strategy_id": "etf-rotation",
        "strategy_version": "1.0.0",
        "account_id": "DU0000000",
        "symbol": "SPY",
        "side": Side.BUY,
        "quantity": "10",
        "order_type": OrderType.LIMIT,
        "limit_price": "500.10",
        "order_class": OrderClass.OPEN,
        "time_in_force": TimeInForce.DAY,
    }
    data.update(overrides)
    return OrderIntent.model_validate(data)


# ---------- Instrument ----------


def test_complete_instrument_is_tradable() -> None:
    spy().require_tradable()


@pytest.mark.parametrize("missing", ["tick_size", "trading_calendar"])
def test_missing_spec_rejects_trading(missing: str) -> None:
    inst = spy(**{missing: None})
    with pytest.raises(InstrumentSpecMissingError):
        inst.require_tradable()


def test_only_stocks_and_etfs_in_phase_one() -> None:
    with pytest.raises(ValidationError):
        spy(asset_class="OPT")


def test_only_usd_in_phase_one() -> None:
    with pytest.raises(ValidationError):
        spy(currency="HKD")


def test_price_tick_alignment() -> None:
    inst = spy()
    assert inst.is_on_tick(Decimal("500.10"))
    assert not inst.is_on_tick(Decimal("500.105"))


def test_tick_check_requires_spec() -> None:
    with pytest.raises(InstrumentSpecMissingError):
        spy(tick_size=None).is_on_tick(Decimal("1"))


# ---------- 账户与现金 ----------


def test_account_type_comes_from_enum() -> None:
    acct = Account(account_id="DU0000000", account_type=AccountType.CASH, base_currency="USD")
    assert acct.account_type is AccountType.CASH
    with pytest.raises(ValidationError):
        Account.model_validate(
            {"account_id": "x", "account_type": "PORTFOLIO_MARGIN", "base_currency": "USD"}
        )


def test_cash_balance_splits_settled_and_unsettled() -> None:
    cash = CashBalance(
        account_id="DU0000000",
        currency="USD",
        settled=Decimal("1000"),
        unsettled=Decimal("250.5"),
        as_of=T0,
    )
    assert cash.total == Decimal("1250.5")


def test_position_records_broker_fact_even_if_negative() -> None:
    # 券商事实优先：负持仓也要能记录下来，交给风控和对账处理
    pos = Position(
        account_id="DU0",
        symbol="SPY",
        quantity=Decimal("-1"),
        average_cost=Decimal("500"),
        as_of=T0,
    )
    assert pos.quantity == Decimal(-1)


def test_empty_identifiers_rejected() -> None:
    with pytest.raises(ValidationError):
        Account(account_id="", account_type=AccountType.CASH, base_currency="USD")


# ---------- 订单意图 ----------


def test_valid_limit_intent() -> None:
    oi = intent()
    assert oi.order_class is OrderClass.OPEN


def test_quantity_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        intent(quantity=Decimal("0"))


def test_no_market_order_type_exists() -> None:
    assert {t.value for t in OrderType} == {"LIMIT", "STOP"}
    with pytest.raises(ValidationError):
        intent(order_type="MARKET")


def test_limit_requires_limit_price() -> None:
    with pytest.raises(ValidationError):
        intent(limit_price=None)


def test_stop_only_for_protective_sell() -> None:
    stop = intent(
        side=Side.SELL,
        order_type=OrderType.STOP,
        limit_price=None,
        stop_price="450",
        order_class=OrderClass.PROTECTIVE_STOP,
        time_in_force=TimeInForce.GTC,
    )
    assert stop.order_class is OrderClass.PROTECTIVE_STOP
    with pytest.raises(ValidationError):  # 开仓不能用止损单
        intent(order_type=OrderType.STOP, limit_price=None, stop_price="450")
    with pytest.raises(ValidationError):  # 保护性止损不能是买单
        intent(
            order_type=OrderType.STOP,
            limit_price=None,
            stop_price="450",
            order_class=OrderClass.PROTECTIVE_STOP,
            time_in_force=TimeInForce.GTC,
        )
    with pytest.raises(ValidationError):  # 保护性止损必须是止损单
        intent(side=Side.SELL, order_class=OrderClass.PROTECTIVE_STOP)


def test_open_orders_must_expire_same_day() -> None:
    # 开仓与再平衡单一律带有效期（V4.1 6.5），只有保护性止损可以 GTC
    with pytest.raises(ValidationError):
        intent(time_in_force=TimeInForce.GTC)


def test_gtd_requires_expiry() -> None:
    with pytest.raises(ValidationError):
        intent(time_in_force=TimeInForce.GTD)
    later = T0 + timedelta(hours=2)
    assert intent(time_in_force=TimeInForce.GTD, expires_at=later).expires_at == later
    with pytest.raises(ValidationError):  # 到期时间必须晚于创建时间
        intent(time_in_force=TimeInForce.GTD, expires_at=T0)


@pytest.mark.parametrize("field", ["intent_id", "order_class", "side", "symbol", "account_id"])
def test_identity_fields_cannot_evolve(field: str) -> None:
    oi = intent()
    replacement = {
        "intent_id": "int-0002",
        "order_class": OrderClass.REDUCE,
        "side": Side.SELL,
        "symbol": "QQQ",
        "account_id": "DU9",
    }[field]
    with pytest.raises(ValueError, match="不可修改"):
        oi.evolve(**{field: replacement})


@pytest.mark.parametrize(
    "change",
    [{"quantity": Decimal(5)}, {"limit_price": Decimal("1")}, {"strategy_version": "2"}],
)
def test_intent_is_fully_immutable(change: dict[str, Any]) -> None:
    # 同一个幂等键只能对应一份内容（评审 I1）
    with pytest.raises(ValueError, match="不可修改"):
        intent().evolve(**change)


# ---------- 风控决定 ----------


def test_reject_and_halt_need_rule_hits() -> None:
    hit = RuleHit(rule_id="max_order_notional", rule_version="1", message="超出单笔上限")
    RiskDecision(
        decision_id="d1",
        intent_id="int-0001",
        verdict=RiskVerdict.REJECT,
        rule_hits=(hit,),
        rule_set_version="1",
        decided_at=T0,
    )
    for verdict in (RiskVerdict.REJECT, RiskVerdict.HALT):
        with pytest.raises(ValidationError):
            RiskDecision(
                decision_id="d1",
                intent_id="int-0001",
                verdict=verdict,
                rule_hits=(),
                rule_set_version="1",
                decided_at=T0,
            )


def test_reduce_needs_positive_approved_quantity() -> None:
    with pytest.raises(ValidationError):
        RiskDecision(
            decision_id="d1",
            intent_id="i",
            verdict=RiskVerdict.REDUCE,
            rule_set_version="1",
            decided_at=T0,
        )
    ok = RiskDecision(
        decision_id="d1",
        intent_id="i",
        verdict=RiskVerdict.REDUCE,
        approved_quantity=Decimal("3"),
        rule_hits=(RuleHit(rule_id="max_position", rule_version="1", message="缩量"),),
        rule_set_version="1",
        decided_at=T0,
    )
    assert ok.approved_quantity == Decimal(3)
    with pytest.raises(ValidationError):  # REDUCE 也必须说明命中的规则
        RiskDecision(
            decision_id="d1",
            intent_id="i",
            verdict=RiskVerdict.REDUCE,
            approved_quantity=Decimal("3"),
            rule_set_version="1",
            decided_at=T0,
        )


def test_allow_must_not_carry_reduced_quantity() -> None:
    with pytest.raises(ValidationError):
        RiskDecision(
            decision_id="d1",
            intent_id="i",
            verdict=RiskVerdict.ALLOW,
            approved_quantity=Decimal("3"),
            rule_set_version="1",
            decided_at=T0,
        )


# ---------- 成交、行情、模式 ----------


def test_execution_requires_positive_fill() -> None:
    with pytest.raises(ValidationError):
        Execution(
            execution_id="e1",
            intent_id="i",
            broker_order_id="1",
            symbol="SPY",
            side=Side.BUY,
            quantity=Decimal("0"),
            price=Decimal("1"),
            commission=Decimal("0"),
            executed_at=T0,
        )


def test_quote_flags_delayed_data() -> None:
    q = Quote(
        symbol="SPY",
        ts=T0,
        bid=Decimal("500"),
        ask=Decimal("500.02"),
        last=Decimal("500.01"),
        source="fake",
        delayed=True,
    )
    assert q.delayed is True


def test_bar_interval_must_be_ordered() -> None:
    with pytest.raises(ValidationError):
        MarketBar(
            symbol="SPY",
            start=T0,
            end=T0,
            open=Decimal("1"),
            high=Decimal("1"),
            low=Decimal("1"),
            close=Decimal("1"),
            volume=Decimal("0"),
            source="fake",
            adjustment_version="raw",
        )


def test_six_modes() -> None:
    assert {m.value for m in Mode} == {
        "RESEARCH",
        "BACKTEST",
        "PAPER",
        "ASSISTED_LIVE",
        "LIMITED_AUTO_LIVE",
        "HALT",
    }


def test_halt_state_requires_reason_when_halted() -> None:
    with pytest.raises(ValidationError):
        HaltState(halted=True, since=T0, set_by="system")
    assert not HaltState(halted=False).halted


# ---------- 评审 C-01：绕过校验的入口全部关闭 ----------


def test_class_level_settings_cannot_be_overwritten_on_instance() -> None:
    oi = intent()
    assert "evolvable_fields" in type(oi).__class_vars__
    assert "evolvable_fields" not in type(oi).__private_attributes__
    with pytest.raises((AttributeError, ValidationError, TypeError)):
        oi.evolvable_fields = None  # type: ignore[misc]
    with pytest.raises(ValueError, match="不可修改"):
        oi.evolve(side=Side.SELL, order_class=OrderClass.REDUCE)


def test_model_construct_is_forbidden() -> None:
    with pytest.raises(TypeError):
        # 故意缺字段：无论参数如何，model_construct 都必须拒绝
        OrderIntent.model_construct(intent_id="x")  # type: ignore[call-arg]


def test_legacy_copy_is_forbidden() -> None:
    with pytest.raises(TypeError):
        intent().copy(update={"side": Side.SELL})


def test_existing_instances_are_revalidated() -> None:
    hit = RuleHit(rule_id="r", rule_version="1", message="m")
    # 即使有人用 object.__setattr__ 改坏了实例，再次校验也会发现
    object.__setattr__(hit, "rule_id", "")
    with pytest.raises(ValidationError):
        RuleHit.model_validate(hit)
    with pytest.raises(ValidationError):
        RiskDecision(
            decision_id="d",
            intent_id="i",
            verdict=RiskVerdict.REJECT,
            rule_hits=(hit,),
            rule_set_version="1",
            decided_at=T0,
        )


def test_execution_cannot_evolve() -> None:
    ex = Execution(
        execution_id="e1",
        intent_id="i",
        broker_order_id="1",
        symbol="SPY",
        side=Side.BUY,
        quantity=Decimal(1),
        price=Decimal(1),
        commission=Decimal(0),
        executed_at=T0,
    )
    with pytest.raises(ValueError, match="不可修改"):
        ex.evolve(quantity=Decimal(2))


def test_perm_id_can_only_be_filled_once() -> None:
    bo = BrokerOrder(
        intent_id="i", broker_order_id="101", order_class=OrderClass.OPEN, submitted_at=T0
    )
    filled = bo.evolve(perm_id="9001")
    assert filled.perm_id == "9001"
    assert filled.evolve(perm_id="9001").perm_id == "9001"
    with pytest.raises(ValueError, match="perm_id"):
        filled.evolve(perm_id="9002")
    with pytest.raises(ValueError, match="不可修改"):
        bo.evolve(broker_order_id="102")


@pytest.mark.parametrize(
    ("side", "order_class"),
    [(Side.SELL, OrderClass.OPEN), (Side.BUY, OrderClass.REDUCE)],
)
def test_long_only_in_phase_one(side: Side, order_class: OrderClass) -> None:
    with pytest.raises(ValidationError, match="仅多头"):
        intent(side=side, order_class=order_class)


@pytest.mark.parametrize("qty", ["0.5", "1e-20", "10.1"])
def test_order_quantity_must_be_whole_shares(qty: str) -> None:
    with pytest.raises(ValidationError):
        intent(quantity=qty)


def test_whole_share_quantity_in_decimal_notation_accepted() -> None:
    assert intent(quantity="10.0").quantity == Decimal(10)
