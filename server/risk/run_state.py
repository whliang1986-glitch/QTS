"""运行状态机与健康检查（R-08，V4.1 7.4 / 7.5）。

三种运行状态：
- NORMAL：关键组件健康、数据与账户同步可信，允许已授权策略运行；
- DEGRADED：非关键组件故障（模型、某个数据源等），或处于券商重启窗口：
  - 券商窗口内一律不新增订单（包括降风险单：券商此时不接单）；
  - 非关键组件故障时，只允许“不依赖故障组件、且明确获准在降级下运行”的策略；
    降风险单（OMS 保护性止损、紧急平仓）不受组件故障影响；
- SAFE HALT：账户、订单、风险或关键数据状态不可信：停止新增风险，持久化（ModeManager.safe_halt），
  不因组件恢复而自动解除，只能人工经 resume 恢复。

健康检查：每个组件登记“关键 / 非关键”与报告时效；组件自己报告健康状况（report），
超过时效没有报告即视为故障（失败即关闭）。关键组件故障 → 持久化 SAFE HALT（同一组故障只写一次）。

恢复门槛（resume）：数据、账户、订单、风控、数据库、对账六项全部通过，
且由人工发起（ModeManager 校验）：
- data_ok：全部数据类组件健康；account_ok：账户同步组件健康；
- orders_ok：没有 SUBMITTING / UNKNOWN 订单，也没有结果未确认的改单；
- risk_ok：风控规则集可用；database_ok：账本完整性核对通过；reconciled：本实例最近一次对账通过。
每项检查自身出错一律记为不通过。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from server.domain.order_state import TERMINAL, OrderStatus
from server.oms.manager import OrderManager
from server.risk.engine import RuleSet, Subject, reduces_risk
from server.risk.mode_manager import ModeManager, ResumeChecks
from server.storage.ledger import LedgerRepository, ModeChange
from server.storage.logs import log_event

log = logging.getLogger(__name__)

UNRESOLVED = frozenset({OrderStatus.SUBMITTING, OrderStatus.UNKNOWN})


class RunState(StrEnum):
    NORMAL = "NORMAL"
    DEGRADED = "DEGRADED"
    SAFE_HALT = "SAFE_HALT"


class Kind(StrEnum):
    DATA = "DATA"  # 行情、数据源
    ACCOUNT = "ACCOUNT"  # 券商连接与账户同步
    OTHER = "OTHER"  # 模型等


@dataclass(frozen=True)
class Component:
    name: str
    kind: Kind
    critical: bool
    stale_after: timedelta  # 超过这么久没有报告即视为故障


@dataclass(frozen=True)
class Health:
    ok: bool
    detail: str
    reported_at: datetime


@dataclass(frozen=True)
class Assessment:
    state: RunState
    failing: tuple[str, ...]  # 故障组件（含报告过期）
    in_broker_window: bool
    reason: str


class RunStateError(Exception):
    """运行状态相关操作被拒绝。"""


class RunStateMachine:
    def __init__(  # noqa: PLR0913 - 依赖全部显式注入
        self,
        repo: LedgerRepository,
        oms: OrderManager,
        modes: ModeManager,
        *,
        components: Collection[Component],
        rule_set: Callable[[], RuleSet | None],
        reconciled: Callable[[], bool],
        broker_window: Callable[[datetime], bool],
        degraded_allowed: Mapping[str, frozenset[str]],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        actor: str = "tradingd",
    ) -> None:
        """degraded_allowed：策略 → 它依赖的组件名；只有登记在这里的策略才可能在降级下运行。"""
        names = [c.name for c in components]
        if len(names) != len(set(names)):
            raise ValueError("组件名重复")
        unknown = {d for deps in degraded_allowed.values() for d in deps} - set(names)
        if unknown:
            raise ValueError(f"降级许可引用了未登记的组件：{sorted(unknown)}")
        self._repo = repo
        self._oms = oms
        self._modes = modes
        self._components = {c.name: c for c in components}
        self._rule_set = rule_set
        self._reconciled = reconciled
        self._broker_window = broker_window
        self._degraded_allowed = dict(degraded_allowed)
        self._clock = clock
        self._actor = actor
        self._health: dict[str, Health] = {}
        self._halted_for: frozenset[str] = frozenset()  # 已为哪组关键故障写过 SAFE HALT
        self._last_state: RunState | None = None

    # ---------- 健康报告 ----------

    def report(self, component: str, *, ok: bool, detail: str = "") -> None:
        if component not in self._components:
            raise RunStateError(f"未登记的组件：{component}")
        if not isinstance(ok, bool):
            raise RunStateError("健康状况只接受布尔值")
        self._health[component] = Health(ok=ok, detail=detail, reported_at=self._clock())

    def _failing(self, now: datetime) -> list[str]:
        """故障组件：从未报告、报告不健康、报告过期，或报告时间在未来（时钟回拨，不可信）。"""
        failing = []
        for name, component in sorted(self._components.items()):
            health = self._health.get(name)
            if (
                health is None
                or not health.ok
                or health.reported_at > now
                or now - health.reported_at > component.stale_after
            ):
                failing.append(name)
        return failing

    # ---------- 评估 ----------

    def assess(self) -> Assessment:
        """计算当前运行状态；关键组件故障时持久化 SAFE HALT（同一组故障只写一次）。"""
        now = self._clock()
        try:
            failing = self._failing(now)
            window = self._broker_window(now)
            if not isinstance(window, bool):  # 窗口判断给不出确定答案：按不可信处理
                raise RunStateError("券商窗口判断没有给出布尔值")
            safe = self._modes_untrusted()
        except Exception as exc:  # 状态本身算不出来：按不可信处理
            reason = f"运行状态无法评估：{type(exc).__name__}"
            assessment = Assessment(RunState.SAFE_HALT, (), False, reason)
            self._transition(assessment)
            return assessment
        critical = [n for n in failing if self._components[n].critical]
        if critical:
            reason = f"关键组件故障：{'、'.join(critical)}"
            key = frozenset(critical)
            # 出现新的关键故障，或账本当前不在 SAFE HALT（例如被别的入口恢复过）：持久化 SAFE HALT
            if key - self._halted_for or not safe:
                self._modes.safe_halt(reason, actor=self._actor)
            self._halted_for |= key
            assessment = Assessment(RunState.SAFE_HALT, tuple(failing), window, reason)
        elif safe:
            assessment = Assessment(RunState.SAFE_HALT, tuple(failing), window, "处于 SAFE HALT")
        elif failing or window:
            parts = (["券商重启窗口"] if window else []) + [f"组件故障：{n}" for n in failing]
            assessment = Assessment(RunState.DEGRADED, tuple(failing), window, "；".join(parts))
        else:
            self._halted_for = frozenset()  # 全部健康：之后再出现的关键故障会重新写 SAFE HALT
            assessment = Assessment(RunState.NORMAL, (), False, "")
        self._transition(assessment)
        return assessment

    def _modes_untrusted(self) -> bool:
        """处于 HALT 且降风险通道关闭，即 SAFE HALT（ModeManager 的持久化判定）。"""
        state = self._modes.current()
        return state is not None and state.halt.halted and not self._modes.risk_reducing_allowed()

    def _transition(self, assessment: Assessment) -> None:
        if assessment.state is not self._last_state:
            level = logging.INFO if assessment.state is RunState.NORMAL else logging.WARNING
            log_event(
                log, level, "运行状态变化", state=assessment.state.value, reason=assessment.reason
            )
            self._last_state = assessment.state

    # ---------- 风控门禁（交给 RiskEngine 的 run_gate） ----------

    def gate(self, subject: Subject) -> str | None:
        """返回拒绝原因；None 表示运行状态允许本单。SAFE HALT 同时由 ModeManager 的模式门拦截。"""
        assessment = self.assess()
        if assessment.state is RunState.SAFE_HALT:
            return assessment.reason or "处于 SAFE HALT"
        if assessment.state is RunState.NORMAL:
            return None
        if assessment.in_broker_window:
            return "券商重启窗口内不新增订单"
        return None if reduces_risk(subject) else self._degraded_reason(subject, assessment)

    def _degraded_reason(self, subject: Subject, assessment: Assessment) -> str | None:
        strategy = subject.intent.strategy_id
        dependencies = self._degraded_allowed.get(strategy)
        if dependencies is None:
            return f"降级运行中，策略 {strategy} 未获准在降级下运行"
        broken = sorted(set(dependencies) & set(assessment.failing))
        if broken:
            return f"降级运行中，策略 {strategy} 依赖的组件故障：{'、'.join(broken)}"
        return None

    # ---------- 恢复 ----------

    def resume_checks(self) -> ResumeChecks:
        now = self._clock()
        failing = set(self._safe(lambda: self._failing(now), default=list(self._components)))
        return ResumeChecks(
            # 数据类组件健康，且没有任何关键组件故障（OTHER 类也算，否则恢复后立刻又 SAFE HALT）
            data_ok=not (self._of_kind(Kind.DATA) | self._critical_other()) & failing,
            account_ok=not self._of_kind(Kind.ACCOUNT) & failing,
            orders_ok=self._safe(self._orders_settled, default=False),
            risk_ok=self._safe(self._rules_ready, default=False),
            database_ok=self._safe(self._database_intact, default=False),
            reconciled=self._safe(self._reconciled, default=False) is True,
        )

    def resume(self, *, actor: str) -> ModeChange:
        """人工恢复交易：六项检查全部通过才放行（ModeManager 复核人工身份、目标模式与 G7 门禁）。"""
        target = self._modes.configured_mode()
        if target is None:
            raise RunStateError("本进程尚未完成启动校验，不能恢复")
        change = self._modes.resume(target, actor=actor, checks=self.resume_checks())
        self._halted_for = frozenset()
        return change

    def _critical_other(self) -> set[str]:
        return {n for n, c in self._components.items() if c.critical and c.kind is Kind.OTHER}

    def _of_kind(self, kind: Kind) -> set[str]:
        return {n for n, c in self._components.items() if c.kind is kind}

    def _orders_settled(self) -> bool:
        for intent_id in self._repo.submitted_intents():
            status = self._oms.status(intent_id)
            if status in UNRESOLVED:
                return False
            if status not in TERMINAL and any(
                a.result in {None, "UNKNOWN"} for a in self._repo.amendments_for(intent_id)
            ):
                return False
        return True

    def _rules_ready(self) -> bool:
        rules = self._rule_set()
        return rules is not None and bool(rules.rules)

    def _database_intact(self) -> bool:
        self._repo.check_integrity()
        return True

    @staticmethod
    def _safe[T](check: Callable[[], T], *, default: T) -> T:
        try:
            return check()
        except Exception as exc:
            log_event(log, logging.WARNING, "恢复检查出错", error=type(exc).__name__)
            return default


__all__ = (
    "Assessment",
    "Component",
    "Health",
    "Kind",
    "RunState",
    "RunStateError",
    "RunStateMachine",
)
