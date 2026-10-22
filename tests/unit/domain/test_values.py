"""C-01：基础值类型——Decimal 与 UTC 强制。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from typing import ClassVar

import pytest
from pydantic import ValidationError
from server.domain.modes import AuditEvent
from server.domain.values import Money, Price, Quantity, UtcDatetime, ValueModel

NEW_YORK_SUMMER = timezone(timedelta(hours=-4))


class _Box(ValueModel):
    evolvable_fields: ClassVar[frozenset[str] | None] = None

    price: Price
    qty: Quantity
    cash: Money
    at: UtcDatetime


def _box(**overrides: object) -> _Box:
    data: dict[str, object] = {
        "price": Decimal("10.25"),
        "qty": Decimal("3"),
        "cash": Decimal("-1.50"),
        "at": datetime(2026, 10, 22, 13, 30, tzinfo=UTC),
    }
    data.update(overrides)
    return _Box.model_validate(data)


def test_accepts_decimal_int_and_numeric_string() -> None:
    box = _box(price="10.25", qty=3, cash="0")
    assert box.price == Decimal("10.25")
    assert isinstance(box.qty, Decimal)
    assert box.qty == Decimal(3)


@pytest.mark.parametrize("field", ["price", "qty", "cash"])
@pytest.mark.parametrize(
    "bad", [1.5, 0.1, True, "NaN", "Infinity", "-inf", Decimal("NaN"), "abc", None]
)
def test_rejects_float_bool_and_non_finite(field: str, bad: object) -> None:
    with pytest.raises(ValidationError):
        _box(**{field: bad})


@pytest.mark.parametrize("bad", ["0", "-0.01", Decimal("-5")])
def test_price_must_be_positive(bad: object) -> None:
    with pytest.raises(ValidationError):
        _box(price=bad)


def test_quantity_may_be_zero_but_not_negative() -> None:
    assert _box(qty="0").qty == 0
    with pytest.raises(ValidationError):
        _box(qty="-1")


def test_money_may_be_negative() -> None:
    assert _box(cash="-100.5").cash == Decimal("-100.5")


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValidationError):
        _box(at=datetime(2026, 10, 22, 13, 30))  # noqa: DTZ001  故意构造无时区时间


def test_aware_datetime_normalized_to_utc() -> None:
    box = _box(at=datetime(2026, 10, 22, 9, 30, tzinfo=NEW_YORK_SUMMER))
    assert box.at.tzinfo is UTC
    assert box.at == datetime(2026, 10, 22, 13, 30, tzinfo=UTC)


def test_iso_string_without_offset_rejected() -> None:
    with pytest.raises(ValidationError):
        _box(at="2026-10-22T13:30:00")


def test_json_serializes_decimal_as_string_and_round_trips() -> None:
    box = _box(price=Decimal("10.250"))
    raw = box.model_dump_json()
    assert '"price":"10.250"' in raw
    assert _Box.model_validate_json(raw) == box


def test_json_number_with_fraction_rejected() -> None:
    raw = '{"price": 10.25, "qty": "1", "cash": "0", "at": "2026-10-22T13:30:00Z"}'
    with pytest.raises(ValidationError):
        _Box.model_validate_json(raw)


def test_frozen_and_extra_forbidden() -> None:
    box = _box()
    with pytest.raises(ValidationError):
        box.price = Decimal(1)  # type: ignore[misc]
    with pytest.raises(ValidationError):
        _Box.model_validate({**box.model_dump(), "unexpected": 1})


def test_model_copy_with_update_is_forbidden() -> None:
    with pytest.raises(TypeError):
        _box().model_copy(update={"price": Decimal("-1")})


def test_evolve_revalidates() -> None:
    box = _box()
    assert box.evolve(price=Decimal("11")).price == Decimal("11")
    with pytest.raises(ValidationError):
        box.evolve(price=1.5)


# ---------- 评审 C-01 补充 ----------


@pytest.mark.parametrize(
    "bad",
    [Decimal("sNaN"), Decimal("-Infinity"), " 10 ", "1_000", "1e999999999", "0.00000000001",
     "1e15", "+5", ""],
)  # fmt: skip
def test_rejects_non_canonical_or_out_of_range_numbers(bad: object) -> None:
    with pytest.raises(ValidationError):
        _box(qty=bad)


@pytest.mark.parametrize("ok", ["1e3", "123456789012345", "0.0000000001", ".5", "5."])
def test_accepts_canonical_numbers_in_range(ok: str) -> None:
    assert _box(qty=ok).qty == Decimal(ok)


@pytest.mark.parametrize("raw_value", ["true", "1e3", "1.5"])
def test_json_non_string_non_integer_numbers_rejected(raw_value: str) -> None:
    raw = '{"price": "1", "qty": ' + raw_value + ', "cash": "0", "at": "2026-10-22T13:30:00Z"}'
    with pytest.raises(ValidationError):
        _Box.model_validate_json(raw)


def test_json_integer_accepted() -> None:
    raw = '{"price": "1", "qty": 7, "cash": "0", "at": "2026-10-22T13:30:00Z"}'
    assert _Box.model_validate_json(raw).qty == Decimal(7)


class _NoOffset(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        return "none"


def test_tzinfo_without_offset_rejected() -> None:
    with pytest.raises(ValidationError):
        _box(at=datetime(2026, 10, 22, 13, 30, tzinfo=_NoOffset()))


class _WeirdDatetime(datetime):
    pass


def test_datetime_subclass_normalized_to_plain_datetime() -> None:
    box = _box(at=_WeirdDatetime(2026, 10, 22, 13, 30, tzinfo=UTC))
    assert type(box.at) is datetime


# 末尾换行、开头换行、阿拉伯-印度数字、全角数字
@pytest.mark.parametrize("bad", ["10\n", "\n10", "١٠", "１"])
def test_rejects_trailing_newline_and_unicode_digits(bad: str) -> None:
    with pytest.raises(ValidationError):
        _box(qty=bad)


def test_models_without_declared_fields_cannot_evolve() -> None:
    ev = AuditEvent(
        event_id="a", occurred_at=datetime(2026, 1, 2, tzinfo=UTC), actor="x", action="y",
        correlation_id="c",
    )  # fmt: skip
    with pytest.raises(ValueError, match="不可修改"):
        ev.evolve(action="z")
