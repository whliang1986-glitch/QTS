"""B-01：OrderTicket 与 OrderAmendment 的构造校验。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError
from server.broker.base import OrderAmendment, OrderTicket
from server.domain.enums import AssetClass, OrderClass, OrderType, RiskVerdict, Side, TimeInForce
from server.domain.instrument import Instrument
from server.domain.orders import OrderIntent, RiskDecision, RuleHit

T0 = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
HIT = (RuleHit(rule_id="r", rule_version="1", message="m"),)


def spy(**kw: Any) -> Instrument:
    data: dict[str, Any] = {
        "symbol": "SPY", "asset_class": AssetClass.ETF, "exchange": "ARCA", "currency": "USD",
        "tick_size": "0.01", "trading_calendar": "XNYS",
    }  # fmt: skip
    data.update(kw)
    return Instrument.model_validate(data)


def intent(price: str = "500.10", symbol: str = "SPY", intent_id: str = "i-1") -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        created_at=T0,
        strategy_id="s",
        strategy_version="1",
        account_id="DU0",
        symbol=symbol,
        side=Side.BUY,
        quantity=Decimal(10),
        order_type=OrderType.LIMIT,
        limit_price=Decimal(price),
        order_class=OrderClass.OPEN,
        time_in_force=TimeInForce.DAY,
    )


def decision(verdict: RiskVerdict = RiskVerdict.ALLOW, **kw: Any) -> RiskDecision:
    data: dict[str, Any] = {
        "decision_id": "d-1", "intent_id": "i-1", "verdict": verdict, "rule_set_version": "1",
        "decided_at": T0,
    }  # fmt: skip
    if verdict is not RiskVerdict.ALLOW:
        data["rule_hits"] = HIT
    data.update(kw)
    return RiskDecision.model_validate(data)


def ticket(**kw: Any) -> OrderTicket:
    data: dict[str, Any] = {
        "intent": intent(), "decision": decision(), "quantity": Decimal(10),
        "broker_order_id": "101", "instrument": spy(),
    }  # fmt: skip
    data.update(kw)
    return OrderTicket.model_validate(data)


def test_valid_ticket() -> None:
    t = ticket()
    assert t.order_ref == "i-1" and t.price == Decimal("500.10")


def test_reduce_ticket_uses_approved_quantity() -> None:
    reduced = decision(RiskVerdict.REDUCE, approved_quantity="6")
    assert ticket(decision=reduced, quantity=Decimal(6)).quantity == Decimal(6)


@pytest.mark.parametrize(
    "bad",
    [
        {"instrument": spy(tick_size=None)},  # 规格缺失
        {"instrument": spy(trading_calendar=None)},
        {"instrument": spy(multiplier="100")},  # 首期只交易乘数为 1 的现货
        {"intent": intent(price="500.105")},  # 不在最小跳动上
        {"intent": intent(symbol="QQQ")},  # 标的不一致
        {"broker_order_id": ""},
        {"decision": decision(RiskVerdict.REJECT)},  # 风控拒绝
        {"decision": decision(RiskVerdict.HALT)},
        {"decision": decision(intent_id="i-2")},  # 决定不属于这个意图
        {"quantity": Decimal(9)},  # ALLOW 时数量必须等于意图数量
        {"decision": decision(RiskVerdict.REDUCE, approved_quantity="6"), "quantity": Decimal(7)},
        {"decision": decision(RiskVerdict.REDUCE, approved_quantity="12"), "quantity": Decimal(12)},
    ],
)
def test_invalid_tickets_are_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises((ValidationError, ValueError)):
        ticket(**bad)


def test_ticket_is_immutable() -> None:
    with pytest.raises(ValueError, match="不可修改"):
        ticket().evolve(broker_order_id="102")


# ---------- 改单 ----------


def new_decision(verdict: RiskVerdict = RiskVerdict.ALLOW, **kw: Any) -> RiskDecision:
    """改单用的新风控决定：新的 decision_id，时间不早于原决定。"""
    kw.setdefault("decision_id", "d-2")
    kw.setdefault("decided_at", T0 + timedelta(minutes=1))
    return decision(verdict, **kw)


def amendment(**kw: Any) -> OrderAmendment:
    data: dict[str, Any] = {
        "amendment_id": "a-1", "original": ticket(), "new_price": Decimal("500.05"),
        "new_quantity": Decimal(10), "decision": new_decision(),
    }  # fmt: skip
    data.update(kw)
    return OrderAmendment.model_validate(data)


def test_valid_amendment_keeps_identity() -> None:
    a = amendment()
    assert a.broker_order_id == "101" and a.order_ref == "i-1"


def test_amendment_reduce_with_new_decision() -> None:
    reduced = new_decision(RiskVerdict.REDUCE, approved_quantity="8")
    assert amendment(new_quantity=Decimal(8), decision=reduced).new_quantity == Decimal(8)


def test_amendment_reusing_decision_names_the_reason() -> None:
    with pytest.raises(ValueError, match="不得复用"):
        amendment(decision=decision())


@pytest.mark.parametrize(
    "bad",
    [
        {"new_price": Decimal("500.055")},  # 不在跳动上
        {"new_quantity": Decimal(11)},  # 超过意图数量
        {"new_quantity": Decimal(8)},  # 缩量必须有 REDUCE 决定
        {"decision": new_decision(RiskVerdict.REJECT)},
        {"decision": new_decision(intent_id="i-2")},
        {"decision": decision()},  # 复用原票据的决定：改单必须重新过风控
        {"decision": new_decision(decided_at=T0 - timedelta(seconds=1))},  # 新决定早于原决定
    ],
)
def test_invalid_amendments_are_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises((ValidationError, ValueError)):
        amendment(**bad)


def test_amendment_cannot_express_class_side_or_symbol_changes() -> None:
    # 只有价格与数量字段：类别、方向、标的、类型、有效期、账户在类型层面不可表达
    assert set(OrderAmendment.model_fields) == {
        "amendment_id", "original", "new_price", "new_quantity", "decision",
    }  # fmt: skip
