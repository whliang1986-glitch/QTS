"""K 线入湖（D-01a，V4.1 5.3 落盘顺序：原始层 → 标准化层；质量报告在 D-05 接入）。

- 先校验整批返回，再写任何分区：违反接口契约（标的、来源、周期、区间、重复开始时间）整批拒绝；
  所有表也先构造好，精度超出 schema 同样在写入前拒绝。
- 按 K 线开始时间的 UTC 日期分组。每个日期分区在最新版本基础上只替换本标的的行，其他标的原样保留；
  内容相同则幂等（不产生新版本），不同则写新版本，旧版本保留（回测按版本绑定）。
- 标准化分区的 lineage 指向同日原始分区的版本键；manifest 记录供应商、拉取时间与授权条款链接。
- 合并是“读最新版本 → 写新版本”：同一数据集只能有一个写入者（workerd 的 DataJob，D-02）。
- 合并与合并后的校验（与湖内已有分区的 schema 冲突、已有分区损坏、合并后不合法）也全部在写入前完成；
  写入前还会复核全部日期的最新版本仍是合并时那一版（防并发写入者）。只有磁盘等写入本身失败、
  或复核与提交之间极短窗口内的并发写入，才会出现“部分日期已写”；已写的日期分区各自完整，重跑幂等补齐。
- 首期标准化层只允许单一来源：同日分区里有别的供应商写入的行时拒绝（否则 manifest 的来源、
  授权与 lineage 会把别家的行算在本供应商名下）。多供应商合并在 D-04 / D-05 设计。
- 只合并本批有 K 线的日期：供应商修订时删掉的 K 线不会被删除（旧行保留），
  缺口由 D-05 的连续性检查发现。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from server.data.lake import UTC_US, DataLake, LakeError, PartitionRef, bars_table, check_bars
from server.data.provider import (
    PROVIDER_NAME,
    BarBatch,
    DataProvider,
    FetchedBar,
    Interval,
    UnsupportedCapabilityError,
    check_range,
)
from server.domain.market import MarketBar
from server.domain.values import to_utc

_RAW_FIELDS: list[pa.Field[Any]] = [
    pa.field("symbol", pa.string()),
    pa.field("start", UTC_US),
    pa.field("raw", pa.string()),
]
RAW_BARS_SCHEMA = pa.schema(_RAW_FIELDS)


class IngestError(Exception):
    """供应商返回违反接口契约，或数据湖中已有分区与之不兼容：整批拒绝。"""


@dataclass(frozen=True)
class IngestResult:
    days: tuple[date, ...]
    raw: tuple[PartitionRef, ...]
    normalized: tuple[PartitionRef, ...]
    bars: int


def raw_dataset(provider: str, interval: Interval) -> str:
    return f"{provider}_bars_{interval.value}"


def bars_dataset(interval: Interval) -> str:
    return f"bars_{interval.value}"


def _validate(  # noqa: PLR0913 - 请求参数逐项与返回核对
    batch: BarBatch, *, provider: str, symbol: str, start: date, end: date, interval: Interval
) -> tuple[datetime, dict[date, list[FetchedBar]]]:
    if (batch.symbol, batch.interval, batch.start, batch.end) != (symbol, interval, start, end):
        raise IngestError(
            f"返回的批次与请求不符：{(batch.symbol, batch.interval, batch.start, batch.end)}"
        )
    try:
        fetched_at = to_utc(batch.fetched_at)
    except ValueError as exc:
        raise IngestError(f"拉取时间不合法：{exc}") from exc
    by_day: dict[date, list[FetchedBar]] = defaultdict(list)
    seen: set[datetime] = set()
    for item in batch.bars:
        if not isinstance(item, FetchedBar) or not isinstance(item.bar, MarketBar):
            raise IngestError("返回的 K 线类型不对")
        bar = item.bar
        if bar.symbol != symbol:
            raise IngestError(f"请求 {symbol}，返回了 {bar.symbol} 的 K 线")
        if bar.source != provider:
            raise IngestError(f"K 线来源是 {bar.source}，不是 {provider}")
        day = bar.start.astimezone(UTC).date()
        if not start <= day <= end:
            raise IngestError(f"{symbol} 的 K 线 {bar.start.isoformat()} 超出请求区间")
        if bar.start in seen:
            raise IngestError(f"{symbol} 在 {bar.start.isoformat()} 有重复 K 线")
        if not isinstance(item.raw, str) or not item.raw.strip():
            raise IngestError(f"{symbol} {bar.start.isoformat()} 缺少供应商原始记录")
        seen.add(bar.start)
        by_day[day].append(item)
    return fetched_at, by_day


def _key(ref: PartitionRef | None) -> str | None:
    return None if ref is None else ref.key


def _merge(
    lake: DataLake, existing: PartitionRef | None, new: pa.Table, symbol: str, *, source: str | None
) -> pa.Table:
    """最新版本中去掉本标的的行，再并入新的行，按 (symbol, start) 排序。

    source 不为 None 时（标准化层），保留下来的行必须都来自同一供应商。
    """
    if existing is not None:
        old = lake.read_partition(existing)
        # schema.equals 不比较 list 子字段名；读回的 Parquet 会把 list 的 item 改名为 element，
        # 下面按本次 schema 还原，否则同内容会得到不同的内容哈希，重跑不再幂等
        if not old.schema.equals(new.schema):
            raise IngestError(f"{existing.key} 的 schema 与本次写入不一致")
        if source is not None:
            others = {str(s) for s in old.column("source").to_pylist()} - {source}
            if others:
                raise IngestError(
                    f"{existing.key} 含其他供应商 {sorted(others)} 的行，首期不合并多来源"
                )
        try:
            kept = old.filter(pc.not_equal(old.column("symbol"), pa.scalar(symbol)))
            new = pa.concat_tables([kept.cast(new.schema), new])
        except pa.ArrowException as exc:
            raise IngestError(f"{existing.key} 无法与本次写入合并：{exc}") from exc
    return new.sort_by([("symbol", "ascending"), ("start", "ascending")])


async def ingest_bars(  # noqa: PLR0913 - 周期是可选的关键字参数
    provider: DataProvider,
    lake: DataLake,
    symbol: str,
    start: date,
    end: date,
    *,
    interval: Interval = Interval.DAY,
) -> IngestResult:
    """拉取 [start, end] 内某标的的 K 线并入湖。供应商异常原样上抛，此时不写任何分区。"""
    check_range(start, end)
    if not PROVIDER_NAME.fullmatch(provider.name):
        raise IngestError(f"供应商名不合法：{provider.name!r}")
    capabilities = provider.capabilities
    if interval not in capabilities.intervals:
        raise UnsupportedCapabilityError(f"{provider.name} 不提供 {interval} K 线")
    batch = await provider.fetch_bars(symbol, start, end, interval)
    fetched_at, by_day = _validate(
        batch, provider=provider.name, symbol=symbol, start=start, end=end, interval=interval
    )
    raw_name, bars_name = raw_dataset(provider.name, interval), bars_dataset(interval)
    # 阶段一（只在内存中）：建表、与湖内最新版本合并、校验合并结果
    tables: list[tuple[date, pa.Table, pa.Table, tuple[str | None, str | None]]] = []
    for day in sorted(by_day):
        items = sorted(by_day[day], key=lambda i: i.bar.start)
        try:
            raw = pa.table(
                {
                    "symbol": [i.bar.symbol for i in items],
                    "start": [i.bar.start for i in items],
                    "raw": [i.raw for i in items],
                },
                schema=RAW_BARS_SCHEMA,
            )
            normalized = bars_table([i.bar for i in items])
        except (pa.ArrowException, LakeError) as exc:
            raise IngestError(f"{symbol} {day} 的 K 线无法按 schema 存储：{exc}") from exc
        try:
            raw_base = lake.latest("raw", raw_name, day)
            bars_base = lake.latest("normalized", bars_name, day)
            raw = _merge(lake, raw_base, raw, symbol, source=None)
            merged = _merge(lake, bars_base, normalized, symbol, source=provider.name)
            check_bars(merged, day)
        except LakeError as exc:
            raise IngestError(f"{day} 无法与湖内已有分区合并：{exc}") from exc
        tables.append((day, raw, merged, (_key(raw_base), _key(bars_base))))
    # 写入前先复核全部日期：单写者约定被打破时，基于旧版本合并会丢掉别人的改动
    for day, _, _, base in tables:
        now = (
            _key(lake.latest("raw", raw_name, day)),
            _key(lake.latest("normalized", bars_name, day)),
        )
        if now != base:
            raise IngestError(f"{day} 的分区在合并后被别的写入者改动：{base} → {now}")
    # 阶段二：只写入
    raw_refs: list[PartitionRef] = []
    bar_refs: list[PartitionRef] = []
    for day, raw, merged, _ in tables:
        raw_ref = lake.write_partition(
            "raw", raw_name, day, raw, provider=provider.name, fetched_at=fetched_at,
            license_url=capabilities.license_url,
        )  # fmt: skip
        bar_ref = lake.write_partition(
            "normalized", bars_name, day, merged, provider=provider.name, fetched_at=fetched_at,
            license_url=capabilities.license_url, lineage=[raw_ref.key],
        )  # fmt: skip
        raw_refs.append(raw_ref)
        bar_refs.append(bar_ref)
    return IngestResult(
        days=tuple(day for day, *_ in tables),
        raw=tuple(raw_refs),
        normalized=tuple(bar_refs),
        bars=len(batch.bars),
    )
