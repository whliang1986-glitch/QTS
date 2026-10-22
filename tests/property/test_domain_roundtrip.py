"""C-01：序列化稳定——任意合法对象 JSON 往返后完全相等，且两次序列化结果逐字节一致。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st
from server.domain.enums import OrderClass, OrderType, Side, TimeInForce
from server.domain.orders import Execution, OrderIntent

positive = st.decimals(min_value=Decimal("0.0001"), max_value=Decimal("1e9"), places=4)
shares = st.integers(min_value=1, max_value=10**9).map(Decimal)
money = st.decimals(min_value=Decimal("-1e9"), max_value=Decimal("1e9"), places=4)
ids = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=24)
moments = st.datetimes(
    min_value=datetime(2000, 1, 1),  # noqa: DTZ001  hypothesis 要求无时区边界，时区由 timezones 参数提供
    max_value=datetime(2100, 1, 1),  # noqa: DTZ001
    timezones=st.sampled_from([UTC, timezone(timedelta(hours=-5)), timezone(timedelta(hours=8))]),
)


@given(ids, moments, shares, positive, st.sampled_from([Side.BUY, Side.SELL]))
def test_limit_intent_round_trip(
    intent_id: str, at: datetime, qty: Decimal, px: Decimal, side: Side
) -> None:
    oi = OrderIntent(
        intent_id=intent_id,
        created_at=at,
        strategy_id="s",
        strategy_version="1",
        account_id="DU0",
        symbol="SPY",
        side=side,
        quantity=qty,
        order_type=OrderType.LIMIT,
        limit_price=px,
        order_class=OrderClass.OPEN if side is Side.BUY else OrderClass.REDUCE,
        time_in_force=TimeInForce.DAY,
    )
    raw = oi.model_dump_json()
    back = OrderIntent.model_validate_json(raw)
    assert back == oi
    assert back.model_dump_json() == raw
    assert back.created_at.tzinfo is UTC


@given(ids, moments, shares, positive, money)
def test_execution_round_trip(
    eid: str, at: datetime, qty: Decimal, px: Decimal, fee: Decimal
) -> None:
    ex = Execution(
        execution_id=eid,
        intent_id="i",
        broker_order_id="1",
        symbol="SPY",
        side=Side.BUY,
        quantity=qty,
        price=px,
        commission=fee,
        executed_at=at,
    )
    raw = ex.model_dump_json()
    assert Execution.model_validate_json(raw) == ex
    assert Execution.model_validate_json(raw).model_dump_json() == raw
