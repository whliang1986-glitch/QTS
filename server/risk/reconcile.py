"""对账核心（R-07，V4.1 6.6 / 7.5）：券商事实与本地账本逐项比对，未解释差异进入 SAFE HALT 并出报告。

预期持仓 = 持仓调整合计（人工基准、公司行动、外来成交确认，见迁移 0006）
         + 本系统全部有效成交（买正卖负）。每次对账核对：
1. 账户：本进程已完成启动校验；券商记录的账户都必须是已校验账户（首期单账户）；
2. 订单：本地有 UNKNOWN / SUBMITTING 订单；券商未结束订单按 OMS 回报认领口径认领
   （context.claim_open_orders），外来、重复、账户归属不一致、本地终态而券商未结束、
   本地未结束而券商没有、剩余数量或限价与本地（签发量、被接受或未决的改单）都不符，都是差异；
3. 成交：券商近期成交里属于本系统的必须已入账；
   外来成交与账本里的未匹配成交必须已由人工确认（ACKNOWLEDGE）；
4. 持仓：券商持仓与预期持仓逐标的比较；不一致时与公司行动数据匹配（已生效、晚于该标的最近一次基准、
   未用过的拆股合股事件，按整数比例相乘，允许零股向下取整），匹配上的逐事件写 CORPORATE_ACTION 调整
   并记为已解释（同一事件只能用一次；保护性止损随之调整由 R-06 落实），匹配不上的、负持仓是未解释。
盘中对账容忍正常时序（下单中、部分成交未回报、刚成交未入账、订单状态时差）：这些类别同一键在最近三次
对账里出现两次才算未解释，首次记为待观察；外来、重复、负持仓、账户不符、UNKNOWN、取数失败一律立即升级。
启动与日终对账一次即算。对账过程中任何异常都记为未解释并进入 SAFE HALT（先停，再尽量写报告）。
现金、佣金的日终核对依赖 Flex Query（R-07 后续，BLOCKED），日终报告标注未核对项。

last_clean() 只认本实例最近一次完成的对账：没有未解释、没有待观察项，且晚于最近一次 HALT。

基准采用（adopt_broker_positions）：首次启动、备份恢复或人工处理差异之后，
人工在 HALT 下确认“以券商持仓为基准”，写 BASELINE 调整并确认现存外来成交；
有未决订单或未入账成交时拒绝。只能在本模块与 server/api 中出现（arch_scan recon-adopt）。
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import deque
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from pydantic import PositiveInt

from server.domain.enums import OrderType, Side
from server.domain.order_state import TERMINAL, OrderStatus
from server.domain.values import Identifier, UtcDatetime, ValueModel
from server.oms.manager import OrderManager
from server.risk.context import (
    AccountSource,
    BrokerOpenOrder,
    BrokerOrderSource,
    claim_open_orders,
    owned_by_us,
)
from server.risk.mode_manager import NON_HUMAN_ACTORS, ModeManager
from server.storage.ledger import LedgerRepository, PositionAdjustment
from server.storage.logs import log_event, record_audit

log = logging.getLogger(__name__)

RECON_ACTION = "reconciliation"
ADOPT_ACTION = "reconciliation_baseline"
ADOPT_PHRASE = "确认以券商持仓为基准"
UNRESOLVED = frozenset({OrderStatus.SUBMITTING, OrderStatus.UNKNOWN})
TIMING_WINDOW = 3  # 盘中：同一键在最近 3 次对账里出现 2 次即升级


class ReconKind(StrEnum):
    STARTUP = "STARTUP"
    INTRADAY = "INTRADAY"
    END_OF_DAY = "END_OF_DAY"


class Code(StrEnum):
    RECONCILE_FAILED = "reconcile_failed"
    ACCOUNT_UNVERIFIED = "account_unverified"
    ACCOUNT_MISMATCH = "account_mismatch"
    SOURCE_UNAVAILABLE = "source_unavailable"
    ORDER_UNRESOLVED = "order_unresolved"
    ORDER_SUBMITTING = "order_submitting"
    AMENDMENT_UNRESOLVED = "amendment_unresolved"
    FOREIGN_OPEN_ORDER = "foreign_open_order"
    DUPLICATE_BROKER_ORDER = "duplicate_broker_order"
    ORDER_MISMATCH = "order_mismatch"
    BROKER_OPEN_LOCAL_TERMINAL = "broker_open_local_terminal"
    LOCAL_OPEN_MISSING_AT_BROKER = "local_open_missing_at_broker"
    EXECUTION_MISSING = "execution_missing"
    FOREIGN_EXECUTION = "foreign_execution"
    POSITION_MISMATCH = "position_mismatch"
    NEGATIVE_POSITION = "negative_position"
    CORPORATE_ACTION = "corporate_action"
    NOT_RECONCILED = "not_reconciled"


# 盘中容忍正常时序的类别（其余一律立即升级）
TIMING_CODES = frozenset(
    {
        Code.ORDER_SUBMITTING,
        Code.AMENDMENT_UNRESOLVED,
        Code.ORDER_MISMATCH,
        Code.BROKER_OPEN_LOCAL_TERMINAL,
        Code.LOCAL_OPEN_MISSING_AT_BROKER,
        Code.EXECUTION_MISSING,
        Code.POSITION_MISMATCH,
    }
)


class Status(StrEnum):
    UNEXPLAINED = "UNEXPLAINED"  # 进入 SAFE HALT
    EXPLAINED = "EXPLAINED"  # 已解释（公司行动等）
    PENDING = "PENDING"  # 盘中时序类首次出现：不停机，但也不算“对账通过”
    INFO = "INFO"  # 本次未核对的项目（如现金、佣金待 Flex Query）


@dataclass(frozen=True)
class Discrepancy:
    code: Code
    status: Status
    key: str  # 去重与追踪用：标的、订单号或成交号
    detail: str


@dataclass(frozen=True)
class ReconReport:
    recon_id: str
    kind: ReconKind
    at: datetime
    items: tuple[Discrepancy, ...] = field(default_factory=tuple)
    # 开始取数时的模式变更编号：之后若又有 HALT（编号变了），这份报告不能作为恢复依据
    mode_change_id: str | None = None

    @property
    def clean(self) -> bool:
        """没有未解释差异（已解释、待观察、未核对项不触发 SAFE HALT）。"""
        return all(i.status is not Status.UNEXPLAINED for i in self.items)

    @property
    def settled(self) -> bool:
        """既没有未解释、也没有待观察的差异：可以作为恢复交易的依据。"""
        return all(i.status in {Status.EXPLAINED, Status.INFO} for i in self.items)

    @property
    def unexplained(self) -> tuple[Discrepancy, ...]:
        return tuple(i for i in self.items if i.status is Status.UNEXPLAINED)


class CorporateAction(ValueModel):
    """公司行动事件（D-04 提供）：每 denominator 股变为 numerator 股。

    2 拆 1 为 2/1，10 合 1 为 1/10；整数比例保证精确比较。
    """

    event_id: Identifier
    symbol: Identifier
    numerator: PositiveInt
    denominator: PositiveInt
    effective_at: UtcDatetime


class CorporateActionSource(Protocol):
    async def actions(self, symbols: Collection[str]) -> Sequence[CorporateAction]: ...


class BrokerFill(Protocol):
    @property
    def execution_id(self) -> str: ...

    @property
    def order_ref(self) -> str | None: ...

    @property
    def broker_order_id(self) -> str | None: ...

    @property
    def perm_id(self) -> str | None: ...

    @property
    def account_id(self) -> str: ...


class ExecutionSource(Protocol):
    async def executions(self, since: datetime) -> Sequence[BrokerFill]: ...


@dataclass(frozen=True)
class _Snapshot:
    open_orders: list[BrokerOpenOrder]
    fills: list[BrokerFill]
    positions: dict[str, Decimal]
    foreign_accounts: list[str]


class Reconciler:
    def __init__(  # noqa: PLR0913 - 数据来源全部显式注入
        self,
        repo: LedgerRepository,
        oms: OrderManager,
        modes: ModeManager,
        *,
        account: AccountSource,
        broker_orders: BrokerOrderSource,
        executions: ExecutionSource,
        corporate_actions: CorporateActionSource,
        execution_lookback: timedelta = timedelta(days=7),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
        actor: str = "tradingd",
    ) -> None:
        self._repo = repo
        self._oms = oms
        self._modes = modes
        self._account = account
        self._broker_orders = broker_orders
        self._executions = executions
        self._corporate_actions = corporate_actions
        self._lookback = execution_lookback
        self._clock = clock
        self._new_id = new_id
        self._actor = actor
        # 盘中最近几次对账里出现过的时序类差异键（只在本实例内存中）
        self._history: deque[set[tuple[Code, str]]] = deque(maxlen=TIMING_WINDOW - 1)
        self._last: ReconReport | None = None

    # ---------- 对账 ----------

    async def reconcile(self, kind: ReconKind) -> ReconReport:
        """执行一次对账：有未解释差异即进入 SAFE HALT（先停机，再写报告）；任何异常都算未解释。"""
        recon_id = self._new_id()
        self._last = None  # 对账开始即作废旧结果（中途取消也不会沿用）
        state = self._modes.current()
        mode_change_id = None if state is None else state.change_id
        try:
            items = await self._collect()
            if kind is ReconKind.INTRADAY:
                items = self._apply_timing_rule(items)
        except Exception as exc:
            log_event(log, logging.ERROR, "对账过程出错", error=type(exc).__name__)
            items = [
                _bad(Code.RECONCILE_FAILED, "reconcile", f"对账过程出错：{type(exc).__name__}")
            ]
        if kind is ReconKind.END_OF_DAY:
            items.append(
                Discrepancy(
                    Code.NOT_RECONCILED,
                    Status.INFO,
                    "cash_commission",
                    "现金（已结算 / 未结算）与佣金待 Flex Query 日终核对（R-07 后续）",
                )
            )
        report = ReconReport(
            recon_id, kind, self._clock(), tuple(items), mode_change_id=mode_change_id
        )
        self._last = report
        try:
            if not report.clean:
                codes = sorted({i.code.value for i in report.unexplained})
                self._modes.safe_halt(f"对账发现未解释差异：{'、'.join(codes)}", actor=self._actor)
        finally:
            self._record(report)
        return report

    async def _collect(self) -> list[Discrepancy]:
        account = self._modes.verified_account()
        if account is None:
            return [_bad(Code.ACCOUNT_UNVERIFIED, "account", "本进程尚未完成启动校验，无从对账")]
        account_id = account.account_id
        try:
            snapshot = await self._snapshot(account_id)
        except Exception as exc:
            detail = f"券商数据取不到：{type(exc).__name__}"
            return [_bad(Code.SOURCE_UNAVAILABLE, "broker", detail)]
        items = [
            _bad(Code.ACCOUNT_MISMATCH, key, "券商记录的账户不是已校验账户")
            for key in snapshot.foreign_accounts
        ]
        items += self._check_orders(snapshot.open_orders, account_id)
        items += self._check_executions(snapshot.fills, account_id)
        # 同一轮里有时序类差异或未决订单时不做公司行动匹配：避免用掉事件去解释其实是未入账成交的变化
        unsettled = bool(items)  # 本轮已有任何差异（含外来成交）时都不做匹配
        items += await self._check_positions(
            snapshot.positions, account_id, match_actions=not unsettled
        )
        return items

    async def _snapshot(self, account_id: str) -> _Snapshot:
        # 先取持仓、再取成交和订单：取数间隙里新到的成交会表现为“未入账”，而不是被持仓悄悄吸收
        positions = list(await self._account.positions())
        fills = list(await self._executions.executions(self._clock() - self._lookback))
        orders = list(await self._broker_orders.open_orders())
        foreign_accounts = sorted(
            {f"order:{o.broker_order_id}" for o in orders if o.account_id != account_id}
            | {f"execution:{f.execution_id}" for f in fills if f.account_id != account_id}
            | {f"position:{p.symbol}" for p in positions if p.account_id != account_id}
        )
        held: dict[str, Decimal] = {}
        for p in positions:
            if p.account_id == account_id:
                held[p.symbol] = held.get(p.symbol, Decimal(0)) + p.quantity
        return _Snapshot(
            open_orders=[o for o in orders if o.account_id == account_id],
            fills=[f for f in fills if f.account_id == account_id],
            positions=held,
            foreign_accounts=foreign_accounts,
        )

    # ---------- 订单 ----------

    def _check_orders(
        self, open_at_broker: Sequence[BrokerOpenOrder], account_id: str
    ) -> list[Discrepancy]:
        items: list[Discrepancy] = []
        known = set(self._repo.submitted_intents())
        mine = [i for i in sorted(known) if self._account_of(i) == account_id]
        for intent_id in mine:
            status = self._oms.status(intent_id)
            if status is OrderStatus.UNKNOWN:
                items.append(_bad(Code.ORDER_UNRESOLVED, intent_id, "订单状态未知"))
            elif status is OrderStatus.SUBMITTING:
                items.append(_bad(Code.ORDER_SUBMITTING, intent_id, "订单仍在提交中"))
            if status not in TERMINAL:
                for amendment in self._repo.amendments_for(intent_id):
                    if amendment.result in {None, "UNKNOWN"}:
                        detail = f"改单 {amendment.amendment_id} 结果未确认"
                        items.append(_bad(Code.AMENDMENT_UNRESOLVED, intent_id, detail))
        claim = claim_open_orders(self._repo, open_at_broker, known)
        items += [
            _bad(Code.FOREIGN_OPEN_ORDER, k, "券商侧有本系统不认识的未结束订单")
            for k in claim.foreign
        ]
        items += [
            _bad(Code.DUPLICATE_BROKER_ORDER, k, "同一意图在券商侧有多张未结束订单")
            for k in claim.duplicates
        ]
        for intent_id, record in claim.claimed.items():
            problem = self._claimed_problem(intent_id, record, account_id)
            if problem is not None:
                items.append(problem)
        for intent_id in mine:
            status = self._oms.status(intent_id)
            if (
                status not in TERMINAL
                and status not in UNRESOLVED
                and intent_id not in claim.claimed
            ):
                detail = f"本地 {status.value}，券商没有这张单"
                items.append(_bad(Code.LOCAL_OPEN_MISSING_AT_BROKER, intent_id, detail))
        return items

    def _claimed_problem(
        self, intent_id: str, record: BrokerOpenOrder, account_id: str
    ) -> Discrepancy | None:
        intent = self._repo.get_intent(intent_id)
        status = self._oms.status(intent_id)
        if intent is None or intent.account_id != account_id:
            return _bad(Code.ACCOUNT_MISMATCH, intent_id, "券商订单的账户与本地意图不一致")
        if status in TERMINAL:
            detail = f"本地 {status.value}，券商仍未结束"
            return _bad(Code.BROKER_OPEN_LOCAL_TERMINAL, intent_id, detail)
        if status in UNRESOLVED:
            return None
        problem = self._order_terms_problem(intent_id, record)
        return None if problem is None else _bad(Code.ORDER_MISMATCH, intent_id, problem)

    def _order_terms_problem(self, intent_id: str, record: BrokerOpenOrder) -> str | None:
        """券商订单条款须与本地任一可能的条款相符：签发值、最后一次被接受的改单、尚无结果的改单。"""
        intent = self._repo.get_intent(intent_id)
        issued = self._repo.issuance_for(intent_id)
        if intent is None or issued is None:
            return "本地签发记录缺失"
        if record.symbol != intent.symbol or record.side is not intent.side:
            return "标的或方向与本地不一致"
        current = (issued.quantity, intent.limit_price)
        pending: list[tuple[Decimal, Decimal | None]] = []
        for amendment in self._repo.amendments_for(intent_id):
            if amendment.result == "ACCEPTED":
                current, pending = (amendment.new_quantity, amendment.new_price), []
            elif amendment.result in {None, "UNKNOWN"}:
                pending.append((amendment.new_quantity, amendment.new_price))
        filled = self._oms.filled_quantity(intent_id)
        limit = intent.order_type is OrderType.LIMIT
        for quantity, price in [current, *pending]:
            if record.remaining_quantity == quantity - filled and (
                not limit or record.price == price
            ):
                return None
        quantity, price = current
        if record.remaining_quantity != quantity - filled:
            return f"剩余数量不一致：本地 {quantity - filled}，券商 {record.remaining_quantity}"
        return f"限价不一致：本地 {price}，券商 {record.price}"

    def _account_of(self, intent_id: str) -> str | None:
        intent = self._repo.get_intent(intent_id)
        return None if intent is None else intent.account_id

    # ---------- 成交 ----------

    def _check_executions(self, fills: Sequence[BrokerFill], account_id: str) -> list[Discrepancy]:
        items: list[Discrepancy] = []
        acknowledged = self._references(account_id, "ACKNOWLEDGE")
        for fill in fills:
            ours = owned_by_us(self._repo, fill.broker_order_id, fill.order_ref, fill.perm_id)
            if ours and not self._repo.has_execution(fill.execution_id):
                detail = "本系统订单的成交未入账"
                items.append(_bad(Code.EXECUTION_MISSING, fill.execution_id, detail))
            elif not ours and fill.execution_id not in acknowledged:
                detail = "外来成交未经人工确认"
                items.append(_bad(Code.FOREIGN_EXECUTION, fill.execution_id, detail))
        seen = {i.key for i in items}
        for execution_id, reason, _payload in self._repo.unmatched_executions():
            if execution_id not in acknowledged and execution_id not in seen:
                detail = f"未匹配成交未经人工确认：{reason}"
                items.append(_bad(Code.FOREIGN_EXECUTION, execution_id, detail))
        return items

    # ---------- 持仓 ----------

    def expected_positions(self, account_id: str) -> dict[str, Decimal]:
        """预期持仓 = 持仓调整合计 + 本系统有效成交（买正卖负）。"""
        expected: dict[str, Decimal] = {}
        for adjustment in self._repo.position_adjustments(account_id):
            expected[adjustment.symbol] = (
                expected.get(adjustment.symbol, Decimal(0)) + adjustment.quantity_delta
            )
        for intent_id in self._repo.submitted_intents():
            if self._account_of(intent_id) != account_id:
                continue
            for execution in self._repo.effective_executions_for(intent_id):
                sign = Decimal(1) if execution.side is Side.BUY else Decimal(-1)
                expected[execution.symbol] = (
                    expected.get(execution.symbol, Decimal(0)) + sign * execution.quantity
                )
        return expected

    async def _check_positions(
        self, broker: Mapping[str, Decimal], account_id: str, *, match_actions: bool = True
    ) -> list[Discrepancy]:
        items: list[Discrepancy] = [
            _bad(Code.NEGATIVE_POSITION, symbol, f"券商持仓为负：{broker[symbol]}")
            for symbol in sorted(broker)
            if broker[symbol] < 0
        ]
        expected = self.expected_positions(account_id)
        mismatched = sorted(
            s
            for s in set(broker) | set(expected)
            if broker.get(s, Decimal(0)) != expected.get(s, Decimal(0))
        )
        if not mismatched:
            return items
        actions: list[CorporateAction] = []
        if match_actions:
            try:
                actions = list(await self._corporate_actions.actions(mismatched))
            except Exception as exc:
                detail = f"公司行动数据取不到：{type(exc).__name__}"
                items.append(_bad(Code.SOURCE_UNAVAILABLE, "corporate_actions", detail))
        for symbol in mismatched:
            have, want = broker.get(symbol, Decimal(0)), expected.get(symbol, Decimal(0))
            events = self._eligible_events(account_id, symbol, actions)
            if events and want > 0 and _matches(want, have, events):
                self._apply_corporate_action(account_id, symbol, want, have, events)
                refs = "、".join(a.event_id for a in events)
                detail = f"公司行动 {refs}：{want} → {have}（保护性止损须同步调整，R-06）"
                items.append(Discrepancy(Code.CORPORATE_ACTION, Status.EXPLAINED, symbol, detail))
            else:
                detail = f"持仓不一致：预期 {want}，券商 {have}"
                items.append(_bad(Code.POSITION_MISMATCH, symbol, detail))
        return items

    def _eligible_events(
        self, account_id: str, symbol: str, actions: Sequence[CorporateAction]
    ) -> list[CorporateAction]:
        """已生效、晚于该标的最近一次基准、尚未用过的事件，按生效时间排序。"""
        adjustments = self._repo.position_adjustments(account_id)
        applied = {a.reference for a in adjustments if a.kind == "CORPORATE_ACTION"}
        baselines = [
            a.recorded_at for a in adjustments if a.kind == "BASELINE" and a.symbol == symbol
        ]
        since = max(baselines) if baselines else None
        now = self._clock()
        events = [
            a
            for a in actions
            if a.symbol == symbol
            and _action_ref(a) not in applied
            and a.effective_at <= now
            and (since is None or a.effective_at > since)
        ]
        return sorted(events, key=lambda a: (a.effective_at, a.event_id))

    def _apply_corporate_action(
        self,
        account_id: str,
        symbol: str,
        want: Decimal,
        have: Decimal,
        events: Sequence[CorporateAction],
    ) -> None:
        """逐事件写调整（每个事件一条，引用唯一）；最后一个事件吸收零股取整，合计 = have − want。"""
        quantity = want
        for index, event in enumerate(events):
            last = index == len(events) - 1
            after = have if last else (quantity * event.numerator) // event.denominator
            self._adjust(
                account_id,
                symbol,
                after - quantity,
                kind="CORPORATE_ACTION",
                reference=_action_ref(event),
                actor=self._actor,
            )
            quantity = after

    def _references(self, account_id: str, kind: str) -> set[str]:
        return {a.reference for a in self._repo.position_adjustments(account_id) if a.kind == kind}

    # ---------- 盘中时序规则 ----------

    def _apply_timing_rule(self, items: list[Discrepancy]) -> list[Discrepancy]:
        current = {(i.code, i.key) for i in items if i.code in TIMING_CODES}
        seen_before = set().union(*self._history) if self._history else set()
        out = []
        for item in items:
            if item.code in TIMING_CODES and (item.code, item.key) not in seen_before:
                out.append(Discrepancy(item.code, Status.PENDING, item.key, item.detail))
            else:
                out.append(item)
        self._history.append(current)
        return out

    # ---------- 基准采用（人工） ----------

    async def adopt_broker_positions(self, *, actor: str, confirmation: str) -> list[str]:
        """人工确认以券商持仓为基准：只能在 HALT 下由人工发起，逐字输入 ADOPT_PHRASE。

        写 BASELINE 调整使预期持仓等于券商持仓，并确认现存的外来成交（ACKNOWLEDGE）；有未决订单或
        本系统成交未入账时拒绝（否则之后会重复计数）。不恢复交易：之后仍须对账通过、由人工恢复。
        返回写入的调整号；审计里记下每个标的的调整量与确认的成交号。
        """
        who = actor.strip()
        if not who or who.lower() in NON_HUMAN_ACTORS:
            raise ReconError("基准只能由人工操作者确认")
        if confirmation != ADOPT_PHRASE:
            raise ReconError("确认短语不一致，未采用券商持仓")
        self._require_halted()
        account = self._modes.verified_account()
        if account is None:
            raise ReconError("本进程尚未完成启动校验")
        account_id = account.account_id
        expected_before = self.expected_positions(account_id)
        snapshot = await self._snapshot(account_id)
        if snapshot.foreign_accounts:
            raise ReconError("券商记录里有不属于已校验账户的条目，先查清再采用基准")
        self._require_settled_orders(snapshot, account_id)
        self._require_halted()  # 取数期间可能已恢复：写入前再查一次
        batch = self._new_id()
        expected = self.expected_positions(account_id)
        if expected != expected_before:  # 取数期间有成交入账：持仓快照可能没包含它
            raise ReconError("取数期间有成交入账，请稍后重新采用基准")
        deltas = {
            symbol: snapshot.positions.get(symbol, Decimal(0)) - expected.get(symbol, Decimal(0))
            for symbol in sorted(set(snapshot.positions) | set(expected))
        }
        written = [
            self._adjust(
                account_id, symbol, delta, kind="BASELINE", reference=f"{batch}:{symbol}", actor=who
            )
            for symbol, delta in deltas.items()
            if delta != 0
        ]
        acknowledged = self._references(account_id, "ACKNOWLEDGE")
        foreign = {
            f.execution_id
            for f in snapshot.fills
            if not owned_by_us(self._repo, f.broker_order_id, f.order_ref, f.perm_id)
        }
        foreign |= {e for e, _r, _p in self._repo.unmatched_executions()}
        newly = sorted(foreign - acknowledged)
        written += [
            self._adjust(account_id, "-", Decimal(0), kind="ACKNOWLEDGE", reference=e, actor=who)
            for e in newly
        ]
        record_audit(
            self._repo,
            action=ADOPT_ACTION,
            actor=who,
            at=self._clock(),
            detail={
                "batch": batch,
                "deltas": json.dumps({s: str(d) for s, d in deltas.items() if d != 0}),
                "acknowledged": json.dumps(newly),
            },
        )
        log_event(log, logging.WARNING, "已采用券商持仓为对账基准", adjustments=len(written))
        return written

    def _require_halted(self) -> None:
        state = self._modes.current()
        if state is None or not state.halt.halted:
            raise ReconError("只能在 HALT 下采用基准")

    def _require_settled_orders(self, snapshot: _Snapshot, account_id: str) -> None:
        for intent_id in self._repo.submitted_intents():
            if (
                self._account_of(intent_id) == account_id
                and self._oms.status(intent_id) in UNRESOLVED
            ):
                raise ReconError(f"订单 {intent_id} 状态未决，先查证再采用基准")
        for fill in snapshot.fills:
            ours = owned_by_us(self._repo, fill.broker_order_id, fill.order_ref, fill.perm_id)
            if ours and not self._repo.has_execution(fill.execution_id):
                raise ReconError(f"成交 {fill.execution_id} 尚未入账，先处理回报再采用基准")

    def _adjust(  # noqa: PLR0913 - 调整记录的字段全部显式给出
        self, account_id: str, symbol: str, delta: Decimal, *, kind: str, reference: str, actor: str
    ) -> str:
        adjustment_id = self._new_id()
        self._repo.add_position_adjustment(
            PositionAdjustment(
                adjustment_id=adjustment_id,
                account_id=account_id,
                symbol=symbol,
                quantity_delta=delta,
                kind=kind,
                reference=reference,
                actor=actor,
                recorded_at=self._clock(),
            )
        )
        return adjustment_id

    # ---------- 报告 ----------

    def _record(self, report: ReconReport) -> None:
        items = [
            {"code": i.code.value, "status": i.status.value, "key": i.key, "detail": i.detail}
            for i in report.items
        ]
        try:
            record_audit(
                self._repo,
                action=RECON_ACTION,
                actor=self._actor,
                at=report.at,
                detail={
                    "recon_id": report.recon_id,
                    "kind": report.kind.value,
                    "clean": str(report.clean),
                    "settled": str(report.settled),
                    "items": json.dumps(items, ensure_ascii=False),
                },
            )
        except Exception as exc:  # 报告写不进去不影响已经发生的 SAFE HALT
            log_event(log, logging.ERROR, "对账报告写入失败", error=type(exc).__name__)
        level = logging.INFO if report.clean else logging.ERROR
        log_event(log, level, "对账完成", kind=report.kind.value, items=len(report.items))

    @property
    def last_report(self) -> ReconReport | None:
        """本实例最近一次完成的对账报告（apid 展示用的只读访问器）。"""
        return self._last

    def last_clean(self) -> bool:
        """ResumeChecks.reconciled 的依据。

        本实例最近一次对账没有未解释、没有待观察项；处于 HALT 时，还要求对账开始之后没有新的
        模式变更（用变更编号比较，不依赖时钟精度）。
        """
        report = self._last
        if report is None or not report.settled:
            return False
        state = self._modes.current()
        if state is None or not state.halt.halted:
            return True
        return state.change_id == report.mode_change_id  # 对账开始之后没有新的模式变更


class ReconError(Exception):
    """对账相关的人工操作被拒绝。"""


def _bad(code: Code, key: str, detail: str) -> Discrepancy:
    return Discrepancy(code, Status.UNEXPLAINED, key, detail)


def _action_ref(action: CorporateAction) -> str:
    return f"{action.event_id}:{action.symbol}"


def _matches(expected: Decimal, broker: Decimal, events: Sequence[CorporateAction]) -> bool:
    """公司行动能否解释差异：预期 × Π(新/旧) = 券商持仓（整数比例精确比较），
    或逐事件向下取整后相等（零股以现金补偿）。
    """
    numerator, denominator = 1, 1
    for event in events:
        numerator *= event.numerator
        denominator *= event.denominator
    scaled = expected * numerator
    if broker * denominator == scaled:
        return True
    stepwise = expected  # 零股：券商逐事件向下取整（现金补偿零股）
    for event in events:
        stepwise = (stepwise * event.numerator) // event.denominator
    return broker == stepwise


__all__ = (
    "ADOPT_PHRASE",
    "Code",
    "CorporateAction",
    "CorporateActionSource",
    "Discrepancy",
    "ExecutionSource",
    "ReconError",
    "ReconKind",
    "ReconReport",
    "Reconciler",
    "Status",
)
