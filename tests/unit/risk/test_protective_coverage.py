"""保护性止损覆盖核对（R-06a）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from server.domain.enums import Side
from server.risk.default_deny import ProtectiveStopParams
from server.risk.protective_coverage import (
    Coverage,
    check_broker_coverage,
    check_coverage,
    plan_protective_stops,
    to_working_orders,
)
from tests.support.oms import SPY

ACCOUNT = "DU1234567"
NOW = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Pos:
    symbol: str
    quantity: Decimal
    account_id: str = ACCOUNT
    average_cost: Decimal | None = Decimal(100)


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


# ---------- R-06b：券商记录视图转换、空快照拒绝与止损建单规划 ----------


@dataclass(frozen=True)
class BrokerOrder:
    symbol: str
    remaining_quantity: Decimal
    raw_order_type: str = "STP"
    time_in_force: str = "GTC"
    side: Side = Side.SELL
    state: str = "ACKNOWLEDGED"
    account_id: str = ACCOUNT


def test_to_working_orders_maps_only_plain_stp_to_stop() -> None:
    views = to_working_orders(
        [
            BrokerOrder("SPY", Decimal(10), "STP"),
            BrokerOrder("QQQ", Decimal(10), "LMT"),
            BrokerOrder("IWM", Decimal(10), "STP LMT"),
            BrokerOrder("TLT", Decimal(10), "TRAIL"),
            BrokerOrder("GLD", Decimal(10), "STP", state="FILLED"),
        ]
    )
    assert [(v.symbol, v.kind, v.working) for v in views] == [
        ("SPY", "STOP", True),
        ("QQQ", "LIMIT", True),
        ("IWM", "OTHER", True),
        ("TLT", "OTHER", True),
        ("GLD", "STOP", False),
    ]


def test_check_broker_coverage_refuses_missing_snapshots() -> None:
    with pytest.raises(ValueError, match="快照不可用"):
        check_broker_coverage(ACCOUNT, None, [])
    with pytest.raises(ValueError, match="快照不可用"):
        check_broker_coverage(ACCOUNT, [], None)


def test_plan_protective_stops_covers_normal_fractional_and_below_stop() -> None:
    params = ProtectiveStopParams(
        stop_distance=Decimal("0.10"),
        below_stop_distance=Decimal("0.05"),
        allow_fractional_floor=True,
    )
    plan = plan_protective_stops(
        ACCOUNT,
        [Pos("SPY", Decimal("10.5"), average_cost=Decimal(100))],
        [],
        instruments={"SPY": SPY},
        last_prices={"SPY": Decimal(98)},
        params=params,
        now=NOW,
    )
    assert plan.fractional_symbols == ("SPY",)
    assert plan.below_stop_symbols == ()
    assert len(plan.intents) == 1
    assert plan.intents[0].quantity == Decimal(10)
    assert plan.intents[0].stop_price == Decimal("90.00")

    # 跌破止损线（last=88 <= 90）：记录告警并按当前价下方 5% 挂止损（83.60）
    below = plan_protective_stops(
        ACCOUNT,
        [Pos("SPY", Decimal(10), average_cost=Decimal(100))],
        [],
        instruments={"SPY": SPY},
        last_prices={"SPY": Decimal(88)},
        params=params,
        now=NOW,
    )
    assert below.below_stop_symbols == ("SPY",)
    assert "SPY" in below.alert_symbols()
    assert len(below.intents) == 1 and below.intents[0].stop_price == Decimal("83.60")

    # 未配置 below_stop_distance 时 fail closed：只告警，不建止损单
    strict_params = ProtectiveStopParams(stop_distance=Decimal("0.10"))
    strict_plan = plan_protective_stops(
        ACCOUNT,
        [Pos("SPY", Decimal(10), average_cost=Decimal(100))],
        [],
        instruments={"SPY": SPY},
        last_prices={"SPY": Decimal(88)},
        params=strict_params,
        now=NOW,
    )
    assert strict_plan.below_stop_symbols == ("SPY",)
    assert strict_plan.intents == ()
