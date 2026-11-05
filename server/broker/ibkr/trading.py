"""IBKR 模拟盘下单适配器（B-05，不连 TWS 的部分）。

在只读适配器（B-03b）之上加：分配订单号、下单、改单、逐单撤单。**只用于模拟盘**：
- 连接规则沿用 B-03a：PAPER 模式、本机、模拟盘端口 7497、DU 开头的账户；实盘端口任何时候都拒绝；
- 要同时满足四道开关才可能真的下单：
  - 连接策略 `readonly=False`；
  - 用下单适配器（只读适配器拒绝非只读策略，也没有下单路径）；
  - 客户端显式 `allow_orders=True`（构造后不能再改）；
  - TWS 侧的 Read-Only API 由 Dylan 本人取消勾选（F-05 清单）；
- 下单只接受 OMS 签发的票据（OrderTicket / OrderAmendment，内嵌风控决定），这里不做风控判断；
- 只有限价单与保护性止损单（止损单，R-04 专项豁免），没有市价单；GTC 只给保护性止损；
- 不在内部重试：同一订单号只发一次，发送失败也不重发，由 OMS 计时进入 UNKNOWN 后查单；
- 撤单只能逐单进行，没有全局撤单。止损单与认不出的类型（OTHER）未经显式确认一律拒绝撤销；
  是不是止损单，以券商侧的订单为准；
- 别的 clientId 或 TWS 手工下的单，本连接不能改、不能撤：一律拒绝，不假装成功。
  guardd 跨 clientId 撤单的可行性，留到联调时验证（B-02 待实测清单第 1 条）。

**未在模拟盘实测**：订单字段、GTD 时间格式、改单与撤单的回报顺序，都要在联调时核对
（docs/tasks/B-05.md）。在那之前标为 PARTIALLY VERIFIED，不得拿单元测试冒充联调结果。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import ib_async

from server.broker.base import (
    AccountMismatchError,
    AmendmentRejectedError,
    BrokerOrderType,
    BrokerUnavailableError,
    DuplicateOrderError,
    OrderAmendment,
    OrderTicket,
    ProtectiveStopCancelError,
    UnknownOrderError,
)
from server.broker.ibkr.adapter import IbkrReadOnlyAdapter
from server.broker.ibkr.client import IbAsyncClient
from server.broker.ibkr.connection import ConnectionPolicy
from server.broker.ibkr.convert import _order_kind, decimal_of
from server.broker.ibkr.mapping import MappingError
from server.domain.enums import OrderType, Side, TimeInForce

# 已结束的 IBKR 状态：不能再改、再撤（Inactive 含义不确定，也不改，由调用方先查单）
_DONE = frozenset({"Filled", "Cancelled", "ApiCancelled", "Inactive"})
_CANCELLING = frozenset({"PendingCancel"})
_ACTIONS = {Side.BUY: "BUY", Side.SELL: "SELL"}
_TIF = {TimeInForce.DAY: "DAY", TimeInForce.GTD: "GTD", TimeInForce.GTC: "GTC"}


def _to_float(value: Decimal, what: str) -> float:
    """Decimal → ib_async 需要的 float，并核对转换没有改变数值（变了就拒绝，不悄悄取整）。"""
    result = float(value)
    if decimal_of(result, what) != value:
        raise MappingError(f"{what} {value} 无法精确表示，拒绝下单")
    return result


def _order_id(broker_order_id: str) -> int:
    if not broker_order_id.isdigit() or int(broker_order_id) <= 0:
        raise MappingError(f"订单号不是本连接分配的正整数：{broker_order_id!r}")
    return int(broker_order_id)


def good_till(expires_at: datetime) -> str:
    """GTD 的到期时间：IBKR 的 "yyyyMMdd HH:mm:ss 时区" 写法，统一用 UTC（未实测）。"""
    return expires_at.astimezone(UTC).strftime("%Y%m%d %H:%M:%S UTC")


def ib_order(ticket: OrderTicket, account_id: str) -> ib_async.Order:
    """OrderTicket → ib_async.Order。数量用风控批准的 ticket.quantity，不用意图数量。"""
    intent = ticket.intent
    if intent.account_id != account_id:
        raise AccountMismatchError("票据的账户与连接的账户不一致")
    # 止损单与保护性止损一一对应（OrderIntent 保证）；broker 包不引用止损类别名（arch_scan）
    if intent.time_in_force is TimeInForce.GTC and intent.order_type is not OrderType.STOP:
        raise MappingError("GTC 只允许保护性止损使用")
    quantity = ticket.quantity
    if quantity != quantity.to_integral_value() or quantity <= 0:
        raise MappingError(f"下单数量必须是正整数股：{quantity}")
    order = ib_async.Order(
        orderId=_order_id(ticket.broker_order_id),
        action=_ACTIONS[intent.side],
        totalQuantity=_to_float(quantity, "数量"),
        tif=_TIF[intent.time_in_force],
        account=account_id,
        orderRef=ticket.order_ref,
        transmit=True,
        outsideRth=False,
    )
    if intent.order_type is OrderType.LIMIT:
        order.orderType, order.lmtPrice = "LMT", _to_float(ticket.price, "限价")
    else:  # 只剩止损单：OrderIntent 已保证只用于保护性止损
        order.orderType, order.auxPrice = "STP", _to_float(ticket.price, "止损价")
    if intent.time_in_force is TimeInForce.GTD:
        if intent.expires_at is None:
            raise MappingError("GTD 订单缺少到期时间")
        order.goodTillDate = good_till(intent.expires_at)
    return order


class IbkrPaperAdapter(IbkrReadOnlyAdapter):
    """模拟盘下单适配器。与只读适配器共用快照与回报事件。"""

    _READONLY = False

    def __init__(
        self,
        policy: ConnectionPolicy,
        *,
        client: IbAsyncClient,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if client.allow_orders is not True:
            raise ValueError("下单适配器要求客户端显式开启 allow_orders")
        super().__init__(policy, client=client, clock=clock)
        self._contracts: dict[str, Any] = {}
        self._sent: set[str] = set()  # 本会话发出过的订单号：不论成败都不再发第二次

    # ---------- 订单 ----------

    async def next_order_id(self) -> str:
        return str(self._ib().client.getReqId())

    async def place_order(self, ticket: OrderTicket) -> None:
        ib = self._ib()
        order = ib_order(ticket, self._account_id())
        broker_order_id = ticket.broker_order_id
        if broker_order_id in self._sent or self._find_own(ib, order.orderId) is not None:
            raise DuplicateOrderError(f"订单号 {broker_order_id} 已经提交过")
        # 检查通过后、任何 await 之前立刻占号：并发的同号提交只能有一个走下去。
        # 之后无论确认合约失败还是发送失败，这个号都不再发（OMS 进入 UNKNOWN 后先查单）
        self._sent.add(broker_order_id)
        contract = await self._contract(ib, ticket.intent.symbol)
        self._ib()  # 确认合约期间可能断线
        try:
            ib.placeOrder(contract, order)
        except Exception as exc:
            reason = type(exc).__name__
            raise BrokerUnavailableError(f"下单结果未知（{reason}），先查单再决定") from exc

    async def modify_order(self, amendment: OrderAmendment) -> None:
        ib = self._ib()
        original = amendment.original
        if original.intent.account_id != self._account_id():
            raise AccountMismatchError("改单的账户与连接的账户不一致")
        trade = self._own_trade(ib, amendment.broker_order_id)
        if str(trade.order.orderRef or "") != amendment.order_ref:
            # 同号不同意图（重连后订单号回退、账本从备份恢复等）：不改别人的单
            raise UnknownOrderError(f"订单 {amendment.broker_order_id} 的 orderRef 与意图不一致")
        expected = (
            BrokerOrderType.LIMIT
            if original.intent.order_type is OrderType.LIMIT
            else BrokerOrderType.STOP
        )
        if _order_kind(trade.order) is not expected:  # 含 OTHER（带条件、可调整止损等）
            raise AmendmentRejectedError("券商侧的订单类型与意图不一致，不改单")
        status = str(trade.orderStatus.status)
        if status in _CANCELLING:
            raise AmendmentRejectedError("订单正在撤销，不能改单")
        filled = decimal_of(trade.orderStatus.filled, "已成交量") or Decimal(0)
        if amendment.new_quantity <= filled:
            raise AmendmentRejectedError(f"新数量 {amendment.new_quantity} 不大于已成交量 {filled}")
        changes: dict[str, Any] = {"totalQuantity": _to_float(amendment.new_quantity, "数量")}
        price = _to_float(amendment.new_price, "价格")
        if expected is BrokerOrderType.LIMIT:
            changes["lmtPrice"] = price
        else:
            changes["auxPrice"] = price
        # 复制一份再改：不改动 ib_async 持有的原订单对象（改单被拒时它仍反映券商侧事实）
        try:
            ib.placeOrder(trade.contract, dataclasses.replace(trade.order, **changes))
        except Exception as exc:
            reason = type(exc).__name__
            raise BrokerUnavailableError(f"改单结果未知（{reason}），先查单再决定") from exc

    async def cancel_order(
        self, broker_order_id: str, *, allow_protective_stop: bool = False
    ) -> None:
        ib = self._ib()
        if not broker_order_id.isdigit():
            # perm-<permId>：别的 clientId 或 TWS 手工单。本连接不能撤（clientId 0 才能接管），
            # 不假装成功、也不当作“没有这张单”（联调验证 guardd 的可行方案）
            raise BrokerUnavailableError(f"订单 {broker_order_id} 不是本连接下的单，不能从这里撤")
        trade = self._own_trade(ib, broker_order_id)
        if not str(trade.order.orderRef or ""):
            # 本系统下的单一定带 orderRef（意图 ID）：没有就不是我们的单，不撤
            raise UnknownOrderError(f"订单 {broker_order_id} 没有 orderRef，不是本系统下的单")
        needs_confirm = _order_kind(trade.order) is not BrokerOrderType.LIMIT
        if needs_confirm and allow_protective_stop is not True:  # 先查确认，再看是否已在撤
            raise ProtectiveStopCancelError(f"订单 {broker_order_id} 是止损或认不出的类型，要确认")
        if str(trade.orderStatus.status) in _CANCELLING:
            return  # 已经在撤：不重复发送（结果照常经事件流报回）
        ib.cancelOrder(trade.order)  # 结果经 orderStatusEvent 报回（先 CANCEL_PENDING）

    # ---------- 内部 ----------

    def _own_trade(self, ib: Any, broker_order_id: str) -> Any:
        """本连接下、还没结束的订单；没有或已结束都抛 UnknownOrderError。"""
        trade = self._find_own(ib, _order_id(broker_order_id))
        if trade is None:
            raise UnknownOrderError(f"本连接下没有订单 {broker_order_id}")
        status = str(trade.orderStatus.status)
        if status in _DONE:
            raise UnknownOrderError(f"订单 {broker_order_id} 已结束（{status}）")
        return trade

    def _find_own(self, ib: Any, order_id: int) -> Any | None:
        """本连接（同一 clientId）下这个订单号的 Trade；别的 clientId 的同号订单不算。"""
        for trade in ib.trades():
            order = trade.order
            if order.orderId == order_id and order.clientId == self._policy.client_id:
                return trade
        return None

    async def _contract(self, ib: Any, symbol: str) -> Any:
        cached = self._contracts.get(symbol)
        if cached is not None:
            return cached
        qualified = await ib.qualifyContractsAsync(ib_async.Stock(symbol, "SMART", "USD"))
        found: Any = qualified[0] if qualified else None
        if found is None or not getattr(found, "conId", 0):
            raise MappingError(f"{symbol} 的合约没有确认（IBKR 找不到或有歧义）")
        if str(getattr(found, "secType", "")).upper() != "STK":  # IBKR 的 ETF 也是 STK
            raise MappingError(f"{symbol} 不是股票或 ETF")
        if str(getattr(found, "currency", "")).upper() != "USD":
            raise MappingError(f"{symbol} 不是美元计价")
        self._contracts[symbol] = found
        return found


__all__ = ("IbkrPaperAdapter", "good_till", "ib_order")
