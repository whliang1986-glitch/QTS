"""IBKR 历史数据源跑同一套 DataProvider 契约（D-01b）——这里用替身 IB，**不是 IBKR 联调结果**。

联调时把 `provider` 夹具换成连着 TWS 模拟盘的 IbAsyncClient，原样再跑一遍（docs/tasks/D-01b.md）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any

import ib_async
import pytest
from server.broker.ibkr.client import IbAsyncClient
from server.data.calendar import TradingCalendar
from server.data.ibkr_provider import IbkrHistoryProvider
from server.data.provider import DataProvider

from tests.contract.data_provider_contract import DataProviderContract, ProviderSample

CALENDAR = TradingCalendar.nyse(start=date(2025, 1, 1), end=date(2026, 12, 31))
NOW = datetime(2026, 11, 5, 21, 0, tzinfo=UTC)
SAMPLE = ProviderSample(
    symbol="SPY",
    start=date(2026, 10, 1),
    end=date(2026, 10, 30),
    closed_start=date(2026, 10, 24),  # 周六、周日
    closed_end=date(2026, 10, 25),
    unknown_symbol="NOPE",
)


class FakeHistoryIB:
    """替身：按交易日历给每只已知标的生成日线；记录每次历史数据请求。"""

    def __init__(self, symbols: tuple[str, ...] = ("SPY", "QQQ")) -> None:
        self.symbols = symbols
        self.connected = True
        self.requests: list[tuple[str, str, str]] = []
        self.override: list[Any] | None = None  # 设置后原样返回（ib_async 超时、报错时就是 []）
        self.fail: Exception | None = None  # 连接层抛异常（例如断线时 client.send 报错）
        self.hang_qualify = False

    def isConnected(self) -> bool:  # noqa: N802
        return self.connected

    async def qualifyContractsAsync(self, contract: Any) -> list[Any]:  # noqa: N802
        if self.hang_qualify:
            await asyncio.sleep(3600)  # TWS 不回复
        if contract.symbol not in self.symbols:
            return [None]
        found = ib_async.Stock(contract.symbol, "SMART", "USD")
        found.conId, found.secType = 1000 + self.symbols.index(contract.symbol), "STK"
        return [found]

    async def reqHistoricalDataAsync(self, contract: Any, endDateTime: str,  # noqa: N802, N803
                                     durationStr: str, **kw: Any) -> list[Any]:  # noqa: N803  # fmt: skip
        self.requests.append((contract.symbol, endDateTime, durationStr))
        if self.fail is not None:
            raise self.fail
        if self.override is not None:
            return self.override
        end = datetime.strptime(endDateTime[:8], "%Y%m%d").replace(tzinfo=UTC).date()
        days = int(durationStr.split(maxsplit=1)[0])
        start = end - timedelta(days=days - 1)
        return [
            ib_async.BarData(date=s.day, open=100.0 + i, high=101.5 + i, low=99.25 + i,
                             close=100.5 + i, volume=1_000_000.0 + i, average=100.4, barCount=500)
            for i, s in enumerate(CALENDAR.sessions_between(start, end))
        ]  # fmt: skip


def provider_for(ib: FakeHistoryIB) -> IbkrHistoryProvider:
    return IbkrHistoryProvider(IbAsyncClient(ib), CALENDAR, clock=lambda: NOW)


class TestIbkrHistoryContract(DataProviderContract):
    @pytest.fixture
    def provider(self) -> DataProvider:
        return provider_for(FakeHistoryIB())

    @pytest.fixture
    def sample(self) -> ProviderSample:
        return SAMPLE
