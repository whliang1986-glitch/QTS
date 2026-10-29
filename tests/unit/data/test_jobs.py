"""DataJob：分片、断点续传、重试、限速、单写者、增量（D-02）。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from server.data.calendar import TradingCalendar
from server.data.fake_provider import FakeDataProvider, synthetic_daily_bars
from server.data.jobs import (
    LOCK_FILE,
    CheckpointStore,
    DataJob,
    DataJobError,
    RateLimiter,
    RetryPolicy,
    Shard,
    ShardStatus,
    plan_shards,
    report_issues,
)
from server.data.lake import DataLake
from server.data.provider import BarBatch, Interval, ProviderUnavailableError, RateLimit
from server.data.quality import QualityThresholds
from server.domain.corporate_actions import CorporateActionKind, CorporateActionRecord
from server.domain.market import MarketBar
from server.platforms.files import InstanceLock

CALENDAR = TradingCalendar.nyse(start=date(2025, 1, 1), end=date(2026, 12, 31))
NOW = datetime(2026, 10, 10, 1, 0, tzinfo=UTC)  # 周六凌晨：最近已发布的是 10-09
THRESHOLDS = QualityThresholds(
    publish_lag=timedelta(hours=1), max_source_diff_bps=Decimal(20), split_tolerance=Decimal("0.25")
)
RETRY = RetryPolicy(max_attempts=3, backoff=timedelta(seconds=2))


def _bars(symbol: str) -> list[MarketBar]:
    return synthetic_daily_bars(
        symbol, CALENDAR, date(2025, 1, 2), date(2026, 12, 31), source="fake"
    )


class _Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _job(
    tmp_path: Path, provider: FakeDataProvider, *, now: datetime = NOW
) -> tuple[DataJob, DataLake, CheckpointStore, _Sleeps]:
    lake = DataLake(tmp_path / "lake")
    store = CheckpointStore(tmp_path / "meta.db")
    sleeps = _Sleeps()
    limiter = RateLimiter(
        RateLimit(1000, timedelta(seconds=1)), monotonic=lambda: 0.0, sleep=sleeps
    )
    job = DataJob(
        provider, lake, CALENDAR, store, thresholds=THRESHOLDS, retry=RETRY,
        clock=lambda: now, sleep=sleeps, limiter=limiter,
    )  # fmt: skip
    return job, lake, store, sleeps


def _provider(**kwargs: object) -> FakeDataProvider:
    return FakeDataProvider(bars={"SPY": _bars("SPY"), "QQQ": _bars("QQQ")}, clock=lambda: NOW,
                            **kwargs)  # type: ignore[arg-type]  # fmt: skip


def _files(lake: DataLake) -> list[str]:
    return sorted(
        str(p.relative_to(lake.root))
        for p in lake.root.rglob("*")
        if p.is_file() and p.name != LOCK_FILE
    )


# ---------- 分片 ----------


def test_shards_are_symbol_by_year() -> None:
    shards = plan_shards(["SPY", "AAPL", "SPY"], date(2025, 12, 1), date(2026, 3, 1))
    assert [s.key for s in shards] == [
        "AAPL:2025-12-01:2025-12-31", "AAPL:2026-01-01:2026-03-01",
        "SPY:2025-12-01:2025-12-31", "SPY:2026-01-01:2026-03-01",
    ]  # fmt: skip
    with pytest.raises(DataJobError, match="至少给出一个标的"):
        plan_shards([], date(2026, 1, 1), date(2026, 1, 2))
    with pytest.raises(ValueError, match="区间"):
        plan_shards(["SPY"], date(2026, 1, 2), date(2026, 1, 1))


# ---------- 回补与断点续传 ----------


@pytest.mark.asyncio
async def test_backfill_writes_every_shard_with_a_quality_report(tmp_path: Path) -> None:
    job, lake, store, _ = _job(tmp_path, _provider())
    summary = await job.backfill("backfill", ["SPY", "QQQ"], date(2025, 12, 1), date(2026, 10, 9))
    assert len(summary.done) == 4
    assert summary.failed == summary.skipped == ()
    assert summary.healthy
    for key in summary.done:
        assert report_issues(summary.reports[key]) == []  # 历史分片按分片末日评估新鲜度
    bars = lake.read_bars("SPY", date(2025, 12, 1), date(2026, 10, 9))
    assert bars[0].start.date() == date(2025, 12, 1)
    assert bars[-1].start.date() == date(2026, 10, 9)
    row = store.row("backfill", Shard("SPY", date(2026, 1, 1), date(2026, 10, 9)))
    assert row is not None
    assert (row["status"], row["attempts"], row["detail"]) == ("DONE", 1, "ok")


@pytest.mark.asyncio
async def test_rerun_skips_done_shards_and_force_is_idempotent(tmp_path: Path) -> None:
    provider = _provider()
    job, lake, _, _ = _job(tmp_path, provider)
    await job.backfill("b", ["SPY"], date(2026, 9, 1), date(2026, 10, 9))
    files = _files(lake)
    calls = len(provider.calls)
    again = await job.backfill("b", ["SPY"], date(2026, 9, 1), date(2026, 10, 9))
    assert again.skipped == ("SPY:2026-09-01:2026-10-09",)
    assert len(provider.calls) == calls  # 跳过的分片不再请求供应商
    forced = await job.backfill("b", ["SPY"], date(2026, 9, 1), date(2026, 10, 9), force=True)
    assert forced.done == ("SPY:2026-09-01:2026-10-09",)
    assert _files(lake) == files  # 重跑同内容：没有新版本


@pytest.mark.asyncio
async def test_end_is_clipped_to_the_latest_published_session(tmp_path: Path) -> None:
    job, _, _, _ = _job(tmp_path, _provider())
    summary = await job.backfill("b", ["SPY"], date(2026, 10, 1), date(2026, 12, 31))
    assert summary.done == ("SPY:2026-10-01:2026-10-09",)
    with pytest.raises(DataJobError, match="还没有已发布"):
        await job.backfill("b", ["SPY"], date(2026, 10, 12), date(2026, 10, 20))


class _Flaky(FakeDataProvider):
    """前 failures 次拉日线时报供应商不可用；可指定只对某个标的生效。"""

    def __init__(self, failures: int, symbol: str | None = None) -> None:
        super().__init__(bars={"SPY": _bars("SPY"), "QQQ": _bars("QQQ")}, clock=lambda: NOW)
        self.failures = failures
        self.only = symbol

    async def fetch_bars(
        self, symbol: str, start: date, end: date, interval: Interval = Interval.DAY
    ) -> BarBatch:
        if self.failures > 0 and self.only in (None, symbol):
            self.failures -= 1
            raise ProviderUnavailableError("维护中")
        return await super().fetch_bars(symbol, start, end, interval)


@pytest.mark.asyncio
async def test_transient_failures_are_retried_with_exponential_backoff(tmp_path: Path) -> None:
    job, _, store, sleeps = _job(tmp_path, _Flaky(failures=2))
    summary = await job.backfill("b", ["SPY"], date(2026, 10, 1), date(2026, 10, 9))
    assert summary.done == ("SPY:2026-10-01:2026-10-09",)
    assert sleeps.calls == [2.0, 4.0]
    row = store.row("b", Shard("SPY", date(2026, 10, 1), date(2026, 10, 9)))
    assert row is not None
    assert row["attempts"] == 3


@pytest.mark.asyncio
async def test_exhausted_retries_fail_the_shard_and_resume_later(tmp_path: Path) -> None:
    provider = _Flaky(failures=10, symbol="QQQ")
    job, lake, store, _ = _job(tmp_path, provider)
    first = await job.backfill("b", ["SPY", "QQQ"], date(2026, 10, 1), date(2026, 10, 9))
    assert first.done == ("SPY:2026-10-01:2026-10-09",)
    assert first.failed == ("QQQ:2026-10-01:2026-10-09",)
    assert not first.healthy
    failed = Shard("QQQ", date(2026, 10, 1), date(2026, 10, 9))
    assert store.status("b", failed) is ShardStatus.FAILED
    row = store.row("b", failed)
    assert row is not None
    assert "共尝试 3 次" in str(row["detail"])
    assert lake.read_bars("QQQ", date(2026, 10, 1), date(2026, 10, 9)) == []
    provider.failures = 0  # 供应商恢复：断点续传只补失败的分片
    second = await job.backfill("b", ["SPY", "QQQ"], date(2026, 10, 1), date(2026, 10, 9))
    assert second.skipped == ("SPY:2026-10-01:2026-10-09",)
    assert second.done == ("QQQ:2026-10-01:2026-10-09",)
    row = store.row("b", failed)
    assert row is not None
    assert row["attempts"] == 4  # 累计尝试次数
    assert len(lake.read_bars("QQQ", date(2026, 10, 1), date(2026, 10, 9))) == 7


class _WrongSource(FakeDataProvider):
    async def fetch_bars(
        self, symbol: str, start: date, end: date, interval: Interval = Interval.DAY
    ) -> BarBatch:
        batch = await super().fetch_bars(symbol, start, end, interval)
        from dataclasses import replace  # noqa: PLC0415

        from server.data.provider import FetchedBar  # noqa: PLC0415

        first = batch.bars[0]
        bad = MarketBar.model_validate({**first.bar.model_dump(), "source": "other"})
        return replace(batch, bars=(FetchedBar(bad, first.raw), *batch.bars[1:]))


@pytest.mark.asyncio
async def test_contract_violations_are_not_retried(tmp_path: Path) -> None:
    provider = _WrongSource(bars={"SPY": _bars("SPY")}, clock=lambda: NOW)
    job, lake, store, sleeps = _job(tmp_path, provider)
    summary = await job.backfill("b", ["SPY"], date(2026, 10, 1), date(2026, 10, 9))
    assert summary.failed == ("SPY:2026-10-01:2026-10-09",)
    assert sleeps.calls == []
    row = store.row("b", Shard("SPY", date(2026, 10, 1), date(2026, 10, 9)))
    assert row is not None
    assert row["attempts"] == 1
    assert "IngestError" in str(row["detail"])
    assert _files(lake) == []


class _Broken(FakeDataProvider):
    async def fetch_bars(
        self, symbol: str, start: date, end: date, interval: Interval = Interval.DAY
    ) -> BarBatch:
        raise RuntimeError("适配器缺陷")


@pytest.mark.asyncio
async def test_unexpected_errors_are_recorded_then_raised(tmp_path: Path) -> None:
    job, _, store, _ = _job(tmp_path, _Broken(bars={"SPY": _bars("SPY")}))
    with pytest.raises(RuntimeError, match="适配器缺陷"):
        await job.backfill("b", ["SPY"], date(2026, 10, 1), date(2026, 10, 9))
    shard = Shard("SPY", date(2026, 10, 1), date(2026, 10, 9))
    assert store.status("b", shard) is ShardStatus.FAILED


# ---------- 质量报告与健康 ----------


@pytest.mark.asyncio
async def test_quality_problems_block_the_symbol(tmp_path: Path) -> None:
    """登记了 4 拆 1 但价格没有跳变（未复权口径不符）：复权一致性不通过，标的被阻断。"""
    split = CorporateActionRecord(
        event_id="spy-split", symbol="SPY", kind=CorporateActionKind.SPLIT,
        ex_date=date(2026, 10, 7), source="fake", numerator=4, denominator=1,
    )  # fmt: skip
    job, lake, _, _ = _job(tmp_path, _provider(actions=[split]))
    summary = await job.backfill("b", ["SPY"], date(2026, 10, 1), date(2026, 10, 9))
    assert summary.done == ("SPY:2026-10-01:2026-10-09",)
    assert summary.blocked == frozenset({"SPY"})
    assert not summary.healthy
    issues = report_issues(summary.reports["SPY:2026-10-01:2026-10-09"])
    assert [i["check"] for i in issues] == ["ADJUSTMENT"]
    from server.data.corporate_actions import read_corporate_actions  # noqa: PLC0415

    stored = read_corporate_actions(lake, ["SPY"], date(2026, 10, 1), date(2026, 10, 9))
    assert [a.event_id for a in stored] == ["spy-split"]  # 公司行动同时入湖


# ---------- 单写者 ----------


@pytest.mark.asyncio
async def test_a_second_writer_is_refused(tmp_path: Path) -> None:
    job, lake, _, _ = _job(tmp_path, _provider())
    with InstanceLock(lake.root / LOCK_FILE), pytest.raises(DataJobError, match="单写者"):
        await job.backfill("b", ["SPY"], date(2026, 10, 1), date(2026, 10, 9))
    summary = await job.backfill("b", ["SPY"], date(2026, 10, 1), date(2026, 10, 9))
    assert summary.done  # 锁释放后可以运行


# ---------- 增量 ----------


@pytest.mark.asyncio
async def test_incremental_waits_for_the_close_and_skips_holidays(tmp_path: Path) -> None:
    job, lake, _, _ = _job(tmp_path, _provider(), now=datetime(2026, 10, 9, 20, 30, tzinfo=UTC))
    with pytest.raises(DataJobError, match="还没有收盘"):
        await job.incremental("daily", ["SPY"], date(2026, 10, 9))
    assert (
        await job.incremental("daily", ["SPY"], date(2026, 10, 10))
        == type(await job.incremental("daily", ["SPY"], date(2026, 10, 11)))()
    )  # 周末：什么都不做
    later, lake, _, _ = _job(tmp_path, _provider(), now=datetime(2026, 10, 9, 21, 0, tzinfo=UTC))
    summary = await later.incremental("daily", ["SPY", "QQQ"], date(2026, 10, 9))
    assert summary.done == ("QQQ:2026-10-09:2026-10-09", "SPY:2026-10-09:2026-10-09")
    assert [
        b.start.date() for b in lake.read_bars("SPY", date(2026, 10, 9), date(2026, 10, 9))
    ] == [date(2026, 10, 9)]
    with pytest.raises(DataJobError, match="交易日历不覆盖"):
        await later.incremental("daily", ["SPY"], date(2027, 6, 1))


# ---------- 限速与参数 ----------


@pytest.mark.asyncio
async def test_rate_limiter_sliding_window() -> None:
    clock = [0.0]
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        clock[0] += seconds

    limiter = RateLimiter(
        RateLimit(2, timedelta(seconds=10)), monotonic=lambda: clock[0], sleep=sleep
    )
    for _ in range(5):
        await limiter.acquire()
        clock[0] += 1
    assert waits == [8.0, 8.0]  # 每 10 秒最多 2 次


def test_retry_policy_validation() -> None:
    with pytest.raises(ValueError, match="重试"):
        RetryPolicy(max_attempts=0, backoff=timedelta(seconds=1))
    with pytest.raises(ValueError, match="退避"):
        RetryPolicy(max_attempts=1, backoff=timedelta(seconds=-1))


def test_checkpoints_survive_reopening(tmp_path: Path) -> None:
    shard = Shard("SPY", date(2026, 1, 1), date(2026, 1, 31))
    store = CheckpointStore(tmp_path / "meta.db")
    store.record("b", shard, ShardStatus.DONE, attempts=1, at=NOW, detail="ok", report="{}")
    store.close()
    reopened = CheckpointStore(tmp_path / "meta.db")
    assert reopened.status("b", shard) is ShardStatus.DONE
    assert reopened.status("other-job", shard) is None
    reopened.close()


# ---------- 评审补充 ----------


def _split_without_jump() -> CorporateActionRecord:
    return CorporateActionRecord(
        event_id="spy-split", symbol="SPY", kind=CorporateActionKind.SPLIT,
        ex_date=date(2026, 10, 7), source="fake", numerator=4, denominator=1,
    )  # fmt: skip


@pytest.mark.asyncio
async def test_skipped_shards_still_report_their_blocked_symbols(tmp_path: Path) -> None:
    """重跑跳过已完成的分片时，上次报告里的受阻标的仍计入（评审 P1-1）。"""
    job, _, _, _ = _job(tmp_path, _provider(actions=[_split_without_jump()]))
    first = await job.backfill("b", ["SPY"], date(2026, 10, 1), date(2026, 10, 9))
    assert first.blocked == {"SPY"}
    again = await job.backfill("b", ["SPY"], date(2026, 10, 1), date(2026, 10, 9))
    assert again.skipped == ("SPY:2026-10-01:2026-10-09",)
    assert again.blocked == {"SPY"}
    assert not again.healthy


@pytest.mark.asyncio
async def test_a_done_shard_with_an_unreadable_report_is_rerun(tmp_path: Path) -> None:
    job, _, store, _ = _job(tmp_path, _provider())
    shard = Shard("SPY", date(2026, 10, 1), date(2026, 10, 9))
    store.record("b", shard, ShardStatus.DONE, attempts=1, at=NOW, detail="ok", report="garbage")
    summary = await job.backfill("b", ["SPY"], date(2026, 10, 1), date(2026, 10, 9))
    assert summary.done == (shard.key,)  # 报告损坏：当作未完成重跑，而不是一直报失败
    assert summary.healthy
    assert summary.repaired == (shard.key,)  # 损坏不被静默修复：结果里列出
    row = store.row("b", shard)
    assert row is not None
    assert row["attempts"] == 2
    assert report_issues(str(row["report"])) == []


@pytest.mark.asyncio
async def test_ingest_quarantine_is_recorded_in_the_report(tmp_path: Path) -> None:
    """入湖时隔离的坏 K 线只在原始层，原因必须进报告（评审 P1-2）。"""
    bars = [
        MarketBar.model_validate({**b.model_dump(), "high": b.close - 1})
        if b.start.date() == date(2026, 10, 7) else b
        for b in _bars("SPY")
    ]  # fmt: skip
    job, _, _, _ = _job(tmp_path, FakeDataProvider(bars={"SPY": bars}, clock=lambda: NOW))
    summary = await job.backfill("b", ["SPY"], date(2026, 10, 1), date(2026, 10, 9))
    import json  # noqa: PLC0415

    report = json.loads(summary.reports["SPY:2026-10-01:2026-10-09"])
    assert [(i["check"], i["day"]) for i in report["ingest_quarantined"]] == [
        ("COMPLETENESS", "2026-10-07")
    ]
    assert ("CONTINUITY", "2026-10-07") in [(i["check"], i["day"]) for i in report["issues"]]


@pytest.mark.asyncio
async def test_split_on_the_first_day_of_an_incremental_shard_is_checked(tmp_path: Path) -> None:
    """单日增量分片遇上除权日：往前多看一个交易日，正确口径不被误停（评审 P2-1）。"""
    split = _split_without_jump()
    unadjusted = [
        b if b.start.date() < date(2026, 10, 7)
        else MarketBar.model_validate(
            {**b.model_dump(), **{k: getattr(b, k) / 4 for k in ("open", "high", "low", "close")}}
        )
        for b in _bars("SPY")
    ]  # fmt: skip
    provider = FakeDataProvider(bars={"SPY": unadjusted}, actions=[split], clock=lambda: NOW)
    job, _, _, _ = _job(tmp_path, provider)
    await job.backfill("b", ["SPY"], date(2026, 10, 1), date(2026, 10, 6))
    summary = await job.incremental("daily", ["SPY"], date(2026, 10, 7))
    assert summary.done == ("SPY:2026-10-07:2026-10-07",)
    assert summary.blocked == frozenset()


@pytest.mark.asyncio
async def test_shards_without_sessions_are_dropped(tmp_path: Path) -> None:
    job, _, _, _ = _job(tmp_path, _provider())
    summary = await job.backfill("b", ["SPY"], date(2025, 12, 29), date(2026, 1, 1))
    assert summary.done == ("SPY:2025-12-29:2025-12-31",)  # 2026-01-01 元旦休市，没有分片


@pytest.mark.asyncio
async def test_holiday_incremental_and_lock_release_after_unexpected_error(tmp_path: Path) -> None:
    from server.data.jobs import JobSummary  # noqa: PLC0415

    job, _, store, sleeps = _job(tmp_path, _Broken(bars={"SPY": _bars("SPY")}),
                                 now=datetime(2026, 11, 27, tzinfo=UTC))  # fmt: skip
    assert await job.incremental("daily", ["SPY"], date(2026, 11, 26)) == JobSummary()  # 感恩节
    with pytest.raises(RuntimeError):
        await job.backfill("b", ["SPY"], date(2026, 10, 1), date(2026, 10, 9))
    row = store.row("b", Shard("SPY", date(2026, 10, 1), date(2026, 10, 9)))
    assert row is not None
    assert row["attempts"] == 1
    assert sleeps.calls == []
    with pytest.raises(RuntimeError):  # 锁已释放：能再次运行（而不是“另一个任务在写”）
        await job.backfill("b", ["SPY"], date(2026, 10, 1), date(2026, 10, 9))


class _FlakyActions(FakeDataProvider):
    def __init__(self) -> None:
        super().__init__(bars={"SPY": _bars("SPY")}, clock=lambda: NOW)
        self.failures = 1

    async def fetch_corporate_actions(
        self, symbols: object, start: date, end: date
    ) -> list[CorporateActionRecord]:
        if self.failures:
            self.failures -= 1
            raise ProviderUnavailableError("限流")
        return list(await super().fetch_corporate_actions(symbols, start, end))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_corporate_action_outage_is_retried_and_each_shard_uses_two_requests(
    tmp_path: Path,
) -> None:
    lake = DataLake(tmp_path / "lake")
    store = CheckpointStore(tmp_path / "meta.db")
    acquired: list[int] = []

    class _Counting(RateLimiter):
        async def acquire(self) -> None:
            acquired.append(1)

    job = DataJob(
        _FlakyActions(), lake, CALENDAR, store, thresholds=THRESHOLDS, retry=RETRY,
        clock=lambda: NOW, sleep=_Sleeps(), limiter=_Counting(RateLimit(1, timedelta(seconds=1))),
    )  # fmt: skip
    summary = await job.backfill("b", ["SPY"], date(2026, 10, 1), date(2026, 10, 9))
    assert summary.done == ("SPY:2026-10-01:2026-10-09",)
    assert len(acquired) == 4  # 两次尝试，每次日线与公司行动各一次请求
    store.close()


@pytest.mark.asyncio
async def test_the_session_after_an_ex_date_is_not_blocked(tmp_path: Path) -> None:
    """除权日的下一个交易日做增量：往前看到除权日，但不重复核对那次拆股（复审 P1）。"""
    split = _split_without_jump()
    unadjusted = [
        b if b.start.date() < date(2026, 10, 7)
        else MarketBar.model_validate(
            {**b.model_dump(), **{k: getattr(b, k) / 4 for k in ("open", "high", "low", "close")}}
        )
        for b in _bars("SPY")
    ]  # fmt: skip
    provider = FakeDataProvider(bars={"SPY": unadjusted}, actions=[split], clock=lambda: NOW)
    job, _, _, _ = _job(tmp_path, provider)
    await job.backfill("b", ["SPY"], date(2026, 10, 1), date(2026, 10, 7))
    summary = await job.incremental("daily", ["SPY"], date(2026, 10, 8))
    assert summary.done == ("SPY:2026-10-08:2026-10-08",)
    assert summary.blocked == frozenset()


@pytest.mark.asyncio
async def test_year_boundary_after_an_ex_date_on_the_last_session(tmp_path: Path) -> None:
    """上一年最后一个交易日除权：下一年分片不被阻断。"""
    split = CorporateActionRecord(
        event_id="ye-split", symbol="SPY", kind=CorporateActionKind.SPLIT,
        ex_date=date(2025, 12, 31), source="fake", numerator=4, denominator=1,
    )  # fmt: skip
    unadjusted = [
        b if b.start.date() < date(2025, 12, 31)
        else MarketBar.model_validate(
            {**b.model_dump(), **{k: getattr(b, k) / 4 for k in ("open", "high", "low", "close")}}
        )
        for b in _bars("SPY")
    ]  # fmt: skip
    provider = FakeDataProvider(bars={"SPY": unadjusted}, actions=[split], clock=lambda: NOW)
    job, _, _, _ = _job(tmp_path, provider)
    summary = await job.backfill("b", ["SPY"], date(2025, 12, 1), date(2026, 1, 31))
    assert summary.done == ("SPY:2025-12-01:2025-12-31", "SPY:2026-01-01:2026-01-31")
    assert summary.blocked == frozenset()


def test_checkpoint_store_requires_wal() -> None:
    with pytest.raises(DataJobError, match="WAL"):
        CheckpointStore(Path(":memory:"))


def test_report_helpers_reject_malformed_reports() -> None:
    from server.data.jobs import blocked_symbols  # noqa: PLC0415

    for bad in ("garbage", "[]", "{}", '{"issues": 1}', '{"blocked_symbols": [1]}'):
        with pytest.raises(DataJobError, match="质量报告格式不对"):
            report_issues(bad)
        with pytest.raises(DataJobError, match="质量报告格式不对"):
            blocked_symbols(bad)


@pytest.mark.asyncio
async def test_a_wrong_basis_on_the_shards_first_day_is_still_caught(tmp_path: Path) -> None:
    """反向用例：单日增量正好是除权日、价格没有按拆股跳变，仍判为复权口径不符而受阻。"""
    job, _, _, _ = _job(tmp_path, _provider(actions=[_split_without_jump()]))
    await job.backfill("b", ["SPY"], date(2026, 10, 1), date(2026, 10, 6))
    summary = await job.incremental("daily", ["SPY"], date(2026, 10, 7))
    assert summary.blocked == {"SPY"}
    issues = report_issues(summary.reports["SPY:2026-10-07:2026-10-07"])
    assert [i["check"] for i in issues] == ["ADJUSTMENT"]
