"""FakeBroker（B-01 版本）：确定性的内存券商，供契约测试与 OMS 开发使用。

- 下单立即回报 ACKNOWLEDGED 并分配 permId；成交由测试或撮合内核调用 `fill()` 驱动，
  成交价不得劣于限价；撤单先报 CANCEL_PENDING 再报 CANCELLED。
- 撤单期间的部分成交保持 CANCEL_PENDING（与 C-02 一致）。
- 撤单保护以券商侧订单类型为准：STOP 单（含外来止损单）未显式确认一律拒绝。
- 可注入“外来”订单与成交（没有 orderRef，类型可能是 OTHER），用于验证适配器如实报出、不抛异常。
- 时钟严格递增（同一时刻的多次读取会相差 1 微秒），满足 C-02 的“下单 < 成交”时间约定。
- 事件流是单消费者；连接与断开发出 ConnectionEvent。
- 故障注入、可控的撤单确认、按行情撮合、现金结算在 B-02 扩展。
- 只记录券商事实，不做风控判断（风控在 server/risk）。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from server.broker.base import (
    TERMINAL_STATES,
    AccountMismatchError,
    AmendmentRejectedError,
    BrokerAdapter,
    BrokerEvent,
    BrokerExecution,
    BrokerOrderState,
    BrokerOrderType,
    BrokerUnavailableError,
    ConnectionEvent,
    ConnectionState,
    DuplicateOrderError,
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
from server.domain.account import Account, CashBalance, Position
from server.domain.enums import OrderType, Side
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


@dataclass
class _Order:
    order_ref: str | None
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
    filled: Decimal = Decimal(0)

    @property
    def remaining(self) -> Decimal:
        return self.quantity - self.filled if self.state in _ACTIVE else Decimal(0)


class FakeBroker(BrokerAdapter):
    def __init__(
        self,
        account: Account,
        *,
        settled_cash: Decimal = Decimal(100_000),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        first_order_id: int = 1,
    ) -> None:
        self._account = account
        self._settled_cash = settled_cash
        self._clock = clock
        self._last_time: datetime | None = None
        self._connected = False
        self._next_id = first_order_id
        self._orders: dict[str, _Order] = {}
        self._executions: list[BrokerExecution] = []
        self._queue: asyncio.Queue[BrokerEvent] = asyncio.Queue()
        self._run = uuid.uuid4().hex[:8]  # 运行级前缀：崩溃重启后的新实例不会撞号
        self._seq = 0

    # ---------- 连接 ----------

    async def connect(self) -> None:
        self._connected = True
        self._queue.put_nowait(ConnectionEvent(state=ConnectionState.CONNECTED, at=self._now()))

    async def disconnect(self) -> None:
        self._connected = False
        self._queue.put_nowait(ConnectionEvent(state=ConnectionState.DISCONNECTED, at=self._now()))

    @property
    def connected(self) -> bool:
        return self._connected

    def _require_connected(self) -> None:
        if not self._connected:
            raise BrokerUnavailableError("FakeBroker 未连接")

    # ---------- 快照 ----------

    async def account(self) -> Account:
        self._require_connected()
        return self._account

    async def positions(self) -> list[Position]:
        self._require_connected()
        totals: dict[str, Decimal] = {}
        for ex in self._executions:
            sign = 1 if ex.side is Side.BUY else -1
            totals[ex.symbol] = totals.get(ex.symbol, Decimal(0)) + sign * ex.quantity
        now = self._now()
        return [
            Position(
                account_id=self._account.account_id,
                symbol=s,
                quantity=q,
                average_cost=None,
                as_of=now,
            )
            for s, q in sorted(totals.items())
            if q != 0
        ]

    async def cash(self) -> CashBalance:
        self._require_connected()
        return CashBalance(
            account_id=self._account.account_id,
            currency="USD",
            settled=self._settled_cash,
            unsettled=Decimal(0),
            as_of=self._now(),
        )

    async def open_orders(self) -> list[OrderRecord]:
        self._require_connected()
        return [
            self._record(oid, o, OrderSource.OPEN)
            for oid, o in self._orders.items()
            if o.state in _ACTIVE
        ]

    async def completed_orders(self) -> list[OrderRecord]:
        self._require_connected()
        return [
            self._record(oid, o, OrderSource.COMPLETED)
            for oid, o in self._orders.items()
            if o.state in TERMINAL_STATES
        ]

    async def executions(
        self, since: datetime, until: datetime | None = None, order_ref: str | None = None
    ) -> list[BrokerExecution]:
        self._require_connected()
        start = to_utc(since)
        end = to_utc(until) if until is not None else None
        return [
            e
            for e in self._executions
            if e.executed_at >= start
            and (end is None or e.executed_at < end)
            and (order_ref is None or e.order_ref == order_ref)
        ]

    # ---------- 订单 ----------

    async def next_order_id(self) -> str:
        self._require_connected()
        oid = str(self._next_id)
        self._next_id += 1
        return oid

    async def place_order(self, ticket: OrderTicket) -> None:
        self._require_connected()
        intent = ticket.intent
        if intent.account_id != self._account.account_id:
            raise AccountMismatchError("票据账户与连接的账户不一致")
        oid = ticket.broker_order_id
        if oid in self._orders:
            raise DuplicateOrderError(f"订单号 {oid} 已提交过")
        order_type = (
            BrokerOrderType.LIMIT if intent.order_type is OrderType.LIMIT else BrokerOrderType.STOP
        )
        order = _Order(
            order_ref=ticket.order_ref,
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
        )
        self._orders[oid] = order
        self._emit_status(oid, order)

    async def modify_order(self, amendment: OrderAmendment) -> None:
        self._require_connected()
        oid = amendment.broker_order_id
        order = self._orders.get(oid)
        if order is None or order.state not in _ACTIVE:
            raise UnknownOrderError(f"订单号 {oid} 不存在或已结束")
        if order.order_ref != amendment.order_ref:
            raise UnknownOrderError("改单必须针对同一意图")
        # 先校验、再修改：不合法时订单保持原样
        if order.state is BrokerOrderState.CANCEL_PENDING:
            raise AmendmentRejectedError("撤单进行中，不能改单")
        if amendment.new_quantity <= order.filled:
            raise AmendmentRejectedError("新数量必须大于已成交数量")
        order.quantity = amendment.new_quantity
        order.price = amendment.new_price
        self._emit_status(oid, order)

    async def cancel_order(
        self, broker_order_id: str, *, allow_protective_stop: bool = False
    ) -> None:
        self._require_connected()
        order = self._orders.get(broker_order_id)
        if order is None or order.state not in _ACTIVE:
            raise UnknownOrderError(f"订单号 {broker_order_id} 不存在或已结束")
        # 以券商侧类型为准：外来止损单没有票据，同样受保护；
        # 类型认不出（OTHER）也按止损对待（失败即关闭）
        if order.order_type is not BrokerOrderType.LIMIT and not allow_protective_stop:
            raise ProtectiveStopCancelError("撤销保护性止损需要显式确认")
        order.state = BrokerOrderState.CANCEL_PENDING
        self._emit_status(broker_order_id, order)
        order.state = BrokerOrderState.CANCELLED
        self._emit_status(broker_order_id, order)

    async def lookup_order(self, order_ref: str) -> OrderLookup:
        self._require_connected()
        records = tuple(
            self._record(oid, o, OrderSource.OPEN if o.state in _ACTIVE else OrderSource.COMPLETED)
            for oid, o in self._orders.items()
            if o.order_ref == order_ref
        )
        execs = tuple(e for e in self._executions if e.order_ref == order_ref)
        return OrderLookup(order_ref=order_ref, orders=records, executions=execs)

    # ---------- 事件 ----------

    async def events(self) -> AsyncIterator[BrokerEvent]:
        while True:
            yield await self._queue.get()

    # ---------- 测试与撮合内核使用的控制 ----------

    def fill(self, broker_order_id: str, quantity: Decimal, price: Decimal) -> BrokerExecution:
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
        execution = self._book(order, broker_order_id, quantity, price)
        if order.filled == order.quantity:
            order.state = BrokerOrderState.FILLED
        elif order.state is not BrokerOrderState.CANCEL_PENDING:
            order.state = BrokerOrderState.PARTIALLY_FILLED  # 撤单期间的部分成交保持 CANCEL_PENDING
        self._emit_status(broker_order_id, order)
        return execution

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
        oid = f"foreign-{self._next_id}"
        self._next_id += 1
        kind = _FOREIGN_TYPES.get(raw_order_type, BrokerOrderType.OTHER)
        order = _Order(
            order_ref=None,
            account_id=self._account.account_id,
            symbol=symbol,
            side=side,
            order_type=kind,
            raw_order_type=raw_order_type,
            time_in_force="GTC",
            perm_id=self._new_perm_id(),
            state=BrokerOrderState.ACKNOWLEDGED,
            quantity=quantity,
            price=price,
        )
        self._orders[oid] = order
        self._emit_status(oid, order)
        return oid

    def inject_foreign_execution(
        self, symbol: str, side: Side, quantity: Decimal, price: Decimal
    ) -> BrokerExecution:
        """模拟一笔无法关联到本系统订单的成交（如券商强平、公司行动），数量可以是小数。"""
        self._seq += 1
        execution = BrokerExecution(
            execution_id=f"{self._run}-exec-{self._seq}",
            order_ref=None,
            broker_order_id=None,
            perm_id=None,
            account_id=self._account.account_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            commission=None,
            executed_at=self._now(),
        )
        self._executions.append(execution)
        self._queue.put_nowait(ExecutionReport(execution=execution))
        return execution

    # ---------- 内部 ----------

    def _new_perm_id(self) -> str:
        self._seq += 1
        return f"{self._run}-perm-{self._seq}"

    def _book(self, order: _Order, oid: str, quantity: Decimal, price: Decimal) -> BrokerExecution:
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
            commission=Decimal("0.00"),
            executed_at=self._now(),
        )
        order.filled += quantity
        self._executions.append(execution)
        self._queue.put_nowait(ExecutionReport(execution=execution))
        return execution

    def _now(self) -> datetime:
        now = to_utc(self._clock())
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

    def _emit_status(self, oid: str, order: _Order) -> None:
        self._queue.put_nowait(
            OrderStatusUpdate(
                broker_order_id=oid,
                order_ref=order.order_ref,
                perm_id=order.perm_id,
                state=order.state,
                filled_quantity=order.filled,
                remaining_quantity=order.remaining,
                at=self._now(),
            )
        )
