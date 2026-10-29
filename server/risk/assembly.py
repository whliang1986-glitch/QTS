"""tradingd 交易核心的装配函数（S-01c）。

装配层 server/app 只调这里，不自己构造 OMS（契约 app-no-direct-oms）。固定接线（与
tests/integration/risk/test_core.py 的 Rig 一致）：ModeManager → OrderManager → 风控上下文 → 对账
→ 运行状态机 → 风控引擎（接运行状态门禁）→ 急停 → TradingCore。

- OMS 报“订单状态无法查证”、风控上下文报“状态不可信”都直接持久化 SAFE HALT（V4.1 7.4）。
  这两个回调不拿交易核心的锁（S-01a 移交：asyncio 锁不可重入）；
  写不进去时 ModeManager 已闩锁为拒绝。
- broker 参数按“任意对象”接收：risk 不得导入 broker 包（依赖规则），由 OrderManager 在运行时使用；
  装配层传进来的是 server.broker 的适配器实例。
- 本模块不导出 OrderManager：装配层拿到的是 TradingCore 与只读用的组件句柄。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from server.domain.instrument import Instrument
from server.oms.manager import OrderManager
from server.risk.context import LedgerContextProvider, QuoteSource
from server.risk.core import TradingCore
from server.risk.emergency import EmergencyControls
from server.risk.engine import RiskEngine, StopCooldown
from server.risk.mode_manager import ModeConfig, ModeManager
from server.risk.parameters import RiskParameters
from server.risk.reconcile import CorporateActionSource, Reconciler
from server.risk.rulesets import build_rule_set
from server.risk.run_state import Component, RunStateMachine
from server.storage.ledger import LedgerRepository
from server.storage.logs import log_event

log = logging.getLogger(__name__)
ACTOR = "tradingd"
# tradingd 入口的意图限龄（S-01b 评审 P1-4 / S-01c 评审 P1-3）：与 workerd 的 outbox 限龄一致。
# 只会更严、不会更松：放宽属于风控限值，须 Dylan 本人决定。
INTENT_MAX_AGE = timedelta(minutes=5)


@dataclass(frozen=True)
class CoreInputs:
    """交易核心依赖的外部数据来源（行情、账户同步、日历等由各自任务提供，装配层注入）。"""

    mode_config: ModeConfig
    instruments: Mapping[str, Instrument]
    components: Collection[Component]
    parameters: Callable[[], RiskParameters | None]
    quotes: QuoteSource
    account_synced: Callable[[], bool | None]
    clock_skew_ms: Callable[[], Awaitable[int | None]]
    start_of_day_nav: Callable[[str], Decimal | None]
    strategy_drawdown: Callable[[], Mapping[str, Decimal] | None]
    stop_cooldown: Callable[[], Mapping[str, StopCooldown] | None]
    corporate_actions: CorporateActionSource
    broker_window: Callable[[datetime], bool]
    degraded_allowed: Mapping[str, frozenset[str]]
    session_day: Callable[[datetime], date] | None = None  # 交易日历：当日意图跨时段作废
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


@dataclass(frozen=True)
class Assembled:
    core: TradingCore
    modes: ModeManager
    run_state: RunStateMachine
    reconciler: Reconciler


def build_trading_core(repo: LedgerRepository, broker: Any, inputs: CoreInputs) -> Assembled:
    """按固定顺序接好交易核心。broker：已构造（可未连接）的券商适配器。"""
    clock = inputs.clock
    modes = ModeManager(repo, clock=clock)

    def halt(reason: str) -> None:
        try:
            modes.safe_halt(reason, actor=ACTOR)
        except Exception as exc:  # 写不进去：ModeManager 已闩锁拒绝；只记日志
            log_event(log, logging.ERROR, "SAFE HALT 写入失败", error=type(exc).__name__)

    def rule_set() -> Any:
        params = inputs.parameters()
        return None if params is None else build_rule_set(params)

    om = OrderManager(
        repo, broker, instruments=inputs.instruments, clock=clock,
        on_unresolved=lambda intent_id: halt(f"订单 {intent_id} 状态无法查证"),
    )  # fmt: skip
    provider = LedgerContextProvider(
        repo, om, account=broker, broker_orders=broker, quotes=inputs.quotes,
        instruments=inputs.instruments, account_synced=inputs.account_synced,
        clock_skew_ms=inputs.clock_skew_ms, start_of_day_nav=inputs.start_of_day_nav,
        strategy_drawdown=inputs.strategy_drawdown, stop_cooldown=inputs.stop_cooldown,
        delayed_data_allowed=modes.delayed_data_allowed, on_untrusted=halt, clock=clock,
    )  # fmt: skip
    reconciler = Reconciler(
        repo, om, modes, account=broker, broker_orders=broker, executions=broker,
        corporate_actions=inputs.corporate_actions, clock=clock,
    )  # fmt: skip
    run_state = RunStateMachine(
        repo, om, modes, components=inputs.components, rule_set=rule_set,
        reconciled=reconciler.last_clean, broker_window=inputs.broker_window,
        degraded_allowed=inputs.degraded_allowed, clock=clock,
    )  # fmt: skip
    engine = RiskEngine(
        repo, om, modes, rule_set=rule_set, context=provider, clock=clock,
        run_gate=run_state.gate,
    )  # fmt: skip
    emergency = EmergencyControls(
        repo, om, modes, engine, context=provider, parameters=inputs.parameters, clock=clock
    )
    core = TradingCore(
        repo, om, modes, mode_config=inputs.mode_config, broker_account=broker.account,
        engine=engine, reconciler=reconciler, run_state=run_state, emergency=emergency,
        context=provider, clock=clock, intent_max_age=INTENT_MAX_AGE,
        session_day=inputs.session_day,
    )  # fmt: skip
    return Assembled(core, modes, run_state, reconciler)


__all__ = ("ACTOR", "INTENT_MAX_AGE", "Assembled", "CoreInputs", "build_trading_core")
