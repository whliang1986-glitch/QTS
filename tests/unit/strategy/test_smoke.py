"""冒烟策略（T-02）：只在模拟盘；一天买一天卖；远离市价的挂单；单笔金额上限。"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from server.domain.enums import OrderClass, Side
from server.domain.market import Quote
from server.strategy.base import StrategyContext, StrategyError
from server.strategy.smoke import SmokeParams, SmokeStrategy

DAY = date(2026, 12, 2)
AT = datetime.combine(DAY, time(15, 0), UTC)


def quote(symbol: str, bid: str, ask: str, *, delayed: bool = False) -> Quote:
    return Quote(symbol=symbol, ts=AT, bid=Decimal(bid), ask=Decimal(ask), last=None,
                 source="test", delayed=delayed)  # fmt: skip


def ctx(
    *,
    mode: str | None = "PAPER",
    positions: dict[str, Decimal] | None = None,
    nav: Decimal | None = Decimal(1000000),
    quotes: dict[str, Quote] | None = None,
) -> StrategyContext:
    return StrategyContext(
        decision_at=AT, session_day=DAY, account_id="DU1", bars={}, positions=positions or {},
        quotes=quotes if quotes is not None else {
            "IVV": quote("IVV", "600.00", "600.10"), "VOO": quote("VOO", "550.00", "550.10"),
        },
        nav=nav, mode=mode,
    )  # fmt: skip


SMOKE = SmokeStrategy()


@pytest.mark.parametrize("mode", [None, "HALT", "UNKNOWN", "LIMITED_AUTO_LIVE"])
def test_refuses_outside_paper(mode: str | None) -> None:
    with pytest.raises(StrategyError, match="只能在模拟盘"):
        SMOKE.decide(ctx(mode=mode))


def test_flat_account_buys_a_fill_sample_and_a_passive_order() -> None:
    fill, passive = SMOKE.decide(ctx())
    want = ("IVV", Side.BUY, OrderClass.OPEN, Decimal(1))
    assert (fill.symbol, fill.side, fill.order_class, fill.quantity) == want
    assert fill.limit_price == Decimal("601.30")  # 600.10 × 1.002 = 601.3002
    assert (passive.symbol, passive.side) == ("VOO", Side.BUY)
    assert passive.limit_price == Decimal("533.50")  # 550.00 × 0.97：远离市价，不会成交


def test_next_day_sells_what_it_holds() -> None:
    fill, passive = SMOKE.decide(ctx(positions={"IVV": Decimal(1), "VOO": Decimal(1)}))
    want = ("IVV", Side.SELL, OrderClass.REDUCE, Decimal(1))
    assert (fill.symbol, fill.side, fill.order_class, fill.quantity) == want
    assert fill.limit_price == Decimal("598.80")  # 600.00 × 0.998
    assert (passive.symbol, passive.side) == ("VOO", Side.SELL)  # 挂单意外成交：第二天卖掉


def test_order_cap_skips_oversized_orders() -> None:
    # NAV 10 万：上限 0.5% = 500 美元，一股 IVV 约 601 美元，超过上限就不下
    proposals = SMOKE.decide(ctx(nav=Decimal(100000)))
    assert [p.symbol for p in proposals] == []
    cheap = SmokeStrategy(SmokeParams(max_order_nav=Decimal("0.01")))  # 上限 1000 美元
    assert [p.symbol for p in cheap.decide(ctx(nav=Decimal(100000)))] == ["IVV", "VOO"]


def test_requires_nav_and_live_quotes() -> None:
    with pytest.raises(StrategyError, match="净值"):
        SMOKE.decide(ctx(nav=None))
    with pytest.raises(StrategyError, match="实时买卖报价"):
        SMOKE.decide(ctx(quotes={"IVV": quote("IVV", "600", "600.1", delayed=True),
                                 "VOO": quote("VOO", "550", "550.1")}))  # fmt: skip


def test_does_not_overlap_the_benchmark_universe() -> None:
    from server.strategy.benchmark import UNIVERSE  # noqa: PLC0415

    assert SMOKE.universe.isdisjoint(UNIVERSE)
    with pytest.raises(ValueError, match="不同的标的"):
        SmokeParams(fill_symbol="IVV", passive_symbol="IVV")
