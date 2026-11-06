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
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from server.domain.instrument import Instrument
from server.risk.assembly import CoreInputs
from server.risk.config import REQUIRED_CRITICAL
from server.risk.context import QuoteSource
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
    missing = sorted(REQUIRED_CRITICAL - {c.name for c in feeds.components if c.critical})
    if missing:
        raise ValueError(f"组件清单必须把这些组件登记为关键：{'、'.join(missing)}")
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
    "Feeds",
    "UnavailableCorporateActions",
    "core_inputs",
    "health_reporter",
)
