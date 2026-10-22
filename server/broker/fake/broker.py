"""FakeBroker（B-01 接口，B-02 故障注入）：确定性的内存券商，供契约、OMS 与故障场景测试使用。

结构：
- `FakeBrokerServer` 是券商侧：订单、成交、账本（撮合内核 `server.broker.sim.kernel`）
  和已连接的客户端。订单号全局单调；断线不影响券商侧状态，
  进程崩溃后用同一个 server 新建客户端即可查回。
- `FakeBroker` 是一个 clientId 的连接，实现 `BrokerAdapter`。`FakeBroker(account)` 自带一个 server，
  可以单独使用（B-01 契约）。
- 只有 backtest 与测试可以导入本包（import-linter 契约 fake-broker-test-only）：
  server 内的 guard、oms 等不能借 server 的控制面绕过 clientId 隔离或撤单保护。

券商事实：
- 下单立即回报 ACKNOWLEDGED 并分配 permId；成交由测试或 `match_bar()` 驱动，限价单成交价不劣于限价。
- 撤单先报 CANCEL_PENDING，默认立即 CANCELLED；`manual_cancel_confirm` 打开后由测试确认或拒绝。
  撤单期间的部分成交保持 CANCEL_PENDING（与 C-02 一致）。
- 撤单保护以券商侧订单类型为准：只有 LIMIT 可以不经确认撤销，STOP 与认不出的 OTHER 一律需要确认。
- 买入资金不足（购买力 − 未成交买单占用 < 成交额 + 预估佣金）以拒单回报，不占用资金；
  购买力默认只算已结算现金（保守口径）。
- 现金账户默认拒绝数量超过持仓的卖单（不能卖空）。
- 事件只发给已连接的客户端：订单事件发给下单的客户端，外来订单与成交发给全部客户端；
  断线期间的事件丢失，重连后只能靠查询得到。
- 时钟严格递增（同一时刻的多次读取相差 1 微秒）；按 K 线撮合的成交时间不早于 K 线开始。
- 只记录券商事实，不做风控判断（风控在 server/risk）。

以上凡涉及 IBKR 行为的地方（未结算款能否买入、现金账户卖单的检查口径、拆股时挂单如何处理、
断线重连后 ib_async 是否主动同步、DAY 到期的报法、成交更正的 execId 规则等）都**未实测**，
清单见 docs/tasks/B-02.md 的移交小节，B-05 实测后修正本替身。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from server.broker.base import (
    TERMINAL_STATES,
    AccountMismatchError,
    AmendmentRejectedError,
    BrokerAdapter,
    BrokerError,
    BrokerEvent,
    BrokerExecution,
    BrokerOrderState,
    BrokerOrderType,
    BrokerUnavailableError,
    CommissionReport,
    ConnectionEvent,
    ConnectionState,
    DuplicateOrderError,
    ErrorCategory,
    ExecutionReport,
    OrderAmendment,
    OrderLookup,
    OrderRecord,
    OrderSource,
    OrderStatusUpdate,
    OrderTicket,
    ProtectiveStopCancelError,
    UnknownOrderError,
)
from server.broker.sim.kernel import (
    CommissionModel,
    Ledger,
    NoCommission,
    RestingOrder,
    Trade,
    match_bar,
)
from server.domain.account import Account, CashBalance, Position
from server.domain.enums import AccountType, OrderType, Side
from server.domain.market import MarketBar
from server.domain.values import to_utc

# 外来订单的券商原始类型 → 券商侧类型；止损类（含跟踪止损）一律归为 STOP，其余认不出的归为 OTHER
_FOREIGN_TYPES = {
    "LMT": BrokerOrderType.LIMIT,
    "STP": BrokerOrderType.STOP,
    "STP LMT": BrokerOrderType.STOP,
    "TRAIL": BrokerOrderType.STOP,
    "TRAIL LIMIT": BrokerOrderType.STOP,
}
_ACTIVE = frozenset(
    {
        BrokerOrderState.ACKNOWLEDGED,
        BrokerOrderState.PARTIALLY_FILLED,
        BrokerOrderState.CANCEL_PENDING,
    }
)
_RAW_TYPE = {BrokerOrderType.LIMIT: "LMT", BrokerOrderType.STOP: "STP"}
_SATURDAY = 5


def next_weekday(executed_at: datetime) -> date:
    """默认结算日：成交日（UTC）后的下一个工作日。

    局限：不认交易所假日，美东晚间成交按 UTC 次日算。回测与模拟应传入按 D-03 交易日历计算的 T+1。
    """
    day = executed_at.date() + timedelta(days=1)
    while day.weekday() >= _SATURDAY:
        day += timedelta(days=1)
    return day


@dataclass
class _Order:
    order_ref: str | None
    client_id: int | None  # 外来订单为空
    account_id: str
    symbol: str
    side: Side
    order_type: BrokerOrderType
    raw_order_type: str
    time_in_force: str
    perm_id: str
    state: BrokerOrderState
    quantity: Decimal
    price: Decimal | None
    submitted_at: datetime
    filled: Decimal = Decimal(0)

    @property
    def remaining(self) -> Decimal:
        # CANCEL_REJECTED 之后订单仍然活着，剩余量照实报出
        return Decimal(0) if self.state in TERMINAL_STATES else self.quantity - self.filled


@dataclass(frozen=True)
class _Reject:
    category: ErrorCategory
    reason: str


@dataclass(frozen=True)
class _Held:
    client_id: int | None
    event: BrokerEvent
    audience: frozenset[int]  # 暂存时已连接的客户端（对象 id）：之后重连的客户端收不到


class FakeBrokerServer:
    """券商侧状态与故障注入控制面。只在测试与回测中使用，不连接任何真实券商。"""

    def __init__(  # noqa: PLR0913 - 测试替身的初始状态全部显式给出
        self,
        account: Account,
        *,
        settled_cash: Decimal = Decimal(100_000),
        positions: dict[str, tuple[Decimal, Decimal]] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        first_order_id: int = 1,
        commission: CommissionModel | None = None,
        settlement: Callable[[datetime], date] = next_weekday,
        cross_client_cancel: bool = False,
        buy_with_unsettled: bool = False,
        reject_uncovered_sells: bool | None = None,
    ) -> None:
        self.account = account
        self.ledger = Ledger(settled=settled_cash, positions=positions)
        self._clock = clock
        self._last_time: datetime | None = None
        self._next_id = first_order_id
        self._foreign_seq = 0
        self._commission: CommissionModel = commission or NoCommission()
        self._settlement = settlement
        # IBKR 是否允许跨 clientId 撤单需要 B-03 实测；默认不允许（guard 不能假设能撤别人的单）
        self.cross_client_cancel = cross_client_cancel
        # 未实测：现金账户能否用未结算卖出款买入；默认不能（保守）
        self.buy_with_unsettled = buy_with_unsettled
        # 未实测：IBKR 现金账户对卖单的检查口径；默认现金账户拒绝数量超过持仓的卖单
        self.reject_uncovered_sells = (
            account.account_type is AccountType.CASH
            if reject_uncovered_sells is None
            else reject_uncovered_sells
        )
        self._orders: dict[str, _Order] = {}
        self._executions: list[BrokerExecution] = []
        self._trades: dict[str, Trade] = {}  # execution_id → 入账的成交
        self._superseded: set[str] = set()  # 已被更正的 execution_id
        self._revisions: dict[str, int] = {}  # 原始 execution_id → 已更正次数
        self._commissions: dict[str, Decimal] = {}
        self._clients: dict[int, FakeBroker] = {}
        self._run = uuid.uuid4().hex[:8]  # 运行级前缀：permId 与 execId 不与别的实例撞号
        self._seq = 0
        # ---- 故障注入状态 ----
        self._held: list[_Held] | None = None
        self._restart_window = False
        self._lose_next_ack = False
        self._lose_next_request = False
        self._reject_next: _Reject | None = None
        self.manual_cancel_confirm = False
        self.commission_late = False
        self._late_commissions: list[str] = []

    # ---------- 连接 ----------

    def _attach(self, client: FakeBroker) -> None:
        if self._restart_window:
            raise BrokerUnavailableError("券商重启窗口内无法连接")
        current = self._clients.get(client.client_id)
        if current is not None and current is not client:
            raise BrokerUnavailableError(f"clientId {client.client_id} 已被占用")
        self._clients[client.client_id] = client

    def _detach(self, client: FakeBroker) -> None:
        if self._clients.get(client.client_id) is client:
            del self._clients[client.client_id]

    def drop_client(self, client_id: int) -> None:
        """连接意外中断（网络、TWS 崩溃）：客户端收到 DISCONNECTED，此后的事件全部丢失。"""
        client = self._clients.pop(client_id, None)
        if client is not None:
            client._lost(ConnectionState.DISCONNECTED, self._now())

    def enter_restart_window(self) -> None:
        """券商每日重启窗口：所有客户端断开并收到 RESTART_WINDOW，窗口结束前不能重连。"""
        self._restart_window = True
        at = self._now()
        for client in list(self._clients.values()):
            client._lost(ConnectionState.RESTART_WINDOW, at)
        self._clients.clear()

    def exit_restart_window(self) -> None:
        self._restart_window = False

    # ---------- 事件投递 ----------

    def hold_events(self) -> None:
        """暂存此后的订单与成交事件（连接事件不受影响），用于构造乱序回报。

        释放时只投递给暂存时已经连接、且仍在连接的客户端（断线期间的事件不补发）。
        """
        if self._held is None:
            self._held = []

    def release_events(self, *, reverse: bool = False) -> None:
        held, self._held = self._held or [], None
        for item in reversed(held) if reverse else held:
            for client in self._targets(item.client_id):
                if id(client) in item.audience:
                    client._deliver(item.event)

    def _targets(self, client_id: int | None) -> list[FakeBroker]:
        if client_id is None:
            return list(self._clients.values())
        target = self._clients.get(client_id)
        return [target] if target is not None else []

    def _send(self, client_id: int | None, event: BrokerEvent) -> None:
        targets = self._targets(client_id)
        if self._held is not None:
            self._held.append(_Held(client_id, event, frozenset(id(c) for c in targets)))
            return
        for client in targets:
            client._deliver(event)

    # ---------- 故障开关 ----------

    def lose_next_ack(self) -> None:
        """下一张订单券商已接收，但确认回报丢失（场景 1：响应丢失）。"""
        self._lose_next_ack = True

    def lose_next_request(self) -> None:
        """下一张订单的请求没有到达券商：不建单、无回报（场景 1 的另一种结局）。"""
        self._lose_next_request = True

    def reject_next(self, category: ErrorCategory, reason: str) -> None:
        """下一张订单被券商拒绝（场景 5：价格偏离、无权限、合约未找到等）。"""
        self._reject_next = _Reject(category, reason)

    # ---------- 查询（客户端已校验连接） ----------

    def _positions(self) -> list[Position]:
        now = self._now()
        return [
            Position(
                account_id=self.account.account_id,
                symbol=h.symbol,
                quantity=h.quantity,
                # 不含佣金（IBKR 的 avgCost 含佣金，R-07 对账时注意口径）
                average_cost=h.average_cost,
                as_of=now,
            )
            for h in self.ledger.positions()
        ]

    def _cash(self) -> CashBalance:
        return CashBalance(
            account_id=self.account.account_id,
            currency="USD",
            settled=self.ledger.settled,
            unsettled=self.ledger.unsettled,
            as_of=self._now(),
        )

    def _open_orders(self) -> list[OrderRecord]:
        """全部未结束订单（含别的 clientId 与外来订单），相当于 IBKR 的 reqAllOpenOrders。"""
        return [
            self._record(oid, o, OrderSource.OPEN)
            for oid, o in self._orders.items()
            if o.state in _ACTIVE
        ]

    def _completed_orders(self) -> list[OrderRecord]:
        return [
            self._record(oid, o, OrderSource.COMPLETED)
            for oid, o in self._orders.items()
            if o.state in TERMINAL_STATES
        ]

    def executions(
        self, since: datetime, until: datetime | None = None, order_ref: str | None = None
    ) -> list[BrokerExecution]:
        start = to_utc(since)
        end = to_utc(until) if until is not None else None
        return [
            e
            for e in self._executions
            if e.executed_at >= start
            and (end is None or e.executed_at < end)
            and (order_ref is None or e.order_ref == order_ref)
        ]

    def _lookup(self, order_ref: str) -> OrderLookup:
        records = tuple(
            self._record(oid, o, OrderSource.OPEN if o.state in _ACTIVE else OrderSource.COMPLETED)
            for oid, o in self._orders.items()
            if o.order_ref == order_ref
        )
        execs = tuple(e for e in self._executions if e.order_ref == order_ref)
        return OrderLookup(order_ref=order_ref, orders=records, executions=execs)

    def _allocate_id(self) -> str:
        oid = str(self._next_id)
        self._next_id += 1
        return oid

    # ---------- 订单（只由 FakeBroker 客户端调用） ----------

    def _place(self, client_id: int, ticket: OrderTicket) -> None:
        intent = ticket.intent
        if intent.account_id != self.account.account_id:
            raise AccountMismatchError("票据账户与连接的账户不一致")
        oid = ticket.broker_order_id
        if oid in self._orders:
            raise DuplicateOrderError(f"订单号 {oid} 已提交过")
        if self._lose_next_request:
            self._lose_next_request = False
            return
        order_type = (
            BrokerOrderType.LIMIT if intent.order_type is OrderType.LIMIT else BrokerOrderType.STOP
        )
        order = _Order(
            order_ref=ticket.order_ref,
            client_id=client_id,
            account_id=intent.account_id,
            symbol=intent.symbol,
            side=intent.side,
            order_type=order_type,
            raw_order_type=_RAW_TYPE[order_type],
            time_in_force=intent.time_in_force.value,
            perm_id=self._new_perm_id(),
            state=BrokerOrderState.ACKNOWLEDGED,
            quantity=ticket.quantity,
            price=ticket.price,
            submitted_at=self._now(),
        )
        reject, self._reject_next = self._reject_next, None
        if reject is None:
            reject = self._account_check(order, order.quantity, order.price)
        self._orders[oid] = order
        if reject is not None:
            order.state = BrokerOrderState.REJECTED
            self._reject(oid, order, reject)
            return
        if self._lose_next_ack:
            self._lose_next_ack = False
            return
        self._emit_status(oid, order)

    def _account_check(
        self, order: _Order, quantity: Decimal, price: Decimal | None, *, exclude: str | None = None
    ) -> _Reject | None:
        """券商侧的账户检查：买入看购买力（扣除其他未成交买单的占用），现金账户卖出不得超过持仓。"""
        if price is None:  # 本系统的订单都有价格；没有价格无法估算，按不通过处理
            return _Reject(ErrorCategory.OTHER, "缺少价格")
        if order.side is Side.SELL:
            held = self.ledger.position(order.symbol).quantity
            if self.reject_uncovered_sells and quantity - order.filled > held:
                return _Reject(ErrorCategory.NO_PERMISSION, "现金账户不能卖空（卖出数量超过持仓）")
            return None
        reserved = sum(
            (
                o.remaining * o.price + self._commission(o.remaining, o.price)
                for oid, o in self._orders.items()
                if oid != exclude
                and o.side is Side.BUY
                and o.state in _ACTIVE
                and o.price is not None
            ),
            Decimal(0),
        )
        unfilled = quantity - order.filled
        cost = unfilled * price + self._commission(unfilled, price)
        power = self.ledger.buying_power(include_unsettled=self.buy_with_unsettled)
        if power - reserved < cost:
            return _Reject(ErrorCategory.INSUFFICIENT_FUNDS, "购买力不足")
        return None

    def _modify(self, client_id: int, amendment: OrderAmendment) -> None:
        oid = amendment.broker_order_id
        order = self._orders.get(oid)
        if order is None or order.state not in _ACTIVE or order.client_id != client_id:
            raise UnknownOrderError(f"订单号 {oid} 不存在、已结束或不属于本连接")
        if order.order_ref != amendment.order_ref:
            raise UnknownOrderError("改单必须针对同一意图")
        # 先校验、再修改：不合法时订单保持原样
        if order.state is BrokerOrderState.CANCEL_PENDING:
            raise AmendmentRejectedError("撤单进行中，不能改单")
        if amendment.new_quantity <= order.filled:
            raise AmendmentRejectedError("新数量必须大于已成交数量")
        problem = self._account_check(
            order, amendment.new_quantity, amendment.new_price, exclude=oid
        )
        if problem is not None:
            raise AmendmentRejectedError(problem.reason)
        order.quantity = amendment.new_quantity
        order.price = amendment.new_price
        self._emit_status(oid, order)

    def _cancel(self, client_id: int, broker_order_id: str, *, allow_protective_stop: bool) -> None:
        order = self._orders.get(broker_order_id)
        if order is None or order.state not in _ACTIVE:
            raise UnknownOrderError(f"订单号 {broker_order_id} 不存在或已结束")
        # 以券商侧类型为准：外来止损单没有票据，同样受保护；
        # 类型认不出（OTHER）也按止损对待（失败即关闭）
        if order.order_type is not BrokerOrderType.LIMIT and not allow_protective_stop:
            raise ProtectiveStopCancelError("撤销保护性止损需要显式确认")
        if order.client_id != client_id and not self.cross_client_cancel:
            raise UnknownOrderError(f"订单号 {broker_order_id} 不属于本连接")
        self._begin_cancel(broker_order_id, order)

    def broker_side_cancel(self, broker_order_id: str) -> None:
        """券商侧撤单（有人在 TWS 手工撤单、券商撤单等）：不经本系统，用于状态漂移场景。"""
        order = self._orders[broker_order_id]
        if order.state not in _ACTIVE:
            raise UnknownOrderError(f"订单号 {broker_order_id} 已结束")
        self._begin_cancel(broker_order_id, order)

    def _begin_cancel(self, oid: str, order: _Order) -> None:
        if order.state is BrokerOrderState.CANCEL_PENDING:
            return  # 重复撤单：券商只处理一次
        order.state = BrokerOrderState.CANCEL_PENDING
        self._emit_status(oid, order)
        if not self.manual_cancel_confirm:
            self._finish_cancel(oid, order)

    def confirm_cancel(self, broker_order_id: str) -> None:
        """确认撤单（场景 4）。撤单期间已全部成交时报撤单无效。"""
        order = self._orders[broker_order_id]
        if order.state is BrokerOrderState.FILLED:
            self._send(
                order.client_id,
                BrokerError(
                    category=ErrorCategory.CANCEL_REJECTED,
                    code="FAKE-CANCEL-TOO-LATE",
                    message="订单已全部成交，撤单无效",
                    retryable=False,
                    broker_order_id=broker_order_id,
                    at=self._now(),
                ),
            )
            return
        if order.state is not BrokerOrderState.CANCEL_PENDING:
            raise UnknownOrderError(f"订单号 {broker_order_id} 没有待确认的撤单")
        self._finish_cancel(broker_order_id, order)

    def reject_cancel(self, broker_order_id: str, reason: str) -> None:
        """拒绝撤单：报 CANCEL_REJECTED（剩余量照实），订单回到撤单前的活动状态。"""
        order = self._orders[broker_order_id]
        if order.state is not BrokerOrderState.CANCEL_PENDING:
            raise UnknownOrderError(f"订单号 {broker_order_id} 没有待确认的撤单")
        order.state = BrokerOrderState.CANCEL_REJECTED
        self._emit_status(broker_order_id, order, reason=reason)
        order.state = (
            BrokerOrderState.PARTIALLY_FILLED if order.filled > 0 else BrokerOrderState.ACKNOWLEDGED
        )
        self._emit_status(broker_order_id, order)

    def _finish_cancel(self, oid: str, order: _Order) -> None:
        order.state = BrokerOrderState.CANCELLED
        self._emit_status(oid, order)

    def expire_day_orders(self, *, as_cancelled: bool = False) -> list[str]:
        """收盘后当日有效订单到期。IBKR 可能以 Cancelled 报出（as_cancelled=True），未实测。"""
        state = BrokerOrderState.CANCELLED if as_cancelled else BrokerOrderState.EXPIRED
        expired = [
            oid
            for oid, o in self._orders.items()
            if o.state in _ACTIVE and o.time_in_force == "DAY"
        ]
        for oid in expired:
            order = self._orders[oid]
            order.state = state
            self._emit_status(oid, order, reason="当日有效订单到期")
        return expired

    # ---------- 成交 ----------

    def fill(
        self,
        broker_order_id: str,
        quantity: Decimal,
        price: Decimal,
        *,
        not_before: datetime | None = None,
    ) -> BrokerExecution:
        """按给定数量与价格成交：数量不超过剩余，限价单的价格不劣于限价（买不高于、卖不低于）。"""
        order = self._orders.get(broker_order_id)
        if order is None or order.state not in _ACTIVE:
            raise UnknownOrderError(f"订单号 {broker_order_id} 不可成交")
        if quantity <= 0 or quantity > order.remaining:
            raise ValueError("成交数量必须为正且不超过剩余数量")
        if order.order_type is BrokerOrderType.LIMIT and order.price is not None:
            worse = price > order.price if order.side is Side.BUY else price < order.price
            if worse:
                raise ValueError("成交价劣于限价")
        execution = self._book(order, broker_order_id, quantity, price, not_before=not_before)
        if order.filled == order.quantity:
            order.state = BrokerOrderState.FILLED
        elif order.state is not BrokerOrderState.CANCEL_PENDING:
            order.state = BrokerOrderState.PARTIALLY_FILLED  # 撤单期间的部分成交保持 CANCEL_PENDING
        self._emit_status(broker_order_id, order)
        return execution

    def match_bar(
        self, bar: MarketBar, participation: Decimal | None = None
    ) -> list[BrokerExecution]:
        """用一根 K 线撮合该标的的全部活动订单。

        下单晚于 K 线开始的不参与；成交时间不早于 K 线开始。
        """
        fills: list[BrokerExecution] = []
        for oid, order in list(self._orders.items()):
            eligible = (
                order.symbol == bar.symbol
                and order.state in _ACTIVE
                and order.order_type is not BrokerOrderType.OTHER
                and order.submitted_at <= bar.start
            )
            if not eligible or order.price is None:
                continue
            resting = RestingOrder(
                side=order.side,
                stop=order.order_type is BrokerOrderType.STOP,
                price=order.price,
                remaining=order.remaining,
                submitted_at=order.submitted_at,
            )
            hit = match_bar(resting, bar, participation)
            if hit is not None:
                fills.append(self.fill(oid, hit.quantity, hit.price, not_before=bar.start))
        return fills

    def duplicate_execution(self, execution_id: str) -> None:
        """把一笔成交回报再发一次（场景 2）。"""
        execution = self._execution(execution_id)
        self._send(self._owner(execution), ExecutionReport(execution=execution))

    def late_fill(self, broker_order_id: str, quantity: Decimal, price: Decimal) -> BrokerExecution:
        """订单已撤销或到期后才报出的成交（终态后迟到成交）。"""
        order = self._orders[broker_order_id]
        if order.state not in {BrokerOrderState.CANCELLED, BrokerOrderState.EXPIRED}:
            raise ValueError("迟到成交只用于已撤销或已到期的订单")
        if quantity <= 0 or order.filled + quantity > order.quantity:
            raise ValueError("迟到成交数量必须为正且不超过未成交数量")
        return self._book(order, broker_order_id, quantity, price)

    def correct_execution(
        self, execution_id: str, quantity: Decimal, price: Decimal
    ) -> BrokerExecution:
        """成交更正：发出一笔新成交，corrects 指向被更正的那笔；只能更正当前有效的成交。

        新 execution_id 为“原始 id.序号”（序号逐次递增；IBKR 的实际规则待 B-05 实测），
        账本按与当前有效那笔的差额调整，订单已成交量随之调整。先校验、后入账。
        """
        current = self._execution(execution_id)
        if execution_id in self._superseded:
            raise ValueError("只能更正当前有效的成交")
        if quantity <= 0 or price <= 0:
            raise ValueError("更正后的数量与价格必须为正")
        order = self._orders.get(current.broker_order_id or "")
        if order is not None and order.filled - current.quantity + quantity > order.quantity:
            raise ValueError("更正后的成交量不得超过订单数量")
        root = execution_id.split(".", 1)[0]
        revision = self._revisions.get(root, 0) + 1
        old_trade = self._trades[execution_id]
        commission = self._commission(quantity, price) if current.commission is not None else None
        new_commission = self._commission(quantity, price)
        corrected = BrokerExecution(
            execution_id=f"{root}.{revision:02d}",
            order_ref=current.order_ref,
            broker_order_id=current.broker_order_id,
            perm_id=current.perm_id,
            account_id=current.account_id,
            symbol=current.symbol,
            side=current.side,
            quantity=quantity,
            price=price,
            commission=commission,
            executed_at=self._now(),
            corrects=execution_id,
        )
        new_trade = Trade(
            old_trade.side, old_trade.symbol, quantity, price, new_commission, old_trade.settle_on
        )
        # ---- 校验全部通过，入账 ----
        self.ledger.correct_fill(old_trade, new_trade)
        self._revisions[root] = revision
        self._superseded.add(execution_id)
        self._trades[corrected.execution_id] = new_trade
        self._commissions[corrected.execution_id] = new_commission
        if order is not None:
            order.filled += quantity - current.quantity
        self._executions.append(corrected)
        if commission is None:
            self._late_commissions.append(corrected.execution_id)
        self._send(self._owner(corrected), ExecutionReport(execution=corrected))
        return corrected

    def report_commissions(self) -> None:
        """佣金后到模式下，补发全部未报的佣金回报。"""
        pending, self._late_commissions = self._late_commissions, []
        for execution_id in pending:
            execution = self._execution(execution_id)
            self._send(
                self._owner(execution),
                CommissionReport(
                    execution_id=execution_id,
                    commission=self._commissions[execution_id],
                    at=self._now(),
                ),
            )

    # ---------- 外来订单、公司行动与券商侧变动 ----------

    def inject_foreign_order(
        self,
        symbol: str,
        side: Side,
        quantity: Decimal,
        raw_order_type: str = "TRAIL",
        price: Decimal | None = None,
    ) -> str:
        """模拟一张非本系统下的挂单（TWS 手工单、别的 clientId）：没有 orderRef。"""
        if raw_order_type == "LMT" and price is None:
            raise ValueError("外来限价单必须带价格")
        self._foreign_seq += 1
        oid = f"foreign-{self._foreign_seq}"  # 不占用本系统的订单号序列
        order = _Order(
            order_ref=None,
            client_id=None,
            account_id=self.account.account_id,
            symbol=symbol,
            side=side,
            order_type=_FOREIGN_TYPES.get(raw_order_type, BrokerOrderType.OTHER),
            raw_order_type=raw_order_type,
            time_in_force="GTC",
            perm_id=self._new_perm_id(),
            state=BrokerOrderState.ACKNOWLEDGED,
            quantity=quantity,
            price=price,
            submitted_at=self._now(),
        )
        self._orders[oid] = order
        self._emit_status(oid, order)
        return oid

    def inject_foreign_execution(
        self, symbol: str, side: Side, quantity: Decimal, price: Decimal
    ) -> BrokerExecution:
        """模拟一笔无法关联到本系统订单的成交（如券商强平），数量可以是小数；计入账本。"""
        executed_at = self._now()
        self._seq += 1
        execution = BrokerExecution(
            execution_id=f"{self._run}-exec-{self._seq}",
            order_ref=None,
            broker_order_id=None,
            perm_id=None,
            account_id=self.account.account_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            commission=None,
            executed_at=executed_at,
        )
        trade = Trade(side, symbol, quantity, price, Decimal(0), self._settlement(executed_at))
        self.ledger.apply_fill(trade)
        self._trades[execution.execution_id] = trade
        self._executions.append(execution)
        self._send(None, ExecutionReport(execution=execution))
        return execution

    def adjust_position(self, symbol: str, delta: Decimal) -> None:
        """无回报的券商侧持仓变动（场景 11：持仓与本地不一致）。"""
        self.ledger.adjust_position(symbol, delta)

    def apply_split(self, symbol: str, numerator: Decimal, denominator: Decimal) -> None:
        """拆股或合股（场景 16）：只调整持仓，不动挂单。

        IBKR 拆股时对挂单是调整、撤销还是保持未实测（B-05）；R-07 的测试不得只依赖本行为。
        """
        self.ledger.apply_split(symbol, numerator, denominator)

    def apply_dividend(self, symbol: str, per_share: Decimal) -> None:
        """现金分红（场景 16）。"""
        self.ledger.apply_dividend(symbol, per_share)

    def settle(self, through: date) -> None:
        self.ledger.settle(through)

    # ---------- 内部 ----------

    def _execution(self, execution_id: str) -> BrokerExecution:
        for e in self._executions:
            if e.execution_id == execution_id:
                return e
        raise ValueError(f"没有成交 {execution_id}")

    def _owner(self, execution: BrokerExecution) -> int | None:
        oid = execution.broker_order_id
        order = self._orders.get(oid) if oid is not None else None
        return order.client_id if order is not None else None

    def _new_perm_id(self) -> str:
        self._seq += 1
        return f"{self._run}-perm-{self._seq}"

    def _book(
        self,
        order: _Order,
        oid: str,
        quantity: Decimal,
        price: Decimal,
        *,
        not_before: datetime | None = None,
    ) -> BrokerExecution:
        executed_at = self._now(not_before)
        commission = self._commission(quantity, price)
        self._seq += 1
        execution = BrokerExecution(
            execution_id=f"{self._run}-exec-{self._seq}",
            order_ref=order.order_ref,
            broker_order_id=oid,
            perm_id=order.perm_id,
            account_id=order.account_id,
            symbol=order.symbol,
            side=order.side,
            quantity=quantity,
            price=price,
            commission=None if self.commission_late else commission,
            executed_at=executed_at,
        )
        trade = Trade(
            order.side, order.symbol, quantity, price, commission, self._settlement(executed_at)
        )
        self.ledger.apply_fill(trade)
        order.filled += quantity
        self._trades[execution.execution_id] = trade
        self._executions.append(execution)
        self._commissions[execution.execution_id] = commission
        if self.commission_late:
            self._late_commissions.append(execution.execution_id)
        self._send(order.client_id, ExecutionReport(execution=execution))
        return execution

    def _reject(self, oid: str, order: _Order, reject: _Reject) -> None:
        code = f"FAKE-{reject.category.value}"
        self._send(
            order.client_id,
            BrokerError(
                category=reject.category,
                code=code,
                message=reject.reason,
                retryable=False,
                broker_order_id=oid,
                at=self._now(),
            ),
        )
        self._emit_status(oid, order, reason=reject.reason, error_code=code)

    def _now(self, not_before: datetime | None = None) -> datetime:
        now = to_utc(self._clock())
        if not_before is not None and now < not_before:
            now = to_utc(not_before)
        if self._last_time is not None and now <= self._last_time:
            now = self._last_time + timedelta(microseconds=1)
        self._last_time = now
        return now

    def _record(self, oid: str, order: _Order, source: OrderSource) -> OrderRecord:
        return OrderRecord(
            broker_order_id=oid,
            order_ref=order.order_ref,
            perm_id=order.perm_id,
            account_id=order.account_id,
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            raw_order_type=order.raw_order_type,
            price=order.price,
            time_in_force=order.time_in_force,
            total_quantity=order.quantity,
            filled_quantity=order.filled,
            remaining_quantity=order.remaining,
            state=order.state,
            source=source,
        )

    def _emit_status(
        self, oid: str, order: _Order, *, reason: str | None = None, error_code: str | None = None
    ) -> None:
        self._send(
            order.client_id,
            OrderStatusUpdate(
                broker_order_id=oid,
                order_ref=order.order_ref,
                perm_id=order.perm_id,
                state=order.state,
                filled_quantity=order.filled,
                remaining_quantity=order.remaining,
                reason=reason,
                error_code=error_code,
                at=self._now(),
            ),
        )


class FakeBroker(BrokerAdapter):
    """FakeBrokerServer 上的一个 clientId 连接。

    传入 Account 时自建一个 server（settled_cash、clock、first_order_id 用于它）；
    传入 server 时这些参数不得给出（避免被静默忽略）。
    """

    def __init__(
        self,
        account: Account | FakeBrokerServer,
        *,
        client_id: int = 1,
        settled_cash: Decimal | None = None,
        clock: Callable[[], datetime] | None = None,
        first_order_id: int | None = None,
    ) -> None:
        if isinstance(account, FakeBrokerServer):
            if settled_cash is not None or clock is not None or first_order_id is not None:
                raise ValueError("连接已有 server 时不能再给初始资金、时钟或起始订单号")
            self.server = account
        else:
            self.server = FakeBrokerServer(
                account,
                settled_cash=Decimal(100_000) if settled_cash is None else settled_cash,
                clock=clock or (lambda: datetime.now(UTC)),
                first_order_id=1 if first_order_id is None else first_order_id,
            )
        self.client_id = client_id
        self._connected = False
        self._queue: asyncio.Queue[BrokerEvent] = asyncio.Queue()

    # ---------- 连接 ----------

    async def connect(self) -> None:
        self.server._attach(self)
        self._connected = True
        self._queue.put_nowait(
            ConnectionEvent(state=ConnectionState.CONNECTED, at=self.server._now())
        )

    async def disconnect(self) -> None:
        if not self._connected:
            return  # 已断开：不重复发事件
        self.server._detach(self)
        self._lost(ConnectionState.DISCONNECTED, self.server._now())

    @property
    def connected(self) -> bool:
        return self._connected

    def _lost(self, state: ConnectionState, at: datetime) -> None:
        self._connected = False
        self._queue.put_nowait(ConnectionEvent(state=state, at=at))

    def _deliver(self, event: BrokerEvent) -> None:
        if self._connected:
            self._queue.put_nowait(event)

    def _require_connected(self) -> FakeBrokerServer:
        if not self._connected:
            raise BrokerUnavailableError("FakeBroker 未连接")
        return self.server

    # ---------- 快照 ----------

    async def account(self) -> Account:
        return self._require_connected().account

    async def positions(self) -> list[Position]:
        return self._require_connected()._positions()

    async def cash(self) -> CashBalance:
        return self._require_connected()._cash()

    async def open_orders(self) -> list[OrderRecord]:
        return self._require_connected()._open_orders()

    async def completed_orders(self) -> list[OrderRecord]:
        return self._require_connected()._completed_orders()

    async def executions(
        self, since: datetime, until: datetime | None = None, order_ref: str | None = None
    ) -> list[BrokerExecution]:
        return self._require_connected().executions(since, until, order_ref)

    # ---------- 订单 ----------

    async def next_order_id(self) -> str:
        return self._require_connected()._allocate_id()

    async def place_order(self, ticket: OrderTicket) -> None:
        self._require_connected()._place(self.client_id, ticket)

    async def modify_order(self, amendment: OrderAmendment) -> None:
        self._require_connected()._modify(self.client_id, amendment)

    async def cancel_order(
        self, broker_order_id: str, *, allow_protective_stop: bool = False
    ) -> None:
        self._require_connected()._cancel(
            self.client_id, broker_order_id, allow_protective_stop=allow_protective_stop
        )

    async def lookup_order(self, order_ref: str) -> OrderLookup:
        return self._require_connected()._lookup(order_ref)

    # ---------- 事件 ----------

    async def events(self) -> AsyncIterator[BrokerEvent]:
        while True:
            yield await self._queue.get()

    # ---------- 测试驱动（转交 server，保持 B-01 契约驱动器的用法） ----------

    def fill(self, broker_order_id: str, quantity: Decimal, price: Decimal) -> BrokerExecution:
        return self.server.fill(broker_order_id, quantity, price)

    def inject_foreign_order(
        self,
        symbol: str,
        side: Side,
        quantity: Decimal,
        raw_order_type: str = "TRAIL",
        price: Decimal | None = None,
    ) -> str:
        return self.server.inject_foreign_order(symbol, side, quantity, raw_order_type, price)

    def inject_foreign_execution(
        self, symbol: str, side: Side, quantity: Decimal, price: Decimal
    ) -> BrokerExecution:
        return self.server.inject_foreign_execution(symbol, side, quantity, price)
