"""R-04：默认禁用（衍生品、市价、杠杆、做空）与保护性止损专项豁免。拒绝原因按完整文本断言。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError
from server.domain.account import CashBalance, Position
from server.domain.enums import AssetClass, OrderClass, OrderType, RiskVerdict, Side, TimeInForce
from server.domain.instrument import Instrument
from server.domain.orders import PROTECTIVE_STOP_SOURCE, OrderIntent
from server.risk import default_deny as dd
from server.risk.default_deny import DefaultDenyRule, ProtectiveStopParams
from server.risk.engine import OpenOrder, RiskContext, Subject
from tests.support.oms import ACCOUNT, SPY, T0

PARAMS = ProtectiveStopParams(stop_distance=Decimal("0.10"))  # 测试夹具值，不是批准的参数


def intent(**kw: Any) -> OrderIntent:
    data: dict[str, Any] = {
        "intent_id": "i-1", "created_at": T0, "strategy_id": "s", "strategy_version": "1",
        "account_id": ACCOUNT.account_id, "symbol": "SPY", "side": Side.BUY,
        "quantity": Decimal(10), "order_type": OrderType.LIMIT, "limit_price": Decimal(100),
        "order_class": OrderClass.OPEN, "time_in_force": TimeInForce.DAY,
    }  # fmt: skip
    data.update(kw)
    return OrderIntent.model_validate(data)


def reduce_(qty: int = 10, **kw: Any) -> OrderIntent:
    return intent(side=Side.SELL, order_class=OrderClass.REDUCE, quantity=Decimal(qty), **kw)


def stop(qty: int = 100, price: str = "95", **kw: Any) -> OrderIntent:
    data: dict[str, Any] = {
        "side": Side.SELL, "order_class": OrderClass.PROTECTIVE_STOP,
        "order_type": OrderType.STOP, "limit_price": None, "stop_price": Decimal(price),
        "quantity": Decimal(qty), "time_in_force": TimeInForce.GTC,
        "strategy_id": PROTECTIVE_STOP_SOURCE,
    }  # fmt: skip
    data.update(kw)
    return intent(**data)


def position(qty: str, cost: str | None = "100", account: str = ACCOUNT.account_id) -> Position:
    return Position(
        account_id=account,
        symbol="SPY",
        quantity=Decimal(qty),
        average_cost=None if cost is None else Decimal(cost),
        as_of=T0,
    )


def cash(
    settled: str = "100000", unsettled: str = "0", account: str = ACCOUNT.account_id
) -> CashBalance:
    return CashBalance(
        account_id=account,
        currency="USD",
        settled=Decimal(settled),
        unsettled=Decimal(unsettled),
        as_of=T0,
    )


UNSET: Any = object()


def context(
    *,
    positions: Any = UNSET,
    orders: Any = (),
    money: Any = UNSET,
    instruments: Any = UNSET,
    last: Any = UNSET,
) -> RiskContext:
    return RiskContext(
        now=T0,
        positions=(position("100"),) if positions is UNSET else positions,
        open_orders=orders,
        cash=cash() if money is UNSET else money,
        instruments={"SPY": SPY} if instruments is UNSET else instruments,
        last_prices={"SPY": Decimal("98")} if last is UNSET else last,
    )


def open_order(
    cls: OrderClass, side: Side, qty: str, price: str | None = "100", **kw: Any
) -> OpenOrder:
    data: dict[str, Any] = {
        "intent_id": f"o-{cls.value}-{qty}", "symbol": "SPY", "side": side, "order_class": cls,
        "remaining_quantity": Decimal(qty),
        "limit_price": None if price is None else Decimal(price),
    }  # fmt: skip
    data.update(kw)
    return OpenOrder.model_validate(data)


def check(
    order: OrderIntent,
    ctx: RiskContext,
    params: ProtectiveStopParams | None = PARAMS,
    *,
    subject: Subject | None = None,
) -> str | None:
    result = DefaultDenyRule(params).check(subject or Subject.new_order(order), ctx)
    if result is None:
        return None
    assert result.verdict is RiskVerdict.REJECT
    return result.message


# ---------- 规格、组合、账户 ----------


def test_plain_cash_buy_covered_sell_and_valid_stop_pass() -> None:
    assert check(intent(), context()) is None
    assert check(reduce_(), context()) is None
    assert check(stop(), context()) is None


def test_spec_checks() -> None:
    assert check(intent(), context(instruments={})) == dd.NO_SPEC
    assert check(intent(), context(instruments=None)) == dd.NO_SPEC
    other = Instrument.model_validate({**SPY.model_dump(), "symbol": "QQQ"})
    assert check(intent(), context(instruments={"SPY": other})) == dd.NO_SPEC
    no_tick = Instrument.model_validate({**SPY.model_dump(), "tick_size": None})
    assert check(intent(), context(instruments={"SPY": no_tick})) == dd.NOT_TRADABLE
    option_like = Instrument.model_validate({**SPY.model_dump(), "multiplier": Decimal(100)})
    assert check(intent(), context(instruments={"SPY": option_like})) == dd.NOT_SPOT


def test_domain_refuses_to_build_bypassing_objects() -> None:
    # 组合白名单是第二道防线；第一道是领域层：不合法组合与跳过校验的构造方式都被禁止
    with pytest.raises(TypeError):
        OrderIntent.model_construct(**intent().model_dump())
    with pytest.raises(ValidationError):
        intent(order_type=OrderType.STOP, limit_price=None, stop_price=Decimal(99))
    with pytest.raises(ValidationError):
        intent(side=Side.SELL)  # 卖出开仓 = 做空
    assert set(AssetClass) == {AssetClass.STK, AssetClass.ETF}  # 新资产类别须 G8 批准


def test_account_mismatch_and_negative_positions() -> None:
    foreign = (position("100", account="DU9999999"),)
    assert check(intent(), context(positions=foreign)) == dd.ACCOUNT_MISMATCH
    assert check(intent(), context(money=cash(account="DU9999999"))) == dd.ACCOUNT_MISMATCH
    split = (position("150"), position("-50"))  # 求和为正，但有一条为负
    for order in (intent(), reduce_(1), stop()):
        assert check(order, context(positions=split)) == dd.NEGATIVE_POSITION


# ---------- 不加杠杆 ----------


def test_leverage_uses_available_cash() -> None:
    assert check(intent(), context(money=cash("999.99"))) == dd.LEVERAGE  # 10 × 100
    assert check(intent(), context(money=cash("1000"))) is None
    # 未结算的买入应付款先扣掉；未结算的卖出收入不算可用
    assert check(intent(), context(money=cash("1400", "-500"))) == dd.LEVERAGE
    assert check(intent(), context(money=cash("900", "500"))) == dd.LEVERAGE


def test_leverage_counts_all_other_pending_buys() -> None:
    other_symbol = (open_order(OrderClass.OPEN, Side.BUY, "5", symbol="QQQ"),)
    assert check(intent(), context(money=cash("1499"), orders=other_symbol)) == dd.LEVERAGE
    assert check(intent(), context(money=cash("1500"), orders=other_symbol)) is None
    sells = (open_order(OrderClass.REDUCE, Side.SELL, "50"),)  # 卖单不占现金
    assert check(intent(), context(money=cash("1000"), orders=sells)) is None
    unpriced = (open_order(OrderClass.OPEN, Side.BUY, "5", price=None),)
    assert check(intent(), context(orders=unpriced)) == dd.UNPRICED_BUY


def test_leverage_unknown_inputs_reject() -> None:
    assert check(intent(), context(money=None)) == dd.CASH_UNKNOWN
    assert check(intent(), context(orders=None)) == dd.CASH_UNKNOWN


def test_amend_does_not_count_its_own_order() -> None:
    own = (open_order(OrderClass.OPEN, Side.BUY, "10", intent_id="i-1"),)
    subject = Subject(intent=intent(), amendment_id="am", price=Decimal(100), quantity=Decimal(10))
    assert check(intent(), context(money=cash("1000"), orders=own), subject=subject) is None
    raised = Subject(intent=intent(), amendment_id="am", price=Decimal(101), quantity=Decimal(10))
    assert check(intent(), context(money=cash("1000"), orders=own), subject=raised) == dd.LEVERAGE


# ---------- 不做空 ----------


def test_short_checks() -> None:
    assert check(reduce_(100), context()) is None
    pending = (open_order(OrderClass.REDUCE, Side.SELL, "95"),)
    assert check(reduce_(10), context(orders=pending)) == dd.SHORT
    stops = (open_order(OrderClass.PROTECTIVE_STOP, Side.SELL, "100", price=None),)
    assert check(reduce_(10), context(orders=stops)) is None  # 止损是保护，不占减仓额度
    assert check(reduce_(1), context(positions=())) == dd.NO_HOLDING
    assert check(reduce_(1), context(positions=None)) == dd.HOLDING_UNKNOWN
    assert check(reduce_(1), context(orders=None)) == dd.HOLDING_UNKNOWN


def test_short_amend_does_not_count_its_own_order() -> None:
    own = (open_order(OrderClass.REDUCE, Side.SELL, "100", intent_id="i-1"),)
    subject = Subject(
        intent=reduce_(100), amendment_id="am", price=Decimal(100), quantity=Decimal(100)
    )
    assert check(reduce_(100), context(orders=own), subject=subject) is None


# ---------- 保护性止损专项豁免 ----------


def test_stop_at_the_approved_floor_passes() -> None:
    assert check(stop(price="90"), context()) is None


@pytest.mark.parametrize(
    ("order", "ctx", "expected"),
    [
        (stop(strategy_id="momentum"), context(), dd.STOP_SOURCE),
        (stop(), context(positions=None), dd.STOP_CONTEXT),
        (stop(), context(orders=None), dd.STOP_CONTEXT),
        (stop(), context(last=None), dd.STOP_CONTEXT),
        (stop(), context(last={}), dd.STOP_CONTEXT),
        (stop(), context(positions=()), dd.STOP_NO_HOLDING),
        (
            stop(),
            context(orders=(open_order(OrderClass.PROTECTIVE_STOP, Side.SELL, "1", None),)),
            dd.STOP_DUPLICATE,
        ),
        (stop(qty=1), context(), dd.STOP_QUANTITY),  # 小止损单占位
        (stop(qty=101), context(), dd.STOP_QUANTITY),
        (stop(), context(positions=(position("100", cost=None),)), dd.STOP_COST),
        (stop(price="89.99"), context(), dd.STOP_RANGE),  # 低于批准距离
        (stop(price="100"), context(), dd.STOP_RANGE),  # 不在成本价下方
        (stop(price="95.005"), context(), dd.STOP_TICK),
        (stop(price="95"), context(last={"SPY": Decimal("95")}), dd.STOP_TRIGGERS),
        (stop(price="95"), context(last={"SPY": Decimal("94")}), dd.STOP_TRIGGERS),
    ],
)
def test_protective_stop_rejections(order: OrderIntent, ctx: RiskContext, expected: str) -> None:
    assert check(order, ctx) == expected


def test_stop_cost_must_be_unambiguous() -> None:
    two = (position("60", cost="100"), position("40", cost="101"))
    assert check(stop(), context(positions=two)) == dd.STOP_COST


def test_protective_stop_without_approved_params_is_rejected() -> None:
    assert check(stop(), context(), params=None) == dd.STOP_PARAMS
    assert check(intent(), context(), params=None) is None  # 其他订单不依赖止损参数


@pytest.mark.parametrize("distance", ["0", "1", "-0.1", "1.5"])
def test_stop_distance_must_be_a_proportion(distance: str) -> None:
    with pytest.raises((ValidationError, ValueError)):
        ProtectiveStopParams(stop_distance=Decimal(distance))
