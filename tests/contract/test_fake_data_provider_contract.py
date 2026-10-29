"""FakeDataProvider 必须通过与真实供应商相同的契约（D-01a）。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from server.data.calendar import TradingCalendar
from server.data.fake_provider import FakeDataProvider, synthetic_daily_bars
from server.data.provider import DataProvider
from server.domain.corporate_actions import CorporateActionKind, CorporateActionRecord

from tests.contract.data_provider_contract import DataProviderContract, ProviderSample

CALENDAR = TradingCalendar.nyse(start=date(2026, 9, 1), end=date(2026, 12, 31))
SAMPLE = ProviderSample(
    symbol="SPY",
    start=date(2026, 10, 1),
    end=date(2026, 10, 30),
    closed_start=date(2026, 10, 24),  # 周六、周日
    closed_end=date(2026, 10, 25),
    unknown_symbol="NOPE",
)


def _provider(*, full: bool) -> FakeDataProvider:
    bars = {
        "SPY": synthetic_daily_bars(
            "SPY", CALENDAR, date(2026, 9, 1), date(2026, 12, 31), source="fake"
        ),
        "QQQ": synthetic_daily_bars(
            "QQQ", CALENDAR, date(2026, 9, 1), date(2026, 12, 31), source="fake"
        ),
    }
    dividend = CorporateActionRecord(
        event_id="fake-spy-div-2026q4", symbol="SPY", kind=CorporateActionKind.CASH_DIVIDEND,
        ex_date=date(2026, 10, 16), source="fake", amount=Decimal("1.75"), currency="USD",
    )  # fmt: skip
    return FakeDataProvider(
        bars=bars,
        actions=[dividend],
        calendar=CALENDAR if full else None,
        symbol_listing=full,
        corporate_actions=full,
    )


class TestFakeDataProviderContract(DataProviderContract):
    @pytest.fixture
    def provider(self) -> DataProvider:
        return _provider(full=True)

    @pytest.fixture
    def sample(self) -> ProviderSample:
        return replace(SAMPLE, has_actions=True)


class TestFakeDataProviderWithoutOptionalCapabilities(DataProviderContract):
    """不提供标的列表、公司行动与日历的供应商（例如 IBKR 不提供日历）：必须明确报“不支持”。"""

    @pytest.fixture
    def provider(self) -> DataProvider:
        return _provider(full=False)

    @pytest.fixture
    def sample(self) -> ProviderSample:
        return SAMPLE


@pytest.mark.asyncio
async def test_fake_returns_the_dividend_inside_the_range() -> None:
    actions = await _provider(full=True).fetch_corporate_actions(
        ["SPY", "QQQ"], SAMPLE.start, SAMPLE.end
    )
    assert [a.event_id for a in actions] == ["fake-spy-div-2026q4"]


@pytest.mark.asyncio
async def test_fake_failure_injection_raises_for_every_call() -> None:
    from server.data.provider import ProviderUnavailableError  # noqa: PLC0415

    provider = _provider(full=True)
    provider.fail_with = ProviderUnavailableError("维护中")
    with pytest.raises(ProviderUnavailableError):
        await provider.fetch_bars("SPY", SAMPLE.start, SAMPLE.end)
    with pytest.raises(ProviderUnavailableError):
        await provider.list_symbols()


def test_provider_name_must_be_a_lake_identifier() -> None:
    with pytest.raises(ValueError, match="供应商名"):
        FakeDataProvider("Bad-Name")
