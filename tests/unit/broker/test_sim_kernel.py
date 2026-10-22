"""B-02：撮合内核（与 T-04 回测共用）。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from server.broker.sim.kernel import (
    BarFill,
    FixedCommission,
    Ledger,
    NoCommission,
    RestingOrder,
    Trade,
    match_bar,
)
from server.domain.enums import Side
from server.domain.market import MarketBar

T0 = datetime(2026, 10, 22, 13, 30, tzinfo=UTC)
D0 = date(2026, 10, 22)
D1 = date(2026, 10, 23)


def bar(o: str, h: str, low: str, c: str, *, volume: int = 1_000_000) -> MarketBar:
    start = T0
    return MarketBar(
        symbol="SPY",
        start=start,
        end=start + timedelta(hours=6, minutes=30),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal(volume),
        source="test",
        adjustment_version="raw",
    )


B = bar("100", "105", "95", "102")


# ---------- 撮合 ----------


def mb(
    *,
    side: Side,
    stop: bool,
    price: Decimal,
    remaining: Decimal,
    bar: MarketBar,
    submitted_at: datetime,
    participation: Decimal | None = None,
) -> BarFill | None:
    return match_bar(RestingOrder(side, stop, price, remaining, submitted_at), bar, participation)


@pytest.mark.parametrize(
    ("side", "limit", "expected"),
    [
        (Side.BUY, "96", "96"),  # 盘中触及限价，按限价成交
        (Side.BUY, "101", "100"),  # 开盘已优于限价，按开盘价
        (Side.BUY, "94", None),  # 最低价未触及
        (Side.SELL, "104", "104"),
        (Side.SELL, "99", "100"),
        (Side.SELL, "106", None),
    ],
)
def test_limit_orders(side: Side, limit: str, expected: str | None) -> None:
    fill = mb(
        side=side, stop=False, price=Decimal(limit), remaining=Decimal(10), bar=B, submitted_at=T0
    )
    if expected is None:
        assert fill is None
    else:
        assert fill is not None and fill.price == Decimal(expected) and fill.quantity == 10


@pytest.mark.parametrize(
    ("side", "stop", "b", "expected"),
    [
        (Side.SELL, "97", B, "97"),  # 盘中跌破止损价，按止损价成交
        (Side.SELL, "96", bar("90", "92", "88", "91"), "90"),  # 跳空低开：按更差的开盘价
        (Side.SELL, "94", B, None),
        (Side.BUY, "104", B, "104"),
        (Side.BUY, "104", bar("110", "112", "108", "111"), "110"),
        (Side.BUY, "106", B, None),
    ],
)
def test_stop_orders(side: Side, stop: str, b: MarketBar, expected: str | None) -> None:
    fill = mb(
        side=side, stop=True, price=Decimal(stop), remaining=Decimal(5), bar=b, submitted_at=T0
    )
    if expected is None:
        assert fill is None
    else:
        assert fill is not None and fill.price == Decimal(expected)


def test_participation_limits_quantity_to_whole_shares() -> None:
    thin = bar("100", "105", "95", "102", volume=99)
    fill = mb(
        side=Side.BUY,
        stop=False,
        price=Decimal(100),
        remaining=Decimal(50),
        bar=thin,
        submitted_at=T0,
        participation=Decimal("0.1"),
    )
    assert fill is not None and fill.quantity == Decimal(9)
    none = mb(
        side=Side.BUY,
        stop=False,
        price=Decimal(100),
        remaining=Decimal(50),
        bar=bar("100", "105", "95", "102", volume=5),
        submitted_at=T0,
        participation=Decimal("0.1"),
    )
    assert none is None


def test_bar_before_submission_is_lookahead() -> None:
    with pytest.raises(ValueError, match="前视"):
        mb(
            side=Side.BUY,
            stop=False,
            price=Decimal(100),
            remaining=Decimal(1),
            bar=B,
            submitted_at=T0 + timedelta(seconds=1),
        )


@pytest.mark.parametrize("participation", [Decimal(0), Decimal("1.5"), Decimal(-1)])
def test_participation_must_be_in_range(participation: Decimal) -> None:
    with pytest.raises(ValueError, match="参与率"):
        mb(
            side=Side.BUY,
            stop=False,
            price=Decimal(100),
            remaining=Decimal(1),
            bar=B,
            submitted_at=T0,
            participation=participation,
        )


def test_remaining_must_be_positive() -> None:
    with pytest.raises(ValueError, match="剩余"):
        mb(
            side=Side.BUY,
            stop=False,
            price=Decimal(100),
            remaining=Decimal(0),
            bar=B,
            submitted_at=T0,
        )


# ---------- 佣金 ----------


def test_fixed_commission_minimum_per_share_and_cap() -> None:
    model = FixedCommission()
    assert model(Decimal(10), Decimal(100)) == Decimal("1.00")  # 最低 1 美元
    assert model(Decimal(1000), Decimal(100)) == Decimal("5.00")  # 每股 0.005
    assert model(Decimal(100), Decimal("0.50")) == Decimal("0.50")  # 不超过成交额的 1%
    assert NoCommission()(Decimal(10), Decimal(100)) == Decimal(0)


def test_commission_rounds_to_cents() -> None:
    assert FixedCommission()(Decimal(333), Decimal(100)) == Decimal("1.67")  # 1.665 → 1.67


# ---------- 账本 ----------


def test_buy_debits_settled_and_sets_weighted_cost() -> None:
    ledger = Ledger(settled=Decimal(10_000))
    ledger.apply_fill(Trade(Side.BUY, "SPY", Decimal(10), Decimal(100), Decimal(1), D1))
    ledger.apply_fill(Trade(Side.BUY, "SPY", Decimal(10), Decimal(110), Decimal(1), D1))
    assert ledger.settled == Decimal(10_000 - 1000 - 1100 - 2)
    pos = ledger.position("SPY")
    assert pos.quantity == Decimal(20) and pos.average_cost == Decimal(105)


def test_sell_proceeds_are_unsettled_until_settlement_date() -> None:
    ledger = Ledger(settled=Decimal(0), positions={"SPY": (Decimal(10), Decimal(100))})
    ledger.apply_fill(Trade(Side.SELL, "SPY", Decimal(4), Decimal(120), Decimal(1), D1))
    assert ledger.settled == Decimal(-1)  # 佣金立即扣已结算
    assert ledger.unsettled == Decimal(480)
    assert ledger.position("SPY").quantity == Decimal(6)
    assert ledger.position("SPY").average_cost == Decimal(100)  # 卖出不改成本
    ledger.settle(D0)
    assert ledger.unsettled == Decimal(480)
    ledger.settle(D1)
    assert ledger.unsettled == 0 and ledger.settled == Decimal(479)


def test_buy_consumes_unsettled_proceeds_after_settled_cash() -> None:
    ledger = Ledger(settled=Decimal(100), positions={"SPY": (Decimal(10), Decimal(50))})
    ledger.apply_fill(Trade(Side.SELL, "SPY", Decimal(10), Decimal(50), Decimal(0), D1))
    ledger.apply_fill(Trade(Side.BUY, "QQQ", Decimal(3), Decimal(100), Decimal(0), D1))
    assert ledger.settled == 0 and ledger.unsettled == Decimal(300)
    assert ledger.buying_power(include_unsettled=True) == Decimal(300)
    assert ledger.buying_power(include_unsettled=False) == 0


def test_selling_to_flat_resets_cost_and_negative_position_is_visible() -> None:
    ledger = Ledger(settled=Decimal(0), positions={"SPY": (Decimal(5), Decimal(100))})
    ledger.apply_fill(Trade(Side.SELL, "SPY", Decimal(5), Decimal(100), Decimal(0), D1))
    assert ledger.position("SPY").quantity == 0 and ledger.position("SPY").average_cost is None
    ledger.apply_fill(Trade(Side.SELL, "SPY", Decimal(2), Decimal(100), Decimal(0), D1))
    neg = ledger.position("SPY")
    assert (
        neg.quantity == Decimal(-2) and neg.average_cost is None
    )  # 负持仓如实显示，由风控与对账处理


def test_split_and_reverse_split_adjust_quantity_and_cost() -> None:
    ledger = Ledger(settled=Decimal(0), positions={"SPY": (Decimal(10), Decimal(300))})
    ledger.apply_split("SPY", Decimal(3), Decimal(1))
    assert ledger.position("SPY").quantity == Decimal(30)
    assert ledger.position("SPY").average_cost == Decimal(100)
    ledger.apply_split("SPY", Decimal(1), Decimal(4))  # 1 合 4：出现零股，如实保留
    assert ledger.position("SPY").quantity == Decimal("7.5")
    assert ledger.position("SPY").average_cost == Decimal(400)


@pytest.mark.parametrize(("num", "den"), [(Decimal(0), Decimal(1)), (Decimal(1), Decimal(-1))])
def test_split_ratio_must_be_positive(num: Decimal, den: Decimal) -> None:
    with pytest.raises(ValueError, match="拆股"):
        Ledger(settled=Decimal(0)).apply_split("SPY", num, den)


def test_cash_dividend_credits_settled_cash() -> None:
    ledger = Ledger(settled=Decimal(0), positions={"SPY": (Decimal(10), Decimal(100))})
    ledger.apply_dividend("SPY", Decimal("1.25"))
    assert ledger.settled == Decimal("12.50")
    ledger.apply_dividend("QQQ", Decimal(1))  # 没有持仓：不变
    assert ledger.settled == Decimal("12.50")


def test_silent_adjustment_changes_position_only() -> None:
    ledger = Ledger(settled=Decimal(5), positions={"SPY": (Decimal(10), Decimal(100))})
    ledger.adjust_position("SPY", Decimal(-3))
    assert ledger.position("SPY").quantity == Decimal(7) and ledger.settled == Decimal(5)


@pytest.mark.parametrize(
    ("qty", "price", "commission"),
    [(Decimal(0), Decimal(1), Decimal(0)), (Decimal(1), Decimal(0), Decimal(0)),
     (Decimal(1), Decimal(1), Decimal(-1))],
)  # fmt: skip
def test_fill_values_must_be_valid(qty: Decimal, price: Decimal, commission: Decimal) -> None:
    with pytest.raises(ValueError, match="成交"):
        Ledger(settled=Decimal(0)).apply_fill(Trade(Side.BUY, "SPY", qty, price, commission, D1))


def test_positions_snapshot_excludes_flat() -> None:
    ledger = Ledger(settled=Decimal(0), positions={"SPY": (Decimal(1), Decimal(1))})
    ledger.adjust_position("SPY", Decimal(-1))
    ledger.adjust_position("QQQ", Decimal(2))
    assert [p.symbol for p in ledger.positions()] == ["QQQ"]


# ---------- 属性测试 ----------

FILLS = st.lists(
    st.tuples(
        st.sampled_from([Side.BUY, Side.SELL]),
        st.sampled_from(["SPY", "QQQ"]),
        st.integers(min_value=1, max_value=500),
        st.integers(min_value=1, max_value=100_000),  # 价格（分）
        st.integers(min_value=0, max_value=500),  # 佣金（分）
        st.integers(min_value=0, max_value=3),  # 结算延迟天数
    ),
    max_size=40,
)


@settings(max_examples=300, deadline=None)
@given(FILLS)
def test_ledger_conservation(fills: list[tuple[Side, str, int, int, int, int]]) -> None:
    start = Decimal(1_000_000)
    ledger = Ledger(settled=start)
    expected_cash = start
    expected_qty: dict[str, Decimal] = {}
    for side, symbol, qty, cents, fee, lag in fills:
        q, price, commission = Decimal(qty), Decimal(cents) / 100, Decimal(fee) / 100
        ledger.apply_fill(Trade(side, symbol, q, price, commission, D0 + timedelta(days=lag)))
        sign = 1 if side is Side.BUY else -1
        expected_cash += -sign * q * price - commission
        expected_qty[symbol] = expected_qty.get(symbol, Decimal(0)) + sign * q
        assert ledger.settled + ledger.unsettled == expected_cash
    for symbol, q in expected_qty.items():
        assert ledger.position(symbol).quantity == q
    ledger.settle(D0 + timedelta(days=3))
    assert ledger.unsettled == 0 and ledger.settled == expected_cash


# ---------- 成交更正与成本未知 ----------


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_correction_equals_booking_the_corrected_trade(side: Side) -> None:
    start = {"SPY": (Decimal(20), Decimal(90))}
    old = Trade(side, "SPY", Decimal(10), Decimal(100), Decimal(1), D1)
    new = Trade(side, "SPY", Decimal(8), Decimal("99.50"), Decimal("0.80"), D1)
    corrected = Ledger(settled=Decimal(5000), positions=start)
    corrected.apply_fill(old)
    corrected.correct_fill(old, new)
    direct = Ledger(settled=Decimal(5000), positions=start)
    direct.apply_fill(new)
    assert (corrected.settled, corrected.unsettled) == (direct.settled, direct.unsettled)
    a, b = corrected.position("SPY"), direct.position("SPY")
    assert a.quantity == b.quantity
    q = Decimal("1e-12")  # 成本比较量化，避免 28 位精度下的舍入差异
    assert (a.average_cost is None) == (b.average_cost is None)
    if a.average_cost is not None and b.average_cost is not None:
        assert a.average_cost.quantize(q) == b.average_cost.quantize(q)


def test_correction_cannot_change_side_or_symbol() -> None:
    old = Trade(Side.BUY, "SPY", Decimal(1), Decimal(1), Decimal(0), D1)
    with pytest.raises(ValueError, match="只能改"):
        Ledger(settled=Decimal(0)).correct_fill(
            old, Trade(Side.SELL, "SPY", Decimal(1), Decimal(1), Decimal(0), D1)
        )


def test_unknown_cost_stays_unknown_after_buy() -> None:
    ledger = Ledger(settled=Decimal(1000))
    ledger.adjust_position("SPY", Decimal(5))  # 券商侧转入，成本未知
    ledger.apply_fill(Trade(Side.BUY, "SPY", Decimal(5), Decimal(100), Decimal(0), D1))
    assert ledger.position("SPY").quantity == 10 and ledger.position("SPY").average_cost is None
