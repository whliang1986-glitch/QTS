"""标的规格（C-01）。规格缺失即禁止下单。"""

from __future__ import annotations

from decimal import Decimal

from server.domain.enums import AssetClass
from server.domain.errors import InstrumentSpecMissingError
from server.domain.values import Currency, Identifier, Price, ValueModel


class Instrument(ValueModel):
    symbol: Identifier
    asset_class: AssetClass
    exchange: Identifier
    currency: Currency
    multiplier: Price = Decimal(1)
    tick_size: Price | None
    trading_calendar: Identifier | None  # 例如 XNYS，由交易日历（D-03）解释
    broker_contract_id: Identifier | None = None

    def require_tradable(self) -> None:
        """下单前调用：任何规格缺失都抛 InstrumentSpecMissingError。"""
        missing = [
            name
            for name, value in (
                ("tick_size", self.tick_size),
                ("trading_calendar", self.trading_calendar),
            )
            if value is None
        ]
        if missing:
            raise InstrumentSpecMissingError(f"{self.symbol} 缺少规格：{', '.join(missing)}")

    def is_on_tick(self, price: Decimal) -> bool:
        if self.tick_size is None:
            raise InstrumentSpecMissingError(f"{self.symbol} 缺少 tick_size")
        return price % self.tick_size == 0
