"""数据任务 DataJob：历史回补与每日增量（D-02，V4.1 5.3）。

- 回补按“标的 × 年”分片（与交易日历求交，只到已收盘并过了发布延迟的最近交易日为止）；
  每个分片：限速 → 日线入湖 → 公司行动入湖（供应商支持时）→ 质量报告 → 记检查点。
- 断点续传：检查点存在 SQLite（WAL、synchronous=FULL）里，已完成的分片跳过（force 时重跑）；
  入湖本身同内容幂等，重跑同一分片结果相同。
- 重试：只有 ProviderUnavailableError（网络、限流、维护）按指数退避重试，次数有上限；
  契约违反、数据不合格、湖内冲突直接记为失败，不重试（重试不会让坏数据变好）。
- 限速：按供应商的 RateLimit 做滑动窗口，每次请求前排队。
- 单写者：任务运行期间持有数据湖目录下的文件锁，另一个任务拿不到锁即拒绝运行
  （D-01a / D-04 的合并依赖它）。
- 失败即关闭：日历不覆盖、参数不合法即拒绝；意外异常先记失败再原样上抛，不吞掉。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections import deque
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from server.data.calendar import CalendarRangeError, TradingCalendar
from server.data.corporate_actions import (
    CorporateActionError,
    ingest_corporate_actions,
    read_corporate_actions,
)
from server.data.ingest import IngestError, ingest_bars
from server.data.lake import DataLake, LakeError
from server.data.provider import (
    DataProvider,
    DataProviderError,
    Interval,
    ProviderUnavailableError,
    RateLimit,
    check_range,
)
from server.data.quality import QualityThresholds, assess_daily_bars, expected_latest_session
from server.domain.values import to_utc
from server.platforms.files import InstanceLock, InstanceLockedError

LOCK_FILE = ".datajob.lock"


class DataJobError(Exception):
    """任务无法运行（参数、日历、锁）：拒绝，不做部分工作。"""


class ShardStatus(StrEnum):
    DONE = "DONE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class Shard:
    """分片键由标的与（截断后的）起止日期组成：续传要求调用参数完全相同；
    当年分片的末日随最近交易日变化，所以当年分片每天会重拉一次（入湖幂等，不产生重复）。
    换供应商时应使用新的 job 名。"""

    symbol: str
    start: date
    end: date

    @property
    def key(self) -> str:
        return f"{self.symbol}:{self.start.isoformat()}:{self.end.isoformat()}"


def plan_shards(symbols: Collection[str], start: date, end: date) -> list[Shard]:
    """按“标的 × 年”切分 [start, end]，顺序固定（标的、年份）。"""
    check_range(start, end)
    if not symbols:
        raise DataJobError("至少给出一个标的")
    shards = []
    for symbol in sorted(set(symbols)):
        for year in range(start.year, end.year + 1):
            lo, hi = max(start, date(year, 1, 1)), min(end, date(year, 12, 31))
            shards.append(Shard(symbol, lo, hi))
    return shards


# ---------- 检查点 ----------


class CheckpointStore:
    """分片检查点（meta.db 的一张表）。同一 (job, shard) 只有一行，状态覆盖写，尝试次数累加。"""

    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(path, isolation_level=None)
        mode = self._db.execute("PRAGMA journal_mode=WAL").fetchone()
        if mode is None or str(mode[0]).lower() != "wal":
            self._db.close()
            raise DataJobError(f"检查点数据库无法启用 WAL（{mode}）")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS datajob_shards ("
            " job TEXT NOT NULL, shard TEXT NOT NULL, status TEXT NOT NULL,"
            " attempts INTEGER NOT NULL, updated_at TEXT NOT NULL, detail TEXT NOT NULL,"
            " report TEXT, PRIMARY KEY (job, shard))"
        )

    def close(self) -> None:
        self._db.close()

    def status(self, job: str, shard: Shard) -> ShardStatus | None:
        row = self._db.execute(
            "SELECT status FROM datajob_shards WHERE job = ? AND shard = ?", (job, shard.key)
        ).fetchone()
        return None if row is None else ShardStatus(row[0])

    def record(  # noqa: PLR0913 - 一行检查点的全部字段
        self,
        job: str,
        shard: Shard,
        status: ShardStatus,
        *,
        attempts: int,
        at: datetime,
        detail: str,
        report: str | None = None,
    ) -> None:
        self._db.execute(
            "INSERT INTO datajob_shards (job, shard, status, attempts, updated_at, detail, report)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (job, shard) DO UPDATE SET status = excluded.status,"
            " attempts = datajob_shards.attempts + excluded.attempts,"
            " updated_at = excluded.updated_at, detail = excluded.detail, report = excluded.report",
            (job, shard.key, status.value, attempts, to_utc(at).isoformat(), detail, report),
        )

    def row(self, job: str, shard: Shard) -> dict[str, object] | None:
        cursor = self._db.execute(
            "SELECT status, attempts, updated_at, detail, report FROM datajob_shards"
            " WHERE job = ? AND shard = ?",
            (job, shard.key),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return dict(zip(("status", "attempts", "updated_at", "detail", "report"), row, strict=True))


# ---------- 限速 ----------


class RateLimiter:
    """滑动窗口限速：任意 per 时间内最多 requests 次请求。时钟与等待可注入（测试用）。"""

    def __init__(
        self,
        limit: RateLimit,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._limit = limit
        self._window = limit.per.total_seconds()
        self._monotonic = monotonic
        self._sleep = sleep
        self._sent: deque[float] = deque()

    async def acquire(self) -> None:
        while True:
            now = self._monotonic()
            while self._sent and now - self._sent[0] >= self._window:
                self._sent.popleft()
            if len(self._sent) < self._limit.requests:
                self._sent.append(now)
                return
            await self._sleep(self._window - (now - self._sent[0]))


# ---------- 任务 ----------


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    backoff: timedelta  # 第 n 次重试前等待 backoff × 2^(n-1)

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.backoff < timedelta(0):
            raise ValueError("重试次数至少 1 次，退避不能为负")


@dataclass(frozen=True)
class JobSummary:
    done: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    blocked: frozenset[str] = frozenset()  # 质量报告要求不得新增风险的标的
    reports: dict[str, str] = field(default_factory=dict)  # 分片 → 质量报告 JSON
    repaired: tuple[str, ...] = ()  # 检查点报告损坏、已重跑的分片（需要排查原因）

    @property
    def healthy(self) -> bool:
        """供 DATA 组件健康上报（R-08）：没有失败分片、没有被质量门禁阻断的标的。"""
        return not self.failed and not self.blocked


class DataJob:
    def __init__(  # noqa: PLR0913 - 依赖全部显式注入
        self,
        provider: DataProvider,
        lake: DataLake,
        calendar: TradingCalendar,
        store: CheckpointStore,
        *,
        thresholds: QualityThresholds,
        retry: RetryPolicy,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._provider = provider
        self._lake = lake
        self._calendar = calendar
        self._store = store
        self._thresholds = thresholds
        self._retry = retry
        self._clock = clock
        self._sleep = sleep
        self._limiter = limiter or RateLimiter(provider.rate_limit, sleep=sleep)

    def _now(self) -> datetime:
        return to_utc(self._clock())

    async def backfill(
        self, job: str, symbols: Collection[str], start: date, end: date, *, force: bool = False
    ) -> JobSummary:
        """回补 [start, end]（截到已发布的最近交易日）。已完成的分片跳过，force 时重跑。"""
        try:
            latest = expected_latest_session(
                self._calendar, self._now(), self._thresholds.publish_lag
            ).day
        except (CalendarRangeError, ValueError) as exc:
            raise DataJobError(f"交易日历不覆盖当前时刻：{exc}") from exc
        if start > latest:
            raise DataJobError(f"{start} 之后还没有已发布的日线（最近的是 {latest}）")
        shards = [s for s in plan_shards(symbols, start, min(end, latest)) if self._has_sessions(s)]
        return await self._run(job, shards, force=force)

    def _has_sessions(self, shard: Shard) -> bool:
        try:
            return any(self._calendar.sessions_between(shard.start, shard.end))
        except CalendarRangeError as exc:
            raise DataJobError(f"交易日历不覆盖 {shard.key}：{exc}") from exc

    async def incremental(self, job: str, symbols: Collection[str], day: date) -> JobSummary:
        """每日增量：只在当日收盘并过了发布延迟后拉取这一天；非交易日什么都不做。"""
        try:
            session = self._calendar.session(day)
        except CalendarRangeError as exc:
            raise DataJobError(f"交易日历不覆盖 {day}：{exc}") from exc
        if session is None:
            return JobSummary()
        if self._now() < session.close + self._thresholds.publish_lag:
            raise DataJobError(f"{day} 还没有收盘或还没过发布延迟，不能做增量")
        return await self._run(job, plan_shards(symbols, day, day), force=False)

    async def _run(self, job: str, shards: Sequence[Shard], *, force: bool) -> JobSummary:
        lock = InstanceLock(self._lake.root / LOCK_FILE)
        try:
            lock.acquire()
        except InstanceLockedError as exc:
            raise DataJobError("另一个数据任务正在写数据湖（单写者）") from exc
        try:
            done, skipped, failed, repaired = [], [], [], []
            blocked: set[str] = set()
            reports: dict[str, str] = {}
            for shard in shards:
                if not force and self._store.status(job, shard) is ShardStatus.DONE:
                    # 跳过的分片也要计入上次报告里被阻断的标的，否则重跑会把“受阻”变成“健康”
                    previous = self._previous_report(job, shard)
                    if previous is not None:
                        skipped.append(shard.key)
                        reports[shard.key] = previous
                        blocked |= blocked_symbols(previous)
                        continue
                    # 报告读不出来：当作没有完成，重跑这个分片（入湖幂等），并在结果里列出待排查
                    repaired.append(shard.key)
                report = await self._shard(job, shard)
                if report is None:
                    failed.append(shard.key)
                    continue
                done.append(shard.key)
                reports[shard.key] = report[0]
                blocked |= report[1]
            return JobSummary(
                tuple(done), tuple(skipped), tuple(failed), frozenset(blocked), reports,
                tuple(repaired),
            )  # fmt: skip
        finally:
            lock.release()

    async def _shard(self, job: str, shard: Shard) -> tuple[str, frozenset[str]] | None:
        """跑一个分片；成功返回（质量报告 JSON，被阻断的标的），失败记检查点并返回 None。"""
        attempts = 0
        while True:
            attempts += 1
            try:
                result = await self._attempt(shard)
            except ProviderUnavailableError as exc:
                if attempts < self._retry.max_attempts:
                    await self._sleep(self._retry.backoff.total_seconds() * 2 ** (attempts - 1))
                    continue
                self._fail(job, shard, attempts, f"供应商不可用，共尝试 {attempts} 次：{exc}")
                return None
            except (
                DataProviderError,
                IngestError,
                CorporateActionError,
                LakeError,
                CalendarRangeError,
            ) as exc:
                self._fail(job, shard, attempts, f"{type(exc).__name__}：{exc}")
                return None
            except Exception as exc:
                self._fail(job, shard, attempts, f"意外错误 {type(exc).__name__}：{exc}")
                raise
            self._store.record(
                job, shard, ShardStatus.DONE, attempts=attempts, at=self._now(),
                detail="ok", report=result[0],
            )  # fmt: skip
            return result

    def _previous_report(self, job: str, shard: Shard) -> str | None:
        row = self._store.row(job, shard)
        report = None if row is None else row["report"]
        if not isinstance(report, str):
            return None
        try:
            blocked_symbols(report)
        except DataJobError:
            return None
        return report

    def _fail(self, job: str, shard: Shard, attempts: int, detail: str) -> None:
        self._store.record(
            job, shard, ShardStatus.FAILED, attempts=attempts, at=self._now(), detail=detail
        )

    async def _attempt(self, shard: Shard) -> tuple[str, frozenset[str]]:
        lag = self._thresholds.publish_lag
        await self._limiter.acquire()
        ingested = await ingest_bars(
            self._provider, self._lake, shard.symbol, shard.start, shard.end,
            calendar=self._calendar, publish_lag=lag, interval=Interval.DAY, clock=self._clock,
        )  # fmt: skip
        if self._provider.capabilities.corporate_actions:
            await self._limiter.acquire()
            await ingest_corporate_actions(
                self._provider, self._lake, [shard.symbol], shard.start, shard.end,
                fetched_at=self._now(),
            )  # fmt: skip
        # 质量检查往前多看一个交易日（湖里有的话）：除权日正好是分片首日时也能核对复权口径
        start = self._lookback(shard)
        actions = []
        if self._provider.capabilities.corporate_actions:
            # 公司行动只取本分片：往前看的那一天若是除权日，它已在上一个分片核对过，
            # 再算一次会因“没有前一交易日”误判为无法核对
            actions = read_corporate_actions(self._lake, [shard.symbol], shard.start, shard.end)
        bars = self._lake.read_bars(shard.symbol, start, shard.end)
        sessions = list(self._calendar.sessions_between(shard.start, shard.end))
        # 历史分片按分片末日评估新鲜度：否则过去年份的分片永远“陈旧”。
        # 所以只回补过去年份时的 healthy 不代表当前数据新鲜；DATA 健康取自覆盖到最近交易日的结果
        as_of = min(self._now(), sessions[-1].close + lag)
        report = assess_daily_bars(
            shard.symbol, bars, calendar=self._calendar, start=start, end=shard.end,
            now=as_of, thresholds=self._thresholds, actions=actions,
        )  # fmt: skip
        data = json.loads(report.to_json())
        # 入湖时隔离的 K 线只在原始层：把原因并进报告，作为隔离记录
        data["ingest_quarantined"] = [i.to_dict() for i in ingested.quarantined]
        data["checked_from"] = start.isoformat()  # 往前多看的那一天的问题也会出现在报告里
        return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), (
            report.blocked_symbols()
        )

    def _lookback(self, shard: Shard) -> date:
        probe = shard.start - timedelta(days=1)
        try:
            for _ in range(14):
                if self._calendar.is_trading_day(probe):
                    have = self._lake.read_bars(shard.symbol, probe, probe)
                    return probe if have else shard.start
                probe -= timedelta(days=1)
        except CalendarRangeError:
            pass
        return shard.start


def blocked_symbols(report_json: str) -> frozenset[str]:
    """读回检查点里质量报告的受阻标的；格式不对即报错（调用方按失败处理）。"""
    try:
        blocked = json.loads(report_json)["blocked_symbols"]
    except (ValueError, KeyError, TypeError) as exc:
        raise DataJobError(f"质量报告格式不对：{exc}") from exc
    if not isinstance(blocked, list) or not all(isinstance(x, str) for x in blocked):
        raise DataJobError("质量报告格式不对：blocked_symbols")
    return frozenset(blocked)


def report_issues(report_json: str) -> list[dict[str, object]]:
    """读回检查点里的质量报告问题列表（运维与界面用）。"""
    try:
        issues = json.loads(report_json)["issues"]
    except (ValueError, KeyError, TypeError) as exc:
        raise DataJobError(f"质量报告格式不对：{exc}") from exc
    if not isinstance(issues, list):
        raise DataJobError("质量报告格式不对")
    return issues
