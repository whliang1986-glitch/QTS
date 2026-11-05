"""workerd 里的策略调度（T-01 移交）：按交易日历在决策时刻运行策略，意图放进 outbox。

分两步：第一步在线程里，第二步在 workerd 的事件循环线程里
（outbox 的 SQLite 连接只能在创建它的线程里用）：
1. `prepare(now)`：读数据湖、算策略，只把意图收集起来，不碰 outbox。
   不在运行窗口内时直接返回 None，不读任何数据。
2. `commit(prepared, outbox)`：逐条入队。同键同内容去重；同键不同内容拒绝，绝不会多下一单。

每个交易日只认真跑一次，而且“已运行”**落盘**（state_path，原子写入）：
- 第一步开始计算之前先写下当天日期，写不进去就不运行（失败即关闭）；
- 重启后读到同一天就跳过。这样避免第一轮的卖单在窗口内成交后，重跑或重启后当天就提议买入
  （T-02 评审记录的“同日重跑”问题）；
- 当天这一次如果因为数据源出错而跳过，当天不再补跑，等人处理。

外部数据（持仓、账户与行情输入、数据门禁、账户 ID）由 `RunnerSources` 注入。生产的提供方
（账本只读连接、行情订阅）在 B-03、B-04 之后接上；在那之前策略调度不启用，workerd 照常投递。

时间预算（check_delivery）：运行窗口 + 计算上限 + 投递间隔必须短于意图限龄，
否则窗口末尾算出的意图会一入队就过期（T-01 移交）。计算超时由 workerd 执行；
超时的线程碰不到 outbox，所以它晚些返回也不会写入任何东西。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from server.app.strategies import (
    RunReport,
    StrategyInputs,
    StrategySchedule,
    decision_time,
    run_strategies,
)
from server.data.calendar import TradingCalendar
from server.data.lake import DataLake
from server.domain.orders import OrderIntent
from server.domain.values import to_utc
from server.ipc.outbox import Outbox, OutboxConflictError
from server.storage.logs import log_event
from server.strategy.base import Strategy

log = logging.getLogger(__name__)

# 基准策略需要约 230 根日线（6 个月动量、200 日均线、60 日波动率）：按日历日留足余量
DEFAULT_SCHEDULE = StrategySchedule(
    decision_offset=timedelta(minutes=30),  # 开盘后 30 分钟：避开开盘后的剧烈波动
    run_window=timedelta(minutes=4),
    lookback_days=400,
    publish_lag=timedelta(hours=1),
)
DEFAULT_COMPUTE_TIMEOUT = timedelta(seconds=30)


class RunnerConfigError(Exception):
    """调度参数与投递参数不兼容：拒绝启用策略调度。"""


@dataclass(frozen=True)
class RunnerSources:
    """一轮运行需要的外部数据。任何一个出错都整天跳过。"""

    positions: Callable[[], Mapping[str, Decimal]]
    inputs: Callable[[], StrategyInputs]
    blocked: Callable[[], frozenset[str]]  # 数据质量门禁阻断的标的（D-02 / D-05）
    account_id: Callable[[], str]


@dataclass
class Prepared:
    """第一步的结果：收集到的意图，以及已经确定的跳过与失败。"""

    day: date
    intents: list[OrderIntent] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)


class _Collector:
    """替代 outbox 收集意图（只在第一步的线程里用）。"""

    def __init__(self) -> None:
        self.intents: list[OrderIntent] = []

    def enqueue(self, intent: OrderIntent) -> bool:
        self.intents.append(intent)
        return True


class StrategyRunner:
    def __init__(  # noqa: PLR0913 - 调度的全部依赖显式给出
        self,
        strategies: Sequence[Strategy],
        *,
        lake: DataLake,
        calendar: TradingCalendar,
        sources: RunnerSources,
        state_path: Path,
        schedule: StrategySchedule = DEFAULT_SCHEDULE,
        compute_timeout: timedelta = DEFAULT_COMPUTE_TIMEOUT,
    ) -> None:
        self._strategies = tuple(strategies)
        self._lake = lake
        self._calendar = calendar
        self._sources = sources
        self._state = state_path
        self._schedule = schedule
        self.compute_timeout = compute_timeout
        self._calendar_error: str | None = None

    def check_delivery(self, *, deliver_seconds: float, max_age: timedelta) -> None:
        window, compute = self._schedule.run_window, self.compute_timeout
        budget = window + compute + timedelta(seconds=deliver_seconds)
        if budget >= max_age:
            raise RunnerConfigError(
                f"运行窗口 {window} + 计算上限 {compute} + 投递间隔 {deliver_seconds} 秒"
                f"不短于意图限龄 {max_age}：窗口后段的意图会一入队就过期"
            )

    # ---------- 第一步：线程里计算 ----------

    def prepare(self, now: datetime) -> Prepared | None:
        moment = to_utc(now)
        day = self._due_day(moment)
        if day is None:
            return None
        last = self._last_run()
        if last is not None and last >= day:  # 含时钟回拨：记录里的日期比今天还晚也不跑
            return None
        self._mark(day)  # 先落盘再计算：写不进去就抛出，当天不运行
        try:
            account_id = self._sources.account_id()
            if not isinstance(account_id, str) or not account_id.strip():
                raise ValueError("账户 ID 为空")
            blocked = self._sources.blocked()
            collector = _Collector()
            report = run_strategies(
                self._strategies, lake=self._lake, calendar=self._calendar,
                outbox=collector,
                schedule=self._schedule, now=moment, account_id=account_id,
                positions=self._sources.positions, blocked=blocked, inputs=self._sources.inputs,
            )  # fmt: skip
        except Exception as exc:  # 数据源、日历出错：当天跳过，不补跑
            log_event(log, logging.ERROR, "策略调度出错，本日跳过", day=day.isoformat(),
                      error=type(exc).__name__)  # fmt: skip
            return Prepared(day, skipped={"*": f"调度出错：{type(exc).__name__}"})
        return Prepared(day, collector.intents, dict(report.skipped), dict(report.failed))

    def _due_day(self, moment: datetime) -> date | None:
        """现在是否在某个交易日的运行窗口内。日历出错只在出错信息变化时记一次日志。"""
        try:
            if not self._calendar.is_open(moment):
                return None
            day = self._calendar.session_day(moment)
            at = decision_time(self._calendar, day, self._schedule)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if message != self._calendar_error:
                log_event(log, logging.ERROR, "交易日历不可用，策略调度暂停", reason=message)
                self._calendar_error = message
            return None
        self._calendar_error = None
        if at is None or moment < at or moment > at + self._schedule.run_window:
            return None
        return day

    def _last_run(self) -> date | None:
        try:
            raw = json.loads(self._state.read_text(encoding="utf-8"))
            return date.fromisoformat(raw["last_run"])
        except FileNotFoundError:
            return None
        # 记录损坏：抛出（workerd 记 ERROR），不当作“没跑过”而重跑

    def _mark(self, day: date) -> None:
        self._state.parent.mkdir(parents=True, exist_ok=True)
        temp = self._state.with_name(self._state.name + ".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps({"last_run": day.isoformat()}))
            handle.flush()
            os.fsync(handle.fileno())  # 先落到磁盘再替换：断电后不会读到旧日期而重跑
        os.replace(temp, self._state)

    # ---------- 第二步：事件循环线程里入队 ----------

    def commit(self, prepared: Prepared, outbox: Outbox) -> RunReport:
        """逐条入队。outbox 本身出错时上抛（已入队的保留，入队不是整批原子的）。"""
        enqueued: list[str] = []
        duplicates: list[str] = []
        failed = dict(prepared.failed)
        stopped: set[str] = set()
        for intent in prepared.intents:
            if intent.strategy_id in stopped:
                continue
            try:
                fresh = outbox.enqueue(intent)
            except OutboxConflictError as exc:  # 同键不同内容：绝不多下一单，该策略其余意图不再入队
                failed[intent.strategy_id] = f"{intent.intent_id} 入队被拒：{exc}"
                stopped.add(intent.strategy_id)
                continue
            (enqueued if fresh else duplicates).append(intent.intent_id)
        report = RunReport(tuple(enqueued), tuple(duplicates), prepared.skipped, failed)
        log_event(log, logging.WARNING if failed else logging.INFO, "策略调度完成",
                  day=prepared.day.isoformat(), enqueued=len(enqueued),
                  duplicates=len(duplicates), skipped=dict(report.skipped),
                  failed=dict(report.failed))  # fmt: skip
        return report


__all__ = (
    "DEFAULT_COMPUTE_TIMEOUT",
    "DEFAULT_SCHEDULE",
    "Prepared",
    "RunnerConfigError",
    "RunnerSources",
    "StrategyRunner",
)
