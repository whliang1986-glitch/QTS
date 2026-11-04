"""workerd 里的策略调度（T-01 移交）：按交易日历在决策时刻运行策略，意图放进 outbox。

- 每一轮由 workerd 调用一次 `StrategyRunner.run(now, outbox)`；
  不在运行窗口内时直接返回，不读任何数据。
- 运行窗口内，同一个交易日只认真跑一次：第一轮跑完（不论结果）就记下日期，之后的轮次跳过。
  这样避免第一轮的卖单在窗口内成交后，重跑当天就提议买入（T-02 评审记录的“同日重跑”问题）。
  进程重启后可能再跑一次：意图的生成时间与输入都按决策时刻固定，同键同内容由 outbox 去重，
  同键不同内容由 outbox 拒绝，绝不会多下一单。
- 持仓、账户与行情输入来自可注入的提供方。生产的提供方（账本只读连接、行情订阅）
  在 B-03、B-04 之后接上；在那之前没有提供方，策略调度整体不启用（workerd 照常投递）。
  提供方出错时这一轮跳过，不拿缺省值硬跑；outbox 本身出错时上抛，由 workerd 记录。
- 运行窗口加投递间隔必须短于意图限龄，否则窗口后段生成的意图一入队就过期（T-01 移交）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from server.app.strategies import (
    RunReport,
    StrategyInputs,
    StrategySchedule,
    decision_time,
    run_strategies,
)
from server.data.calendar import TradingCalendar
from server.data.lake import DataLake
from server.domain.values import to_utc
from server.ipc.outbox import Outbox, OutboxError
from server.storage.logs import log_event
from server.strategy.base import Strategy

log = logging.getLogger(__name__)

# 基准策略需要约 230 根日线（6 个月动量、200 日均线、60 日波动率）：按日历日留足余量
DEFAULT_SCHEDULE = StrategySchedule(
    decision_offset=timedelta(minutes=30),  # 开盘后 30 分钟：避开开盘集合竞价后的剧烈波动
    run_window=timedelta(minutes=4),
    lookback_days=400,
    publish_lag=timedelta(hours=1),
)


class RunnerConfigError(Exception):
    """调度参数与投递参数不兼容：拒绝启用策略调度。"""


@dataclass(frozen=True)
class RunnerSources:
    """一轮运行需要的外部数据。任何一个出错都整轮跳过。"""

    positions: Callable[[], Mapping[str, Decimal]]
    inputs: Callable[[], StrategyInputs]
    blocked: Callable[[], frozenset[str]]  # 数据质量门禁阻断的标的（D-02 / D-05）
    account_id: Callable[[], str]


class StrategyRunner:
    def __init__(
        self,
        strategies: Sequence[Strategy],
        *,
        lake: DataLake,
        calendar: TradingCalendar,
        sources: RunnerSources,
        schedule: StrategySchedule = DEFAULT_SCHEDULE,
    ) -> None:
        self._strategies = tuple(strategies)
        self._lake = lake
        self._calendar = calendar
        self._sources = sources
        self._schedule = schedule
        self._done: date | None = None

    def check_delivery(self, *, deliver_seconds: float, max_age: timedelta) -> None:
        """运行窗口加投递间隔必须短于意图限龄。"""
        if self._schedule.run_window + timedelta(seconds=deliver_seconds) >= max_age:
            raise RunnerConfigError(
                f"运行窗口 {self._schedule.run_window} 加投递间隔 {deliver_seconds} 秒"
                f"不短于意图限龄 {max_age}：窗口后段生成的意图会一入队就过期"
            )

    def run(self, now: datetime, outbox: Outbox) -> RunReport | None:
        """不在运行窗口内、或当天已经跑过时返回 None；否则跑一次并返回结果。"""
        moment = to_utc(now)
        if not self._calendar.is_open(moment):
            return None
        day = self._calendar.session_day(moment)
        if day == self._done:
            return None
        at = decision_time(self._calendar, day, self._schedule)
        if at is None or moment < at or moment > at + self._schedule.run_window:
            return None
        self._done = day  # 先记下：即使本轮出错，当天也不再重跑（失败即关闭，等人处理）
        try:
            account_id = self._sources.account_id()
            blocked = self._sources.blocked()
        except Exception as exc:
            log_event(log, logging.ERROR, "策略调度读不到账户或数据门禁，本日跳过",
                      error=type(exc).__name__)  # fmt: skip
            return RunReport(skipped={"*": f"读不到账户或数据门禁：{type(exc).__name__}"})
        try:
            report = run_strategies(
                self._strategies, lake=self._lake, calendar=self._calendar, outbox=outbox,
                schedule=self._schedule, now=moment, account_id=account_id,
                positions=self._sources.positions, blocked=blocked, inputs=self._sources.inputs,
            )  # fmt: skip
        except OutboxError:
            raise
        except Exception as exc:  # 持仓读不到、日历不覆盖等：本日跳过
            log_event(log, logging.ERROR, "策略调度出错，本日跳过", error=type(exc).__name__)
            return RunReport(skipped={"*": f"调度出错：{type(exc).__name__}"})
        log_event(log, logging.WARNING if report.failed else logging.INFO, "策略调度完成",
                  day=day.isoformat(), enqueued=len(report.enqueued),
                  duplicates=len(report.duplicates), skipped=dict(report.skipped),
                  failed=dict(report.failed))  # fmt: skip
        return report


__all__ = ("DEFAULT_SCHEDULE", "RunnerConfigError", "RunnerSources", "StrategyRunner")
