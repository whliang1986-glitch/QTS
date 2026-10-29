"""DataProvider 契约测试套件（D-01a）。

任何数据供应商适配器都要通过同一套用例：子类提供 `provider` 与 `sample` 两个夹具。
为了能原样跑在真实供应商上（D-01b 的 IBKR、D-08 的回测供应商）：
- 只断言接口约定（区间、标的、来源、排序、去重、类型），不断言具体价格；
- sample 给出一个已知有 K 线的标的与区间、一个已知没有交易日的区间、一个不存在的标的；
  has_actions 为真时，样例区间内必须有该标的的公司行动（否则公司行动断言没有约束力）。
- 真实供应商需要异步连接与断开：D-01b 用 pytest_asyncio 的异步夹具覆盖 provider。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, timedelta

import pytest
from server.data.provider import (
    PROVIDER_NAME,
    DataProvider,
    Interval,
    UnknownSymbolError,
    UnsupportedCapabilityError,
)
from server.domain.corporate_actions import CorporateActionRecord


@dataclass(frozen=True)
class ProviderSample:
    symbol: str
    start: date
    end: date
    closed_start: date  # 区间内没有交易日（例如周末）
    closed_end: date
    unknown_symbol: str
    has_actions: bool = False


class DataProviderContract:
    @pytest.fixture
    def provider(self) -> DataProvider:
        raise NotImplementedError

    @pytest.fixture
    def sample(self) -> ProviderSample:
        raise NotImplementedError

    def test_identity_and_capabilities(self, provider: DataProvider) -> None:
        assert PROVIDER_NAME.fullmatch(provider.name)
        caps = provider.capabilities
        assert Interval.DAY in caps.intervals
        assert caps.license_url.strip()
        assert provider.rate_limit.requests > 0
        assert provider.rate_limit.per > timedelta(0)

    @pytest.mark.asyncio
    async def test_list_symbols_contains_sample(
        self, provider: DataProvider, sample: ProviderSample
    ) -> None:
        if not provider.capabilities.symbol_listing:
            with pytest.raises(UnsupportedCapabilityError):
                await provider.list_symbols()
            return
        symbols = await provider.list_symbols()
        assert sample.symbol in symbols
        assert len(set(symbols)) == len(symbols)

    @pytest.mark.asyncio
    async def test_bars_honour_the_request(
        self, provider: DataProvider, sample: ProviderSample
    ) -> None:
        batch = await provider.fetch_bars(sample.symbol, sample.start, sample.end)
        assert (batch.symbol, batch.interval) == (sample.symbol, Interval.DAY)
        assert (batch.start, batch.end) == (sample.start, sample.end)
        assert batch.fetched_at.utcoffset() == timedelta(0)
        assert batch.bars, "样例区间内应当有 K 线"
        starts = [item.bar.start for item in batch.bars]
        assert starts == sorted(starts)
        assert len(set(starts)) == len(starts)
        for item in batch.bars:
            bar = item.bar
            assert bar.symbol == sample.symbol
            assert bar.source == provider.name
            assert bar.start.tzinfo is UTC
            assert sample.start <= bar.start.date() <= sample.end
            assert isinstance(item.raw, str)
            assert item.raw.strip()

    @pytest.mark.asyncio
    async def test_range_without_sessions_is_empty_not_an_error(
        self, provider: DataProvider, sample: ProviderSample
    ) -> None:
        batch = await provider.fetch_bars(sample.symbol, sample.closed_start, sample.closed_end)
        assert batch.bars == ()

    @pytest.mark.asyncio
    async def test_inverted_range_is_refused(
        self, provider: DataProvider, sample: ProviderSample
    ) -> None:
        with pytest.raises(ValueError, match="区间"):
            await provider.fetch_bars(sample.symbol, sample.end, sample.start)

    @pytest.mark.asyncio
    async def test_datetime_is_not_a_date(
        self, provider: DataProvider, sample: ProviderSample
    ) -> None:
        from datetime import datetime  # noqa: PLC0415

        moment = datetime.combine(sample.start, datetime.min.time(), tzinfo=UTC)
        with pytest.raises(TypeError):
            await provider.fetch_bars(sample.symbol, moment, sample.end)

    @pytest.mark.asyncio
    async def test_unknown_symbol(self, provider: DataProvider, sample: ProviderSample) -> None:
        with pytest.raises(UnknownSymbolError):
            await provider.fetch_bars(sample.unknown_symbol, sample.start, sample.end)

    @pytest.mark.asyncio
    async def test_corporate_actions_or_explicitly_unsupported(
        self, provider: DataProvider, sample: ProviderSample
    ) -> None:
        if not provider.capabilities.corporate_actions:
            with pytest.raises(UnsupportedCapabilityError):
                await provider.fetch_corporate_actions([sample.symbol], sample.start, sample.end)
            return
        actions = await provider.fetch_corporate_actions([sample.symbol], sample.start, sample.end)
        if sample.has_actions:
            assert actions, "样例区间内应当有公司行动"
        for action in actions:
            assert isinstance(action, CorporateActionRecord)
            assert action.symbol == sample.symbol
            assert sample.start <= action.ex_date <= sample.end
            assert action.source == provider.name

    @pytest.mark.asyncio
    async def test_calendar_or_explicitly_unsupported(
        self, provider: DataProvider, sample: ProviderSample
    ) -> None:
        if not provider.capabilities.calendar:
            with pytest.raises(UnsupportedCapabilityError):
                await provider.fetch_calendar(sample.start, sample.end)
            return
        sessions = await provider.fetch_calendar(sample.start, sample.end)
        assert sessions
        assert [s.day for s in sessions] == sorted({s.day for s in sessions})
        for session in sessions:
            assert sample.start <= session.day <= sample.end
            assert session.open < session.close
        assert not await provider.fetch_calendar(sample.closed_start, sample.closed_end)
