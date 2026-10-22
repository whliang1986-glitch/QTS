"""行情数据（C-01）。是否可用由数据门禁（D-05）判定，这里只保证结构正确。"""

from __future__ import annotations

from typing import Self

from pydantic import model_validator

from server.domain.enums import DataQuality
from server.domain.values import Identifier, Price, Quantity, UtcDatetime, ValueModel


class MarketBar(ValueModel):
    symbol: Identifier
    start: UtcDatetime
    end: UtcDatetime
    open: Price
    high: Price
    low: Price
    close: Price
    volume: Quantity
    source: Identifier
    delayed: bool = False
    quality: frozenset[DataQuality] = frozenset()
    adjustment_version: Identifier

    @model_validator(mode="after")
    def _interval_ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("bar 的 end 必须晚于 start")
        return self


class Quote(ValueModel):
    symbol: Identifier
    ts: UtcDatetime
    bid: Price | None
    ask: Price | None
    last: Price | None
    bid_size: Quantity | None = None
    ask_size: Quantity | None = None
    source: Identifier
    delayed: bool
    quality: frozenset[DataQuality] = frozenset()
