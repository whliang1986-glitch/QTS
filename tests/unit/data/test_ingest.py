"""K 线入湖（D-01a）：原始层 → 标准化层、合并、幂等、修订新版本、契约违反整批拒绝。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from server.data.calendar import TradingCalendar
from server.data.fake_provider import FAKE_LICENSE_URL, FakeDataProvider, synthetic_daily_bars
from server.data.ingest import IngestError, ingest_bars
from server.data.lake import DataLake
from server.data.provider import (
    BarBatch,
    FetchedBar,
    Interval,
    ProviderCapabilities,
    ProviderUnavailableError,
    RateLimit,
    UnsupportedCapabilityError,
)
from server.domain.market import MarketBar

CALENDAR = TradingCalendar.nyse(start=date(2026, 9, 1), end=date(2026, 12, 31))
START, END = date(2026, 10, 5), date(2026, 10, 9)  # 周一到周五，5 个交易日
FETCHED = datetime(2026, 10, 10, 1, 0, tzinfo=UTC)


def _bars(symbol: str, base: str = "100.00") -> list[MarketBar]:
    return synthetic_daily_bars(
        symbol, CALENDAR, date(2026, 9, 1), date(2026, 12, 31), source="fake", base=Decimal(base)
    )


def _with(bar: MarketBar, **changes: object) -> MarketBar:
    return MarketBar.model_validate({**bar.model_dump(), **changes})


def _provider(**bars: list[MarketBar]) -> FakeDataProvider:
    return FakeDataProvider(bars=bars or {"SPY": _bars("SPY")}, clock=lambda: FETCHED)


def _files(lake: DataLake) -> list[str]:
    return sorted(str(p.relative_to(lake.root)) for p in lake.root.rglob("*") if p.is_file())


@pytest.mark.asyncio
async def test_raw_then_normalized_with_lineage_and_license(tmp_path: Path) -> None:
    lake = DataLake(tmp_path)
    result = await ingest_bars(_provider(), lake, "SPY", START, END)
    assert result.days == (date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7),
                           date(2026, 10, 8), date(2026, 10, 9))  # fmt: skip
    assert result.bars == 5
    for raw, bars in zip(result.raw, result.normalized, strict=True):
        assert raw.manifest.dataset == "fake_bars_1d"
        assert bars.manifest.dataset == "bars_1d"
        assert bars.manifest.lineage == (raw.key,)
        for ref in (raw, bars):
            assert ref.manifest.provider == "fake"
            assert ref.manifest.license_url == FAKE_LICENSE_URL
            assert ref.manifest.fetched_at == FETCHED
    raw_rows = lake.read_partition(result.raw[0]).to_pylist()
    assert [r["symbol"] for r in raw_rows] == ["SPY"]
    assert json.loads(raw_rows[0]["raw"])["symbol"] == "SPY"  # 供应商原始记录原样保存
    read = lake.read_bars("SPY", START, END)
    assert [b.close for b in read] == [
        b.close for b in _bars("SPY") if START <= b.start.date() <= END
    ]


@pytest.mark.asyncio
async def test_rerun_is_idempotent(tmp_path: Path) -> None:
    lake = DataLake(tmp_path)
    first = await ingest_bars(_provider(), lake, "SPY", START, END)
    files = _files(lake)
    second = await ingest_bars(_provider(), lake, "SPY", START, END)
    assert _files(lake) == files
    assert [r.key for r in second.normalized] == [r.key for r in first.normalized]


@pytest.mark.asyncio
async def test_second_symbol_merges_into_the_same_day_partitions(tmp_path: Path) -> None:
    lake = DataLake(tmp_path)
    provider = _provider(SPY=_bars("SPY"), QQQ=_bars("QQQ", "400.00"))
    await ingest_bars(provider, lake, "SPY", START, END)
    result = await ingest_bars(provider, lake, "QQQ", START, END)
    assert all(ref.manifest.version == 2 for ref in (*result.raw, *result.normalized))
    assert len(lake.read_bars("SPY", START, END)) == 5  # 第一个标的没有丢
    assert len(lake.read_bars("QQQ", START, END)) == 5
    assert {r["symbol"] for r in lake.read_partition(result.raw[0]).to_pylist()} == {"SPY", "QQQ"}


@pytest.mark.asyncio
async def test_vendor_revision_writes_a_new_version_and_keeps_the_old(tmp_path: Path) -> None:
    lake = DataLake(tmp_path)
    await ingest_bars(_provider(), lake, "SPY", START, END)
    pinned = lake.snapshot("bars_1d", START, END)
    revised = _provider(SPY=_bars("SPY", "100.01"))
    result = await ingest_bars(revised, lake, "SPY", START, END)
    assert all(ref.manifest.version == 2 for ref in result.normalized)
    old = lake.read_bars("SPY", START, END, versions=pinned)
    new = lake.read_bars("SPY", START, END)
    assert [b.close for b in old] == [
        b.close for b in _bars("SPY") if START <= b.start.date() <= END
    ]
    assert new[0].close == old[0].close + Decimal("0.01")


@pytest.mark.asyncio
async def test_partial_range_only_touches_days_with_bars(tmp_path: Path) -> None:
    lake = DataLake(tmp_path)
    await ingest_bars(_provider(), lake, "SPY", START, END)
    result = await ingest_bars(_provider(), lake, "SPY", date(2026, 10, 10), date(2026, 10, 11))
    assert result.days == ()  # 周末：没有 K 线，不写任何分区
    assert result.raw == result.normalized == ()


class _Tampering(FakeDataProvider):
    """在假数据源的正常返回上做一次篡改，模拟违反契约的供应商。"""

    def __init__(self, change: str) -> None:
        super().__init__(bars={"SPY": _bars("SPY"), "QQQ": _bars("QQQ")}, clock=lambda: FETCHED)
        self.change = change

    async def fetch_bars(
        self, symbol: str, start: date, end: date, interval: Interval = Interval.DAY
    ) -> BarBatch:
        batch = await super().fetch_bars(symbol, start, end, interval)
        items = list(batch.bars)
        first = items[0]
        if self.change == "symbol":
            items[0] = FetchedBar(_with(first.bar, symbol="QQQ"), first.raw)
        elif self.change == "source":
            items[0] = FetchedBar(_with(first.bar, source="other"), first.raw)
        elif self.change == "range":
            outside = _bars("SPY")[0]  # 9 月 1 日
            items.append(FetchedBar(outside, "{}"))
        elif self.change == "duplicate":
            items.append(first)
        elif self.change == "raw":
            items[0] = FetchedBar(first.bar, "  ")
        elif self.change == "batch":
            return replace(batch, symbol="QQQ")
        elif self.change == "naive":
            return replace(batch, fetched_at=datetime(2026, 10, 10))  # noqa: DTZ001 - 故意不带时区
        elif self.change == "integer":  # MarketBar 允许 15 位整数，decimal(18,6) 只容 12 位
            items[0] = FetchedBar(_with(first.bar, high=Decimal("1234567890123")), first.raw)
        elif self.change == "volume":
            items[0] = FetchedBar(_with(first.bar, volume=Decimal("1.12345")), first.raw)
        elif self.change == "precision":
            items[0] = FetchedBar(_with(first.bar, close=Decimal("100.1234567")), first.raw)
        return replace(batch, bars=tuple(items))


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ("symbol", "返回了 QQQ"),
        ("source", "来源是 other"),
        ("range", "超出请求区间"),
        ("duplicate", "重复"),
        ("raw", "原始记录"),
        ("batch", "与请求不符"),
        ("naive", "拉取时间"),
        ("precision", "schema"),
        ("integer", "schema"),
        ("volume", "schema"),
    ],
)
@pytest.mark.asyncio
async def test_contract_violations_refuse_the_whole_batch(
    tmp_path: Path, change: str, match: str
) -> None:
    lake = DataLake(tmp_path)
    with pytest.raises(IngestError, match=match):
        await ingest_bars(_Tampering(change), lake, "SPY", START, END)
    assert _files(lake) == []  # 一个分区都没写


@pytest.mark.asyncio
async def test_provider_failure_propagates_and_writes_nothing(tmp_path: Path) -> None:
    lake = DataLake(tmp_path)
    provider = _provider()
    provider.fail_with = ProviderUnavailableError("维护中")
    with pytest.raises(ProviderUnavailableError):
        await ingest_bars(provider, lake, "SPY", START, END)
    assert _files(lake) == []


@pytest.mark.asyncio
async def test_unsupported_interval_is_refused_before_fetching(tmp_path: Path) -> None:
    class _NoDaily(FakeDataProvider):
        @property
        def capabilities(self) -> ProviderCapabilities:
            return replace(super().capabilities, intervals=frozenset({"1m"}))  # type: ignore[arg-type]

    provider = _NoDaily(bars={"SPY": _bars("SPY")})
    with pytest.raises(UnsupportedCapabilityError):
        await ingest_bars(provider, DataLake(tmp_path), "SPY", START, END)
    assert provider.calls == []


@pytest.mark.asyncio
async def test_inverted_range_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="区间"):
        await ingest_bars(_provider(), DataLake(tmp_path), "SPY", END, START)


def test_capabilities_and_rate_limit_validation() -> None:
    with pytest.raises(ValueError, match="授权条款"):
        ProviderCapabilities(frozenset({Interval.DAY}), True, True, True, False, " ")
    with pytest.raises(ValueError, match="周期"):
        ProviderCapabilities(frozenset(), True, True, True, False, FAKE_LICENSE_URL)
    with pytest.raises(ValueError, match="限速"):
        RateLimit(0, timedelta(seconds=1))
    with pytest.raises(ValueError, match="限速"):
        RateLimit(1, timedelta(0))


@pytest.mark.asyncio
async def test_rerun_after_merging_another_symbol_is_idempotent(tmp_path: Path) -> None:
    """SPY → QQQ → SPY（内容不变）：从 Parquet 读回的他标的行 + 新行，内容哈希必须稳定。"""
    lake = DataLake(tmp_path)
    provider = _provider(SPY=_bars("SPY"), QQQ=_bars("QQQ", "400.00"))
    await ingest_bars(provider, lake, "SPY", START, END)
    await ingest_bars(provider, lake, "QQQ", START, END)
    files = _files(lake)
    again = await ingest_bars(provider, lake, "SPY", START, END)
    assert _files(lake) == files
    assert all(ref.manifest.version == 2 for ref in (*again.raw, *again.normalized))


@pytest.mark.asyncio
async def test_incompatible_existing_partition_refuses_the_whole_batch(tmp_path: Path) -> None:
    """第 3 天已有一个 schema 不同的原始分区：整批拒绝，第 1、2 天也不产生新版本（评审 P1-1）。"""
    import pyarrow as pa  # noqa: PLC0415

    lake = DataLake(tmp_path)
    lake.write_partition(
        "raw", "fake_bars_1d", date(2026, 10, 7), pa.table({"other": ["x"]}),
        provider="fake", fetched_at=FETCHED,
    )  # fmt: skip
    before = _files(lake)
    with pytest.raises(IngestError, match="schema"):
        await ingest_bars(_provider(), lake, "SPY", START, END)
    assert _files(lake) == before


@pytest.mark.asyncio
async def test_another_providers_rows_are_not_merged(tmp_path: Path) -> None:
    """标准化层首期单一来源：别家写过的日分区不合并，避免来源、授权与 lineage 失真（评审 P1-2）。"""
    lake = DataLake(tmp_path)
    await ingest_bars(_provider(), lake, "SPY", START, END)
    before = _files(lake)
    other = FakeDataProvider(
        "other", bars={"QQQ": synthetic_daily_bars(
            "QQQ", CALENDAR, date(2026, 9, 1), date(2026, 12, 31), source="other"
        )}, clock=lambda: FETCHED,
    )  # fmt: skip
    with pytest.raises(IngestError, match="其他供应商"):
        await ingest_bars(other, lake, "QQQ", START, END)
    assert _files(lake) == before


@pytest.mark.asyncio
async def test_invalid_provider_name_is_refused(tmp_path: Path) -> None:
    class _BadName(FakeDataProvider):
        @property
        def name(self) -> str:
            return "Bad-Name"

    with pytest.raises(IngestError, match="供应商名"):
        await ingest_bars(
            _BadName(bars={"SPY": _bars("SPY")}), DataLake(tmp_path), "SPY", START, END
        )


@pytest.mark.asyncio
async def test_concurrent_writer_between_merge_and_write_is_detected(tmp_path: Path) -> None:
    """合并后、写入前第 2 天被别的写入者改动：整批拒绝，第 1 天也不写（复核先于全部写入）。"""
    import pyarrow as pa  # noqa: PLC0415
    from server.data.ingest import RAW_BARS_SCHEMA  # noqa: PLC0415
    from server.data.lake import PartitionRef  # noqa: PLC0415

    second = date(2026, 10, 6)

    class _Racing(DataLake):
        raw_queries = 0

        def latest(self, layer: str, dataset: str, day: date) -> PartitionRef | None:
            if (layer, day) == ("raw", second):
                self.raw_queries += 1
                if self.raw_queries == 2:  # 第一次是合并，第二次是写入前复核：此时插入别人的写入
                    other = pa.table(
                        {"symbol": ["QQQ"], "start": [datetime(2026, 10, 6, 13, 30, tzinfo=UTC)],
                         "raw": ["{}"]},
                        schema=RAW_BARS_SCHEMA,
                    )  # fmt: skip
                    self.write_partition(
                        "raw", "fake_bars_1d", day, other, provider="fake", fetched_at=FETCHED
                    )
            return super().latest(layer, dataset, day)

    lake = _Racing(tmp_path)
    with pytest.raises(IngestError, match="别的写入者"):
        await ingest_bars(_provider(), lake, "SPY", START, END)
    assert lake.raw_queries == 2  # 确实在复核时插入了改动
    assert lake.partitions("normalized", "bars_1d") == {}
    assert set(lake.partitions("raw", "fake_bars_1d")) == {second}  # 只有别人写的那一天
