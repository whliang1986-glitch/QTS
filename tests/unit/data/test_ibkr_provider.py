"""IBKR 历史数据源的边界情况（D-01b，替身 IB，不是联调结果）。"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, date, datetime
from decimal import Decimal

import ib_async
import pytest
from server.broker.ibkr.history import daily_bar, duration
from server.broker.ibkr.mapping import MappingError
from server.data.ibkr_provider import SPLIT_ADJUSTED, IbkrHistoryProvider
from server.data.provider import ProviderUnavailableError, UnknownSymbolError
from tests.contract.test_ibkr_history_contract import CALENDAR, FakeHistoryIB, provider_for


def bar(day: date | datetime, **kw: float) -> ib_async.BarData:
    values = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000.0}
    values.update(kw)
    return ib_async.BarData(date=day, **values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_bars_take_session_times_from_the_calendar_and_are_exact() -> None:
    batch = await provider_for(FakeHistoryIB()).fetch_bars("SPY", date(2026, 10, 1),
                                                           date(2026, 10, 2))  # fmt: skip
    first = batch.bars[0].bar
    session = CALENDAR.session(date(2026, 10, 1))
    assert session is not None
    assert (first.start, first.end) == (session.open, session.close)
    assert (first.open, first.low, first.close) == (Decimal("100.0"), Decimal("99.25"),
                                                    Decimal("100.5"))  # fmt: skip
    assert first.source == "ibkr" and first.adjustment_version == SPLIT_ADJUSTED
    assert '"barCount": 500' in batch.bars[0].raw


@pytest.mark.asyncio
async def test_closed_range_sends_no_request() -> None:
    ib = FakeHistoryIB()
    batch = await provider_for(ib).fetch_bars("SPY", date(2026, 10, 24), date(2026, 10, 25))
    assert batch.bars == () and ib.requests == []


@pytest.mark.asyncio
async def test_one_call_is_one_request_and_longer_ranges_are_refused() -> None:
    ib = FakeHistoryIB()
    batch = await provider_for(ib).fetch_bars("SPY", date(2025, 1, 1), date(2025, 12, 31))
    assert len(ib.requests) == 1  # 一个日历年（D-02 的分片）只发一次请求：限速计数准确
    assert len(batch.bars) == len(list(CALENDAR.sessions_between(date(2025, 1, 1),
                                                                 date(2025, 12, 31))))  # fmt: skip
    with pytest.raises(ValueError, match="按年切分"):
        await provider_for(ib).fetch_bars("SPY", date(2025, 1, 2), date(2026, 1, 5))
    assert len(ib.requests) == 1


@pytest.mark.asyncio
async def test_timeout_or_ibkr_error_returning_nothing_is_unavailable() -> None:
    """ib_async 超时、限速违规（162）、无权限（354）时返回 []、不抛异常：必须判为不可用。"""
    ib = FakeHistoryIB()
    ib.override = []
    with pytest.raises(ProviderUnavailableError, match="没有返回"):
        await provider_for(ib).fetch_bars("SPY", date(2026, 10, 1), date(2026, 10, 2))


@pytest.mark.asyncio
async def test_bar_on_a_non_trading_day_rejects_the_whole_batch() -> None:
    ib = FakeHistoryIB()
    ib.override = [bar(date(2026, 10, 1)), bar(date(2026, 10, 4))]  # 10-04 是周日
    with pytest.raises(MappingError, match="不是交易日"):
        await provider_for(ib).fetch_bars("SPY", date(2026, 10, 1), date(2026, 10, 5))


@pytest.mark.asyncio
async def test_unordered_or_duplicate_days_are_rejected() -> None:
    ib = FakeHistoryIB()
    ib.override = [bar(date(2026, 10, 2)), bar(date(2026, 10, 1))]
    with pytest.raises(MappingError, match="乱序或重复"):
        await provider_for(ib).fetch_bars("SPY", date(2026, 10, 1), date(2026, 10, 2))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ConnectionError("Not connected"), OSError("socket")])
async def test_connection_layer_exceptions_are_unavailable(failure: Exception) -> None:
    ib = FakeHistoryIB()
    ib.fail = failure
    with pytest.raises(ProviderUnavailableError):
        await provider_for(ib).fetch_bars("SPY", date(2026, 10, 1), date(2026, 10, 2))


@pytest.mark.asyncio
async def test_disconnected_client_is_unavailable() -> None:
    ib = FakeHistoryIB()
    ib.connected = False
    with pytest.raises(ProviderUnavailableError):
        await provider_for(ib).fetch_bars("SPY", date(2026, 10, 1), date(2026, 10, 2))


@pytest.mark.asyncio
async def test_unknown_or_non_usd_contract_is_an_unknown_symbol() -> None:
    ib = FakeHistoryIB()

    async def cad(contract: object) -> list[object]:
        found = ib_async.Stock("SPY", "SMART", "CAD")
        found.conId, found.secType = 5, "STK"
        return [found]

    ib.qualifyContractsAsync = cad  # type: ignore[method-assign]
    with pytest.raises(UnknownSymbolError):
        await IbkrHistoryProvider(provider_for(ib)._client, CALENDAR).fetch_bars(
            "SPY", date(2026, 10, 1), date(2026, 10, 2)
        )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [("close", math.nan, "有效值"), ("low", 0.0, "正数"), ("high", -1.0, "正数"),
     ("volume", -5.0, "为负"), ("open", 1.7976931348623157e308, "有效值")],
)  # fmt: skip
def test_bad_values_are_refused(field: str, value: float, match: str) -> None:
    with pytest.raises(MappingError, match=match):
        daily_bar("SPY", bar(date(2026, 10, 1), **{field: value}))


@pytest.mark.asyncio
async def test_contract_check_that_never_answers_times_out_as_unavailable() -> None:
    ib = FakeHistoryIB()
    ib.hang_qualify = True
    provider = IbkrHistoryProvider(provider_for(ib)._client, CALENDAR, timeout=0.01)
    with pytest.raises(ProviderUnavailableError, match="确认合约"):
        await provider.fetch_bars("SPY", date(2026, 10, 1), date(2026, 10, 2))


@pytest.mark.asyncio
async def test_contract_for_another_symbol_is_refused() -> None:
    ib = FakeHistoryIB()

    async def other(contract: object) -> list[object]:
        found = ib_async.Stock("SPYG", "SMART", "USD")
        found.conId, found.secType = 5, "STK"
        return [found]

    ib.qualifyContractsAsync = other  # type: ignore[method-assign]
    with pytest.raises(UnknownSymbolError):
        await provider_for(ib).fetch_bars("SPY", date(2026, 10, 1), date(2026, 10, 2))


def test_naive_clock_is_refused() -> None:
    provider = IbkrHistoryProvider(provider_for(FakeHistoryIB())._client, CALENDAR,
                                   clock=lambda: datetime(2026, 11, 5, 21, 0))  # noqa: DTZ001  # fmt: skip
    with pytest.raises(ValueError, match="时区"):
        asyncio.run(provider.fetch_bars("SPY", date(2026, 10, 24), date(2026, 10, 25)))


def test_datetime_dates_are_refused() -> None:
    with pytest.raises(MappingError, match="不是 date"):
        daily_bar("SPY", bar(datetime(2026, 10, 1, tzinfo=UTC)))


def test_duration_is_bounded() -> None:
    assert duration(date(2026, 10, 1), date(2026, 10, 1)) == "1 D"
    with pytest.raises(ValueError, match="365"):
        duration(date(2025, 1, 1), date(2026, 1, 1))
