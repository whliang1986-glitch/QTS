"""公司行动入湖与复权（D-04，V4.1 5.3、8.1、R-07 移交）。

入湖（与 K 线同样的落盘顺序：原始层 → 标准化层，版本化）：
- `raw/<供应商>_corp_actions` 存供应商原始记录，`normalized/corp_actions` 存标准化记录，
  都按除权日分区；
- 先校验整批：标的必须是请求的、除权日在区间内、来源是该供应商、事件编号唯一；
  任何一条不合格整批拒绝；
- 每个除权日分区在最新版本上只替换请求标的的行；首期单一来源（别家写过的分区拒绝合并）。

复权（向后复权：最新价格不变，除权日之前的历史价格按因子调整）：
- 拆股 / 合股：除权日之前的价格 × denominator / numerator，成交量 × numerator / denominator；
- 现金分红：除权日之前的价格 × (1 − 每股分红 / 除权日前一交易日的未复权收盘价)；
- 因子累乘；价格按 6 位、成交量按 4 位小数四舍六入五成双（与数据湖 schema 一致），全程 Decimal；
- 复权版本号由“方法 + 所用公司行动的完整内容”确定性生成：同样的输入总得到同一个版本号，
  公司行动被修订则版本号随之改变，回测绑定版本号即可复现。
- 失败即关闭：输入不是未复权序列、混入别的标的、除权日前一交易日缺 K 线、分红不小于前收，一律报错。

持仓口径（与对账 R-07 一致）：拆股后的数量向下取整到整股，成本总额不变；现金分红 = 持仓 × 每股分红。
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from server.data.calendar import CalendarRangeError, TradingCalendar
from server.data.lake import PRICE, DataLake, LakeError, PartitionRef
from server.data.provider import PROVIDER_NAME, DataProvider, check_range
from server.data.quality import UNADJUSTED
from server.domain.corporate_actions import CorporateActionKind, CorporateActionRecord
from server.domain.market import MarketBar
from server.domain.values import to_utc

NORMALIZED_DATASET = "corp_actions"
PRICE_STEP = Decimal("0.000001")  # 与 decimal(18,6) 一致
VOLUME_STEP = Decimal("0.0001")  # 与 decimal(24,4) 一致

_FIELDS: list[pa.Field[Any]] = [
    pa.field("event_id", pa.string()),
    pa.field("symbol", pa.string()),
    pa.field("kind", pa.string()),
    pa.field("ex_date", pa.date32()),
    pa.field("source", pa.string()),
    pa.field("numerator", pa.int64()),
    pa.field("denominator", pa.int64()),
    pa.field("amount", PRICE),
    pa.field("currency", pa.string()),
]
ACTIONS_SCHEMA = pa.schema(_FIELDS)
_RAW_FIELDS: list[pa.Field[Any]] = [
    pa.field("event_id", pa.string()),
    pa.field("symbol", pa.string()),
    pa.field("raw", pa.string()),
]
RAW_ACTIONS_SCHEMA = pa.schema(_RAW_FIELDS)


class CorporateActionError(Exception):
    """公司行动数据违反契约，或无法据此复权：拒绝，不猜测。"""


class AdjustmentMethod(StrEnum):
    SPLITS = "splits"  # 只按拆股 / 合股复权
    SPLITS_AND_DIVIDENDS = "splits_and_dividends"  # 再加现金分红（总收益口径）


# ---------- 表与记录 ----------


def actions_table(records: Sequence[CorporateActionRecord]) -> pa.Table:
    try:
        return pa.table(
            {
                "event_id": [r.event_id for r in records],
                "symbol": [r.symbol for r in records],
                "kind": [r.kind.value for r in records],
                "ex_date": [r.ex_date for r in records],
                "source": [r.source for r in records],
                "numerator": [r.numerator for r in records],
                "denominator": [r.denominator for r in records],
                "amount": [r.amount for r in records],
                "currency": [r.currency for r in records],
            },
            schema=ACTIONS_SCHEMA,
        )
    except (pa.ArrowException, ValueError, TypeError, OverflowError) as exc:  # 精度或范围超出：拒绝
        raise CorporateActionError(f"公司行动无法按标准 schema 存储：{exc}") from exc


def records_from_table(table: pa.Table) -> list[CorporateActionRecord]:
    if not table.schema.equals(ACTIONS_SCHEMA):
        raise CorporateActionError("公司行动分区的 schema 不是标准 schema")
    try:
        return [CorporateActionRecord.model_validate(row) for row in table.to_pylist()]
    except ValueError as exc:  # 含 pydantic ValidationError：存储的数据违反领域约束
        raise CorporateActionError(f"读回的公司行动不合法：{exc}") from exc


def _raw(record: CorporateActionRecord) -> str:
    return json.dumps(record.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)


# ---------- 入湖 ----------


def _validate(
    records: Sequence[CorporateActionRecord],
    *,
    provider: str,
    symbols: frozenset[str],
    start: date,
    end: date,
) -> dict[date, list[CorporateActionRecord]]:
    by_day: dict[date, list[CorporateActionRecord]] = defaultdict(list)
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, CorporateActionRecord):
            raise CorporateActionError("返回的公司行动类型不对")
        if record.symbol not in symbols:
            raise CorporateActionError(f"返回了未请求的标的 {record.symbol}")
        if record.source != provider:
            raise CorporateActionError(f"公司行动来源是 {record.source}，不是 {provider}")
        if not start <= record.ex_date <= end:
            raise CorporateActionError(f"{record.event_id} 的除权日 {record.ex_date} 超出请求区间")
        if record.event_id in seen:
            raise CorporateActionError(f"事件编号重复：{record.event_id}")
        seen.add(record.event_id)
        by_day[record.ex_date].append(record)
    return by_day


def _merge(
    lake: DataLake,
    existing: PartitionRef | None,
    new: pa.Table,
    symbols: frozenset[str],
    *,
    source: str | None,
) -> pa.Table:
    if existing is not None:
        old = lake.read_partition(existing)
        if not old.schema.equals(new.schema):
            raise CorporateActionError(f"{existing.key} 的 schema 与本次写入不一致")
        if source is not None:
            others = {str(s) for s in old.column("source").to_pylist()} - {source}
            if others:
                raise CorporateActionError(
                    f"{existing.key} 含其他供应商 {sorted(others)} 的行，首期不合并多来源"
                )
        try:
            kept = old.filter(pc.invert(pc.is_in(old.column("symbol"), pa.array(sorted(symbols)))))
            new = pa.concat_tables([kept.cast(new.schema), new])
        except pa.ArrowException as exc:
            raise CorporateActionError(f"{existing.key} 无法与本次写入合并：{exc}") from exc
    return new.sort_by([("symbol", "ascending"), ("event_id", "ascending")])


async def ingest_corporate_actions(  # noqa: PLR0913 - 请求参数全部显式给出
    provider: DataProvider,
    lake: DataLake,
    symbols: Collection[str],
    start: date,
    end: date,
    *,
    fetched_at: datetime,
) -> tuple[PartitionRef, ...]:
    """拉取并入湖 [start, end] 内这些标的的公司行动，返回写入（或幂等命中）的标准化分区。

    - 只处理本批有记录的除权日：这样的日期里，请求标的的旧行被本批整体替换
      （包括本批没有返回的事件）；
      本批没有记录的日期，旧事件原样保留。供应商撤销事件的核对由 D-02 负责；
      除权日被修订后同一事件会出现在两个分区，读取与复权时按事件编号唯一性拒绝；
    - 原始层存的是记录的 JSON：DataProvider 接口不提供公司行动的供应商原文；
    - 校验与合并失败时一个分区都不写；写入本身失败时已写的日期分区各自完整，重跑幂等补齐。
    """
    check_range(start, end)
    wanted = frozenset(symbols)
    if not wanted:
        raise CorporateActionError("至少请求一个标的")
    if not PROVIDER_NAME.fullmatch(provider.name):
        raise CorporateActionError(f"供应商名不合法：{provider.name!r}")
    try:
        fetched = to_utc(fetched_at)
    except ValueError as exc:
        raise CorporateActionError(f"拉取时间不合法：{exc}") from exc
    records = await provider.fetch_corporate_actions(sorted(wanted), start, end)
    by_day = _validate(records, provider=provider.name, symbols=wanted, start=start, end=end)
    raw_name = f"{provider.name}_corp_actions"
    license_url = provider.capabilities.license_url
    staged: list[tuple[date, pa.Table, pa.Table, tuple[str | None, str | None]]] = []
    for day in sorted(by_day):  # 阶段一：全部在内存中合并
        items = sorted(by_day[day], key=lambda r: (r.symbol, r.event_id))
        raw = pa.table(
            {
                "event_id": [r.event_id for r in items],
                "symbol": [r.symbol for r in items],
                "raw": [_raw(r) for r in items],
            },
            schema=RAW_ACTIONS_SCHEMA,
        )
        try:
            raw_base = lake.latest("raw", raw_name, day)
            base = lake.latest("normalized", NORMALIZED_DATASET, day)
            raw = _merge(lake, raw_base, raw, wanted, source=None)
            normalized = _merge(lake, base, actions_table(items), wanted, source=provider.name)
        except LakeError as exc:
            raise CorporateActionError(f"{day} 无法与湖内已有分区合并：{exc}") from exc
        staged.append((day, raw, normalized, (_key(raw_base), _key(base))))
    try:
        # 写入前复核全部日期：单写者约定被打破时，基于旧版本合并会丢掉别人刚写入的事件
        for day, _, _, keys in staged:
            now = (
                _key(lake.latest("raw", raw_name, day)),
                _key(lake.latest("normalized", NORMALIZED_DATASET, day)),
            )
            if now != keys:
                raise CorporateActionError(f"{day} 的分区在合并后被别的写入者改动：{keys} → {now}")
        refs: list[PartitionRef] = []
        for day, raw, normalized, _ in staged:  # 阶段二：只写入
            raw_ref = lake.write_partition(
                "raw", raw_name, day, raw, provider=provider.name, fetched_at=fetched,
                license_url=license_url,
            )  # fmt: skip
            ref = lake.write_partition(
                "normalized", NORMALIZED_DATASET, day, normalized, provider=provider.name,
                fetched_at=fetched, license_url=license_url, lineage=[raw_ref.key],
            )  # fmt: skip
            refs.append(ref)
    except LakeError as exc:  # 写入本身失败：已写的日期分区各自完整，重跑幂等补齐
        raise CorporateActionError(f"公司行动写入失败：{exc}") from exc
    return tuple(refs)


def _key(ref: PartitionRef | None) -> str | None:
    return None if ref is None else ref.key


def require_unique_events(actions: Iterable[CorporateActionRecord]) -> None:
    """事件编号必须全局唯一：供应商改了除权日后，旧分区里的同一事件仍在，重复即拒绝（不得重复复权）。"""
    seen: dict[str, CorporateActionRecord] = {}
    for action in actions:
        if action.event_id in seen:
            other = seen[action.event_id]
            raise CorporateActionError(
                f"事件 {action.event_id} 出现两次（{other.ex_date} 与 {action.ex_date}），"
                "可能是除权日被修订：需先核对，不得据此复权"
            )
        seen[action.event_id] = action


def read_corporate_actions(
    lake: DataLake,
    symbols: Collection[str],
    start: date,
    end: date,
    *,
    versions: Mapping[date, int] | None = None,
) -> list[CorporateActionRecord]:
    """读 [start, end] 内这些标的的公司行动。给定 versions 时只按绑定的版本读取，缺版本即报错。"""
    wanted = set(symbols)
    try:
        partitions = lake.partitions("normalized", NORMALIZED_DATASET)
    except LakeError as exc:
        raise CorporateActionError(f"读取公司行动目录失败：{exc}") from exc
    chosen: list[PartitionRef] = []
    if versions is not None:
        for day in sorted(d for d in versions if start <= d <= end):
            match = [r for r in partitions.get(day, []) if r.manifest.version == versions[day]]
            if not match:
                raise CorporateActionError(f"{day} 没有绑定的公司行动版本 v={versions[day]:03d}")
            chosen.append(match[0])
    else:
        chosen = [refs[-1] for day, refs in partitions.items() if start <= day <= end and refs]
    out: list[CorporateActionRecord] = []
    try:
        for ref in chosen:
            day = date.fromisoformat(ref.manifest.partition.removeprefix("date="))
            records = records_from_table(lake.read_partition(ref))
            if any(r.ex_date != day for r in records):
                raise CorporateActionError(f"{ref.key} 内有除权日不等于分区日期的记录")
            out += [r for r in records if r.symbol in wanted]
    except LakeError as exc:
        raise CorporateActionError(f"读取公司行动失败：{exc}") from exc
    require_unique_events(out)
    return sorted(out, key=lambda r: (r.ex_date, r.symbol, r.event_id))


# ---------- 复权 ----------


def _canonical_amount(value: Decimal | None) -> str | None:
    if value is None:
        return None
    quantized = value.quantize(PRICE_STEP, rounding=ROUND_HALF_EVEN)
    if quantized != value:
        raise CorporateActionError(f"金额 {value} 超出存储精度（6 位小数）")
    return str(quantized)


def _canonical_row(action: CorporateActionRecord, inputs: Mapping[str, Decimal]) -> str:
    row = {
        "event_id": action.event_id,
        "symbol": action.symbol,
        "kind": action.kind.value,
        "ex_date": action.ex_date.isoformat(),
        "source": action.source,
        "numerator": action.numerator,
        "denominator": action.denominator,
        "amount": _canonical_amount(action.amount),
        "currency": action.currency,
        "prev_close": _canonical_amount(inputs.get(action.event_id)),
    }
    return json.dumps(row, sort_keys=True)


def adjustment_version(
    method: AdjustmentMethod,
    actions: Iterable[CorporateActionRecord],
    inputs: Mapping[str, Decimal] | None = None,
) -> str:
    """复权因子全部输入的确定性摘要：方法、所用公司行动、分红用到的前收（inputs：事件编号 → 前收）。

    金额按存储精度规范化（1.5 与 1.500000 相同）；按规范化后的整条记录排序，与输入顺序无关。
    """
    rows = [_canonical_row(a, inputs or {}) for a in actions]
    payload = json.dumps({"method": method.value, "actions": sorted(rows)}, sort_keys=True)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return f"adj-{method.value}-{digest[:16]}"


def _relevant(
    symbol: str, actions: Iterable[CorporateActionRecord], method: AdjustmentMethod
) -> list[CorporateActionRecord]:
    kinds = {CorporateActionKind.SPLIT}
    if method is AdjustmentMethod.SPLITS_AND_DIVIDENDS:
        kinds.add(CorporateActionKind.CASH_DIVIDEND)
    return sorted(
        (a for a in actions if a.symbol == symbol and a.kind in kinds),
        key=lambda a: (a.ex_date, a.event_id),
    )


def adjust_bars(
    bars: Sequence[MarketBar],
    actions: Iterable[CorporateActionRecord],
    *,
    method: AdjustmentMethod,
    calendar: TradingCalendar,
) -> list[MarketBar]:
    """把一个标的的未复权日线向后复权；返回的日线带上确定性的复权版本号。

    只用除权日落在 (首日, 末日] 内的公司行动；首日当天或之前的事件不影响这段序列。
    """
    series = sorted(bars, key=lambda b: b.start)
    if not series:
        return []
    symbol = series[0].symbol
    if any(b.symbol != symbol for b in series):
        raise CorporateActionError("复权输入混入了别的标的")
    if any(b.adjustment_version != UNADJUSTED for b in series):
        raise CorporateActionError("只能对未复权序列复权（不得重复复权）")
    if len({b.start for b in series}) != len(series):
        raise CorporateActionError("复权输入有重复的日线")
    first, last = series[0].start.date(), series[-1].start.date()
    candidates = list(actions)
    require_unique_events(candidates)
    used = [a for a in _relevant(symbol, candidates, method) if first < a.ex_date <= last]
    split_days = {a.ex_date for a in used if a.kind is CorporateActionKind.SPLIT}
    clash = sorted(
        a.event_id for a in used
        if a.kind is CorporateActionKind.CASH_DIVIDEND and a.ex_date in split_days
    )  # fmt: skip
    if clash:  # 分红按拆股前还是拆股后的股本计，口径不明：首期拒绝
        raise CorporateActionError(f"同一除权日既拆股又分红，口径不明：{clash}")
    dividend_days = [a.ex_date for a in used if a.kind is CorporateActionKind.CASH_DIVIDEND]
    if len(dividend_days) != len(set(dividend_days)):  # 合计是否小于前收无法逐笔核对：首期拒绝
        raise CorporateActionError("同一除权日有多笔现金分红，首期不支持")
    prev_closes: dict[str, Decimal] = {}
    factors: list[tuple[date, Decimal, Decimal]] = []  # (除权日, 价格因子, 成交量因子)
    for action in used:
        if action.kind is CorporateActionKind.SPLIT:
            assert action.numerator is not None  # noqa: S101 - 模型校验保证拆股有比例
            assert action.denominator is not None  # noqa: S101
            factors.append((
                action.ex_date,
                Decimal(action.denominator) / Decimal(action.numerator),
                Decimal(action.numerator) / Decimal(action.denominator),
            ))  # fmt: skip
            continue
        prev_close = _previous_close(action, series, calendar)
        prev_closes[action.event_id] = prev_close
        assert action.amount is not None  # noqa: S101 - 模型校验保证分红有金额
        factors.append((action.ex_date, 1 - action.amount / prev_close, Decimal(1)))
    version = adjustment_version(method, used, prev_closes)
    out: list[MarketBar] = []
    for bar in series:
        price_factor, volume_factor = Decimal(1), Decimal(1)
        for ex_date, pf, vf in factors:
            if bar.start.date() < ex_date:
                price_factor *= pf
                volume_factor *= vf
        out.append(_scaled(bar, price_factor, volume_factor, version))
    return out


def _previous_close(
    action: CorporateActionRecord, series: Sequence[MarketBar], calendar: TradingCalendar
) -> Decimal:
    """分红用的前收：除权日紧挨着的前一交易日的未复权收盘价；缺这一天或分红不小于前收即拒绝。"""
    assert action.amount is not None  # noqa: S101 - 模型校验保证分红有金额
    before = [b for b in series if b.start.date() < action.ex_date]
    try:
        previous = _previous_session(calendar, action.ex_date)
    except CalendarRangeError as exc:
        raise CorporateActionError(f"{action.event_id} 超出交易日历范围：{exc}") from exc
    if not before or before[-1].start.date() != previous:
        raise CorporateActionError(f"{action.event_id} 的除权日前一交易日 {previous} 缺日线")
    prev_close = before[-1].close
    if action.amount >= prev_close:
        raise CorporateActionError(
            f"{action.event_id} 的每股分红 {action.amount} 不小于前收 {prev_close}"
        )
    return prev_close


def _previous_session(calendar: TradingCalendar, day: date) -> date:
    probe = day - timedelta(days=1)
    for _ in range(14):
        if calendar.is_trading_day(probe):
            return probe
        probe -= timedelta(days=1)
    raise CorporateActionError(f"{day} 之前 14 天内没有交易日")


def _price(value: Decimal, factor: Decimal) -> Decimal:
    scaled = (value * factor).quantize(PRICE_STEP, rounding=ROUND_HALF_EVEN)
    if scaled <= 0:
        raise CorporateActionError(f"复权后价格不为正：{value} × {factor}")
    return scaled


def _scaled(bar: MarketBar, pf: Decimal, vf: Decimal, version: str) -> MarketBar:
    data = bar.model_dump()
    try:
        for name in ("open", "high", "low", "close"):
            data[name] = _price(data[name], pf)
        data["volume"] = (bar.volume * vf).quantize(VOLUME_STEP, rounding=ROUND_HALF_EVEN)
        data["adjustment_version"] = version
        return MarketBar.model_validate(data)
    # ValueError 含 pydantic ValidationError（复权后超出允许范围）；
    # ArithmeticError 含 Decimal 精度溢出
    except (ValueError, ArithmeticError) as exc:
        raise CorporateActionError(f"{bar.start.date()} 复权后的日线不合法：{exc}") from exc


# ---------- 持仓口径（与对账一致） ----------


COST_STEP = Decimal("0.0000000001")  # 领域模型允许的最多 10 位小数


def _whole(quantity: int | Decimal) -> int:
    if isinstance(quantity, bool) or not isinstance(quantity, int | Decimal):
        raise CorporateActionError("持仓数量必须是整数或整数值的 Decimal")
    if isinstance(quantity, Decimal) and (
        not quantity.is_finite() or quantity != quantity.to_integral_value()
    ):
        raise CorporateActionError(f"持仓数量必须是整股：{quantity}")
    if quantity < 0:
        raise CorporateActionError("持仓数量不能为负")
    return int(quantity)


def shares_after_split(quantity: int | Decimal, action: CorporateActionRecord) -> int:
    """拆股后的整股数量（与对账一致：q × 分子 // 分母，向下取整；零股按券商现金结算）。"""
    if action.kind is not CorporateActionKind.SPLIT:
        raise CorporateActionError(f"{action.event_id} 不是拆股")
    assert action.numerator is not None  # noqa: S101
    assert action.denominator is not None  # noqa: S101
    return _whole(quantity) * action.numerator // action.denominator


def cost_after_split(cost_per_share: Decimal, action: CorporateActionRecord) -> Decimal:
    """拆股后的每股成本（成本总额不变；保留 10 位小数，四舍六入五成双）。"""
    if action.kind is not CorporateActionKind.SPLIT:
        raise CorporateActionError(f"{action.event_id} 不是拆股")
    assert action.numerator is not None  # noqa: S101
    assert action.denominator is not None  # noqa: S101
    if not isinstance(cost_per_share, Decimal) or not cost_per_share.is_finite():
        raise CorporateActionError("每股成本必须是有限的 Decimal")
    if cost_per_share < 0:
        raise CorporateActionError("每股成本不能为负")
    cost = cost_per_share * action.denominator / action.numerator
    return cost.quantize(COST_STEP, rounding=ROUND_HALF_EVEN)


def dividend_cash(quantity: int | Decimal, action: CorporateActionRecord) -> Decimal:
    """持有至除权日前一交易日收盘的股数应得的现金分红。"""
    if action.kind is not CorporateActionKind.CASH_DIVIDEND:
        raise CorporateActionError(f"{action.event_id} 不是现金分红")
    assert action.amount is not None  # noqa: S101
    return action.amount * _whole(quantity)


def effective_at(action: CorporateActionRecord, calendar: TradingCalendar) -> datetime:
    """事件在除权日开盘时生效（对账 R-07 的 CorporateAction.effective_at 用它）。"""
    try:
        session = calendar.session(action.ex_date)
    except CalendarRangeError as exc:
        raise CorporateActionError(f"{action.event_id} 超出交易日历范围：{exc}") from exc
    if session is None:
        raise CorporateActionError(f"{action.event_id} 的除权日 {action.ex_date} 不是交易日")
    return session.open
