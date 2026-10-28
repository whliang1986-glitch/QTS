"""预交易风控引擎（R-01，V4.1 7.1–7.2）：唯一能把意图交给 OMS 的入口。

流程：模式门禁 → 规则集是否就绪 → 采集上下文（超时即拒绝）→ 逐条规则 → 汇总四种结论 → 决定落账 →
ALLOW / REDUCE 才调用 OMS。任何一步不确定都不放行（失败即关闭）：

- 模式不允许交易（HALT、未完成启动校验等）→ REJECT（命中 mode_gate）；
- 没有规则集（受保护参数缺失或不合法，R-02）→ REJECT（rule_set_missing）；
- 上下文采集失败或超时、规则抛异常 → REJECT（context_unavailable / risk_timeout / rule_error）；
- 任一规则给 HALT → HALT，并经 ModeManager 持久化停止；任一 REJECT → REJECT；
- REDUCE 取各规则允许的最小数量；只缩数量，不改方向、类别与价格（不能把反向开仓伪装成减仓）。

每个决定都绑定评估对象（server/oms/binding.py）：新单绑定意图内容，
改单绑定 amendment_id、新价格与新数量；OMS 只消费绑定一致的决定。
改单不做缩量（REDUCE 一律按 REJECT 处理，由调用方按允许数量重新发起改单）。

阈值规则在 R-02、数据门禁在 R-03、默认禁用与保护性止损豁免在 R-04 中作为规则加入；本模块只定框架。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import ClassVar, Protocol

from server.domain.account import CashBalance, Position
from server.domain.enums import OrderClass, RiskVerdict, Side
from server.domain.instrument import Instrument
from server.domain.market import Quote
from server.domain.order_state import OrderStatus
from server.domain.orders import (
    EMERGENCY_FLATTEN_SOURCE,
    PROTECTIVE_STOP_SOURCE,
    OrderIntent,
    RiskDecision,
    RuleHit,
)
from server.domain.values import (
    Identifier,
    Money,
    PositiveQuantity,
    Price,
    Quantity,
    UtcDatetime,
    ValueModel,
)
from server.oms.binding import amendment_digest, intent_digest
from server.oms.manager import OrderManager
from server.risk.mode_manager import ModeManager, TradingHaltedError
from server.storage.errors import ConflictError
from server.storage.ledger import LedgerRepository
from server.storage.logs import log_event

log = logging.getLogger(__name__)

V = RiskVerdict
ENGINE_RULE_VERSION = "1"  # 引擎内置检查（模式门禁、规则集、上下文、超时、规则异常）的版本
SEVERITY = {V.ALLOW: 0, V.REDUCE: 1, V.REJECT: 2, V.HALT: 3}


class Subject(ValueModel):
    """被评估的订单：新单即意图本身；改单是意图加上新价格与新数量。"""

    evolvable_fields: ClassVar[frozenset[str] | None] = frozenset()

    intent: OrderIntent
    amendment_id: Identifier | None = None
    price: Price
    quantity: PositiveQuantity

    @classmethod
    def new_order(cls, intent: OrderIntent) -> Subject:
        price = intent.limit_price if intent.limit_price is not None else intent.stop_price
        if price is None:  # pragma: no cover - OrderIntent 保证限价或止损价之一
            raise ValueError("意图没有价格")
        return cls(intent=intent, price=price, quantity=intent.quantity)

    def digest(self) -> str:
        if self.amendment_id is None:
            return intent_digest(self.intent)
        return amendment_digest(
            self.intent,
            amendment_id=self.amendment_id,
            new_price=self.price,
            new_quantity=self.quantity,
        )


class OpenOrder(ValueModel):
    """评估时仍在券商侧未结束的本系统订单（R-04 起用于防做空、防加杠杆与止损计数）。"""

    intent_id: Identifier
    symbol: Identifier
    side: Side
    order_class: OrderClass
    remaining_quantity: Quantity
    limit_price: Price | None = None


class StopCooldown(ValueModel):
    """某标的保护性止损成交后的冷却信息（由上下文提供者按交易日历计算）。"""

    trading_days_since_stop: int
    rebalanced_since_stop: bool  # 止损之后是否已经过了一个再平衡日


class RiskContext(ValueModel):
    """规则评估所需的快照。缺项为 None：用到它的规则必须拒绝（失败即关闭），不得当成 0。

    - positions：券商持仓（负持仓不当作可卖数量，见 domain.account.Position）；
    - open_orders：本系统已签发、尚未确认终态的全部订单（含 SUBMITTING / UNKNOWN 与撤单中）；
    - cash：已结算 / 未结算现金（未结算为负表示买入应付款）；instruments：标的规格；
    - last_prices：最新价（R-03 加新鲜度门禁）；
    - nav / start_of_day_nav：账户净值与当日开盘净值（美元，R-02b 限额与日内亏损）；
    - strategy_drawdown：各策略当前回撤（比例）；
    - recent_submissions：近期本系统签发时间（下单频率）；
    - stop_cooldown：发生过保护性止损成交的标的 → 冷却信息（止损后冷却期）；
    - quotes：各标的的最新报价（R-03 数据门禁核对新鲜度、价差、质量标记；last_prices 取自它）；
    - clock_skew_ms：本机时钟 − 券商时钟（毫秒，带符号）；
    - account_synced：持仓、现金、订单自上次连接以来已与券商同步（None 表示未知）；
    - delayed_data_allowed：受保护配置是否允许延迟行情（ADR-06，只限 PAPER）。
    """

    now: UtcDatetime
    positions: tuple[Position, ...] | None = None
    open_orders: tuple[OpenOrder, ...] | None = None
    cash: CashBalance | None = None
    instruments: Mapping[str, Instrument] | None = None
    last_prices: Mapping[str, Price] | None = None  # 最新价（R-03 接入行情与新鲜度）
    nav: Money | None = None
    start_of_day_nav: Money | None = None
    strategy_drawdown: Mapping[str, Decimal] | None = None
    recent_submissions: tuple[UtcDatetime, ...] | None = None
    stop_cooldown: Mapping[str, StopCooldown] | None = None
    quotes: Mapping[str, Quote] | None = None
    clock_skew_ms: int | None = None
    account_synced: bool | None = None
    delayed_data_allowed: bool = False

    def position(self, symbol: str) -> Decimal | None:
        if self.positions is None:
            return None
        return sum((p.quantity for p in self.positions if p.symbol == symbol), Decimal(0))


class RuleResult(ValueModel):
    """规则的非放行结论。REDUCE 必须给出允许的最大数量。"""

    verdict: RiskVerdict
    message: str
    max_quantity: PositiveQuantity | None = None


class Rule(Protocol):
    @property
    def rule_id(self) -> str: ...

    @property
    def version(self) -> str: ...

    def check(self, subject: Subject, context: RiskContext) -> RuleResult | None:
        """放行返回 None；否则返回 REDUCE / REJECT / HALT。"""


class ContextProvider(Protocol):
    async def snapshot(self, subject: Subject) -> RiskContext: ...


@dataclass(frozen=True)
class RuleSet:
    """一组版本化的规则。version 写入每个决定，便于审计“按哪一版规则放行”。"""

    version: str
    rules: Sequence[Rule]


@dataclass(frozen=True)
class Outcome:
    decision: RiskDecision
    status: OrderStatus | None  # 只有 ALLOW / REDUCE 且交给了 OMS 才有


def reduces_risk(subject: Subject) -> bool:
    """HALT 下可以放行的订单：OMS 随持仓创建的保护性止损、急停入口生成的紧急平仓（都不是改单）。"""
    intent = subject.intent
    if subject.amendment_id is not None:
        return False
    return (
        intent.order_class is OrderClass.PROTECTIVE_STOP
        and intent.strategy_id == PROTECTIVE_STOP_SOURCE
    ) or (
        intent.order_class is OrderClass.REDUCE and intent.strategy_id == EMERGENCY_FLATTEN_SOURCE
    )


class RiskEngine:
    def __init__(  # noqa: PLR0913 - 依赖全部显式注入
        self,
        repo: LedgerRepository,
        oms: OrderManager,
        modes: ModeManager,
        *,
        rule_set: RuleSet | Callable[[], RuleSet | None] | None,
        context: ContextProvider,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
        timeout: timedelta = timedelta(seconds=2),
        actor: str = "risk",
        run_gate: Callable[[Subject], str | None] | None = None,
    ) -> None:
        self._repo = repo
        self._oms = oms
        self._modes = modes
        # 规则集可以是固定的，也可以每次评估时现取（R-02：面板调严立即生效）
        self._rules: Callable[[], RuleSet | None] = (
            rule_set if callable(rule_set) else (lambda: rule_set)
        )
        self._context = context
        self._clock = clock
        self._new_id = new_id
        self._timeout = timeout
        self._actor = actor
        # 运行状态门禁（R-08：DEGRADED / 券商窗口）；None 表示未接入（S-01 必须接上）
        self._run_gate = run_gate

    # ---------- 入口 ----------

    async def submit(self, intent: OrderIntent) -> Outcome:
        """评估新单并在放行时交给 OMS。同一意图重试不重新评估：已签发的沿用原决定（OMS 幂等）。"""
        try:
            self._repo.add_intent(intent)
        except ConflictError:  # 同一幂等键内容不同：不评估，直接拒绝
            return Outcome(
                self._persist(
                    self._engine_reject(
                        Subject.new_order(intent), "intent_conflict", "同一意图 ID 已有不同内容"
                    )
                ),
                None,
            )
        issued = self._repo.issuance_for(intent.intent_id)
        if issued is not None:
            found = [
                d
                for d in self._repo.risk_decisions_for(intent.intent_id)
                if d.decision_id == issued.decision_id
            ]
            if not found:  # pragma: no cover - 外键保证签发记录指向已落账的决定
                raise RuntimeError(f"签发记录指向的风控决定 {issued.decision_id} 不存在")
            decision = found[0]
            return Outcome(decision, await self._oms.submit_approved_intent(intent, decision))
        decision = await self.decide(Subject.new_order(intent))
        if decision.verdict not in {V.ALLOW, V.REDUCE}:
            return Outcome(decision, None)
        return Outcome(decision, await self._oms.submit_approved_intent(intent, decision))

    async def amend(
        self, intent_id: str, *, amendment_id: str, new_price: Decimal, new_quantity: Decimal
    ) -> Outcome:
        """评估改单（新价格与数量）并在放行时交给 OMS；决定绑定这次改单的内容。"""
        intent = self._repo.get_intent(intent_id)
        if intent is None:
            raise ValueError(f"没有意图 {intent_id}")
        subject = Subject(
            intent=intent, amendment_id=amendment_id, price=new_price, quantity=new_quantity
        )
        decision = await self.decide(subject)
        if decision.verdict not in {V.ALLOW, V.REDUCE}:
            return Outcome(decision, None)
        status = await self._oms.request_amend(
            intent_id,
            amendment_id=amendment_id,
            new_price=subject.price,
            new_quantity=subject.quantity,
            decision=decision,
        )
        return Outcome(decision, status)

    # ---------- 评估 ----------

    async def decide(self, subject: Subject) -> RiskDecision:
        """评估并落账，返回决定（不调用 OMS）。"""
        return self._persist(await self._evaluate(subject))

    def _precheck(self, subject: Subject, rule_set: RuleSet | None) -> RiskDecision | None:
        """模式门禁、改单数量、规则集就绪；不通过返回拒绝决定。"""
        try:
            self._modes.require_trading_allowed()
        except TradingHaltedError as exc:
            # HALT 下只放行降低风险的订单（保护性止损、紧急平仓），规则照查；其余一律拒绝（R-05）
            if not (reduces_risk(subject) and self._modes.risk_reducing_allowed()):
                return self._engine_reject(subject, "mode_gate", str(exc))
        except Exception as exc:  # 模式状态读不出来：同样不放行
            return self._engine_reject(
                subject, "mode_gate", f"模式状态不可用：{type(exc).__name__}"
            )
        run_reason = self._run_state_reason(subject)
        if run_reason is not None:
            return self._engine_reject(subject, "run_state", run_reason)
        if subject.amendment_id is not None and (
            subject.quantity > subject.intent.quantity
            or subject.quantity != subject.quantity.to_integral_value()
        ):
            # 改单只能把数量改小且为整股（OMS 票据上限是意图数量）：加量须提交新意图
            return self._engine_reject(
                subject, "amend_quantity", "改单数量必须是整股且不超过意图数量"
            )
        if rule_set is None or not rule_set.rules:
            return self._engine_reject(
                subject, "rule_set_missing", "风控规则集缺失（受保护参数未提交或不合法）"
            )
        return None

    def _run_state_reason(self, subject: Subject) -> str | None:
        if self._run_gate is None:
            return None
        try:
            return self._run_gate(subject)
        except Exception as exc:  # 运行状态算不出来：不放行
            return f"运行状态不可用：{type(exc).__name__}"

    async def _evaluate(self, subject: Subject) -> RiskDecision:
        try:
            rule_set = self._rules()
        except Exception as exc:  # 取不到规则集（参数加载失败等）：按缺失处理
            log_event(log, logging.ERROR, "风控规则集不可用", error=type(exc).__name__)
            rule_set = None
        refused = self._precheck(subject, rule_set)
        if refused is not None:
            return refused
        assert rule_set is not None  # noqa: S101 - _precheck 已确认规则集就绪
        try:
            context = await asyncio.wait_for(
                self._context.snapshot(subject), self._timeout.total_seconds()
            )
        except TimeoutError:
            return self._engine_reject(subject, "risk_timeout", "风控上下文采集超时")
        except Exception as exc:
            return self._engine_reject(
                subject, "context_unavailable", f"上下文不可用：{type(exc).__name__}"
            )
        hits: list[tuple[RuleHit, RuleResult]] = []
        for rule in rule_set.rules:
            try:
                result = rule.check(subject, context)
                if result is None or result.verdict is V.ALLOW:
                    continue
            except Exception as exc:  # 规则自身出错（含返回值不合法）：不放行
                return self._engine_reject(
                    subject, "rule_error", f"规则 {rule.rule_id} 出错：{type(exc).__name__}"
                )
            hits.append(
                (
                    RuleHit(
                        rule_id=rule.rule_id, rule_version=rule.version, message=result.message
                    ),
                    result,
                )
            )
        return self._combine(subject, rule_set.version, hits)

    def _combine(
        self, subject: Subject, version: str, hits: list[tuple[RuleHit, RuleResult]]
    ) -> RiskDecision:
        if not hits:
            if subject.amendment_id is not None and subject.quantity < subject.intent.quantity:
                # OMS 约定（O-03）：改单数量小于意图数量时用 REDUCE 表达，批准数量等于新数量
                hit = RuleHit(
                    rule_id="amend_quantity",
                    rule_version=ENGINE_RULE_VERSION,
                    message=f"改单把数量改为 {subject.quantity}",
                )
                return self._decision(subject, V.REDUCE, version, (hit,), approved=subject.quantity)
            return self._decision(subject, V.ALLOW, version, ())
        worst = max((r.verdict for _, r in hits), key=SEVERITY.__getitem__)
        rule_hits = tuple(h for h, _ in hits)
        if worst is V.HALT:
            reason = "; ".join(h.message for h, r in hits if r.verdict is V.HALT)
            self._halt(f"风控 HALT：{reason}")
            return self._decision(subject, V.HALT, version, rule_hits)
        if worst is V.REJECT:
            return self._decision(subject, V.REJECT, version, rule_hits)
        approved = self._reduced_quantity(subject, [r for _, r in hits])
        if approved is None:
            if subject.intent.order_class is OrderClass.PROTECTIVE_STOP:
                # 保护性止损被要求缩量：拒绝并告警（持仓可能因此没有保护，P0）
                rule_hits = (*rule_hits, RuleHit(
                    rule_id="protective_stop_not_reducible",
                    rule_version=ENGINE_RULE_VERSION,
                    message="保护性止损不缩量：只保护一部分仓位等于放弃保护",
                ))  # fmt: skip
                log_event(
                    log, logging.ERROR, "保护性止损被拒（缩量）", intent_id=subject.intent.intent_id
                )
            return self._decision(subject, V.REJECT, version, rule_hits)
        return self._decision(subject, V.REDUCE, version, rule_hits, approved=approved)

    @staticmethod
    def _reduced_quantity(subject: Subject, results: list[RuleResult]) -> Decimal | None:
        """各 REDUCE 允许数量的最小值（向下取整股）；不能缩量时返回 None（按拒绝处理）。

        REDUCE 没给数量、改单（不缩量）、缩量后不小于原数量（规则结论矛盾）、缩到 0：都不能缩量。
        """
        allowed = [r.max_quantity for r in results if r.max_quantity is not None]
        if len(allowed) != len(results) or subject.amendment_id is not None:
            return None
        if subject.intent.order_class is OrderClass.PROTECTIVE_STOP:
            return None  # 不缩保护性止损（R-04）：由调用方拒绝并告警
        approved = min(allowed).to_integral_value(rounding=ROUND_FLOOR)  # 首期只交易整股
        if approved >= subject.quantity or approved <= 0:
            return None
        return approved

    # ---------- 工具 ----------

    def _engine_reject(self, subject: Subject, rule_id: str, message: str) -> RiskDecision:
        hit = RuleHit(rule_id=rule_id, rule_version=ENGINE_RULE_VERSION, message=message)
        version = "engine"  # 引擎内置检查不属于任何规则集版本
        log_event(
            log, logging.WARNING, "风控拒绝", rule_id=rule_id, intent_id=subject.intent.intent_id
        )
        return self._decision(subject, V.REJECT, version, (hit,))

    def _decision(
        self,
        subject: Subject,
        verdict: RiskVerdict,
        version: str,
        hits: tuple[RuleHit, ...],
        *,
        approved: Decimal | None = None,
    ) -> RiskDecision:
        return RiskDecision(
            decision_id=self._new_id(),
            intent_id=subject.intent.intent_id,
            verdict=verdict,
            approved_quantity=approved,
            rule_hits=hits,
            rule_set_version=version,
            decided_at=self._clock(),
            subject_digest=subject.digest(),
        )

    def _persist(self, decision: RiskDecision) -> RiskDecision:
        self._repo.add_risk_decision(decision)  # 写入失败向上抛：决定没落账就不会交给 OMS
        log_event(
            log,
            logging.INFO,
            "风控决定",
            intent_id=decision.intent_id,
            verdict=decision.verdict.value,
            decision_id=decision.decision_id,
        )
        return decision

    def _halt(self, reason: str) -> None:
        try:
            self._modes.halt(reason, actor=self._actor)
        except Exception:  # ModeManager 写入失败会闩锁本进程为拒绝；这里照样返回 HALT 决定
            log_event(log, logging.ERROR, "风控 HALT 持久化失败", reason=reason)
