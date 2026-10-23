"""订单管理（O-01，V4.1 6.3–6.5）：唯一的票据签发者与订单状态的唯一写入者。

主路径：
1. `submit_approved_intent`（只由 server/risk 调用，arch_scan 规则 risk-bypass）：
   读回账本里已落账的风控决定并核对；一个决定只能用一次、一个意图只有一条订单链；
   订单号取 max(券商 nextValidId, 账本最大值 + 1)；券商订单、签发记录与 SUBMIT_SENT 事件
   **在下单之前**同一事务落账，然后才调用券商。下单抛任何异常都视为结果未知（UNKNOWN），不重发。
2. `on_broker_event`：券商回报 → C-02 的订单事件，一律先 transition() 再写账本。
   - 同一状态的重复回报记录后忽略，不升级为 HALT；非法转移拒绝并写差异审计。
   - 成交按 execution_id 去重入账（重复返回 False），累计数量决定 PARTIAL_FILL / FULL_FILL；
     终态之后到达的成交照样入账并写差异审计；无法关联的成交进未匹配成交表，不丢弃。
   - permId 从任一路回报先到的取得即落账（写字段，终态下也允许）。
3. UNKNOWN（提交超时或下单异常）：只能经 `resolve_unknown` 按 orderRef 查券商后以
   BROKER_STATUS_OBSERVED 转出；查无此单保持 UNKNOWN 并多轮查证，**绝不映射为 REJECTED 后重下**；
   多轮仍查不到就通知调用方（SAFE HALT 由 ModeManager 执行）。
   UNKNOWN 期间到达的成交先入账再触发查单。
4. 撤单：SUBMITTING / UNKNOWN 的订单不能直接撤，挂起到确认后再撤；保护性止损不走这里（R-05）。
5. 提交超时：离开 SUBMITTING 即取消定时；迟到的超时记录后忽略。

成交更正（O-02）：更正另起一行并记录取代关系，有效成交量只看未被取代的成交。

改单（O-03）：只改价格与数量，必须附带新的、已落账的风控决定；请求先落账再调用券商；
同一时刻只允许一张结果未定的改单，结果未定期间按“未定改单与有效数量中较大者”判定全部成交
（不提前判 FILLED）；
结果 UNKNOWN 由 `resolve_amendments` 查单后转正；保护性止损暂不允许改单（R-04 定规则后再开放）。

不做的事（后续任务）：崩溃恢复与启动对账（O-05、R-07）、佣金后到的入账（B-03）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

from server.broker.base import (
    TERMINAL_STATES,
    AmendmentRejectedError,
    BrokerAdapter,
    BrokerError,
    BrokerEvent,
    BrokerExecution,
    BrokerOrderState,
    CommissionReport,
    ExecutionReport,
    OrderAmendment,
    OrderRecord,
    OrderStatusUpdate,
    OrderTicket,
    UnknownOrderError,
)
from server.domain.enums import OrderClass, RiskVerdict
from server.domain.instrument import Instrument
from server.domain.order_state import (
    TERMINAL,
    EventKind,
    IllegalTransitionError,
    OrderEvent,
    OrderStatus,
    transition,
)
from server.domain.orders import BrokerOrder, Execution, OrderIntent, RiskDecision
from server.storage.errors import ConflictError
from server.storage.ledger import Issuance, LedgerRepository
from server.storage.logs import log_event, record_audit

log = logging.getLogger(__name__)

S, E = OrderStatus, EventKind
DISCREPANCY = "order_discrepancy"
ACTIVE = frozenset({S.SUBMITTED, S.PARTIALLY_FILLED})
FILLABLE = frozenset({S.SUBMITTING, S.SUBMITTED, S.PARTIALLY_FILLED, S.CANCEL_PENDING})
# 券商侧状态 → 从 UNKNOWN 查证后观察到的状态（撤单中 / 撤单被拒按是否已有成交归为活动状态）
OBSERVED: Mapping[BrokerOrderState, OrderStatus] = {
    BrokerOrderState.ACKNOWLEDGED: S.SUBMITTED,
    BrokerOrderState.PARTIALLY_FILLED: S.PARTIALLY_FILLED,
    BrokerOrderState.FILLED: S.FILLED,
    BrokerOrderState.CANCELLED: S.CANCELLED,
    BrokerOrderState.REJECTED: S.REJECTED,
    BrokerOrderState.EXPIRED: S.EXPIRED,
}


def canonical(value: Decimal) -> Decimal:
    """去掉末尾的 0：500.050 → 500.05，100.00 → 100（不用科学计数法），-0 → 0。

    用独立的高精度上下文，不受调用方 decimal 上下文影响；结果与原值不等即拒绝（绝不静默舍入）。
    """
    with localcontext() as ctx:
        ctx.prec = 60
        result = Decimal(format(value.normalize(), "f"))
    if result != value:
        raise ValueError(f"数值规范化改变了取值：{value}")
    return Decimal(0) if result.is_zero() else result


class SubmissionRefusedError(Exception):
    """拒绝签发：风控决定未落账或不匹配、已被使用、标的规格缺失等（失败即关闭，不下单）。"""


class AmendRefusedError(Exception):
    """拒绝改单：状态不允许、风控决定未落账或已被用过、数量不大于已成交等（失败即关闭，不调用券商）。"""


class CancelRefusedError(Exception):
    """拒绝撤单：保护性止损不经本入口撤销（R-05 的二次确认路径）。"""


class OrderManager:
    def __init__(  # noqa: PLR0913 - 依赖全部显式注入
        self,
        repo: LedgerRepository,
        broker: BrokerAdapter,
        *,
        instruments: Mapping[str, Instrument],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        submit_timeout: timedelta = timedelta(seconds=10),
        on_unresolved: Callable[[str], None],
        max_lookup_rounds: int = 3,
        actor: str = "tradingd",
    ) -> None:
        self._repo = repo
        self._broker = broker
        self._instruments = instruments
        self._clock = clock
        self._submit_timeout = submit_timeout
        self._max_lookup_rounds = max_lookup_rounds
        self._on_unresolved = on_unresolved
        self._actor = actor
        self._deadlines: dict[str, datetime] = {}
        self._pending_cancel: set[str] = set()
        self._lookup_misses: dict[str, int] = {}
        self._amending: set[str] = set()  # 正在调用券商的改单（查证时跳过）
        self._amend_pending_rounds: dict[str, int] = {}  # 改单查证仍对不上的轮数

    # ---------- 查询 ----------

    def status(self, intent_id: str) -> OrderStatus:
        return self._repo.latest_status(intent_id) or S.CREATED

    def filled_quantity(self, intent_id: str) -> Decimal:
        """有效成交量：被更正取代的成交不计（O-02）。"""
        executions = self._repo.effective_executions_for(intent_id)
        return sum((e.quantity for e in executions), Decimal(0))

    # ---------- 提交 ----------

    async def submit_approved_intent(
        self, intent: OrderIntent, decision: RiskDecision
    ) -> OrderStatus:
        """签发票据并提交（只由 server/risk 调用）。重复调用不会第二次下单。"""
        intent_id = intent.intent_id
        issued = self._check_submission(intent, decision)
        if issued is not None:
            return self.status(intent_id)  # 幂等：已签发过，不再下单
        ticket = await self._issue_ticket(intent, decision)
        # 全部校验通过后才记 RISK_APPROVED：校验失败的意图不会显得“已批准”
        current = self.status(intent_id)
        if current is S.CREATED:
            current = self._apply(intent_id, E.RISK_APPROVED)
        now = self._clock()
        order = BrokerOrder(
            intent_id=intent_id,
            broker_order_id=ticket.broker_order_id,
            order_class=intent.order_class,
            submitted_at=now,
        )
        submitting = transition(current, E.SUBMIT_SENT)
        # 订单号、签发记录与 SUBMITTING 在下单之前落账：之后任何崩溃都只会查单，不会重下
        committed = self._repo.commit_submission(
            order,
            decision.decision_id,
            ticket.quantity,
            self._event(intent_id, E.SUBMIT_SENT),
            submitting,
        )
        if not committed:  # 同一签发已落账过：不论之前是否发出，都不再下单
            return self.status(intent_id)
        self._deadlines[intent_id] = now + self._submit_timeout
        try:
            await self._broker.place_order(ticket)
        except Exception as exc:  # 结果未知：不重发，转 UNKNOWN 查证
            status = self.status(intent_id)
            if status is S.SUBMITTING:  # 等待期间可能已被超时检查转为 UNKNOWN
                status = self._apply(intent_id, E.SUBMIT_TIMEOUT)
            self._discrepancy(intent_id, "submit_raised", error=type(exc).__name__)
            return status
        return submitting

    def _check_submission(self, intent: OrderIntent, decision: RiskDecision) -> Issuance | None:
        """核对意图与风控决定（必须已落账且一致）；已签发过则返回签发记录。"""
        intent_id = intent.intent_id
        if decision.intent_id != intent_id:
            raise SubmissionRefusedError("风控决定不属于这个意图")
        if decision.verdict not in {RiskVerdict.ALLOW, RiskVerdict.REDUCE}:
            raise SubmissionRefusedError(f"风控结论 {decision.verdict.value} 不允许下单")
        stored = self._repo.get_intent(intent_id)
        if stored is None or stored != intent:
            raise SubmissionRefusedError("意图未落账或与账本内容不一致")
        if decision not in self._repo.risk_decisions_for(intent_id):
            raise SubmissionRefusedError("风控决定未落账或与账本内容不一致，拒绝签发")
        issued = self._repo.issuance_for(intent_id)
        if issued is not None:
            if issued.decision_id != decision.decision_id:
                raise SubmissionRefusedError("这个意图已用别的风控决定签发过，不能再签发")
            return issued
        current = self.status(intent_id)
        if current not in {S.CREATED, S.RISK_APPROVED}:
            raise SubmissionRefusedError(f"意图处于 {current.value}，不能提交")
        return None

    async def _issue_ticket(self, intent: OrderIntent, decision: RiskDecision) -> OrderTicket:
        """唯一的票据签发点：规格、数量、订单号齐全才签发。"""
        instrument = self._instruments.get(intent.symbol)
        if instrument is None:
            raise SubmissionRefusedError(f"缺少 {intent.symbol} 的标的规格，禁止下单")
        quantity = (
            intent.quantity
            if decision.verdict is RiskVerdict.ALLOW
            else decision.approved_quantity or Decimal(0)
        )
        broker_order_id = await self._next_order_id()
        try:
            return OrderTicket(
                intent=intent,
                decision=decision,
                quantity=quantity,
                broker_order_id=broker_order_id,
                instrument=instrument,
            )
        except ValueError as exc:
            raise SubmissionRefusedError(f"票据不合法：{exc}") from exc

    async def _next_order_id(self) -> str:
        reported = await self._broker.next_order_id()
        if not (reported.isascii() and reported.isdecimal()):
            raise SubmissionRefusedError(f"券商返回的订单号不是数字：{reported!r}")
        return str(max(int(reported), self._repo.max_numeric_broker_order_id() + 1))

    # ---------- 超时 ----------

    def check_timeouts(self) -> list[str]:
        """到期仍在 SUBMITTING 的订单转 UNKNOWN。

        已离开 SUBMITTING 的定时直接丢弃（迟到的超时忽略）。
        """
        now = self._clock()
        expired: list[str] = []
        for intent_id, deadline in list(self._deadlines.items()):
            if self.status(intent_id) is not S.SUBMITTING:
                self._deadlines.pop(intent_id, None)
            elif now >= deadline:
                self._apply(intent_id, E.SUBMIT_TIMEOUT)
                expired.append(intent_id)
        return expired

    # ---------- UNKNOWN 查证 ----------

    async def resolve_unknown(self, intent_id: str) -> OrderStatus:
        """按 orderRef 查券商。查到即以 BROKER_STATUS_OBSERVED 转出；查不到保持 UNKNOWN。"""
        if self.status(intent_id) is not S.UNKNOWN:
            return self.status(intent_id)
        try:
            lookup = await self._broker.lookup_order(intent_id)
        except Exception as exc:  # 查询失败：仍不确定，保持 UNKNOWN
            return self._miss(intent_id, f"lookup_failed:{type(exc).__name__}")
        if self.status(intent_id) is not S.UNKNOWN:  # 等待查询期间已被并发查证转出
            return self.status(intent_id)
        # 先入账普通成交、再入账更正：同一批查单结果里的更正不会因顺序落到“更正先到”
        for execution in sorted(lookup.executions, key=lambda e: e.corrects is not None):
            self._book_execution(execution)
        record = self._verified_record(intent_id, lookup.orders)
        if record is None:
            return S.UNKNOWN
        self._lookup_misses.pop(intent_id, None)
        if record.perm_id is not None and self._repo.perm_id_for(intent_id) is None:
            self._record_perm_id(intent_id, record.perm_id)  # 查证时补登 permId，便于之后认领与对账
        status = self._apply(intent_id, E.BROKER_STATUS_OBSERVED, observed=self._observed(record))
        self._sync_fills(intent_id, status)
        await self._after_active(intent_id)  # 挂起的撤单在查证转出后补发
        return self.status(intent_id)

    def _verified_record(
        self, intent_id: str, orders: tuple[OrderRecord, ...]
    ) -> OrderRecord | None:
        """从查单结果里取出本系统那张订单；有疑点（别的订单号、事实不符、查无此单）返回 None。"""
        mine = self._issued(intent_id).broker_order.broker_order_id
        records = [o for o in orders if o.broker_order_id == mine]
        strays = [o.broker_order_id for o in orders if o.broker_order_id != mine]
        if strays:  # 同一 orderRef 下有别的订单：可能重复下单或被冒用，不转出，升级处理
            self._discrepancy(intent_id, "lookup_foreign_order_ids", ids=",".join(strays))
            self._on_unresolved(intent_id)
            return None
        if not records:
            self._miss(intent_id, "not_found")
            return None
        if not self._record_matches(intent_id, records[0]):
            self._discrepancy(intent_id, "lookup_fact_mismatch", broker_order_id=mine)
            self._on_unresolved(intent_id)
            return None
        return records[0]

    def _record_matches(self, intent_id: str, record: OrderRecord) -> bool:
        """查到的券商订单与签发内容一致（标的、方向、账户、数量、permId）。"""
        intent = self._repo.get_intent(intent_id)
        known_perm = self._repo.perm_id_for(intent_id)
        return (
            intent is not None
            and (record.symbol, record.side, record.account_id)
            == (intent.symbol, intent.side, intent.account_id)
            and record.total_quantity
            in {self._ordered(intent_id), *self._pending_amendments(intent_id)}
            and (known_perm is None or record.perm_id in {None, known_perm})
        )

    def _observed(self, record: OrderRecord) -> OrderStatus:
        mapped = OBSERVED.get(record.state)
        if mapped is not None:
            return mapped
        # CANCEL_PENDING / CANCEL_REJECTED：订单仍活着
        return S.PARTIALLY_FILLED if record.filled_quantity > 0 else S.SUBMITTED

    def _miss(self, intent_id: str, reason: str) -> OrderStatus:
        rounds = self._lookup_misses.get(intent_id, 0) + 1
        self._lookup_misses[intent_id] = rounds
        self._discrepancy(intent_id, "unknown_unresolved", reason=reason, round=str(rounds))
        if rounds >= self._max_lookup_rounds:
            self._on_unresolved(intent_id)  # 由调用方进入 SAFE HALT；仍不重发
        return S.UNKNOWN

    # ---------- 撤单 ----------

    async def request_cancel(self, intent_id: str) -> OrderStatus:
        """撤开仓或减仓单。SUBMITTING / UNKNOWN 先挂起，确认后再撤；终态与撤单中不重复发。"""
        intent = self._repo.get_intent(intent_id)
        if intent is None:
            raise CancelRefusedError(f"没有意图 {intent_id}")
        if intent.order_class is OrderClass.PROTECTIVE_STOP:
            raise CancelRefusedError("保护性止损不经此入口撤销（需二次确认，R-05）")
        status = self.status(intent_id)
        if status in {S.SUBMITTING, S.UNKNOWN}:
            self._pending_cancel.add(intent_id)
            log_event(log, logging.INFO, "撤单挂起，等待确认后再撤", intent_id=intent_id)
            return status
        if status in {S.CREATED, S.RISK_APPROVED}:
            self._discrepancy(intent_id, "cancel_before_submit")
            return status
        if status not in ACTIVE:
            return status
        self._pending_cancel.discard(intent_id)
        status = self._apply(intent_id, E.CANCEL_REQUESTED)
        try:
            await self._broker.cancel_order(self._issued(intent_id).broker_order.broker_order_id)
        except UnknownOrderError:
            self._discrepancy(intent_id, "cancel_unknown_order")
        except Exception as exc:  # 撤单请求未确认：保持 CANCEL_PENDING，等回报或对账
            self._discrepancy(intent_id, "cancel_raised", error=type(exc).__name__)
        return status

    # ---------- 改单 ----------

    async def request_amend(
        self,
        intent_id: str,
        *,
        amendment_id: str,
        new_price: Decimal,
        new_quantity: Decimal,
        decision: RiskDecision,
    ) -> OrderStatus:
        """改价格或数量（只改这两项；类别、方向、标的、类型、有效期在类型层面不可表达）。

        - 只允许 SUBMITTED / PARTIALLY_FILLED；撤单中、待撤、提交中、UNKNOWN、终态一律拒绝；
        - 必须附带针对新价格与数量、已落账、从未用过的新风控决定
          （B-01 移交：按 amendment_id 消费）；
        - 改单请求在调用券商之前落账；amendment_id 是幂等键，重复请求不再调用券商；
        - 券商拒绝 → REJECTED；抛其他异常 → UNKNOWN（有效数量保持原值，记差异交对账）。
        """
        new_price, new_quantity = canonical(new_price), canonical(new_quantity)
        owner = self._repo.amendment_owner(amendment_id)
        if owner is not None:
            return self._replayed_amendment(
                intent_id=intent_id,
                owner=owner,
                amendment_id=amendment_id,
                new_price=new_price,
                new_quantity=new_quantity,
                decision=decision,
            )
        status = self.status(intent_id)
        if status not in ACTIVE or intent_id in self._pending_cancel:
            raise AmendRefusedError(f"订单处于 {status.value}，不能改单")
        if self._pending_amendments(intent_id):
            raise AmendRefusedError("上一次改单结果未定，先查证（resolve_amendments）再改单")
        amendment = self._amendment(intent_id, amendment_id, new_price, new_quantity, decision)
        if new_quantity <= self.filled_quantity(intent_id):
            raise AmendRefusedError("新数量必须大于已成交数量")
        try:
            self._repo.add_amendment(
                amendment_id=amendment_id,
                intent_id=intent_id,
                decision_id=decision.decision_id,
                new_price=new_price,
                new_quantity=new_quantity,
                at=self._clock(),
            )
        except ConflictError as exc:
            raise AmendRefusedError("风控决定已被别的改单用过") from exc
        self._amending.add(amendment_id)
        try:
            await self._broker.modify_order(amendment)
        except asyncio.CancelledError:  # 调用方被取消：结果未知，记下后照样向上传递取消
            self._amending.discard(amendment_id)
            self._record_amendment_result(intent_id, amendment_id, "UNKNOWN")
            self._discrepancy(intent_id, "amend_cancelled", amendment_id=amendment_id)
            self._on_unresolved(intent_id)
            raise
        except (AmendmentRejectedError, UnknownOrderError) as exc:
            self._amending.discard(amendment_id)
            self._record_amendment_result(intent_id, amendment_id, "REJECTED")
            self._discrepancy(intent_id, "amend_rejected", error=type(exc).__name__)
            return self._sync_fills(intent_id, self.status(intent_id))
        except Exception as exc:  # 结果未知：不重发；判全部成交按较大数量，查证后转正
            self._amending.discard(amendment_id)
            self._record_amendment_result(intent_id, amendment_id, "UNKNOWN")
            self._discrepancy(intent_id, "amend_unknown", error=type(exc).__name__)
            self._on_unresolved(intent_id)
            return self.status(intent_id)
        self._amending.discard(amendment_id)
        self._record_amendment_result(intent_id, amendment_id, "ACCEPTED")
        log_event(log, logging.INFO, "改单已接受", intent_id=intent_id, amendment_id=amendment_id)
        return self._sync_fills(intent_id, self.status(intent_id))

    def _record_amendment_result(self, intent_id: str, amendment_id: str, result: str) -> None:
        """记改单结果；先后关系不合法（例如已被查证记为别的结果）记差异并升级，不让异常冒出。"""
        try:
            self._repo.record_amendment_result(amendment_id, result, self._clock())
        except ConflictError:
            self._discrepancy(
                intent_id, "amend_result_conflict", amendment_id=amendment_id, result=result
            )
            self._on_unresolved(intent_id)

    def _replayed_amendment(  # noqa: PLR0913
        self,
        *,
        intent_id: str,
        owner: str,
        amendment_id: str,
        new_price: Decimal,
        new_quantity: Decimal,
        decision: RiskDecision,
    ) -> OrderStatus:
        """同一 amendment_id 重放：参数一致则不再调用券商；参数不同或属于别的意图则拒绝。"""
        if owner != intent_id:
            raise AmendRefusedError(f"amendment_id {amendment_id} 已被别的意图用过")
        recorded = next(
            a for a in self._repo.amendments_for(intent_id) if a.amendment_id == amendment_id
        )
        if (recorded.new_price, recorded.new_quantity, recorded.decision_id) != (
            new_price,
            new_quantity,
            decision.decision_id,
        ):
            raise AmendRefusedError(f"amendment_id {amendment_id} 已用于不同的改单内容")
        return self.status(intent_id)

    def _pending_amendments(self, intent_id: str) -> list[Decimal]:
        """结果未定（未记录或 UNKNOWN）的改单数量。"""
        return [
            a.new_quantity
            for a in self._repo.amendments_for(intent_id)
            if a.result in {None, "UNKNOWN"}
        ]

    async def resolve_amendments(self, intent_id: str) -> None:
        """按查单结果给未定的改单补记结果（正在调用券商的改单跳过）。

        - 券商订单的数量与价格等于改单值：ACCEPTED；
        - 对不上且券商订单已是终态：REJECTED（改单再也不会生效）；
        - 对不上但订单还活着：券商可能还没处理完（IBKR 改单是异步的），保持 UNKNOWN 等下一轮，
          绝不提前记成终结的 REJECTED。
        结果为空的改单只在本进程没有在途改单时出现（崩溃后启动恢复，O-05），按同样规则处理。
        """
        pending = [
            a
            for a in self._repo.amendments_for(intent_id)
            if a.result in {None, "UNKNOWN"} and a.amendment_id not in self._amending
        ]
        if not pending:
            return
        mine = self._issued(intent_id).broker_order.broker_order_id
        try:
            lookup = await self._broker.lookup_order(intent_id)
        except Exception as exc:  # 查询失败：仍不确定，保持未定
            self._discrepancy(intent_id, "amend_lookup_failed", error=type(exc).__name__)
            return
        records = [o for o in lookup.orders if o.broker_order_id == mine]
        if not records:
            self._discrepancy(intent_id, "amend_unresolved")
            return
        record = records[0]
        for a in pending:
            if a.amendment_id in self._amending:
                continue  # 查单期间又发出了这张改单
            applied = (record.total_quantity, record.price) == (a.new_quantity, a.new_price)
            if applied:
                result = "ACCEPTED"
            elif record.state in TERMINAL_STATES:
                result = "REJECTED"
            else:
                if a.result is None:
                    self._record_amendment_result(intent_id, a.amendment_id, "UNKNOWN")
                rounds = self._amend_pending_rounds.get(a.amendment_id, 0) + 1
                self._amend_pending_rounds[a.amendment_id] = rounds
                self._discrepancy(
                    intent_id, "amend_still_pending", amendment_id=a.amendment_id, round=str(rounds)
                )
                if rounds >= self._max_lookup_rounds:  # 长期对不上：再次升级
                    self._on_unresolved(intent_id)
                continue
            self._amend_pending_rounds.pop(a.amendment_id, None)
            self._record_amendment_result(intent_id, a.amendment_id, result)
            self._discrepancy(
                intent_id, "amend_resolved", amendment_id=a.amendment_id, result=result
            )
        self._sync_fills(intent_id, self.status(intent_id))

    def _amendment(
        self,
        intent_id: str,
        amendment_id: str,
        new_price: Decimal,
        new_quantity: Decimal,
        decision: RiskDecision,
    ) -> OrderAmendment:
        """校验风控决定并构造改单（原票据按签发记录重建）。"""
        intent = self._repo.get_intent(intent_id)
        issued = self._issued(intent_id)
        if intent is None:  # pragma: no cover - 签发记录保证
            raise AmendRefusedError(f"没有意图 {intent_id}")
        if intent.order_class is OrderClass.PROTECTIVE_STOP:
            # 改止损价可能等于取消保护或立即触发；R-04 定好止损改单规则前一律拒绝（失败即关闭）
            raise AmendRefusedError("保护性止损暂不允许改单")
        persisted = self._repo.risk_decisions_for(intent_id)
        if decision not in persisted:
            raise AmendRefusedError("改单的风控决定未落账或与账本内容不一致")
        if self._repo.decision_is_used(decision.decision_id):
            raise AmendRefusedError("这个风控决定已经用过，改单必须附带新的决定")
        used = {issued.decision_id} | {a.decision_id for a in self._repo.amendments_for(intent_id)}
        latest_used = max(d.decided_at for d in persisted if d.decision_id in used)
        if decision.decided_at <= latest_used:
            raise AmendRefusedError("改单的风控决定必须晚于本订单已用过的全部决定")
        original_decision = next(
            (d for d in persisted if d.decision_id == issued.decision_id), None
        )
        instrument = self._instruments.get(intent.symbol)
        if original_decision is None or instrument is None:
            raise AmendRefusedError("缺少原签发的风控决定或标的规格")
        try:
            original = OrderTicket(
                intent=intent,
                decision=original_decision,
                quantity=issued.quantity,
                broker_order_id=issued.broker_order.broker_order_id,
                instrument=instrument,
            )
            return OrderAmendment(
                amendment_id=amendment_id,
                original=original,
                new_price=new_price,
                new_quantity=new_quantity,
                decision=decision,
            )
        except ValueError as exc:
            raise AmendRefusedError(f"改单不合法：{exc}") from exc

    async def _after_active(self, intent_id: str) -> None:
        if intent_id in self._pending_cancel and self.status(intent_id) in ACTIVE:
            await self.request_cancel(intent_id)

    # ---------- 回报 ----------

    async def on_broker_event(self, event: BrokerEvent) -> None:
        if isinstance(event, OrderStatusUpdate):
            await self._on_status(event)
        elif isinstance(event, ExecutionReport):
            intent_id = self._book_execution(event.execution)
            if intent_id is not None:
                if self.status(intent_id) is S.UNKNOWN:
                    await self.resolve_unknown(intent_id)  # 成交只是证据，状态仍须查证后转出
                else:
                    self._sync_fills(intent_id, self.status(intent_id))
                await self._after_active(intent_id)
        elif isinstance(event, CommissionReport):
            log_event(log, logging.INFO, "佣金回报", execution_id=event.execution_id)
        elif isinstance(event, BrokerError):
            log_event(
                log,
                logging.WARNING,
                "券商错误",
                category=event.category.value,
                code=event.code,
                broker_order_id=event.broker_order_id,
            )

    def _intent_for(
        self, broker_order_id: str | None, order_ref: str | None, perm_id: str | None
    ) -> str | None:
        """回报 → 本系统意图。

        orderRef 必须存在，且与订单号指向同一张已签发的订单；
        回报带的 permId 与已登记的不同也不认领。外来单的订单号可能为 0 或与别的 clientId 重复，
        只凭订单号认领会把别人的状态与成交记到本系统头上。
        """
        if order_ref is None or broker_order_id is None:
            return None
        if self._repo.intent_for_broker_order(broker_order_id) != order_ref:
            return None
        known_perm = self._repo.perm_id_for(order_ref)
        if perm_id is not None and known_perm is not None and perm_id != known_perm:
            return None
        return order_ref

    async def _on_status(self, update: OrderStatusUpdate) -> None:
        intent_id = self._intent_for(update.broker_order_id, update.order_ref, update.perm_id)
        if intent_id is None:
            self._discrepancy(
                None,
                "unmatched_status",
                broker_order_id=update.broker_order_id,
                state=update.state.value,
            )
            return
        if update.perm_id is not None and not self._record_perm_id(intent_id, update.perm_id):
            return  # permId 已属于别的意图：不认领这条回报（失败即关闭）
        status = self.status(intent_id)
        if status is S.UNKNOWN:
            await self.resolve_unknown(intent_id)
            await self._after_active(intent_id)
            return
        if status in TERMINAL:
            reported = OBSERVED.get(update.state)
            if reported in TERMINAL and reported is not status:  # 券商事实与本地终态不一致
                self._discrepancy(
                    intent_id,
                    "status_after_terminal",
                    local=status.value,
                    broker=update.state.value,
                )
            return  # 状态不变；差异交对账（R-07）处理
        for kind, has_fills in self._kinds_for(intent_id, status, update):
            try:
                status = self._apply(intent_id, kind, has_fills=has_fills)
            except IllegalTransitionError:
                self._discrepancy(intent_id, "illegal_transition", state=update.state.value)
                return
        self._sync_fills(intent_id, status)
        await self._after_active(intent_id)

    def _kinds_for(
        self, intent_id: str, status: OrderStatus, update: OrderStatusUpdate
    ) -> list[tuple[EventKind, bool | None]]:
        """券商状态 → 订单事件。成交数量由成交回报驱动，这里只处理接单、撤单、拒单、到期。

        SUBMITTING 时收到任何“订单存在”的状态，先补一个 BROKER_ACK（回报乱序时也正确）。
        """
        state = update.state
        needs_ack = status is S.SUBMITTING and state is not BrokerOrderState.REJECTED
        ack: list[tuple[EventKind, bool | None]] = [(E.BROKER_ACK, None)] if needs_ack else []
        if state is BrokerOrderState.CANCELLED:
            kind = E.CANCEL_CONFIRMED if status is S.CANCEL_PENDING else E.BROKER_CANCELLED
            return [*ack, (kind, None)]
        if state is BrokerOrderState.CANCEL_REJECTED:
            if status is not S.CANCEL_PENDING:
                return ack
            # has_fills 由已入账的累计成交得出，不由回报直接给出目标状态（C-02 移交）
            return [(E.CANCEL_REJECTED, self.filled_quantity(intent_id) > 0)]
        final = {BrokerOrderState.REJECTED: E.BROKER_REJECTED, BrokerOrderState.EXPIRED: E.EXPIRED}
        kind_for_state = final.get(state)
        # 接单、成交、撤单中：只可能需要补 ACK；成交数量另由成交回报推进
        return ack if kind_for_state is None else [*ack, (kind_for_state, None)]

    def _book_execution(self, execution: BrokerExecution) -> str | None:
        """成交入账：能关联到已签发订单的进成交表（按 execution_id 去重），其余进未匹配成交表。"""
        intent_id = self._intent_for(
            execution.broker_order_id, execution.order_ref, execution.perm_id
        )
        reason = self._unmatched_reason(execution, intent_id)
        perm_ok = (
            reason is not None
            or intent_id is None
            or execution.perm_id is None
            or self._record_perm_id(intent_id, execution.perm_id)
        )
        if not perm_ok:
            reason = "perm_conflict"  # permId 已属于别的意图：不计入本意图（失败即关闭）
        if reason is not None or intent_id is None:
            self._park_unmatched(execution, intent_id, reason or "unmatched")
            return None
        booked = Execution(
            execution_id=execution.execution_id,
            intent_id=intent_id,
            broker_order_id=execution.broker_order_id or "",
            symbol=execution.symbol,
            side=execution.side,
            # 数值规范化：同一成交重放时 500.05 与 500.050 视为相同（C-03 移交）
            quantity=canonical(execution.quantity),
            price=canonical(execution.price),
            # 佣金后到时先记 0，最终值由 CommissionReport 入账（B-03 定口径）
            commission=canonical(execution.commission or Decimal(0)),
            executed_at=execution.executed_at,
        )
        if execution.corrects is not None:
            return self._book_correction(execution, booked, execution.corrects)
        try:
            inserted = self._repo.add_execution(booked)
        except ConflictError:
            self._discrepancy(intent_id, "execution_conflict", execution_id=execution.execution_id)
            return None
        if not inserted:
            return None
        if self.status(intent_id) in TERMINAL:
            self._discrepancy(intent_id, "fill_after_terminal", execution_id=execution.execution_id)
        self._pair_waiting_corrections(execution.execution_id)
        return intent_id

    def _park_unmatched(
        self, execution: BrokerExecution, intent_id: str | None, reason: str
    ) -> None:
        """照实（券商原文）存入未匹配成交表；重放不重复记差异，内容变化记冲突差异。"""
        try:
            fresh = self._repo.add_unmatched_execution(
                execution.execution_id, reason, execution.model_dump_json(), self._clock()
            )
        except ConflictError:  # 同一成交重放时内容变了：记差异，不中断事件流
            self._discrepancy(
                intent_id, "unmatched_execution_conflict", execution_id=execution.execution_id
            )
            return
        if fresh:
            self._discrepancy(
                intent_id, "unmatched_execution", execution_id=execution.execution_id, reason=reason
            )

    def _book_correction(self, fact: BrokerExecution, booked: Execution, target: str) -> str | None:
        """成交更正：被更正的成交必须是本意图里仍然有效的一笔；更正另起一行并记录取代关系。

        - 同一更正重放（取代关系已经存在）：静默忽略，不写任何新记录；
        - 被更正的成交还没入账（更正先到）：照实（券商原文）放进未匹配成交表等待配对，
          原成交入账时自动补做取代；
        - 被更正的成交已被别的更正取代：照实放进未匹配成交表并记差异，交对账处理。
        """
        intent_id = booked.intent_id
        if self._repo.superseding_of(target) == booked.execution_id:
            # 重放：内容相同静默忽略；内容变了（数量、价格、佣金）记冲突差异，保留第一次的事实
            try:
                self._repo.add_correction(booked, target, self._clock())
            except ConflictError:
                self._discrepancy(
                    intent_id, "correction_conflict", execution_id=booked.execution_id
                )
            return None
        effective = {e.execution_id for e in self._repo.effective_executions_for(intent_id)}
        if target not in effective:
            reason = (
                "correction_of_superseded"
                if target in self._repo.superseded_executions(intent_id)
                else "correction_unknown_target"
            )
            try:
                fresh = self._repo.add_unmatched_execution(
                    fact.execution_id, reason, fact.model_dump_json(), self._clock()
                )
            except ConflictError:
                fresh = False
            if fresh:  # 重放的未配对更正不重复记差异
                self._discrepancy(
                    intent_id, reason, execution_id=booked.execution_id, corrects=target
                )
            return None
        try:
            inserted = self._repo.add_correction(booked, target, self._clock())
        except ConflictError:
            self._discrepancy(intent_id, "correction_conflict", execution_id=booked.execution_id)
            return None
        if not inserted:
            return None
        self._discrepancy(
            intent_id, "execution_corrected", execution_id=booked.execution_id, corrects=target
        )
        self._pair_waiting_corrections(booked.execution_id)  # 更正链：后面的更正可能先到了
        return intent_id

    def _pair_waiting_corrections(self, execution_id: str) -> None:
        """有成交（或更正）入账后，把先到的、更正它的那笔从等待中取出入账。

        未匹配成交表只追加：配对后原行保留；
        同一 execution_id 出现在成交表即表示已配对（R-07 据此判断）。
        """
        for waiting_id, reason, payload in self._repo.unmatched_executions():
            if reason != "correction_unknown_target" or self._repo.has_execution(waiting_id):
                continue
            try:
                fact = BrokerExecution.model_validate_json(payload)
            except ValueError:  # 无法解析的旧行：记差异后跳过，不中断事件处理
                self._discrepancy(None, "unparsable_waiting_correction", execution_id=waiting_id)
                continue
            if fact.corrects != execution_id:
                continue
            if self._book_execution(fact) is None and not self._repo.has_execution(waiting_id):
                # 配对仍失败：通常是另一笔更正先取代了同一成交
                self._discrepancy(
                    None, "correction_conflict", execution_id=waiting_id, corrects=execution_id
                )

    def _unmatched_reason(self, execution: BrokerExecution, intent_id: str | None) -> str | None:
        if intent_id is None:
            return "no_order"
        intent = self._repo.get_intent(intent_id)
        if intent is None:  # pragma: no cover - 外键保证
            return "no_intent"
        if (intent.symbol, intent.side, intent.account_id) != (
            execution.symbol,
            execution.side,
            execution.account_id,
        ):
            return "fact_mismatch"
        if execution.quantity != execution.quantity.to_integral_value():
            return "fractional"
        return None

    def _sync_fills(self, intent_id: str, status: OrderStatus) -> OrderStatus:
        """按累计成交推进 PARTIAL_FILL / FULL_FILL（成交数量只由已入账的成交决定）。"""
        if status not in FILLABLE:
            if status is S.FILLED:
                filled, ordered = self.filled_quantity(intent_id), self._ordered(intent_id)
                if filled < ordered:
                    self._discrepancy(intent_id, "fills_missing")
                elif filled > ordered:
                    self._discrepancy(
                        intent_id, "overfill", filled=str(filled), ordered=str(ordered)
                    )
            return status
        filled, ordered = self.filled_quantity(intent_id), self._ordered(intent_id)
        # 改单结果未定时取较大者：加量改单可能已生效，不能按旧数量提前判全部成交
        target = max([ordered, *self._pending_amendments(intent_id)])
        if filled > target:
            self._discrepancy(intent_id, "overfill", filled=str(filled), ordered=str(target))
        if filled >= target:
            return self._apply(intent_id, E.FULL_FILL)
        if filled > 0 and (status is not S.PARTIALLY_FILLED and status is not S.CANCEL_PENDING):
            return self._apply(intent_id, E.PARTIAL_FILL)
        return status

    # ---------- 内部 ----------

    def _issued(self, intent_id: str) -> Issuance:
        issued = self._repo.issuance_for(intent_id)
        if issued is None:
            raise SubmissionRefusedError(f"意图 {intent_id} 没有签发记录")
        return issued

    def _ordered(self, intent_id: str) -> Decimal:
        """有效下单数量：最后一次被券商接受的改单数量，没有改单则为签发数量（O-03）。"""
        accepted = [a for a in self._repo.amendments_for(intent_id) if a.result == "ACCEPTED"]
        return accepted[-1].new_quantity if accepted else self._issued(intent_id).quantity

    def _record_perm_id(self, intent_id: str, perm_id: str) -> bool:
        """登记 permId；已属于别的意图（或本意图已有另一个）返回 False。"""
        try:
            self._repo.record_perm_id(intent_id, perm_id, self._clock())
        except ConflictError:
            self._discrepancy(intent_id, "perm_id_conflict", perm_id=perm_id)
            return False
        return True

    def _event(
        self,
        intent_id: str,
        kind: EventKind,
        *,
        observed: OrderStatus | None = None,
        has_fills: bool | None = None,
    ) -> OrderEvent:
        return OrderEvent(
            event_id=f"oe-{uuid.uuid4().hex}",
            intent_id=intent_id,
            kind=kind,
            occurred_at=self._clock(),
            correlation_id=intent_id,  # 一条订单链共用一个关联 ID
            observed=observed,
            has_fills=has_fills,
        )

    def _apply(
        self,
        intent_id: str,
        kind: EventKind,
        *,
        observed: OrderStatus | None = None,
        has_fills: bool | None = None,
    ) -> OrderStatus:
        """先 transition() 再写账本（C-02 移交：订单事件落库的唯一路径）。"""
        current = self.status(intent_id)
        new = transition(current, kind, observed=observed, has_fills=has_fills)
        event = self._event(intent_id, kind, observed=observed, has_fills=has_fills)
        self._repo.append_order_event(event, new)
        if new is not S.SUBMITTING:
            self._deadlines.pop(intent_id, None)  # 离开 SUBMITTING 即取消提交超时
        if new in TERMINAL:
            self._pending_cancel.discard(intent_id)
        return new

    def _discrepancy(self, intent_id: str | None, kind: str, **detail: str | None) -> None:
        record_audit(
            self._repo,
            action=DISCREPANCY,
            actor=self._actor,
            at=self._clock(),
            detail={"kind": kind, "intent_id": intent_id, **detail},
            correlation_id=intent_id,
        )
        log_event(log, logging.WARNING, "订单差异", kind=kind, intent_id=intent_id, **detail)
