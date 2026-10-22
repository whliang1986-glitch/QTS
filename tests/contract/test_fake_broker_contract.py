"""FakeBroker 必须通过与 IBKR 相同的契约（B-01）。"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from server.broker.base import BrokerAdapter, OrderTicket
from server.broker.fake.broker import FakeBroker
from server.domain.account import Account
from server.domain.enums import AccountType, AssetClass, Side
from server.domain.instrument import Instrument

from tests.contract.broker_contract import BrokerContract, MarketDriver, make_ticket

ACCOUNT = Account(account_id="DU0000001", account_type=AccountType.CASH, base_currency="USD")


class FakeDriver:
    account_id = ACCOUNT.account_id
    supports_partial_fills = True
    supports_unbacked_stops = True
    supports_foreign_injection = True

    def inject_foreign(self, adapter: BrokerAdapter) -> None:
        assert isinstance(adapter, FakeBroker)
        adapter.inject_foreign_order("SPY", Side.SELL, Decimal(5), raw_order_type="TRAIL")
        adapter.inject_foreign_execution("SPY", Side.SELL, Decimal("0.5"), Decimal("500.00"))

    def instrument(self) -> Instrument:
        return Instrument(
            symbol="SPY",
            asset_class=AssetClass.ETF,
            exchange="ARCA",
            currency="USD",
            tick_size=Decimal("0.01"),
            trading_calendar="XNYS",
        )

    def resting_price(self) -> Decimal:
        return Decimal("100.00")

    def marketable_price(self) -> Decimal:
        return Decimal("900.00")

    async def fill(self, adapter: BrokerAdapter, ticket: OrderTicket, quantity: Decimal) -> None:
        assert isinstance(adapter, FakeBroker)
        adapter.fill(ticket.broker_order_id, quantity, ticket.price - Decimal("0.01"))


class TestFakeBrokerContract(BrokerContract):
    @pytest.fixture
    def adapter(self) -> BrokerAdapter:
        return FakeBroker(ACCOUNT)

    @pytest.fixture
    def driver(self) -> MarketDriver:
        return FakeDriver()


def test_fake_fill_rejects_price_worse_than_limit() -> None:
    async def run() -> None:
        broker = FakeBroker(ACCOUNT)
        await broker.connect()
        ticket = make_ticket(FakeDriver(), await broker.next_order_id(), "worse")
        await broker.place_order(ticket)
        with pytest.raises(ValueError, match="劣于限价"):
            broker.fill(ticket.broker_order_id, Decimal(1), ticket.price + Decimal("0.01"))

    asyncio.run(run())


def test_fake_foreign_limit_order_requires_price() -> None:
    broker = FakeBroker(ACCOUNT)
    with pytest.raises(ValueError, match="必须带价格"):
        broker.inject_foreign_order("SPY", Side.BUY, Decimal(1), raw_order_type="LMT")
