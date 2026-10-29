"""公司行动记录的字段约束（D-01a）。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError
from server.domain.corporate_actions import CorporateActionKind, CorporateActionRecord

SPLIT: dict[str, Any] = {
    "event_id": "e1", "symbol": "AAPL", "kind": CorporateActionKind.SPLIT,
    "ex_date": date(2026, 6, 1), "source": "fake", "numerator": 4, "denominator": 1,
}  # fmt: skip
DIVIDEND: dict[str, Any] = {
    "event_id": "e2", "symbol": "SPY", "kind": CorporateActionKind.CASH_DIVIDEND,
    "ex_date": date(2026, 6, 1), "source": "fake", "amount": Decimal("1.75"), "currency": "USD",
}  # fmt: skip


def test_valid_split_and_reverse_split_and_dividend() -> None:
    assert CorporateActionRecord(**SPLIT).numerator == 4
    assert CorporateActionRecord(**{**SPLIT, "numerator": 1, "denominator": 10}).denominator == 10
    assert CorporateActionRecord(**DIVIDEND).amount == Decimal("1.75")


@pytest.mark.parametrize(
    ("base", "changes", "match"),
    [
        (SPLIT, {"denominator": None}, "numerator 与 denominator"),
        (SPLIT, {"numerator": 2, "denominator": 2}, "1:1"),
        (SPLIT, {"numerator": 0}, "greater than 0"),
        (SPLIT, {"amount": Decimal(1), "currency": "USD"}, "不能带现金"),
        (DIVIDEND, {"amount": None}, "每股金额"),
        (DIVIDEND, {"currency": None}, "每股金额"),
        (DIVIDEND, {"numerator": 2, "denominator": 1}, "不能带拆股比例"),
        (DIVIDEND, {"amount": 1.75}, "float"),
        (DIVIDEND, {"amount": Decimal(0)}, "大于 0"),
        (DIVIDEND, {"ex_date": datetime(2026, 6, 1, tzinfo=UTC)}, "date"),
        (DIVIDEND, {"ex_date": "2026-06-01"}, "date"),
        (DIVIDEND, {"currency": "EUR"}, "USD"),
    ],
)
def test_invalid_records_are_refused(
    base: dict[str, Any], changes: dict[str, Any], match: str
) -> None:
    with pytest.raises(ValidationError, match=match):
        CorporateActionRecord(**{**base, **changes})
