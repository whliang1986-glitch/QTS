"""IBKR 只读适配器（B-03b）。

提供账户、现金、持仓、挂单、已完成订单、成交、按 orderRef 查单，以及回报事件流。

- 下单、改单、撤单、分配订单号在 B-05 之前一律拒绝（BrokerUnavailableError）。
  注意：ib_async 的 readonly 只影响它启动时同步哪些数据，并**不能**在 TWS 侧禁止下单；
  真正的只读是 TWS 的“Read-Only API”设置（F-05 清单要求勾选，联调时实测下单被 TWS 拒绝）。
  这里防止下单的，是适配器本身不提供下单路径。
- 连接安全规则沿用 B-03a 的 ConnectionPolicy 与 verify_account：先核验账户再提供任何数据，
  核验失败立即断开、记为停机（之后的 connect 一律拒绝，等人工处理），
  并上抛（装配层进入 SAFE HALT）。
- 账户：登记的账户之外出现任何数据（持仓、订单、成交、事件），一律按状态不可信处理，不静默丢弃。
- 挂单用 reqAllOpenOrders：要看到账户内全部未结束订单，含 TWS 手工单与别的 clientId 的单。
- 回报事件只在账户核验之后订阅、断开时退订：重连握手阶段推来的事件不会混进来。
  转换失败、别的账户的事件、事件队列溢出，都上报为不可信（MAPPING），并让适配器回到未核验状态，
  调用方必须重新连接、核验、对账。
- **未在模拟盘实测**：B-03b 联调时逐项核对（docs/tasks/B-03b.md）；
  在那之前标为 PARTIALLY VERIFIED，不得拿这里的单元测试冒充联调结果。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

from server.broker.base import (
    AccountMismatchError,
    BrokerAdapter,
    BrokerError,
    BrokerEvent,
    BrokerExecution,
    BrokerUnavailableError,
    CommissionReport,
    ConnectionEvent,
    ConnectionState,
    ErrorCategory,
    ExecutionReport,
    OrderAmendment,
    OrderLookup,
    OrderRecord,
    OrderSource,
    OrderStatusUpdate,
    OrderTicket,
)
from server.broker.ibkr.client import IbAsyncClient
from server.broker.ibkr.connection import ConnectionPolicy, check_policy, verify_account
from server.broker.ibkr.convert import (
    PendingOrderError,
    cash_balance,
    decimal_of,
    execution,
    order_record,
    order_records,
    position,
    utc,
)
from server.broker.ibkr.mapping import MappingError, error_info
from server.domain.account import Account, CashBalance, Position
from server.storage.logs import log_event, mask_text

log = logging.getLogger(__name__)

READ_ONLY = "B-05 之前 IBKR 适配器只读：不下单、不改单、不撤单"
EVENT_QUEUE_SIZE = 10000
# 这些错误码的 reqId 才是订单号；其他错误的 reqId 可能是行情、账户摘要等请求的编号
_ORDER_ERROR_CODES = frozenset({103, 110, 135, 161, 201, 202, 10147, 10148})


class IbkrReadOnlyAdapter(BrokerAdapter):
    def __init__(
        self,
        policy: ConnectionPolicy,
        *,
        client: IbAsyncClient | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        check_policy(policy)
        self._policy = policy
        self._client = client or IbAsyncClient()
        self._clock = clock
        self._account: Account | None = None
        self._halted: str | None = None
        self._events: asyncio.Queue[BrokerEvent] = asyncio.Queue(EVENT_QUEUE_SIZE)
        self._subscribed = False

    # ---------- 连接 ----------

    async def connect(self) -> None:
        if self._halted is not None:
            raise BrokerUnavailableError(f"账户核验失败后已停机，需人工处理：{self._halted}")
        check_policy(self._policy)  # 纵深防御：真正连接前再查一次
        p = self._policy
        try:
            await self._client.connect(p.host, p.port, p.client_id, readonly=p.readonly)
            self._account = await verify_account(self._client, p)
        except BaseException as exc:
            self._account = None
            if isinstance(exc, AccountMismatchError):
                self._halted = type(exc).__name__
            with contextlib.suppress(Exception):
                await self._client.disconnect()
            raise
        self._subscribe()
        self._emit(ConnectionEvent(state=ConnectionState.CONNECTED, at=self._clock()))

    async def disconnect(self) -> None:
        self._account = None
        self._unsubscribe()  # 先退订：自己断开时不会再从回调里多发一条 DISCONNECTED
        await self._client.disconnect()
        self._emit(ConnectionEvent(state=ConnectionState.DISCONNECTED, at=self._clock()))

    @property
    def connected(self) -> bool:
        try:
            return self._account is not None and self._client.is_connected()
        except Exception:  # 查不到连接状态：按断开处理
            return False

    def _ib(self) -> Any:
        if not self.connected:
            raise BrokerUnavailableError("IBKR 未连接或账户未核验")
        return self._client.ib

    def _account_id(self) -> str:
        if self._account is None:
            raise BrokerUnavailableError("IBKR 账户未核验")
        return self._account.account_id

    def _only_mine(self, accounts: list[str], what: str) -> None:
        account_id = self._account_id()
        foreign = [a for a in accounts if a != account_id]
        if foreign:  # 券商只该管理登记的这一个账户：出现别的账户就是异常，不静默丢弃
            raise MappingError(f"券商返回了 {len(foreign)} 条不属于登记账户的{what}")

    # ---------- 快照 ----------

    async def account(self) -> Account:
        self._ib()
        if self._account is None:  # pragma: no cover - _ib 已检查
            raise BrokerUnavailableError("IBKR 账户未核验")
        return self._account

    async def positions(self) -> list[Position]:
        ib, now = self._ib(), self._clock()
        items = await ib.reqPositionsAsync()
        self._only_mine([str(i.account) for i in items], "持仓")
        return [position(item, now) for item in items]

    async def cash(self) -> CashBalance:
        ib, account_id = self._ib(), self._account_id()
        values = await ib.accountSummaryAsync(account_id)
        # as_of 记录的是读取时刻；摘要是否为 TWS 推送的缓存、多久刷新一次，联调时核对
        return cash_balance(values, account_id, self._clock())

    async def open_orders(self) -> list[OrderRecord]:
        trades = await self._ib().reqAllOpenOrdersAsync()
        records = order_records(trades, OrderSource.OPEN, self._policy.client_id)
        self._only_mine([r.account_id for r in records], "订单")
        return records

    async def completed_orders(self) -> list[OrderRecord]:
        trades = await self._ib().reqCompletedOrdersAsync(apiOnly=False)
        records = order_records(trades, OrderSource.COMPLETED, self._policy.client_id)
        self._only_mine([r.account_id for r in records], "订单")
        return records

    async def executions(
        self, since: datetime, until: datetime | None = None, order_ref: str | None = None
    ) -> list[BrokerExecution]:
        start = utc(since, "since")
        end = None if until is None else utc(until, "until")
        fills = await self._ib().reqExecutionsAsync()
        all_fills = [execution(f, self._policy.client_id) for f in fills]
        self._only_mine([e.account_id for e in all_fills], "成交")
        return [
            e for e in all_fills
            if e.executed_at >= start and (end is None or e.executed_at <= end)
            and (order_ref is None or e.order_ref == order_ref)
        ]  # fmt: skip

    async def lookup_order(self, order_ref: str) -> OrderLookup:
        orders = [*await self.open_orders(), *await self.completed_orders()]
        mine = tuple(o for o in orders if o.order_ref == order_ref)
        fills = await self.executions(datetime(1970, 1, 1, tzinfo=UTC), order_ref=order_ref)
        return OrderLookup(order_ref=order_ref, orders=mine, executions=tuple(fills))

    # ---------- 订单：B-05 之前一律拒绝 ----------

    async def next_order_id(self) -> str:
        raise BrokerUnavailableError(READ_ONLY)

    async def place_order(self, ticket: OrderTicket) -> None:
        raise BrokerUnavailableError(READ_ONLY)

    async def modify_order(self, amendment: OrderAmendment) -> None:
        raise BrokerUnavailableError(READ_ONLY)

    async def cancel_order(
        self, broker_order_id: str, *, allow_protective_stop: bool = False
    ) -> None:
        raise BrokerUnavailableError(READ_ONLY)

    # ---------- 事件 ----------

    async def events(self) -> AsyncIterator[BrokerEvent]:
        while True:
            yield await self._events.get()

    def _emit(self, event: BrokerEvent) -> None:
        try:
            self._events.put_nowait(event)
        except asyncio.QueueFull:
            # 消费者停了：不阻塞 ib_async 的回调；丢了事件，状态就不可信，回到未核验状态，
            # 调用方必须重新连接、核验、对账
            self._account = None
            log_event(log, logging.ERROR, "IBKR 回报事件队列已满，事件被丢弃，适配器回到未核验状态",
                      kind=type(event).__name__)  # fmt: skip

    def _subscribe(self) -> None:
        if self._subscribed:
            return
        ib = self._client.ib
        ib.orderStatusEvent += self._on_order_status
        ib.execDetailsEvent += self._on_execution
        ib.commissionReportEvent += self._on_commission
        ib.errorEvent += self._on_error
        ib.disconnectedEvent += self._on_disconnected
        self._subscribed = True

    def _unsubscribe(self) -> None:
        if not self._subscribed:
            return
        ib = self._client.ib
        # 逐个退订：一个出错不影响其他几个（字面量写法，不用反射）
        with contextlib.suppress(Exception):
            ib.orderStatusEvent -= self._on_order_status
        with contextlib.suppress(Exception):
            ib.execDetailsEvent -= self._on_execution
        with contextlib.suppress(Exception):
            ib.commissionReportEvent -= self._on_commission
        with contextlib.suppress(Exception):
            ib.errorEvent -= self._on_error
        with contextlib.suppress(Exception):
            ib.disconnectedEvent -= self._on_disconnected
        self._subscribed = False

    def _untrusted(self, what: str, exc: Exception) -> None:
        self._account = None  # 状态不可信：回到未核验，强制重新核验与对账
        self._emit(BrokerError(category=ErrorCategory.OTHER, code="MAPPING",
                               message=mask_text(f"{what}无法解析：{exc}")[:500],
                               retryable=False, at=self._clock()))  # fmt: skip

    def _check_account(self, account_id: str, what: str) -> None:
        if self._account is None:
            raise MappingError(f"账户未核验时收到{what}")
        if account_id != self._account.account_id:
            raise MappingError(f"收到不属于登记账户的{what}")

    def _on_order_status(self, trade: Any) -> None:
        try:
            record = order_record(trade, OrderSource.OPEN, self._policy.client_id)
            self._check_account(record.account_id, "订单状态")
            self._emit(OrderStatusUpdate(
                broker_order_id=record.broker_order_id, order_ref=record.order_ref,
                perm_id=record.perm_id, state=record.state,
                filled_quantity=record.filled_quantity,
                remaining_quantity=record.remaining_quantity, at=self._clock(),
            ))  # fmt: skip
        except PendingOrderError:
            return  # 状态或数量还没确认：等后续的状态回报（不产生事件）
        except Exception as exc:
            self._untrusted("订单状态", exc)

    def _on_execution(self, trade: Any, fill: Any) -> None:
        del trade
        try:
            report = execution(fill, self._policy.client_id)
            self._check_account(report.account_id, "成交")
            self._emit(ExecutionReport(execution=report))
        except Exception as exc:
            self._untrusted("成交回报", exc)

    def _on_commission(self, trade: Any, fill: Any, report: Any) -> None:
        del trade, fill
        try:
            if self._account is None:
                raise MappingError("账户未核验时收到佣金回报")
            amount = decimal_of(report.commission, "佣金")
            if amount is None:  # pragma: no cover - decimal_of 不允许空时不返回 None
                raise MappingError("佣金缺失")
            currency = str(getattr(report, "currency", "") or "").upper()  # 为空也拒绝
            if currency != "USD":
                raise MappingError(f"佣金币种不是 USD（{currency}）")
            self._emit(CommissionReport(execution_id=str(report.execId), commission=amount,
                                        at=self._clock()))  # fmt: skip
        except Exception as exc:
            self._untrusted("佣金回报", exc)

    def _on_error(self, req_id: int, code: int, message: str, contract: Any = None) -> None:
        del contract
        try:
            info = error_info(int(code))
            if info is None:
                return
            order_id = str(req_id) if int(code) in _ORDER_ERROR_CODES and req_id > 0 else None
            self._emit(BrokerError(category=info.category, code=str(code),
                                   message=mask_text(str(message))[:500],
                                   retryable=info.retryable, broker_order_id=order_id,
                                   at=self._clock()))  # fmt: skip
        except Exception as exc:
            self._untrusted("错误回报", exc)

    def _on_disconnected(self) -> None:
        try:
            self._account = None
            self._emit(ConnectionEvent(state=ConnectionState.DISCONNECTED, at=self._clock()))
        except Exception as exc:  # pragma: no cover - 发事件本身不应出错
            log_event(log, logging.ERROR, "IBKR 断线回调出错", error=type(exc).__name__)


__all__ = ("READ_ONLY", "IbkrReadOnlyAdapter")
