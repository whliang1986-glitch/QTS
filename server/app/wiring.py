"""交易核心数据来源的接线（B-05w）。

把券商适配器之外的数据来源（行情、账户同步、日初净值等）接成 CoreInputs，并生成健康上报函数。

- 本模块不认识任何具体券商：行情由调用方注入，券商实例由 TradingdDeps.broker 提供。
  测试注入 FakeBroker（契约 fake-broker-test-only 禁止 server.app 依赖它）；
  生产入口的 TimeWiring.inputs 与 production_broker 仍然拒绝启动，接入真实券商须 Dylan 批准。
- 还没有真实实现的来源默认“不知道”：返回 None，或者取数时报错。风控按 fail closed 拒绝新增风险。
  这里不提供任何“默认通过”的值。
- 健康只按实际检查结果上报：没有检查的组件不上报，运行状态机按“从未报告”算故障。
  券商组件只看连接状态：这不等于券商可交易（接入 IBKR 前要补，见 docs/tasks/B-05w.md）。
- 配置错误报 ValueError。core_inputs 缺关键组件时，tradingd 拒绝启动；health_reporter 检查了
  未登记的组件时，在构造 TradingdDeps 时报错，生产入口接入时要自己转成拒绝启动（EXIT_REFUSED）。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from server.data.calendar import TradingCalendar
from server.data.corporate_actions import CorporateActionError, read_corporate_actions
from server.data.lake import DataLake
from server.domain.corporate_actions import CorporateActionKind
from server.domain.instrument import Instrument
from server.risk.assembly import CoreInputs
from server.risk.config import require_critical_components
from server.risk.context import LedgerStopCooldownSource, QuoteSource
from server.risk.engine import StopCooldown
from server.risk.mode_manager import ModeConfig
from server.risk.parameters import RiskParameters
from server.risk.reconcile import CorporateAction, CorporateActionSource
from server.risk.run_state import Component, RunStateMachine
from server.storage.logs import log_event

log = logging.getLogger(__name__)
BROKER_COMPONENT = "broker"


def _account_unknown() -> bool | None:
    return None


async def _skew_unknown() -> int | None:
    return None


def _nav_unknown(_account_id: str) -> Decimal | None:
    return None


def _drawdown_unknown() -> Mapping[str, Decimal] | None:
    return None


def _cooldown_unknown() -> Mapping[str, StopCooldown] | None:
    return None


class UnavailableCorporateActions:
    """公司行动来源还没接入：取数报错。

    对账只在持仓对不上、需要公司行动来解释时才取数；那时报错会让对账不干净，进入 SAFE HALT。
    持仓一致时不取数，也就不受影响。
    """

    async def actions(self, symbols: Collection[str]) -> Sequence[CorporateAction]:
        del symbols
        raise RuntimeError("公司行动数据源尚未接入")


@dataclass(frozen=True)
class LakeCorporateActionSource:
    """从数据湖读取拆股/合股公司行动（B-05y，接 D-04 与 R-07）。

    除权日必须是交易日，生效时刻取该日开盘时刻；不可读或除权日非法时直接报错（fail closed）。
    """

    lake: DataLake
    calendar: TradingCalendar
    start: date
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    async def actions(self, symbols: Collection[str]) -> Sequence[CorporateAction]:
        if not symbols:
            return ()
        today = self.calendar.session_day(self.clock())
        if today < self.start:
            raise CorporateActionError("当前交易日早于公司行动查询起点")
        records = read_corporate_actions(self.lake, symbols, self.start, today)
        out: list[CorporateAction] = []
        for record in records:
            if record.kind is not CorporateActionKind.SPLIT:
                continue
            if record.numerator is None or record.denominator is None:
                raise CorporateActionError(f"拆股事件 {record.event_id} 缺少比例")
            session = self.calendar.session(record.ex_date)
            if session is None:
                raise CorporateActionError(f"拆股事件 {record.event_id} 的除权日不是交易日")
            out.append(
                CorporateAction(
                    event_id=record.event_id,
                    symbol=record.symbol,
                    numerator=record.numerator,
                    denominator=record.denominator,
                    effective_at=session.open,
                )
            )
        return tuple(out)


class DailyNavBook:
    """按（账户，交易日）记录并提供当日开盘净值（B-05y，接 R-02b DailyLossRule）。

    未登记当日开盘净值时返回 None（DailyLossRule 拒绝新增风险）；同一天重复登记不同值时报错拒绝。
    """

    def __init__(
        self,
        session_day: Callable[[datetime], date],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_day = session_day
        self._clock = clock
        self._navs: dict[tuple[str, date], Decimal] = {}

    def record(self, account_id: str, day: date, nav: Decimal) -> None:
        if isinstance(day, datetime) or not isinstance(day, date):
            raise ValueError("day 必须是 date")
        if not isinstance(nav, Decimal) or not nav.is_finite() or nav <= 0:
            raise ValueError("开盘净值必须是大于 0 的有限 Decimal")
        key = (account_id, day)
        existing = self._navs.get(key)
        if existing is not None and existing != nav:
            raise ValueError(f"{account_id} 在 {day} 的开盘净值已登记，禁止覆盖为不同值")
        self._navs[key] = nav

    def __call__(self, account_id: str) -> Decimal | None:
        try:
            day = self._session_day(self._clock())
            return self._navs.get((account_id, day))
        except Exception as exc:
            log_event(log, logging.ERROR, "读取当日开盘净值失败", error=type(exc).__name__)
            return None


class StrategyDrawdownBook:
    """按策略追踪峰值与当前净值，计算 [0, 1] 回撤比例（B-05y，接 R-02b StrategyDrawdownRule）。

    超过 stale_after 未更新或时钟回拨的策略不出现在返回映射中，由 StrategyDrawdownRule 按缺失拒绝。
    """

    def __init__(
        self,
        *,
        stale_after: timedelta = timedelta(minutes=5),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if stale_after <= timedelta(0):
            raise ValueError("stale_after 必须大于 0")
        self._stale_after = stale_after
        self._clock = clock
        self._entries: dict[str, tuple[Decimal, Decimal, datetime]] = {}

    def record(self, strategy_id: str, nav: Decimal, *, at: datetime | None = None) -> None:
        if not strategy_id or not isinstance(nav, Decimal) or not nav.is_finite() or nav <= 0:
            raise ValueError("策略净值必须是大于 0 的有限 Decimal")
        ts = self._clock() if at is None else at
        if ts.tzinfo is None or ts.utcoffset() is None:
            raise ValueError("策略净值时间戳必须带时区")
        prev = self._entries.get(strategy_id)
        peak = nav if prev is None or nav > prev[0] else prev[0]
        self._entries[strategy_id] = (peak, nav, ts)

    def __call__(self) -> Mapping[str, Decimal] | None:
        try:
            now = self._clock()
            out: dict[str, Decimal] = {}
            for sid, (peak, nav, ts) in sorted(self._entries.items()):
                if ts > now or now - ts > self._stale_after or peak <= 0:
                    continue
                out[sid] = (peak - nav) / peak
            return out
        except Exception as exc:
            log_event(log, logging.ERROR, "计算策略回撤失败", error=type(exc).__name__)
            return None


class AccountSyncMonitor:
    """账户同步状态的单一事实来源（B-05y）：同时供给 Feeds.account_synced 与 health_reporter。

    只有券商已连接、最近一次对账通过（reconciled 为 True）且同步标记未过期时才返回 True；
    从未同步或读取异常返回 None，断开/未对账/过期返回 False。
    """

    def __init__(
        self,
        broker: Callable[[], Any],
        reconciled: Callable[[], bool],
        *,
        stale_after: timedelta = timedelta(seconds=30),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if stale_after <= timedelta(0):
            raise ValueError("stale_after 必须大于 0")
        self._broker = broker
        self._reconciled = reconciled
        self._stale_after = stale_after
        self._clock = clock
        self._synced_at: datetime | None = None

    def mark_synced(self, at: datetime | None = None) -> None:
        ts = self._clock() if at is None else at
        if ts.tzinfo is None or ts.utcoffset() is None:
            raise ValueError("账户同步时间戳必须带时区")
        self._synced_at = ts

    def is_synced(self) -> bool | None:
        try:
            if self._synced_at is None:
                return None
            now = self._clock()
            if self._synced_at > now or now - self._synced_at > self._stale_after:
                return False
            return bool(self._broker().connected is True and self._reconciled() is True)
        except Exception as exc:
            log_event(log, logging.ERROR, "检查账户同步状态出错", error=type(exc).__name__)
            return None

    def check(self) -> bool:
        """给 health_reporter 的检查函数（同一事实来源）。"""
        return self.is_synced() is True


def clock_skew_probe(
    reference_clock: Callable[[], Awaitable[datetime]],
    *,
    local_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    max_rtt: timedelta = timedelta(seconds=2),
) -> Callable[[], Awaitable[int | None]]:
    """构造时钟偏差探针（毫秒，B-05y）。超时、时钟回拨或无时区返回 None（fail closed）。"""
    if max_rtt <= timedelta(0):
        raise ValueError("max_rtt 必须大于 0")

    async def probe() -> int | None:
        try:
            t0 = local_clock()
            ref = await reference_clock()
            t1 = local_clock()
            for ts in (t0, ref, t1):
                if ts.tzinfo is None or ts.utcoffset() is None:
                    return None
            rtt = t1 - t0
            if rtt < timedelta(0) or rtt > max_rtt:
                return None
            midpoint = t0 + rtt / 2
            return int(abs((ref - midpoint).total_seconds()) * 1000)
        except Exception as exc:
            log_event(log, logging.WARNING, "时钟偏差探测失败", error=type(exc).__name__)
            return None

    return probe


@dataclass(frozen=True)
class Feeds:
    """交易核心的数据来源。前五项必须显式提供，其余默认“不知道”。"""

    mode_config: ModeConfig
    instruments: Mapping[str, Instrument]
    components: Collection[Component]
    parameters: Callable[[], RiskParameters | None]
    quotes: QuoteSource
    account_synced: Callable[[], bool | None] = _account_unknown
    clock_skew_ms: Callable[[], Awaitable[int | None]] = _skew_unknown
    start_of_day_nav: Callable[[str], Decimal | None] = _nav_unknown
    strategy_drawdown: Callable[[], Mapping[str, Decimal] | None] = _drawdown_unknown
    stop_cooldown: Callable[[], Mapping[str, StopCooldown] | None] = _cooldown_unknown
    corporate_actions: CorporateActionSource = field(default_factory=UnavailableCorporateActions)
    degraded_allowed: Mapping[str, frozenset[str]] = field(default_factory=dict)


def core_inputs(
    feeds: Feeds,
    *,
    broker_window: Callable[[datetime], bool],
    session_day: Callable[[datetime], date],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CoreInputs:
    """组装 CoreInputs。broker_window、session_day 必须与调度器同源（见 TimeWiring.wire）。

    组件清单为空或缺关键组件时，运行状态门禁等于没有：拒绝组装（与 parse_health_config 同一规则）。
    """
    require_critical_components(feeds.components)
    return CoreInputs(
        mode_config=feeds.mode_config, instruments=feeds.instruments,
        components=feeds.components, parameters=feeds.parameters, quotes=feeds.quotes,
        account_synced=feeds.account_synced, clock_skew_ms=feeds.clock_skew_ms,
        start_of_day_nav=feeds.start_of_day_nav, strategy_drawdown=feeds.strategy_drawdown,
        stop_cooldown=feeds.stop_cooldown, corporate_actions=feeds.corporate_actions,
        broker_window=broker_window, degraded_allowed=feeds.degraded_allowed,
        session_day=session_day, clock=clock,
    )  # fmt: skip


def _passes(name: str, check: Callable[[], bool]) -> bool:
    """只有检查明确返回 True 才算健康；检查出错、返回其他值都算故障。"""
    try:
        return check() is True
    except Exception as exc:
        log_event(log, logging.ERROR, "健康检查出错", component=name, error=type(exc).__name__)
        return False


def health_reporter(
    checks: Mapping[str, Callable[[], bool]],
    components: Collection[Component],
) -> Callable[[RunStateMachine, Any], None]:
    """生成 TradingdDeps.report_health。券商组件固定看 broker.connected，其余看 checks。"""
    if BROKER_COMPONENT in checks:
        raise ValueError("broker 组件固定看券商连接状态，不接受自定义检查")
    unknown = sorted({*checks, BROKER_COMPONENT} - {c.name for c in components})
    if unknown:  # 不提前拦下，运行状态机会在上报时报错，tradingd 反复崩溃重启
        raise ValueError(f"这些组件没有登记，不能上报健康：{'、'.join(unknown)}")

    def report(run_state: RunStateMachine, broker: Any) -> None:
        run_state.report(BROKER_COMPONENT, ok=_passes(BROKER_COMPONENT, lambda: broker.connected))
        for name, check in checks.items():
            run_state.report(name, ok=_passes(name, check))

    return report


__all__ = (
    "BROKER_COMPONENT",
    "AccountSyncMonitor",
    "DailyNavBook",
    "Feeds",
    "LakeCorporateActionSource",
    "LedgerStopCooldownSource",
    "StrategyDrawdownBook",
    "UnavailableCorporateActions",
    "clock_skew_probe",
    "core_inputs",
    "health_reporter",
)
