"""券商统一接口（B-01）：所有券商调用都隔离在 BrokerAdapter 后面。

- 下单只接受 OMS 签发的 OrderTicket，改单只接受 OrderAmendment（arch_scan 规则 order-ticket：
  只有 oms 能构造；broker 只能做类型标注与处理）。票据内嵌风控决定，数量以风控批准的为准。
- 改单只能改价格与数量；类别、方向、标的、类型、有效期、账户在类型层面不可表达。
- 撤单只能逐单进行，接口里没有全局撤单（全局撤单会撤掉保护性止损，V4.1 7.6）；
  撤保护性止损必须显式 allow_protective_stop=True（arch_scan 限制在 oms 的二次确认路径）。
- 回报统一为事件流：订单状态、成交、佣金、错误、连接状态；
  券商专有的状态与错误码由各适配器映射到这里。
- 适配器不得在内部重试下单；超时由 OMS 计时并进入 UNKNOWN（V4.1 6.4）。
- 契约测试见 tests/contract/broker_contract.py：任何适配器都必须通过同一套契约。
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import model_validator

from server.domain.account import Account, CashBalance, Position
from server.domain.enums import OrderType, RiskVerdict, Side
from server.domain.errors import InstrumentSpecMissingError
from server.domain.instrument import Instrument
from server.domain.orders import OrderIntent, RiskDecision
from server.domain.values import (
    Identifier,
    Money,
    PositiveQuantity,
    Price,
    Quantity,
    ShareQuantity,
    UtcDatetime,
    ValueModel,
)

# ---------- 错误 ----------


class BrokerUnavailableError(Exception):
    """未连接、券商重启窗口或连接中断：不得静默成功。"""


class DuplicateOrderError(Exception):
    """同一券商订单号重复提交：券商侧不得出现第二张订单。"""


class UnknownOrderError(Exception):
    """券商侧没有这张订单，或订单已结束。"""


class AccountMismatchError(Exception):
    """票据的账户与适配器连接的账户不一致。"""


class ProtectiveStopCancelError(Exception):
    """未显式确认就试图撤销保护性止损单。"""


class AmendmentRejectedError(Exception):
    """改单不合法（撤单进行中、已结束、数量不大于已成交量等）。"""


# ---------- OMS 签发的票据 ----------


def _check_decision(
    decision: RiskDecision, intent_id: str, quantity: Decimal, ceiling: Decimal
) -> None:
    if decision.intent_id != intent_id:
        raise ValueError("风控决定与意图不匹配")
    if decision.verdict is RiskVerdict.ALLOW:
        if quantity != ceiling:
            raise ValueError("ALLOW 时下单数量必须等于意图数量")
    elif decision.verdict is RiskVerdict.REDUCE:
        approved = decision.approved_quantity
        if approved is None or quantity != approved or approved > ceiling:
            raise ValueError("REDUCE 时下单数量必须等于风控批准数量，且不超过意图数量")
    else:
        raise ValueError(f"风控结论 {decision.verdict.value} 不允许下单")


def _check_spec(instrument: Instrument) -> None:
    try:
        instrument.require_tradable()
    except InstrumentSpecMissingError as exc:  # 统一成校验错误，规格缺失即拒绝构造
        raise ValueError(str(exc)) from exc
    if instrument.multiplier != 1:
        raise ValueError("首期只交易乘数为 1 的现货标的")


class OrderTicket(ValueModel):
    """OMS 签发的订单票据：内嵌风控决定，券商订单号已在提交前持久化（V4.1 6.5）。"""

    intent: OrderIntent
    decision: RiskDecision
    quantity: ShareQuantity  # 实际下单数量：ALLOW 等于意图数量，REDUCE 等于批准数量
    broker_order_id: Identifier
    instrument: Instrument

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _check_spec(self.instrument)
        if self.instrument.symbol != self.intent.symbol:
            raise ValueError("票据标的与意图不一致")
        _check_decision(self.decision, self.intent.intent_id, self.quantity, self.intent.quantity)
        if not self.instrument.is_on_tick(self.price):
            raise ValueError("价格不在最小跳动上")
        return self

    @property
    def price(self) -> Decimal:
        price = (
            self.intent.limit_price
            if self.intent.order_type is OrderType.LIMIT
            else self.intent.stop_price
        )
        if price is None:  # pragma: no cover - OrderIntent 已保证
            raise ValueError("缺少价格")
        return price

    @property
    def order_ref(self) -> str:
        """写入券商 orderRef 的值：intent_id，用于 UNKNOWN 时按 orderRef 查单。"""
        return self.intent.intent_id


class OrderAmendment(ValueModel):
    """OMS 签发的改单：只能改价格与数量，并附带针对新数量的风控决定。"""

    amendment_id: Identifier  # 幂等键
    original: OrderTicket
    new_price: Price
    new_quantity: ShareQuantity
    decision: RiskDecision

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        intent = self.original.intent
        _check_decision(self.decision, intent.intent_id, self.new_quantity, intent.quantity)
        # 改单必须重新过风控：不得复用原票据的决定，新决定不得早于原决定（价格变化也要重新评估）
        previous = self.original.decision
        if self.decision.decision_id == previous.decision_id:
            raise ValueError("改单必须附带新的风控决定，不得复用原票据的决定")
        if self.decision.decided_at < previous.decided_at:
            raise ValueError("改单的风控决定不得早于原票据的决定")
        if not self.original.instrument.is_on_tick(self.new_price):
            raise ValueError("新价格不在最小跳动上")
        return self

    @property
    def broker_order_id(self) -> str:
        return self.original.broker_order_id

    @property
    def order_ref(self) -> str:
        return self.original.order_ref


# ---------- 回报 ----------


class BrokerOrderState(StrEnum):
    """券商侧订单状态（各适配器把专有状态映射到这里，OMS 再映射到 C-02 的事件）。"""

    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCEL_REJECTED = "CANCEL_REJECTED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class BrokerOrderType(StrEnum):
    """券商侧订单类型。

    外来订单（TWS 手工单、别的 clientId、券商强平等）可能是任何类型，统一归为 OTHER。
    """

    LIMIT = "LIMIT"
    STOP = "STOP"
    OTHER = "OTHER"


TERMINAL_STATES = frozenset(
    {
        BrokerOrderState.FILLED,
        BrokerOrderState.CANCELLED,
        BrokerOrderState.REJECTED,
        BrokerOrderState.EXPIRED,
    }
)


class ErrorCategory(StrEnum):
    PRICE_REJECTED = "PRICE_REJECTED"
    NO_PERMISSION = "NO_PERMISSION"
    CONTRACT_NOT_FOUND = "CONTRACT_NOT_FOUND"
    DUPLICATE_ORDER_ID = "DUPLICATE_ORDER_ID"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    OUTSIDE_TRADING_HOURS = "OUTSIDE_TRADING_HOURS"
    ORDER_PRECAUTION = "ORDER_PRECAUTION"  # TWS 订单预防设置拦截（7.6）
    RESTART_WINDOW = "RESTART_WINDOW"
    RATE_LIMITED = "RATE_LIMITED"
    CONNECTIVITY = "CONNECTIVITY"
    OTHER = "OTHER"


class ConnectionState(StrEnum):
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    RESTART_WINDOW = "RESTART_WINDOW"  # 券商每日重启窗口：窗口内禁止新增订单


class OrderStatusUpdate(ValueModel):
    broker_order_id: Identifier
    order_ref: Identifier | None  # 外来订单没有 orderRef
    perm_id: Identifier | None = None
    state: BrokerOrderState
    filled_quantity: Quantity
    remaining_quantity: Quantity
    reason: str | None = None  # 撤单被拒、拒单等的原因
    error_code: str | None = None
    at: UtcDatetime


class BrokerExecution(ValueModel):
    """券商侧成交事实。外来成交可能没有 orderRef 或订单号，数量可能是小数（零股、公司行动）。

    OMS 按 orderRef / 订单号匹配到意图后再转换成 domain 的 Execution；
    匹配不上的入账为未匹配成交并生成差异事件（C-02 移交），不得丢弃。
    佣金可能后到：commission 为空时以 CommissionReport 为准。
    """

    execution_id: Identifier
    order_ref: Identifier | None
    broker_order_id: Identifier | None
    perm_id: Identifier | None = None
    account_id: Identifier
    symbol: Identifier
    side: Side
    quantity: PositiveQuantity
    price: Price
    commission: Money | None = None
    executed_at: UtcDatetime


class ExecutionReport(ValueModel):
    execution: BrokerExecution


class CommissionReport(ValueModel):
    execution_id: Identifier
    commission: Money
    at: UtcDatetime


class BrokerError(ValueModel):
    category: ErrorCategory
    code: str
    message: str
    # 只表示同一个**查询类**请求可以重试；从不授权重发订单——下单失败一律先查单（V4.1 6.4）
    retryable: bool
    broker_order_id: Identifier | None = None
    at: UtcDatetime


class ConnectionEvent(ValueModel):
    state: ConnectionState
    at: UtcDatetime


BrokerEvent = OrderStatusUpdate | ExecutionReport | CommissionReport | BrokerError | ConnectionEvent


class OrderSource(StrEnum):
    OPEN = "OPEN"
    COMPLETED = "COMPLETED"


class OrderRecord(ValueModel):
    """券商侧订单事实（未成交或已完成）。"""

    broker_order_id: Identifier
    order_ref: Identifier | None  # 外来订单没有 orderRef
    perm_id: Identifier | None = None
    account_id: Identifier
    symbol: Identifier
    side: Side
    order_type: BrokerOrderType
    raw_order_type: str  # 券商原始类型（如 LMT、STP、STP LMT、TRAIL、MKT、LOC）
    price: Price | None  # 市价等类型没有价格
    time_in_force: str  # 券商原始有效期（DAY、GTC、GTD 等）
    total_quantity: PositiveQuantity
    filled_quantity: Quantity
    remaining_quantity: Quantity
    state: BrokerOrderState
    source: OrderSource


class OrderLookup(ValueModel):
    """按 orderRef 查到的全部事实。空结果表示“暂未查到”，不等于从未下过（OMS 须多轮查证）。"""

    order_ref: Identifier
    orders: tuple[OrderRecord, ...] = ()
    executions: tuple[BrokerExecution, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.orders and not self.executions


# ---------- 适配器 ----------


class BrokerAdapter(abc.ABC):
    """券商适配器契约。实现必须通过 tests/contract/broker_contract.py 的全部用例。"""

    # ---------- 连接 ----------

    @abc.abstractmethod
    async def connect(self) -> None:
        """连接并发出 ConnectionEvent(CONNECTED)。"""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """断开并发出 ConnectionEvent(DISCONNECTED)。"""

    @property
    @abc.abstractmethod
    def connected(self) -> bool: ...

    # ---------- 快照 ----------

    @abc.abstractmethod
    async def account(self) -> Account: ...

    @abc.abstractmethod
    async def positions(self) -> list[Position]: ...

    @abc.abstractmethod
    async def cash(self) -> CashBalance: ...

    @abc.abstractmethod
    async def open_orders(self) -> list[OrderRecord]: ...

    @abc.abstractmethod
    async def completed_orders(self) -> list[OrderRecord]:
        """近期已完成的订单（IBKR 只覆盖近期，历史对账走 Flex，见 R-07）。"""

    @abc.abstractmethod
    async def executions(
        self, since: datetime, until: datetime | None = None, order_ref: str | None = None
    ) -> list[BrokerExecution]:
        """近期成交（含外来成交）；since / until 必须带时区。"""

    # ---------- 订单 ----------

    @abc.abstractmethod
    async def next_order_id(self) -> str:
        """分配唯一、单调递增的券商订单号；OMS 必须先持久化再下单，并取 max(券商, 账本 + 1)。"""

    @abc.abstractmethod
    async def place_order(self, ticket: OrderTicket) -> None:
        """提交订单；结果通过事件流报回。不得内部重试。

        同一订单号重复提交抛 DuplicateOrderError；账户不一致抛 AccountMismatchError。
        """

    @abc.abstractmethod
    async def modify_order(self, amendment: OrderAmendment) -> None:
        """改价格或数量。

        订单不存在或已结束抛 UnknownOrderError，不合法抛 AmendmentRejectedError。
        """

    @abc.abstractmethod
    async def cancel_order(
        self, broker_order_id: str, *, allow_protective_stop: bool = False
    ) -> None:
        """逐单撤单；结果通过事件流报回（先 CANCEL_PENDING）。

        订单不存在或已结束抛 UnknownOrderError；止损单未显式确认抛 ProtectiveStopCancelError。
        是否为止损单以**券商侧**订单类型为准（外来止损单没有票据）；只有 LIMIT 可以不经确认撤销，
        STOP 与认不出的类型（OTHER）一律需要确认（失败即关闭）。
        """

    @abc.abstractmethod
    async def lookup_order(self, order_ref: str) -> OrderLookup:
        """按 orderRef 查全部事实（未成交、已完成、成交），用于解决 UNKNOWN。"""

    # ---------- 事件 ----------

    @abc.abstractmethod
    def events(self) -> AsyncIterator[BrokerEvent]:
        """统一回报事件流（单消费者：同一适配器只应有一个消费者）。"""
