"""IBKR 状态、订单类型、错误码映射（B-03a，全部未实测，B-03b 核对）。"""

from __future__ import annotations

from decimal import Decimal

import pytest
from server.broker.base import BrokerOrderState, BrokerOrderType, ErrorCategory
from server.broker.ibkr.mapping import MappingError, error_info, order_state, order_type

S = BrokerOrderState
D = Decimal


@pytest.mark.parametrize(
    ("status", "filled", "remaining", "expected"),
    [
        ("ApiPending", "0", "10", None),
        ("PendingSubmit", "0", "10", None),
        ("PreSubmitted", "0", "10", S.ACKNOWLEDGED),
        ("Submitted", "0", "10", S.ACKNOWLEDGED),
        ("Submitted", "4", "6", S.PARTIALLY_FILLED),
        ("Submitted", "10", "0", S.FILLED),
        ("PendingCancel", "0", "10", S.CANCEL_PENDING),
        ("Cancelled", "3", "7", S.CANCELLED),
        ("ApiCancelled", "0", "10", S.CANCELLED),
        ("Filled", "10", "0", S.FILLED),
    ],
)
def test_order_states(status: str, filled: str, remaining: str, expected: S | None) -> None:
    assert order_state(status, D(filled), D(remaining)) is expected


@pytest.mark.parametrize(
    ("status", "filled", "remaining", "match"),
    [
        ("Expired?", "0", "10", "认不出"),
        ("submitted", "0", "10", "认不出"),  # 大小写不同也不猜
        ("Filled", "9", "1", "不一致"),
        ("Filled", "0", "0", "不一致"),  # 没有成交量的 Filled
        ("Submitted", "NaN", "10", "有限值"),
        ("Submitted", "0", "Infinity", "有限值"),
        ("Submitted", "-1", "10", "为负"),
        ("PendingSubmit", "-1", "10", "为负"),  # 中间状态也先查数量
        ("Inactive", "0", "10", "先查单"),  # 可能未结束：不当作终态
    ],
)
def test_unknown_or_inconsistent_states_fail_closed(
    status: str, filled: str, remaining: str, match: str
) -> None:
    with pytest.raises(MappingError, match=match):
        order_state(status, D(filled), D(remaining))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("LMT", BrokerOrderType.LIMIT),
        (" lmt ", BrokerOrderType.LIMIT),
        ("STP", BrokerOrderType.STOP),
        ("STP LMT", BrokerOrderType.STOP),
        ("stp  lmt", BrokerOrderType.STOP),
        ("TRAIL", BrokerOrderType.STOP),
        ("TRAIL LIMIT", BrokerOrderType.STOP),
        ("STP PRT", BrokerOrderType.STOP),
        ("MKT", BrokerOrderType.OTHER),
        ("LOC", BrokerOrderType.OTHER),
        ("LIT", BrokerOrderType.OTHER),
        ("MIT", BrokerOrderType.OTHER),
        ("", BrokerOrderType.OTHER),
    ],
)
def test_order_types(raw: str, expected: BrokerOrderType) -> None:
    assert order_type(raw) is expected


@pytest.mark.parametrize(
    ("code", "category", "retryable"),
    [
        (103, ErrorCategory.DUPLICATE_ORDER_ID, False),
        (110, ErrorCategory.PRICE_REJECTED, False),
        (161, ErrorCategory.CANCEL_REJECTED, False),
        (10147, ErrorCategory.ORDER_NOT_FOUND, False),
        (200, ErrorCategory.CONTRACT_NOT_FOUND, False),
        (100, ErrorCategory.RATE_LIMITED, True),
        (1100, ErrorCategory.CONNECTIVITY, True),
        (1102, ErrorCategory.CONNECTIVITY, True),
        (326, ErrorCategory.CONNECTIVITY, False),
        (354, ErrorCategory.NO_PERMISSION, False),
        (201, ErrorCategory.OTHER, False),
        (99999, ErrorCategory.OTHER, False),  # 认不出：不可重试
    ],
)
def test_error_codes(code: int, category: ErrorCategory, retryable: bool) -> None:
    info = error_info(code)
    assert info is not None
    assert (info.category, info.retryable) == (category, retryable)


@pytest.mark.parametrize("code", [2104, 2106, 2158, 10167])
def test_informational_codes_are_not_errors(code: int) -> None:
    assert error_info(code) is None
