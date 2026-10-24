"""R-01：风控决定绑定摘要的规范化——稳定、不受 decimal 上下文影响、能区分不同内容。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, localcontext

from server.domain.enums import OrderClass, OrderType, Side, TimeInForce
from server.domain.orders import OrderIntent
from server.oms.binding import amendment_digest, intent_digest

T0 = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)


def intent(**kw: object) -> OrderIntent:
    data: dict[str, object] = {
        "intent_id": "i-1", "created_at": T0, "strategy_id": "s", "strategy_version": "1",
        "account_id": "DU0000001", "symbol": "SPY", "side": Side.BUY, "quantity": Decimal(10),
        "order_type": OrderType.LIMIT, "limit_price": Decimal("100.00"),
        "order_class": OrderClass.OPEN, "time_in_force": TimeInForce.DAY,
    }  # fmt: skip
    data.update(kw)
    return OrderIntent.model_validate(data)


def test_trailing_zeros_and_timezone_do_not_change_digest() -> None:
    eastern = timezone(timedelta(hours=-4))
    same = intent(limit_price=Decimal("100"), created_at=T0.astimezone(eastern))
    assert intent_digest(same) == intent_digest(intent())
    a = amendment_digest(
        intent(), amendment_id="am", new_price=Decimal("99.50"), new_quantity=Decimal("6.0")
    )
    b = amendment_digest(
        intent(), amendment_id="am", new_price=Decimal("99.5"), new_quantity=Decimal(6)
    )
    assert a == b


def test_every_field_changes_digest() -> None:
    base = intent_digest(intent())
    for change in (
        {"intent_id": "i-2"},
        {"quantity": Decimal(11)},
        {"limit_price": Decimal("100.01")},
        {"symbol": "QQQ"},
        {"created_at": T0 + timedelta(seconds=1)},
        {"time_in_force": TimeInForce.GTD, "expires_at": T0 + timedelta(hours=1)},
    ):
        assert intent_digest(intent(**change)) != base, change


def test_low_precision_context_does_not_collapse_prices() -> None:
    with localcontext() as ctx:
        ctx.prec = 4
        low = (
            amendment_digest(
                intent(), amendment_id="am", new_price=Decimal("100.04"), new_quantity=Decimal(6)
            ),
            amendment_digest(
                intent(), amendment_id="am", new_price=Decimal("100.05"), new_quantity=Decimal(6)
            ),
        )
    assert low[0] != low[1]
    normal = amendment_digest(
        intent(), amendment_id="am", new_price=Decimal("100.04"), new_quantity=Decimal(6)
    )
    assert low[0] == normal  # 结果与调用方上下文无关


def test_amendment_fields_change_digest() -> None:
    def digest(aid: str = "am", price: str = "99.5", qty: int = 6) -> str:
        return amendment_digest(
            intent(), amendment_id=aid, new_price=Decimal(price), new_quantity=Decimal(qty)
        )

    base = digest()
    assert len({base, digest(aid="am-2"), digest(price="99.6"), digest(qty=7)}) == 4
    assert base != intent_digest(intent())  # 改单摘要与新单摘要不会相撞
