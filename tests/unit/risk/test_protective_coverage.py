"""保护性止损覆盖核对（R-06a）。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest
from server.domain.enums import Side
from server.risk.protective_coverage import Coverage, check_coverage

ACCOUNT = "DU1234567"


@dataclass(frozen=True)
class Pos:
    symbol: str
    quantity: Decimal
    account_id: str = ACCOUNT


@dataclass(frozen=True)
class Order:
    symbol: str
    remaining_quantity: Decimal
    kind: str = "STOP"
    side: Side = Side.SELL
    time_in_force: str = "GTC"
    working: bool = True
    account_id: str = ACCOUNT


def coverage_of(positions: list[Pos], orders: list[Order]) -> dict[str, Coverage | None]:
    report = check_coverage(ACCOUNT, positions, orders)
    result: dict[str, Coverage | None] = {s: None for s in report.covered}
    result.update({g.symbol: g.coverage for g in report.gaps})
    return result


def test_each_position_with_exactly_one_matching_stop_is_covered() -> None:
    report = check_coverage(ACCOUNT, [Pos("SPY", Decimal(10)), Pos("QQQ", Decimal(5))],
                            [Order("SPY", Decimal(10)), Order("QQQ", Decimal("5.0"))])  # fmt: skip
    assert report.clean and report.covered == ("QQQ", "SPY") and report.alert_symbols() == ()


TEN = [Pos("SPY", Decimal(10))]


@pytest.mark.parametrize(
    ("positions", "orders", "expected"),
    [
        (TEN, [], Coverage.MISSING),
        (TEN, [Order("SPY", Decimal(6))], Coverage.QUANTITY_MISMATCH),
        (TEN, [Order("SPY", Decimal(12))], Coverage.QUANTITY_MISMATCH),
        (TEN, [Order("SPY", Decimal(5)), Order("SPY", Decimal(5))], Coverage.MULTIPLE),
        ([], [Order("SPY", Decimal(10))], Coverage.ORPHAN),
        ([Pos("SPY", Decimal(0))], [Order("SPY", Decimal(10))], Coverage.ORPHAN),
        ([Pos("SPY", Decimal("10.5"))], [Order("SPY", Decimal(10))], Coverage.FRACTIONAL),
        (TEN, [Order("SPY", Decimal(10)), Order("SPY", Decimal(10), "OTHER")],
         Coverage.UNRECOGNISED),
        (TEN, [Order("SPY", Decimal(10), "OTHER")], Coverage.UNRECOGNISED),
        # 当日或 GTD 止损收盘即失效：不算覆盖
        (TEN, [Order("SPY", Decimal(10), time_in_force="DAY")], Coverage.UNRECOGNISED),
        (TEN, [Order("SPY", Decimal(10), time_in_force="GTD")], Coverage.UNRECOGNISED),
        ([], [Order("SPY", Decimal(10), "OTHER")], Coverage.UNRECOGNISED),
        # 止损加减仓：同时成交会超卖
        (TEN, [Order("SPY", Decimal(10)), Order("SPY", Decimal(4), "LIMIT")], Coverage.OVERSELL),
        ([], [Order("SPY", Decimal(4), "LIMIT")], Coverage.OVERSELL),
        ([Pos("SPY", Decimal(-3))], [], Coverage.NEGATIVE),
        ([Pos("SPY", Decimal(-3))], [Order("SPY", Decimal(3))], Coverage.NEGATIVE),
    ],
)  # fmt: skip
def test_gaps_are_classified(positions: list[Pos], orders: list[Order], expected: Coverage) -> None:
    assert coverage_of(positions, orders) == {"SPY": expected}


def test_gap_carries_the_quantities_for_the_report() -> None:
    report = check_coverage(ACCOUNT, TEN, [Order("SPY", Decimal(6))])
    (gap,) = report.gaps
    assert (gap.position, gap.stop_quantity) == (Decimal(10), Decimal(6))
    assert "6" in gap.detail and report.account_id == ACCOUNT


def test_sell_limits_and_buy_stops_do_not_count_as_protection() -> None:
    result = coverage_of(TEN, [Order("SPY", Decimal(10), "STOP", Side.BUY)])
    assert result == {"SPY": Coverage.MISSING}
    report = check_coverage(ACCOUNT, TEN, [Order("SPY", Decimal(10), "LIMIT")])
    (gap,) = report.gaps
    assert gap.coverage is Coverage.MISSING and "卖出限价单 10" in gap.detail


def test_filled_out_orders_are_ignored() -> None:
    assert coverage_of([], [Order("SPY", Decimal(0))]) == {}


def test_positions_split_across_rows_are_summed() -> None:
    assert coverage_of([Pos("SPY", Decimal(4)), Pos("SPY", Decimal(6))],
                       [Order("SPY", Decimal(10))]) == {"SPY": None}  # fmt: skip


def test_negative_positions_are_left_to_reconciliation() -> None:
    report = check_coverage(ACCOUNT, [Pos("SPY", Decimal(-3)), Pos("QQQ", Decimal(5))], [])
    assert report.alert_symbols() == ("QQQ",)


@pytest.mark.parametrize("what", ["position", "order"])
def test_other_accounts_are_refused_not_filtered(what: str) -> None:
    positions = [Pos("SPY", Decimal(10), account_id="DU7654321" if what == "position" else ACCOUNT)]
    orders = [Order("SPY", Decimal(10), account_id="DU7654321" if what == "order" else ACCOUNT)]
    with pytest.raises(ValueError, match="不属于登记账户"):
        check_coverage(ACCOUNT, positions, orders)


@pytest.mark.parametrize(
    ("positions", "orders", "match"),
    [
        (TEN, [Order("SPY", Decimal(10), working=False)], "已结束"),  # 撤掉的止损不能算覆盖
        (TEN, [Order("SPY", Decimal(-1))], "为负"),
        (TEN, [Order("SPY", Decimal("NaN"))], "有限"),
        ([Pos("SPY", Decimal("NaN"))], [], "有限"),
    ],
)
def test_bad_input_is_refused_not_skipped(
    positions: list[Pos], orders: list[Order], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        check_coverage(ACCOUNT, positions, orders)
