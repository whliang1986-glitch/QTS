"""公司行动入湖、复权与持仓口径（D-04）。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from server.data.calendar import TradingCalendar
from server.data.corporate_actions import (
    AdjustmentMethod,
    CorporateActionError,
    adjust_bars,
    adjustment_version,
    cost_after_split,
    dividend_cash,
    effective_at,
    ingest_corporate_actions,
    read_corporate_actions,
    shares_after_split,
)
from server.data.fake_provider import FakeDataProvider
from server.data.lake import DataLake
from server.data.quality import UNADJUSTED, check_adjustment
from server.domain.corporate_actions import CorporateActionKind, CorporateActionRecord
from server.domain.market import MarketBar

CALENDAR = TradingCalendar.nyse(start=date(2026, 9, 1), end=date(2026, 12, 31))
WEEK = [date(2026, 10, d) for d in (5, 6, 7, 8, 9)]
FETCHED = datetime(2026, 10, 10, tzinfo=UTC)
S = CorporateActionKind.SPLIT
D = CorporateActionKind.CASH_DIVIDEND


def split(
    num: int, den: int, day: date = WEEK[2], *, symbol: str = "SPY", source: str = "fake"
) -> CorporateActionRecord:
    return CorporateActionRecord(
        event_id=f"{symbol}-split-{num}-{den}-{day}", symbol=symbol, kind=S, ex_date=day,
        source=source, numerator=num, denominator=den,
    )  # fmt: skip


def dividend(
    amount: str, day: date = WEEK[3], *, symbol: str = "SPY", source: str = "fake"
) -> CorporateActionRecord:
    return CorporateActionRecord(
        event_id=f"{symbol}-div-{day}-{amount}", symbol=symbol, kind=D, ex_date=day,
        source=source, amount=Decimal(amount), currency="USD",
    )  # fmt: skip


def bar(day: date, close: str, *, symbol: str = "SPY", volume: str = "1000") -> MarketBar:
    session = CALENDAR.session(day)
    assert session is not None
    c = Decimal(close)
    return MarketBar(
        symbol=symbol, start=session.open, end=session.close, open=c, high=c + 1, low=c - 1,
        close=c, volume=Decimal(volume), source="fake", adjustment_version=UNADJUSTED,
    )  # fmt: skip


def _files(lake: DataLake) -> list[str]:
    return sorted(str(p.relative_to(lake.root)) for p in lake.root.rglob("*") if p.is_file())


# ---------- 入湖 ----------


@pytest.mark.asyncio
async def test_ingest_writes_raw_then_normalized_per_ex_date(tmp_path: Path) -> None:
    lake = DataLake(tmp_path)
    actions = [split(4, 1), dividend("1.5"), dividend("0.4", symbol="QQQ")]
    refs = await ingest_corporate_actions(
        FakeDataProvider(actions=actions), lake, ["SPY", "QQQ"], WEEK[0], WEEK[-1],
        fetched_at=FETCHED,
    )  # fmt: skip
    assert [r.manifest.partition for r in refs] == ["date=2026-10-07", "date=2026-10-08"]
    for ref in refs:
        assert ref.manifest.dataset == "corp_actions"
        assert ref.manifest.lineage[0].startswith("raw/fake_corp_actions/")
        assert ref.manifest.license_url
    read = read_corporate_actions(lake, ["SPY", "QQQ"], WEEK[0], WEEK[-1])
    assert [(a.symbol, a.kind) for a in read] == [("SPY", S), ("QQQ", D), ("SPY", D)]
    assert read == sorted(actions, key=lambda a: (a.ex_date, a.symbol, a.event_id))


@pytest.mark.asyncio
async def test_ingest_is_idempotent_merges_symbols_and_versions_revisions(tmp_path: Path) -> None:
    lake = DataLake(tmp_path)
    first = FakeDataProvider(actions=[dividend("1.5")])
    await ingest_corporate_actions(first, lake, ["SPY"], WEEK[0], WEEK[-1], fetched_at=FETCHED)
    files = _files(lake)
    await ingest_corporate_actions(first, lake, ["SPY"], WEEK[0], WEEK[-1], fetched_at=FETCHED)
    assert _files(lake) == files  # 同内容幂等
    pinned = lake.snapshot("corp_actions", WEEK[0], WEEK[-1])
    other = FakeDataProvider(actions=[dividend("0.4", symbol="QQQ")])
    await ingest_corporate_actions(other, lake, ["QQQ"], WEEK[0], WEEK[-1], fetched_at=FETCHED)
    both = read_corporate_actions(lake, ["SPY", "QQQ"], WEEK[0], WEEK[-1])
    assert {a.symbol for a in both} == {"SPY", "QQQ"}  # 合并不丢别的标的
    revised = FakeDataProvider(actions=[dividend("1.55")])
    await ingest_corporate_actions(revised, lake, ["SPY"], WEEK[0], WEEK[-1], fetched_at=FETCHED)
    latest = read_corporate_actions(lake, ["SPY"], WEEK[0], WEEK[-1])
    old = read_corporate_actions(lake, ["SPY"], WEEK[0], WEEK[-1], versions=pinned)
    assert [a.amount for a in latest] == [Decimal("1.55")]
    assert [a.amount for a in old] == [Decimal("1.5")]  # 旧版本保留，可按绑定读回
    with pytest.raises(CorporateActionError, match="绑定的公司行动版本"):
        read_corporate_actions(lake, ["SPY"], WEEK[0], WEEK[-1], versions={WEEK[3]: 9})


class _Returns(FakeDataProvider):
    """原样返回给定的记录（不过滤），模拟违反契约的供应商。"""

    def __init__(self, records: list[CorporateActionRecord]) -> None:
        super().__init__()
        self.records = records

    async def fetch_corporate_actions(
        self, symbols: object, start: date, end: date
    ) -> list[CorporateActionRecord]:
        return self.records


@pytest.mark.parametrize(
    ("records", "match"),
    [
        ([dividend("1", symbol="QQQ")], "未请求的标的"),
        ([dividend("1", source="other")], "来源是 other"),
        ([dividend("1", date(2026, 10, 12))], "超出请求区间"),
        ([dividend("1"), dividend("1")], "事件编号重复"),
        ([dividend("1.1234567")], "schema"),  # 7 位小数，存储只容 6 位：拒绝而不截断
    ],
)
@pytest.mark.asyncio
async def test_contract_violations_write_nothing(
    tmp_path: Path, records: list[CorporateActionRecord], match: str
) -> None:
    lake = DataLake(tmp_path)
    with pytest.raises(CorporateActionError, match=match):
        await ingest_corporate_actions(
            _Returns(records), lake, ["SPY"], WEEK[0], WEEK[-1], fetched_at=FETCHED
        )
    assert _files(lake) == []


@pytest.mark.asyncio
async def test_ingest_argument_checks(tmp_path: Path) -> None:
    lake = DataLake(tmp_path)
    provider = FakeDataProvider(actions=[dividend("1")])
    with pytest.raises(CorporateActionError, match="至少请求一个标的"):
        await ingest_corporate_actions(provider, lake, [], WEEK[0], WEEK[-1], fetched_at=FETCHED)
    with pytest.raises(CorporateActionError, match="拉取时间"):
        await ingest_corporate_actions(
            provider, lake, ["SPY"], WEEK[0], WEEK[-1],
            fetched_at=datetime(2026, 10, 10),  # noqa: DTZ001 - 故意不带时区
        )  # fmt: skip
    with pytest.raises(ValueError, match="区间"):
        await ingest_corporate_actions(
            provider, lake, ["SPY"], WEEK[-1], WEEK[0], fetched_at=FETCHED
        )


@pytest.mark.asyncio
async def test_another_providers_partition_is_not_merged(tmp_path: Path) -> None:
    lake = DataLake(tmp_path)
    await ingest_corporate_actions(
        FakeDataProvider(actions=[dividend("1")]), lake, ["SPY"], WEEK[0], WEEK[-1],
        fetched_at=FETCHED,
    )  # fmt: skip
    before = _files(lake)
    other = FakeDataProvider("vendor_b", actions=[dividend("0.4", symbol="QQQ", source="vendor_b")])
    with pytest.raises(CorporateActionError, match="其他供应商"):
        await ingest_corporate_actions(other, lake, ["QQQ"], WEEK[0], WEEK[-1], fetched_at=FETCHED)
    assert _files(lake) == before


# ---------- 复权 ----------


def _relabeled(item: MarketBar) -> MarketBar:
    return MarketBar.model_validate({**item.model_dump(), "adjustment_version": "adj-x"})


def _unadjusted_split_week() -> list[MarketBar]:
    # 4 拆 1 于 10-07：之前约 400，之后约 100
    return [bar(WEEK[0], "400"), bar(WEEK[1], "404"), bar(WEEK[2], "101"), bar(WEEK[3], "102"),
            bar(WEEK[4], "103")]  # fmt: skip


def test_split_back_adjustment() -> None:
    adjusted = adjust_bars(
        _unadjusted_split_week(), [split(4, 1)], method=AdjustmentMethod.SPLITS, calendar=CALENDAR
    )
    assert [b.close for b in adjusted] == [Decimal(100), Decimal(101), Decimal(101),
                                           Decimal(102), Decimal(103)]  # fmt: skip
    assert [b.volume for b in adjusted] == [Decimal(4000), Decimal(4000), Decimal(1000),
                                            Decimal(1000), Decimal(1000)]  # fmt: skip
    version = adjusted[0].adjustment_version
    assert version.startswith("adj-splits-")
    assert {b.adjustment_version for b in adjusted} == {version}
    # 复权后的序列通过 D-05 的复权一致性检查（已复权口径：拆股前后平滑）
    assert check_adjustment("SPY", adjusted, [split(4, 1)], calendar=CALENDAR,
                            tolerance=Decimal("0.25")) == []  # fmt: skip


def test_reverse_split_adjustment() -> None:
    series = [bar(WEEK[0], "2"), bar(WEEK[1], "2.1"), bar(WEEK[2], "21"), bar(WEEK[3], "21")]
    adjusted = adjust_bars(
        series, [split(1, 10)], method=AdjustmentMethod.SPLITS, calendar=CALENDAR
    )
    assert [b.close for b in adjusted] == [Decimal(20), Decimal(21), Decimal(21), Decimal(21)]
    assert adjusted[0].volume == Decimal(100)


def test_dividend_adjustment_uses_the_previous_close() -> None:
    series = [bar(d, "100") for d in WEEK]
    div = dividend("2")  # 10-08 除权，前收 100：因子 0.98
    total = adjust_bars(
        series, [div], method=AdjustmentMethod.SPLITS_AND_DIVIDENDS, calendar=CALENDAR
    )
    assert [b.close for b in total] == [Decimal(98)] * 3 + [Decimal(100)] * 2
    assert [b.volume for b in total] == [Decimal(1000)] * 5  # 分红不改成交量
    price_only = adjust_bars(series, [div], method=AdjustmentMethod.SPLITS, calendar=CALENDAR)
    assert [b.close for b in price_only] == [Decimal(100)] * 5  # 只按拆股复权时忽略分红


def test_split_and_dividend_factors_multiply_and_round_to_storage_precision() -> None:
    series = [bar(WEEK[0], "300"), bar(WEEK[1], "300"), bar(WEEK[2], "100"), bar(WEEK[3], "99"),
              bar(WEEK[4], "99")]  # fmt: skip
    actions = [split(3, 1), dividend("1")]  # 10-07 拆股，10-08 分红（前收 100：因子 0.99）
    adjusted = adjust_bars(
        series, actions, method=AdjustmentMethod.SPLITS_AND_DIVIDENDS, calendar=CALENDAR
    )
    assert adjusted[0].close == Decimal("99.000000")  # 300 × 1/3 × 0.99
    assert adjusted[2].close == Decimal("99.000000")  # 100 × 0.99
    assert adjusted[3].close == Decimal(99)
    odd = adjust_bars([bar(WEEK[0], "100"), bar(WEEK[1], "100")], [split(3, 1, WEEK[1])],
                      method=AdjustmentMethod.SPLITS, calendar=CALENDAR)  # fmt: skip
    assert odd[0].close == Decimal("33.333333")  # 6 位小数，五成双
    assert odd[0].close.as_tuple().exponent == -6


def test_version_is_deterministic_and_changes_with_the_actions() -> None:
    a = adjustment_version(AdjustmentMethod.SPLITS, [split(4, 1), split(2, 1, WEEK[4])])
    b = adjustment_version(AdjustmentMethod.SPLITS, [split(2, 1, WEEK[4]), split(4, 1)])
    assert a == b  # 与输入顺序无关
    assert a != adjustment_version(AdjustmentMethod.SPLITS, [split(4, 1)])
    both = [split(4, 1), split(2, 1, WEEK[4])]
    assert a != adjustment_version(AdjustmentMethod.SPLITS_AND_DIVIDENDS, both)
    revised = adjust_bars(
        [bar(d, "100") for d in WEEK], [dividend("1.5")],
        method=AdjustmentMethod.SPLITS_AND_DIVIDENDS, calendar=CALENDAR,
    )  # fmt: skip
    original = adjust_bars(
        [bar(d, "100") for d in WEEK], [dividend("1.55")],
        method=AdjustmentMethod.SPLITS_AND_DIVIDENDS, calendar=CALENDAR,
    )  # fmt: skip
    assert revised[0].adjustment_version != original[0].adjustment_version


def test_actions_outside_the_window_are_ignored() -> None:
    series = [bar(d, "100") for d in WEEK[1:]]
    adjusted = adjust_bars(
        series, [split(4, 1, WEEK[1]), split(2, 1, date(2026, 10, 20))],
        method=AdjustmentMethod.SPLITS, calendar=CALENDAR,
    )  # fmt: skip
    assert [b.close for b in adjusted] == [Decimal(100)] * 4  # 首日当天与窗口之后的事件不影响
    assert adjusted[0].adjustment_version == adjustment_version(AdjustmentMethod.SPLITS, [])


@pytest.mark.parametrize(
    ("series", "actions", "match"),
    [
        (lambda: [bar(WEEK[0], "100"), bar(WEEK[1], "100", symbol="QQQ")], [], "别的标的"),
        (lambda: [_relabeled(bar(WEEK[0], "100"))], [], "未复权"),
        (lambda: [bar(WEEK[0], "100"), bar(WEEK[3], "100")], [dividend("1")], "前一交易日"),
        (lambda: [bar(d, "100") for d in WEEK], [dividend("100")], "不小于前收"),
    ],
)
def test_adjustment_refusals(
    series: object, actions: list[CorporateActionRecord], match: str
) -> None:
    with pytest.raises(CorporateActionError, match=match):
        adjust_bars(
            series(),  # type: ignore[operator]
            actions, method=AdjustmentMethod.SPLITS_AND_DIVIDENDS, calendar=CALENDAR,
        )  # fmt: skip


def test_empty_series() -> None:
    assert adjust_bars([], [split(4, 1)], method=AdjustmentMethod.SPLITS, calendar=CALENDAR) == []


# ---------- 持仓口径 ----------


def test_holdings_follow_the_split_ratio_with_whole_shares() -> None:
    assert shares_after_split(10, split(4, 1)) == 40
    assert shares_after_split(3, split(3, 2)) == 4  # 4.5 → 4 整股（零股由券商现金结算）
    assert shares_after_split(25, split(1, 10)) == 2  # 合股
    assert cost_after_split(Decimal(400), split(4, 1)) == Decimal(100)
    assert cost_after_split(Decimal(2), split(1, 10)) == Decimal(20)
    # 整除时成本总额不变
    assert shares_after_split(10, split(4, 1)) * cost_after_split(Decimal(400), split(4, 1)) == (
        10 * Decimal(400)
    )
    assert dividend_cash(100, dividend("1.75")) == Decimal("175.00")


def test_holding_helpers_reject_wrong_kinds_and_negative_quantities() -> None:
    with pytest.raises(CorporateActionError, match="不是拆股"):
        shares_after_split(10, dividend("1"))
    with pytest.raises(CorporateActionError, match="不是拆股"):
        cost_after_split(Decimal(1), dividend("1"))
    with pytest.raises(CorporateActionError, match="不是现金分红"):
        dividend_cash(10, split(2, 1))
    with pytest.raises(CorporateActionError, match="不能为负"):
        shares_after_split(-1, split(2, 1))


def test_effective_at_is_the_ex_date_open() -> None:
    assert effective_at(split(4, 1), CALENDAR) == datetime(2026, 10, 7, 13, 30, tzinfo=UTC)
    with pytest.raises(CorporateActionError, match="不是交易日"):
        effective_at(split(4, 1, date(2026, 10, 10)), CALENDAR)
    with pytest.raises(CorporateActionError, match="日历范围"):
        effective_at(split(4, 1, date(2027, 6, 1)), CALENDAR)


# ---------- 评审补充 ----------


def _tiny(day: date) -> MarketBar:
    """价格只有 0.000001 的日线：4 拆 1 复权后四舍五入为 0。"""
    p = Decimal("0.000001")
    return MarketBar.model_validate(
        {**bar(day, "100").model_dump(), "open": p, "high": p, "low": p, "close": p}
    )


@pytest.mark.asyncio
async def test_version_is_the_same_for_vendor_and_lake_read_back_records(tmp_path: Path) -> None:
    """供应商给 1.5、湖里读回 1.500000：数值相同，复权版本号必须相同（评审 P1-1）。"""
    lake = DataLake(tmp_path)
    vendor = [dividend("1.5")]
    await ingest_corporate_actions(
        FakeDataProvider(actions=vendor), lake, ["SPY"], WEEK[0], WEEK[-1], fetched_at=FETCHED
    )
    back = read_corporate_actions(lake, ["SPY"], WEEK[0], WEEK[-1])
    assert str(back[0].amount) == "1.500000"
    series = [bar(d, "100") for d in WEEK]
    m = AdjustmentMethod.SPLITS_AND_DIVIDENDS
    a = adjust_bars(series, vendor, method=m, calendar=CALENDAR)
    b = adjust_bars(series, back, method=m, calendar=CALENDAR)
    assert a == b
    assert a[0].adjustment_version == b[0].adjustment_version


def test_version_covers_the_previous_close_used_by_dividends() -> None:
    m = AdjustmentMethod.SPLITS_AND_DIVIDENDS
    base = adjust_bars([bar(d, "100") for d in WEEK], [dividend("1")], method=m, calendar=CALENDAR)
    revised_bars = [bar(d, "101" if d == WEEK[2] else "100") for d in WEEK]  # 前收被修订
    revised = adjust_bars(revised_bars, [dividend("1")], method=m, calendar=CALENDAR)
    assert base[0].adjustment_version != revised[0].adjustment_version


@pytest.mark.asyncio
async def test_concurrent_writer_between_merge_and_write_is_detected(tmp_path: Path) -> None:
    from server.data.lake import PartitionRef  # noqa: PLC0415

    class _Racing(DataLake):
        calls = 0

        def latest(self, layer: str, dataset: str, day: date) -> PartitionRef | None:
            if (layer, day) == ("normalized", WEEK[3]):
                self.calls += 1
                if self.calls == 2:  # 第一次是合并，第二次是写入前复核：此时插入别人的写入
                    from server.data.corporate_actions import actions_table  # noqa: PLC0415

                    self.write_partition(
                        "normalized", "corp_actions", day,
                        actions_table([dividend("0.4", symbol="QQQ")]), provider="fake",
                        fetched_at=FETCHED,
                    )  # fmt: skip
            return super().latest(layer, dataset, day)

    lake = _Racing(tmp_path)
    with pytest.raises(CorporateActionError, match="别的写入者"):
        await ingest_corporate_actions(
            FakeDataProvider(actions=[split(4, 1), dividend("1")]), lake, ["SPY"], WEEK[0],
            WEEK[-1], fetched_at=FETCHED,
        )  # fmt: skip
    assert lake.calls == 2
    assert lake.partitions("raw", "fake_corp_actions") == {}  # 10-07 也没写


@pytest.mark.asyncio
async def test_conflict_on_the_second_day_writes_nothing(tmp_path: Path) -> None:
    lake = DataLake(tmp_path)
    from server.data.corporate_actions import actions_table  # noqa: PLC0415

    lake.write_partition(
        "normalized", "corp_actions", WEEK[3],
        actions_table([dividend("0.4", symbol="QQQ", source="vendor_b")]), provider="vendor_b",
        fetched_at=FETCHED,
    )  # fmt: skip
    before = _files(lake)
    with pytest.raises(CorporateActionError, match="其他供应商"):
        await ingest_corporate_actions(
            FakeDataProvider(actions=[split(4, 1), dividend("1")]), lake, ["SPY"], WEEK[0],
            WEEK[-1], fetched_at=FETCHED,
        )  # fmt: skip
    assert _files(lake) == before


@pytest.mark.asyncio
async def test_revised_ex_date_leaves_a_duplicate_that_is_refused(tmp_path: Path) -> None:
    """同一事件的除权日从 10-08 改到 10-09：旧分区的行还在，读取与复权都拒绝（评审 P1-3）。"""
    lake = DataLake(tmp_path)
    first = dividend("1")
    moved = CorporateActionRecord.model_validate({**first.model_dump(), "ex_date": WEEK[4]})
    for action in (first, moved):
        await ingest_corporate_actions(
            FakeDataProvider(actions=[action]), lake, ["SPY"], WEEK[0], WEEK[-1],
            fetched_at=FETCHED,
        )  # fmt: skip
    with pytest.raises(CorporateActionError, match="出现两次"):
        read_corporate_actions(lake, ["SPY"], WEEK[0], WEEK[-1])
    with pytest.raises(CorporateActionError, match="出现两次"):
        adjust_bars([bar(d, "100") for d in WEEK], [first, moved],
                    method=AdjustmentMethod.SPLITS_AND_DIVIDENDS, calendar=CALENDAR)  # fmt: skip


@pytest.mark.parametrize(
    ("series", "actions", "match"),
    [
        (
            lambda: [bar(d, "100") for d in WEEK],
            [split(2, 1, WEEK[3]), dividend("1")],
            "既拆股又分红",
        ),
        (lambda: [bar(WEEK[0], "100"), bar(WEEK[0], "101")], [], "重复的日线"),
        (lambda: [_tiny(WEEK[0]), bar(WEEK[1], "2")], [split(4, 1, WEEK[1])], "不为正"),
        (
            lambda: [bar(WEEK[0], "100", volume="900000000000000"), bar(WEEK[1], "25")],
            [split(4, 1, WEEK[1])],
            "复权后的日线不合法",
        ),
    ],
)
def test_more_adjustment_refusals(
    series: object, actions: list[CorporateActionRecord], match: str
) -> None:
    with pytest.raises(CorporateActionError, match=match):
        adjust_bars(
            series(),  # type: ignore[operator]
            actions, method=AdjustmentMethod.SPLITS_AND_DIVIDENDS, calendar=CALENDAR,
        )  # fmt: skip


def test_dividend_whose_previous_session_is_outside_the_calendar() -> None:
    short = TradingCalendar.nyse(start=date(2026, 10, 6), end=date(2026, 12, 31))
    series = [bar(WEEK[1], "100"), bar(WEEK[2], "100")]
    early = dividend("1", WEEK[1])  # 除权日是首日：不在 (首日, 末日] 内，忽略
    adjust_bars(series, [early], method=AdjustmentMethod.SPLITS_AND_DIVIDENDS, calendar=short)
    edge = dividend("1", WEEK[2])
    tiny = TradingCalendar.nyse(start=date(2026, 10, 7), end=date(2026, 12, 31))
    with pytest.raises(CorporateActionError, match="日历范围"):
        adjust_bars(series, [edge], method=AdjustmentMethod.SPLITS_AND_DIVIDENDS, calendar=tiny)


def test_holding_helpers_precision_and_decimal_quantities() -> None:
    assert cost_after_split(Decimal(100), split(3, 1)) == Decimal("33.3333333333")
    assert shares_after_split(Decimal(30), split(3, 2)) == 45  # 对账里的持仓是 Decimal
    with pytest.raises(CorporateActionError, match="整股"):
        shares_after_split(Decimal("1.5"), split(2, 1))
    with pytest.raises(CorporateActionError, match="不能为负"):
        dividend_cash(-1, dividend("1"))
    assert dividend_cash(Decimal(10), dividend("0.25")) == Decimal("2.50")


def test_adjustment_refuses_same_day_dividends_and_excess_precision() -> None:
    m = AdjustmentMethod.SPLITS_AND_DIVIDENDS
    series = [bar(d, "100") for d in WEEK]
    two = [dividend("1"), dividend("0.5")]
    with pytest.raises(CorporateActionError, match="多笔现金分红"):
        adjust_bars(series, two, method=m, calendar=CALENDAR)
    with pytest.raises(CorporateActionError, match="存储精度"):
        adjust_bars(series, [dividend("1.1234567")], method=m, calendar=CALENDAR)


def test_huge_split_ratio_is_wrapped() -> None:
    giant = split(
        10**20, 1, WEEK[1]
    )  # 价格因子小到舍入为 0、成交量因子大到溢出：都转成本模块的错误
    with pytest.raises(CorporateActionError, match="复权后"):
        adjust_bars([bar(WEEK[0], "100", volume="100000000000000"), bar(WEEK[1], "100")], [giant],
                    method=AdjustmentMethod.SPLITS, calendar=CALENDAR)  # fmt: skip


def test_holding_helpers_reject_non_finite_and_bad_costs() -> None:
    with pytest.raises(CorporateActionError, match="整股"):
        shares_after_split(Decimal("Infinity"), split(2, 1))
    with pytest.raises(CorporateActionError, match="有限的 Decimal"):
        cost_after_split(1.5, split(2, 1))  # type: ignore[arg-type]
    with pytest.raises(CorporateActionError, match="不能为负"):
        cost_after_split(Decimal(-1), split(2, 1))


def test_read_refuses_records_whose_ex_date_differs_from_the_partition(tmp_path: Path) -> None:
    from server.data.corporate_actions import actions_table  # noqa: PLC0415

    lake = DataLake(tmp_path)
    lake.write_partition(
        "normalized", "corp_actions", WEEK[3], actions_table([dividend("1", WEEK[4])]),
        provider="fake", fetched_at=FETCHED,
    )  # fmt: skip
    with pytest.raises(CorporateActionError, match="不等于分区日期"):
        read_corporate_actions(lake, ["SPY"], WEEK[0], WEEK[-1])
