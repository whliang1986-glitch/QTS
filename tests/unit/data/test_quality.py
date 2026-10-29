"""数据质量门禁与报告（D-05）：六类检查、报告、报价标记、入湖隔离。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from server.data.calendar import TradingCalendar
from server.data.fake_provider import FakeDataProvider, synthetic_daily_bars
from server.data.ingest import IngestError, ingest_bars
from server.data.lake import DataLake
from server.data.quality import (
    UNADJUSTED,
    LookaheadError,
    QualityAction,
    QualityCheck,
    QualityThresholds,
    assess_daily_bars,
    check_adjustment,
    check_completeness,
    check_continuity,
    check_freshness,
    check_lookahead,
    check_source_consistency,
    expected_latest_session,
    flag_quote,
    require_no_lookahead,
)
from server.domain.corporate_actions import CorporateActionKind, CorporateActionRecord
from server.domain.enums import AssetClass, DataQuality
from server.domain.instrument import Instrument
from server.domain.market import MarketBar, Quote

ZERO = timedelta(0)
CALENDAR = TradingCalendar.nyse(start=date(2026, 9, 1), end=date(2026, 12, 31))
WEEK = [date(2026, 10, d) for d in (5, 6, 7, 8, 9)]  # 周一到周五
LAG = timedelta(hours=1)
LATE = datetime(2026, 12, 31, tzinfo=UTC)  # 远晚于测试用的日线：都已收盘并发布
SKEW = timedelta(seconds=5)
THRESHOLDS = QualityThresholds(
    publish_lag=timedelta(hours=1),
    max_source_diff_bps=Decimal(20),
    split_tolerance=Decimal("0.25"),
)


def bar(
    day: date,
    close: str = "100",
    *,
    symbol: str = "SPY",
    open_: str | None = None,
    high: str | None = None,
    low: str | None = None,
    version: str = UNADJUSTED,
    source: str = "fake",
) -> MarketBar:
    session = CALENDAR.session(day)
    assert session is not None
    c = Decimal(close)
    o = Decimal(open_) if open_ is not None else c
    return MarketBar(
        symbol=symbol, start=session.open, end=session.close, open=o,
        high=Decimal(high) if high is not None else max(o, c) + 1,
        low=Decimal(low) if low is not None else min(o, c) - 1,
        close=c, volume=Decimal(1000), source=source, adjustment_version=version,
    )  # fmt: skip


def week(**kwargs: str) -> list[MarketBar]:
    return [bar(d, **kwargs) for d in WEEK]


def checks(issues: list) -> list[tuple[QualityCheck, date | None]]:  # type: ignore[type-arg]
    return [(i.check, i.day) for i in issues]


# ---------- 完整性：隔离 ----------


def test_good_bars_pass_completeness() -> None:
    accepted, quarantined, issues = check_completeness(
        week(), calendar=CALENDAR, as_of=LATE, publish_lag=LAG
    )
    assert (len(accepted), quarantined, issues) == (5, [], [])


def test_early_close_session_is_a_complete_daily_bar() -> None:
    # 感恩节次日 2026-11-27 提前收市（13:00 ET = 18:00 UTC）
    early = bar(date(2026, 11, 27))
    assert early.end == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
    assert check_completeness([early], calendar=CALENDAR, as_of=LATE, publish_lag=LAG)[2] == []


@pytest.mark.parametrize(
    ("broken", "match"),
    [
        (lambda: bar(WEEK[0], "100", high="99"), "OHLC"),
        (lambda: bar(WEEK[0], "100", low="101"), "OHLC"),
        (lambda: bar(WEEK[0], "100", open_="105", high="104"), "OHLC"),
        (
            lambda: MarketBar.model_validate(
                {
                    **bar(WEEK[0]).model_dump(),
                    "start": datetime(2026, 10, 10, 13, 30, tzinfo=UTC),
                    "end": datetime(2026, 10, 10, 20, 0, tzinfo=UTC),
                }
            ),
            "不是交易日",
        ),  # 周六
        (
            lambda: MarketBar.model_validate(
                {**bar(WEEK[0]).model_dump(), "end": datetime(2026, 10, 5, 14, 30, tzinfo=UTC)}
            ),
            "恰好覆盖交易时段",
        ),  # 其实是一根小时线
    ],
)
def test_incomplete_bars_are_quarantined(broken: object, match: str) -> None:
    item = broken()  # type: ignore[operator]
    accepted, quarantined, issues = check_completeness(
        [item, bar(WEEK[1])], calendar=CALENDAR, as_of=LATE, publish_lag=LAG
    )
    assert [b.start for b in quarantined] == [item.start]
    assert len(accepted) == 1
    assert len(issues) == 1
    assert issues[0].check is QualityCheck.COMPLETENESS
    assert issues[0].action is QualityAction.QUARANTINE
    assert match in issues[0].detail


def test_missing_instrument_spec_quarantines_the_symbol() -> None:
    spec = Instrument(symbol="SPY", asset_class=AssetClass.ETF, exchange="ARCA", currency="USD",
                      tick_size=None, trading_calendar="XNYS")  # fmt: skip
    _, quarantined, issues = check_completeness(
        [bar(WEEK[0]), bar(WEEK[0], symbol="QQQ")],
        calendar=CALENDAR,
        as_of=LATE,
        publish_lag=LAG,
        instruments={"SPY": spec},
    )
    assert len(quarantined) == 2
    assert "tick_size" in issues[0].detail
    assert "没有标的规格" in issues[1].detail


# ---------- 连续性：标记与报告，不填充 ----------


def test_complete_week_has_no_continuity_issue() -> None:
    assert check_continuity("SPY", week(), calendar=CALENDAR, start=WEEK[0], end=WEEK[-1]) == []


def test_holidays_are_not_gaps() -> None:
    days = [date(2026, 11, 25), date(2026, 11, 27)]  # 11-26 感恩节休市
    series = [bar(d) for d in days]
    assert check_continuity("SPY", series, calendar=CALENDAR, start=days[0], end=days[-1]) == []


def test_gap_duplicate_reversal_and_foreign_symbol_are_reported() -> None:
    series = [bar(WEEK[0]), bar(WEEK[2]), bar(WEEK[2]), bar(WEEK[1]), bar(WEEK[3], symbol="QQQ")]
    issues = check_continuity("SPY", series, calendar=CALENDAR, start=WEEK[0], end=WEEK[-1])
    details = [(i.day, i.detail) for i in issues]
    assert (WEEK[2], "重复的日线") in details
    assert any(d == WEEK[1] and "逆序" in t for d, t in details)
    foreign = [i for i in issues if i.detail == "混入了 SPY 的序列"]
    assert [(i.symbol, i.day) for i in foreign] == [("QQQ", WEEK[3])]  # 记在 QQQ 名下
    assert [d for d, t in details if t == "缺少该交易日的日线"] == [WEEK[3], WEEK[4]]
    assert {i.action for i in issues} == {QualityAction.REPORT}


# ---------- 新鲜度：禁止新增风险 ----------


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 10, 9, 21, 0, tzinfo=UTC), date(2026, 10, 9)),  # 收盘 20:00 + 1 小时
        (datetime(2026, 10, 9, 20, 59, tzinfo=UTC), date(2026, 10, 8)),  # 还没过发布延迟
        (datetime(2026, 10, 11, 12, 0, tzinfo=UTC), date(2026, 10, 9)),  # 周日
        (datetime(2026, 11, 27, 19, 0, tzinfo=UTC), date(2026, 11, 27)),  # 提前收市日
        (datetime(2026, 11, 26, 23, 0, tzinfo=UTC), date(2026, 11, 25)),  # 感恩节
    ],
)
def test_expected_latest_session(now: datetime, expected: date) -> None:
    assert expected_latest_session(CALENDAR, now, timedelta(hours=1)).day == expected


def test_freshness() -> None:
    now = datetime(2026, 10, 9, 22, 0, tzinfo=UTC)
    lag = timedelta(hours=1)
    assert check_freshness("SPY", bar(WEEK[4]), calendar=CALENDAR, now=now, publish_lag=lag) == []
    stale = check_freshness("SPY", bar(WEEK[3]), calendar=CALENDAR, now=now, publish_lag=lag)
    assert checks(stale) == [(QualityCheck.FRESHNESS, WEEK[4])]
    assert stale[0].action is QualityAction.BLOCK_NEW_RISK
    empty = check_freshness("SPY", None, calendar=CALENDAR, now=now, publish_lag=lag)
    assert [(i.check, i.day, i.detail) for i in empty] == [
        (QualityCheck.FRESHNESS, WEEK[4], "没有任何日线")
    ]
    foreign = bar(WEEK[4], symbol="QQQ")
    wrong = check_freshness("SPY", foreign, calendar=CALENDAR, now=now, publish_lag=lag)
    assert [(i.check, i.detail) for i in wrong] == [(QualityCheck.FRESHNESS, "最新日线是 QQQ 的")]


def test_freshness_requires_an_aware_now() -> None:
    with pytest.raises(ValueError, match="时区"):
        expected_latest_session(CALENDAR, datetime(2026, 10, 9, 22), timedelta(0))  # noqa: DTZ001


# ---------- 来源一致性：报警 ----------


def test_source_consistency() -> None:
    primary = week(close="100")
    assert (
        check_source_consistency(
            "SPY", primary, week(close="100.1", source="vendor_b"), max_diff_bps=Decimal(20)
        )
        == []
    )
    far = check_source_consistency(
        "SPY", primary, week(close="100.5", source="vendor_b"), max_diff_bps=Decimal(20)
    )
    assert len(far) == 5
    assert "50.00 bps" in far[0].detail
    assert far[0].action is QualityAction.ALERT
    missing = check_source_consistency(
        "SPY", primary, week(source="vendor_b")[:4], max_diff_bps=Decimal(20)
    )
    assert checks(missing) == [(QualityCheck.SOURCE_CONSISTENCY, WEEK[4])]
    other = check_source_consistency(
        "SPY", primary, week(version="adj-1", source="vendor_b"), max_diff_bps=Decimal(20)
    )
    assert len(other) == 5
    assert "复权口径" in other[0].detail


def _quote(last: str | None, symbol: str = "SPY", source: str = "backup") -> Quote:
    return Quote(symbol=symbol, ts=datetime(2026, 10, 9, 15, 0, tzinfo=UTC), bid=Decimal("99.9"),
                 ask=Decimal("100.1"), last=None if last is None else Decimal(last),
                 source=source, delayed=False)  # fmt: skip


@pytest.mark.parametrize(
    ("secondary", "flagged"),
    [
        (None, False),  # 首期只有一个实时源：不比对
        (_quote("100.1"), False),
        (_quote("100.5"), True),
        (_quote(None), True),  # 备源没有最新价：无法核对
        (_quote("100", symbol="QQQ"), True),
    ],
)
def test_flag_quote(secondary: Quote | None, flagged: bool) -> None:
    result = flag_quote(
        _quote("100", source="ibkr"), secondary, max_diff_bps=Decimal(20), max_skew=SKEW
    )
    assert (DataQuality.SOURCE_MISMATCH in result.quality) is flagged


# ---------- 前视：回测失败 ----------


def test_lookahead() -> None:
    lag = timedelta(minutes=30)
    history = [bar(d) for d in WEEK[:4]]  # 到 10-08 收盘（20:00 UTC）
    require_no_lookahead(datetime(2026, 10, 8, 20, 30, tzinfo=UTC), history, publish_lag=lag)
    with pytest.raises(LookaheadError, match="SPY:2026-10-08"):
        require_no_lookahead(datetime(2026, 10, 8, 20, 29, tzinfo=UTC), history, publish_lag=lag)
    with pytest.raises(LookaheadError, match="SPY:2026-10-09"):
        require_no_lookahead(
            datetime(2026, 10, 9, 14, 0, tzinfo=UTC), [*history, bar(WEEK[4])], publish_lag=lag
        )
    issues = check_lookahead(
        datetime(2026, 10, 9, tzinfo=UTC), [("SPY:eps", datetime(2026, 10, 9, 1, tzinfo=UTC))]
    )
    assert checks(issues) == [(QualityCheck.LOOKAHEAD, date(2026, 10, 9))]
    assert issues[0].action is QualityAction.FAIL_BACKTEST


# ---------- 复权一致性：停止该标的 ----------

SPLIT = CorporateActionRecord(
    event_id="spy-split", symbol="SPY", kind=CorporateActionKind.SPLIT, ex_date=WEEK[2],
    source="fake", numerator=4, denominator=1,
)  # fmt: skip
DIVIDEND = CorporateActionRecord(
    event_id="spy-div", symbol="SPY", kind=CorporateActionKind.CASH_DIVIDEND, ex_date=WEEK[3],
    source="fake", amount=Decimal("1.5"), currency="USD",
)  # fmt: skip


def _split_series(after: str, version: str = UNADJUSTED) -> list[MarketBar]:
    before = [bar(d, "400", version=version) for d in WEEK[:2]]
    return [*before, *(bar(d, after, version=version) for d in WEEK[2:])]


def test_unadjusted_split_with_the_expected_jump_is_consistent() -> None:
    issues = check_adjustment(
        "SPY", _split_series("100"), [SPLIT, DIVIDEND], calendar=CALENDAR, tolerance=Decimal("0.25")
    )
    assert issues == []


def test_unadjusted_label_without_a_jump_is_a_mismatch() -> None:
    issues = check_adjustment(
        "SPY", _split_series("400"), [SPLIT], calendar=CALENDAR, tolerance=Decimal("0.25")
    )
    assert checks(issues) == [(QualityCheck.ADJUSTMENT, WEEK[2])]
    assert issues[0].action is QualityAction.HALT_SYMBOL
    assert "未复权" in issues[0].detail


def test_adjusted_series_must_be_smooth_across_the_split() -> None:
    smooth = [bar(d, "100", version="adj-1") for d in WEEK]
    assert (
        check_adjustment("SPY", smooth, [SPLIT], calendar=CALENDAR, tolerance=Decimal("0.25")) == []
    )
    jumpy = _split_series("100", version="adj-1")
    issues = check_adjustment("SPY", jumpy, [SPLIT], calendar=CALENDAR, tolerance=Decimal("0.25"))
    assert checks(issues) == [(QualityCheck.ADJUSTMENT, WEEK[2])]
    assert "已复权" in issues[0].detail


def test_mixed_versions_and_unverifiable_splits() -> None:
    mixed = [*week()[:3], bar(WEEK[3], version="adj-1")]
    issues = check_adjustment("SPY", mixed, [], calendar=CALENDAR, tolerance=Decimal("0.25"))
    assert "混用复权版本" in issues[0].detail
    missing = [b for b in _split_series("100") if b.start.date() != WEEK[2]]
    issues = check_adjustment("SPY", missing, [SPLIT], calendar=CALENDAR, tolerance=Decimal("0.25"))
    assert "无法核对" in issues[0].detail


# ---------- 报告 ----------


def test_assess_clean_week_passes() -> None:
    report = assess_daily_bars(
        "SPY", week(), calendar=CALENDAR, start=WEEK[0], end=WEEK[-1],
        now=datetime(2026, 10, 9, 22, tzinfo=UTC), thresholds=THRESHOLDS,
        secondary=week(source="vendor_b"),
    )  # fmt: skip
    assert report.passed
    assert report.blocked_symbols() == frozenset()
    data = json.loads(report.to_json())
    assert (data["passed"], data["accepted"], data["issues"]) == (True, 5, [])


def test_assess_reports_issues_flags_bars_and_blocks_symbols() -> None:
    series = [*week()[:3], bar(WEEK[3], "100", high="99")]  # 10-08 坏、10-09 缺
    report = assess_daily_bars(
        "SPY", series, calendar=CALENDAR, start=WEEK[0], end=WEEK[-1],
        now=datetime(2026, 10, 9, 22, tzinfo=UTC), thresholds=THRESHOLDS,
    )  # fmt: skip
    found = {(i.check, i.day) for i in report.issues}
    assert (QualityCheck.COMPLETENESS, WEEK[3]) in found
    assert (QualityCheck.CONTINUITY, WEEK[4]) in found
    assert (QualityCheck.FRESHNESS, WEEK[4]) in found  # 隔离掉 10-08 后最新是 10-07
    assert [b.start.date() for b in report.quarantined] == [WEEK[3]]
    assert report.blocked_symbols() == {"SPY"}
    assert all(not b.quality for b in report.flagged())  # 问题都不落在保留的日线上
    data = json.loads(report.to_json())
    assert data["blocked_symbols"] == ["SPY"]
    assert {i["action"] for i in data["issues"]} >= {"QUARANTINE", "REPORT", "BLOCK_NEW_RISK"}


def test_flagged_bars_carry_the_quality_marks() -> None:
    series = [*week()[:4], bar(WEEK[4], version="adj-1")]
    report = assess_daily_bars(
        "SPY", series, calendar=CALENDAR, start=WEEK[0], end=WEEK[-1],
        now=datetime(2026, 10, 9, 22, tzinfo=UTC), thresholds=THRESHOLDS,
    )  # fmt: skip
    assert all(DataQuality.ADJUSTMENT_MISMATCH in b.quality for b in report.flagged())
    assert all(not b.quality for b in report.accepted)  # 原日线不被修改


def test_continuity_only_issues_do_not_block() -> None:
    report = assess_daily_bars(
        "SPY", week(), calendar=CALENDAR, start=date(2026, 10, 2), end=WEEK[-1],
        now=datetime(2026, 10, 9, 22, tzinfo=UTC), thresholds=THRESHOLDS,
    )  # fmt: skip
    assert [i.check for i in report.issues] == [QualityCheck.CONTINUITY]
    assert report.blocked_symbols() == frozenset()


@pytest.mark.parametrize(
    "changes",
    [
        {"publish_lag": timedelta(seconds=-1)},
        {"max_source_diff_bps": Decimal(0)},
        {"split_tolerance": Decimal("NaN")},
        {"split_tolerance": 0.25},
    ],
)
def test_thresholds_validation(changes: dict[str, object]) -> None:
    base: dict[str, object] = {
        "publish_lag": timedelta(hours=1),
        "max_source_diff_bps": Decimal(20),
        "split_tolerance": Decimal("0.25"),
    }
    with pytest.raises(ValueError):
        QualityThresholds(**{**base, **changes})  # type: ignore[arg-type]


# ---------- 入湖隔离 ----------


def _synthetic() -> list[MarketBar]:
    return synthetic_daily_bars(
        "SPY", CALENDAR, date(2026, 9, 1), date(2026, 12, 31), source="fake"
    )


def _fake(bars: list[MarketBar]) -> FakeDataProvider:
    return FakeDataProvider(bars={"SPY": bars}, clock=lambda: LATE)


@pytest.mark.asyncio
async def test_ingest_quarantines_incomplete_bars(tmp_path: Path) -> None:
    broken = [
        MarketBar.model_validate({**b.model_dump(), "high": b.close - 1})
        if b.start.date() == WEEK[2] else b
        for b in _synthetic()
    ]  # fmt: skip
    lake = DataLake(tmp_path)
    result = await ingest_bars(
        _fake(broken),
        lake,
        "SPY",
        WEEK[0],
        WEEK[-1],
        calendar=CALENDAR,
        publish_lag=ZERO,
        clock=lambda: LATE,
    )
    assert [(i.check, i.day) for i in result.quarantined] == [(QualityCheck.COMPLETENESS, WEEK[2])]
    assert len(result.raw) == 5  # 原始层照收（备查）
    assert len(result.normalized) == 4  # 标准化层没有 10-07
    assert [b.start.date() for b in lake.read_bars("SPY", WEEK[0], WEEK[-1])] == [
        d for d in WEEK if d != WEEK[2]
    ]


@pytest.mark.asyncio
async def test_bad_revision_does_not_erase_the_existing_good_bar(tmp_path: Path) -> None:
    good = _synthetic()
    lake = DataLake(tmp_path)
    await ingest_bars(
        _fake(good),
        lake,
        "SPY",
        WEEK[0],
        WEEK[-1],
        calendar=CALENDAR,
        publish_lag=ZERO,
        clock=lambda: LATE,
    )
    bad = [MarketBar.model_validate({**b.model_dump(), "high": b.close - 1}) for b in good]
    result = await ingest_bars(
        _fake(bad),
        lake,
        "SPY",
        WEEK[0],
        WEEK[-1],
        calendar=CALENDAR,
        publish_lag=ZERO,
        clock=lambda: LATE,
    )
    assert result.normalized == ()
    assert len(result.quarantined) == 5
    assert len(lake.read_bars("SPY", WEEK[0], WEEK[-1])) == 5  # 已有的好数据仍在


@pytest.mark.asyncio
async def test_ingest_outside_the_calendar_is_refused(tmp_path: Path) -> None:
    from server.data.ingest import IngestError  # noqa: PLC0415

    short = TradingCalendar.nyse(start=date(2026, 10, 1), end=date(2026, 10, 6))
    with pytest.raises(IngestError, match="交易日历"):
        await ingest_bars(
            _fake(_synthetic()), DataLake(tmp_path), "SPY", WEEK[0], WEEK[-1],
            calendar=short, publish_lag=ZERO, clock=lambda: LATE,
        )  # fmt: skip


# ---------- 评审补充 ----------


@pytest.mark.parametrize(("numerator", "denominator"), [(5, 4), (4, 5), (3, 2)])
@pytest.mark.parametrize("after", ["400", "320"])
def test_splits_too_close_to_one_are_unverifiable(
    numerator: int, denominator: int, after: str
) -> None:
    """5:4 等比例离 1 太近：未复权有跳变与已复权无跳变在容差内分不开，无论数据如何都不通过。"""
    split = CorporateActionRecord(
        event_id="small", symbol="SPY", kind=CorporateActionKind.SPLIT, ex_date=WEEK[2],
        source="fake", numerator=numerator, denominator=denominator,
    )  # fmt: skip
    for version in (UNADJUSTED, "adj-1"):
        issues = check_adjustment(
            "SPY", _split_series(after, version=version), [split], calendar=CALENDAR,
            tolerance=Decimal("0.25"),
        )  # fmt: skip
        assert checks(issues) == [(QualityCheck.ADJUSTMENT, WEEK[2])]
        assert "离 1 太近" in issues[0].detail


def test_split_needs_the_immediately_preceding_session() -> None:
    series = [b for b in _split_series("100") if b.start.date() != WEEK[1]]  # 缺除权日前一日
    issues = check_adjustment("SPY", series, [SPLIT], calendar=CALENDAR, tolerance=Decimal("0.25"))
    assert checks(issues) == [(QualityCheck.ADJUSTMENT, WEEK[2])]
    assert "前一交易日缺日线" in issues[0].detail


def test_split_across_a_weekend_uses_friday_as_the_previous_session() -> None:
    days = [date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 13)]
    split = CorporateActionRecord(
        event_id="monday", symbol="SPY", kind=CorporateActionKind.SPLIT, ex_date=days[2],
        source="fake", numerator=4, denominator=1,
    )  # fmt: skip
    series = [bar(d, "400" if d < days[2] else "100") for d in days]
    assert (
        check_adjustment("SPY", series, [split], calendar=CALENDAR, tolerance=Decimal("0.25")) == []
    )


def test_unfinished_and_future_bars_are_quarantined() -> None:
    """盘中拉到的当日 K 线（未收盘）与收盘后还没过发布延迟的 K 线都是未完成的。"""
    intraday = datetime(2026, 10, 9, 17, 0, tzinfo=UTC)  # 10-09 盘中
    _, quarantined, issues = check_completeness(
        week(), calendar=CALENDAR, as_of=intraday, publish_lag=timedelta(0)
    )
    assert [b.start.date() for b in quarantined] == [WEEK[4]]
    assert "未完成" in issues[0].detail
    just_closed = datetime(2026, 10, 9, 20, 30, tzinfo=UTC)  # 收盘半小时，发布延迟 1 小时
    _, quarantined, _ = check_completeness(
        week(), calendar=CALENDAR, as_of=just_closed, publish_lag=LAG
    )
    assert [b.start.date() for b in quarantined] == [WEEK[4]]


def test_assess_quarantines_future_bars() -> None:
    report = assess_daily_bars(
        "SPY", week(), calendar=CALENDAR, start=WEEK[0], end=WEEK[-1],
        now=datetime(2026, 10, 9, 17, 0, tzinfo=UTC), thresholds=THRESHOLDS,
    )  # fmt: skip
    assert [b.start.date() for b in report.quarantined] == [WEEK[4]]
    assert all(b.start.date() < WEEK[4] for b in report.accepted)


def test_assess_quarantines_foreign_symbols_and_duplicate_days() -> None:
    twin = bar(WEEK[1], "101")  # 同一交易日的第二根、取值不同
    series = [*week(), twin, bar(WEEK[2], symbol="QQQ")]
    report = assess_daily_bars(
        "SPY", series, calendar=CALENDAR, start=WEEK[0], end=WEEK[-1],
        now=datetime(2026, 10, 9, 22, tzinfo=UTC), thresholds=THRESHOLDS,
    )  # fmt: skip
    assert {b.symbol for b in report.accepted} == {"SPY"}
    assert WEEK[1] not in {b.start.date() for b in report.accepted}
    details = [i.detail for i in report.issues if i.check is QualityCheck.COMPLETENESS]
    assert sum("多根日线" in d for d in details) == 2
    assert any("不是 SPY 的日线（QQQ）" in d for d in details)


def test_same_source_as_backup_is_unverifiable() -> None:
    primary = week()
    issues = check_source_consistency("SPY", primary, week(), max_diff_bps=Decimal(20))
    assert len(issues) == 5
    assert "同一来源" in issues[0].detail
    backup = week(source="vendor_b")
    assert check_source_consistency("SPY", primary, backup, max_diff_bps=Decimal(20)) == []


def test_flag_quote_rejects_stale_backup_same_source_and_missing_primary_last() -> None:
    primary = _quote("100", source="ibkr")
    stale = Quote.model_validate(
        {**_quote("100").model_dump(), "ts": primary.ts - timedelta(seconds=6)}
    )
    same = _quote("100", source="ibkr")
    for secondary in (stale, same):
        result = flag_quote(primary, secondary, max_diff_bps=Decimal(20), max_skew=SKEW)
        assert DataQuality.SOURCE_MISMATCH in result.quality
    no_last = flag_quote(
        _quote(None, source="ibkr"), _quote("100"), max_diff_bps=Decimal(20), max_skew=SKEW
    )
    assert DataQuality.SOURCE_MISMATCH in no_last.quality


def test_negative_decimal_threshold_is_refused() -> None:
    with pytest.raises(ValueError, match="max_source_diff_bps"):
        QualityThresholds(timedelta(0), Decimal(-1), Decimal("0.25"))


@pytest.mark.asyncio
async def test_ingest_quarantines_the_intraday_bar(tmp_path: Path) -> None:
    lake = DataLake(tmp_path)
    provider = FakeDataProvider(
        bars={"SPY": _synthetic()}, clock=lambda: datetime(2026, 10, 9, 17, 0, tzinfo=UTC)
    )
    result = await ingest_bars(
        provider,
        lake,
        "SPY",
        WEEK[0],
        WEEK[-1],
        calendar=CALENDAR,
        publish_lag=ZERO,
        clock=lambda: LATE,
    )
    assert [(i.check, i.day) for i in result.quarantined] == [(QualityCheck.COMPLETENESS, WEEK[4])]
    assert WEEK[4] not in {b.start.date() for b in lake.read_bars("SPY", WEEK[0], WEEK[-1])}


@pytest.mark.asyncio
async def test_ingest_refuses_a_range_the_calendar_does_not_cover(tmp_path: Path) -> None:
    """日历不覆盖请求区间：即使供应商只返回覆盖范围内的 K 线，也整批拒绝。"""
    from server.data.ingest import IngestError  # noqa: PLC0415

    short = TradingCalendar.nyse(start=date(2026, 10, 1), end=date(2026, 10, 7))
    inside = [b for b in _synthetic() if b.start.date() <= date(2026, 10, 7)]
    provider = FakeDataProvider(bars={"SPY": inside}, clock=lambda: LATE)
    with pytest.raises(IngestError, match="交易日历不覆盖"):
        await ingest_bars(
            provider,
            DataLake(tmp_path),
            "SPY",
            WEEK[0],
            WEEK[-1],
            calendar=short,
            publish_lag=ZERO,
            clock=lambda: LATE,
        )


# ---------- 评审第二轮补充 ----------


def _ratio_split(numerator: int, denominator: int) -> CorporateActionRecord:
    return CorporateActionRecord(
        event_id=f"split-{numerator}-{denominator}", symbol="SPY", kind=CorporateActionKind.SPLIT,
        ex_date=WEEK[2], source="fake", numerator=numerator, denominator=denominator,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("numerator", "denominator", "before", "after"),
    [(2, 1, "200", "100"), (1, 2, "100", "200"), (1, 4, "25", "100"), (3, 1, "300", "100")],
)
def test_distinguishable_splits_and_reverse_splits_pass_and_mislabels_fail(
    numerator: int, denominator: int, before: str, after: str
) -> None:
    """2:1、1:2、1:4、3:1 两种口径可分辨：正确口径通过，标错口径报不一致。"""
    split = _ratio_split(numerator, denominator)
    tol = Decimal("0.25")
    unadjusted = [bar(d, before) for d in WEEK[:2]] + [bar(d, after) for d in WEEK[2:]]
    adjusted = [bar(d, after, version="adj-1") for d in WEEK]
    assert check_adjustment("SPY", unadjusted, [split], calendar=CALENDAR, tolerance=tol) == []
    assert check_adjustment("SPY", adjusted, [split], calendar=CALENDAR, tolerance=tol) == []
    mislabeled = [bar(d, after) for d in WEEK]  # 标为未复权，实际没有跳变
    issues = check_adjustment("SPY", mislabeled, [split], calendar=CALENDAR, tolerance=tol)
    assert checks(issues) == [(QualityCheck.ADJUSTMENT, WEEK[2])]
    assert "前收 / 除权开盘" in issues[0].detail


def test_ex_date_on_the_first_day_is_unverifiable() -> None:
    series = _split_series("100")[2:]  # 从除权日开始
    issues = check_adjustment("SPY", series, [SPLIT], calendar=CALENDAR, tolerance=Decimal("0.25"))
    assert checks(issues) == [(QualityCheck.ADJUSTMENT, WEEK[2])]
    assert "无法核对" in issues[0].detail


def test_foreign_bars_do_not_flag_the_symbols_good_bars() -> None:
    series = [*week(), bar(WEEK[2], symbol="QQQ")]
    report = assess_daily_bars(
        "SPY", series, calendar=CALENDAR, start=WEEK[0], end=WEEK[-1],
        now=datetime(2026, 10, 9, 22, tzinfo=UTC), thresholds=THRESHOLDS,
    )  # fmt: skip
    assert len(report.flagged()) == 5
    assert all(not b.quality for b in report.flagged())
    assert {i.symbol for i in report.issues} == {"QQQ"}
    assert report.blocked_symbols() == frozenset()


def test_one_good_and_one_bad_bar_on_the_same_day_are_both_quarantined() -> None:
    series = [*week(), bar(WEEK[1], "100", high="99")]
    report = assess_daily_bars(
        "SPY", series, calendar=CALENDAR, start=WEEK[0], end=WEEK[-1],
        now=datetime(2026, 10, 9, 22, tzinfo=UTC), thresholds=THRESHOLDS,
    )  # fmt: skip
    assert WEEK[1] not in {b.start.date() for b in report.accepted}
    assert [b.start.date() for b in report.quarantined] == [WEEK[1], WEEK[1]]


def test_flag_quote_refuses_a_negative_skew() -> None:
    with pytest.raises(ValueError, match="max_skew"):
        flag_quote(_quote("100"), None, max_diff_bps=Decimal(20), max_skew=timedelta(seconds=-1))


@pytest.mark.asyncio
async def test_ingest_uses_the_earlier_of_vendor_and_local_clock(tmp_path: Path) -> None:
    """供应商时间戳偏快（说已到收盘后）但本机还在盘中：按本机时间判断，当日 K 线隔离。"""
    lake = DataLake(tmp_path)
    result = await ingest_bars(
        _fake(_synthetic()), lake, "SPY", WEEK[0], WEEK[-1], calendar=CALENDAR, publish_lag=ZERO,
        clock=lambda: datetime(2026, 10, 9, 17, 0, tzinfo=UTC),
    )  # fmt: skip
    assert [(i.check, i.day) for i in result.quarantined] == [(QualityCheck.COMPLETENESS, WEEK[4])]


@pytest.mark.asyncio
async def test_ingest_holds_back_preliminary_bars_within_the_publish_lag(tmp_path: Path) -> None:
    lake = DataLake(tmp_path)
    result = await ingest_bars(
        _fake(_synthetic()), lake, "SPY", WEEK[0], WEEK[-1], calendar=CALENDAR, publish_lag=LAG,
        clock=lambda: datetime(2026, 10, 9, 20, 30, tzinfo=UTC),  # 收盘半小时
    )  # fmt: skip
    assert [i.day for i in result.quarantined] == [WEEK[4]]
    with pytest.raises(IngestError, match="发布延迟"):
        await ingest_bars(
            _fake(_synthetic()), lake, "SPY", WEEK[0], WEEK[-1], calendar=CALENDAR,
            publish_lag=timedelta(seconds=-1),
        )  # fmt: skip


@pytest.mark.parametrize(
    ("numerator", "denominator", "verifiable"),
    [(5, 3, False), (3, 5, False), (7, 4, True), (4, 7, True)],
)
def test_split_distinguishability_boundary(
    numerator: int, denominator: int, verifiable: bool
) -> None:
    """容差 0.25 时分界是 5/3 与 3/5：恰好在分界上（区间交于一点）按无法核对，越过分界可核对。"""
    split = _ratio_split(numerator, denominator)
    ratio = Decimal(numerator) / Decimal(denominator)
    before = (Decimal(100) * ratio).quantize(Decimal("0.000001"))
    series = [bar(d, str(before)) for d in WEEK[:2]] + [bar(d, "100") for d in WEEK[2:]]
    issues = check_adjustment("SPY", series, [split], calendar=CALENDAR, tolerance=Decimal("0.25"))
    assert (issues == []) is verifiable
    if not verifiable:
        assert "离 1 太近" in issues[0].detail


def test_flag_quote_validates_the_price_threshold() -> None:
    for bad in (Decimal(-1), Decimal(0), Decimal("NaN"), 20.0):
        with pytest.raises(ValueError, match="max_diff_bps"):
            flag_quote(_quote("100"), None, max_diff_bps=bad, max_skew=SKEW)  # type: ignore[arg-type]
