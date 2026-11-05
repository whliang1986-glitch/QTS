"""IBKR 模拟盘下单适配器（B-05，不连 TWS）：ib_async 的真实 Order、Trade、Event，加替身 IB。

这里的结果只说明“票据 → IBKR 订单”的转换、开关与拒绝规则正确，**不代表模拟盘联调通过**，
也不是契约套件在 IBKR 上的运行结果（那要等 F-05 之后在模拟盘上跑）。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import ib_async
import pytest
from server.broker.base import (
    AccountMismatchError,
    AmendmentRejectedError,
    BrokerOrderState,
    BrokerUnavailableError,
    DuplicateOrderError,
    OrderStatusUpdate,
    OrderTicket,
    ProtectiveStopCancelError,
    UnknownOrderError,
)
from server.broker.ibkr.adapter import IbkrReadOnlyAdapter
from server.broker.ibkr.client import IbAsyncClient
from server.broker.ibkr.connection import LOCALHOST, ConnectionPolicy
from server.broker.ibkr.mapping import MappingError
from server.broker.ibkr.trading import IbkrPaperAdapter, _to_float, good_till, ib_order
from server.domain.enums import (
    AccountType,
    AssetClass,
    Mode,
    OrderClass,
    OrderType,
    Side,
    TimeInForce,
)
from server.domain.instrument import Instrument
from tests.contract.broker_contract import make_amendment, make_ticket

ACCOUNT = "DU1234567"
CLIENT = 7
NOW = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)


class Driver:
    """make_ticket 需要的最小驱动器（只用账户与标的）。"""

    account_id = ACCOUNT

    def instrument(self) -> Instrument:
        return Instrument(symbol="SPY", asset_class=AssetClass.ETF, exchange="ARCA",
                          currency="USD", tick_size=Decimal("0.01"),
                          trading_calendar="XNYS")  # fmt: skip

    def resting_price(self) -> Decimal:
        return Decimal("500.25")


DRIVER: Any = Driver()


def ticket(oid: str = "101", intent_id: str = "i-1", **kw: Any) -> Any:
    return make_ticket(DRIVER, oid, intent_id, **kw)


def stop_ticket(oid: str = "102", **kw: Any) -> Any:
    return make_ticket(DRIVER, oid, "i-stop", side=Side.SELL, order_type=OrderType.STOP,
                       limit_price=None, stop_price=Decimal("450.00"),
                       order_class=OrderClass.PROTECTIVE_STOP, time_in_force=TimeInForce.GTC,
                       **kw)  # fmt: skip


def policy(**kw: Any) -> ConnectionPolicy:
    base = ConnectionPolicy(mode=Mode.PAPER, host=LOCALHOST, port=7497, client_id=CLIENT,
                            readonly=False, expected_account_id=ACCOUNT,
                            expected_account_type=AccountType.CASH)  # fmt: skip
    return replace(base, **kw)


class Ids:
    def __init__(self) -> None:
        self.next = 500

    def getReqId(self) -> int:  # noqa: N802
        self.next += 1
        return self.next


class FakeIB:
    """替身：连接、核验所需的方法，加 placeOrder / cancelOrder / trades（行为仿 ib_async）。"""

    def __init__(self) -> None:
        real = ib_async.IB()
        self.orderStatusEvent = real.orderStatusEvent
        self.execDetailsEvent = real.execDetailsEvent
        self.commissionReportEvent = real.commissionReportEvent
        self.errorEvent = real.errorEvent
        self.disconnectedEvent = real.disconnectedEvent
        self.client = Ids()
        self.connected = False
        self.readonly: bool | None = None
        self.placed: list[ib_async.Order] = []
        self.cancelled: list[int] = []
        self._trades: list[ib_async.Trade] = []
        self.qualify: Any = None
        self.fail_place = False

    async def connectAsync(self, host: str, port: int, clientId: int, timeout: float,  # noqa: N802, N803
                           readonly: bool) -> None:  # fmt: skip
        self.readonly, self.connected = readonly, True

    def disconnect(self) -> None:
        self.connected = False

    def isConnected(self) -> bool:  # noqa: N802
        return self.connected

    def managedAccounts(self) -> list[str]:  # noqa: N802
        return [ACCOUNT]

    async def accountSummaryAsync(self, account: str = "") -> list[Any]:  # noqa: N802
        return [ib_async.AccountValue(ACCOUNT, "AccountType", "CASH", "", ""),
                ib_async.AccountValue(ACCOUNT, "Currency", "USD", "USD", "")]  # fmt: skip

    async def qualifyContractsAsync(self, contract: Any) -> list[Any]:  # noqa: N802
        await asyncio.sleep(0)  # 真实的确认要等 TWS 回复：让出事件循环
        if self.qualify is not None:
            return [self.qualify]
        found = ib_async.Stock(contract.symbol, "SMART", "USD")
        found.conId, found.secType = 756733, "STK"
        return [found]

    def trades(self) -> list[ib_async.Trade]:
        return list(self._trades)

    def placeOrder(self, contract: Any, order: ib_async.Order) -> ib_async.Trade:  # noqa: N802
        if self.fail_place:
            raise ConnectionError("Not connected")
        self.placed.append(order)
        for trade in self._trades:
            if trade.order.orderId == order.orderId and trade.order.clientId == CLIENT:
                return trade  # 改单：ib_async 只发请求，Trade 里的订单等 openOrder 回调更新
        order.clientId = CLIENT
        status = ib_async.OrderStatus(orderId=order.orderId, status="PendingSubmit")
        trade = ib_async.Trade(contract=contract, order=order, orderStatus=status)
        self._trades.append(trade)
        return trade

    def cancelOrder(self, order: ib_async.Order) -> None:  # noqa: N802
        self.cancelled.append(order.orderId)
        for trade in self._trades:
            if trade.order is order:
                trade.orderStatus.status = "PendingCancel"
                self.orderStatusEvent.emit(trade)

    def ack(self, order_id: int, *, filled: float = 0.0, status: str = "Submitted") -> None:
        """模拟 TWS 的 orderStatus 回调。"""
        for trade in self._trades:
            if trade.order.orderId == order_id:
                total = trade.order.totalQuantity
                trade.orderStatus.status = status
                trade.orderStatus.filled, trade.orderStatus.remaining = filled, total - filled
                self.orderStatusEvent.emit(trade)

    def foreign(self, order_id: int, client_id: int) -> None:
        order = ib_async.Order(orderId=order_id, clientId=client_id, permId=9000, action="BUY",
                               totalQuantity=5.0, orderType="LMT", lmtPrice=1.0,
                               account=ACCOUNT)  # fmt: skip
        status = ib_async.OrderStatus(orderId=order_id, status="Submitted", remaining=5.0)
        self._trades.append(ib_async.Trade(contract=ib_async.Stock("SPY", "SMART", "USD"),
                                           order=order, orderStatus=status))  # fmt: skip


async def connected(ib: FakeIB | None = None) -> tuple[IbkrPaperAdapter, FakeIB]:
    ib = ib or FakeIB()
    adapter = IbkrPaperAdapter(policy(), client=IbAsyncClient(ib, allow_orders=True),
                               clock=lambda: NOW)  # fmt: skip
    await adapter.connect()
    return adapter, ib


# ---------- 开关 ----------


def test_paper_adapter_needs_both_switches() -> None:
    with pytest.raises(ValueError, match="allow_orders"):
        IbkrPaperAdapter(policy(), client=IbAsyncClient(FakeIB()))
    with pytest.raises(ValueError, match="非只读"):
        IbkrPaperAdapter(policy(readonly=True), client=IbAsyncClient(FakeIB(), allow_orders=True))
    with pytest.raises(ValueError, match="只读适配器"):
        IbkrReadOnlyAdapter(policy(), client=IbAsyncClient(FakeIB(), allow_orders=True))


@pytest.mark.asyncio
async def test_paper_adapter_connects_non_read_only() -> None:
    adapter, ib = await connected()
    assert adapter.connected and ib.readonly is False


@pytest.mark.asyncio
async def test_orders_refused_when_not_connected() -> None:
    adapter = IbkrPaperAdapter(policy(), client=IbAsyncClient(FakeIB(), allow_orders=True))
    with pytest.raises(BrokerUnavailableError):
        await adapter.place_order(ticket())


# ---------- 票据 → IBKR 订单 ----------


def test_limit_ticket_maps_to_a_day_limit_order() -> None:
    order = ib_order(ticket(), ACCOUNT)
    assert (order.orderId, order.action, order.orderType, order.tif) == (101, "BUY", "LMT", "DAY")
    assert order.totalQuantity == 10.0 and order.lmtPrice == 500.25
    assert order.account == ACCOUNT and order.orderRef == "i-1"
    assert order.transmit is True and order.outsideRth is False


def test_protective_stop_maps_to_a_gtc_stop() -> None:
    order = ib_order(stop_ticket(), ACCOUNT)
    assert (order.action, order.orderType, order.tif, order.auxPrice) == ("SELL", "STP", "GTC",
                                                                          450.0)  # fmt: skip


def test_gtd_carries_the_expiry_in_utc() -> None:
    expiry = datetime(2026, 11, 5, 20, 30, tzinfo=UTC)
    order = ib_order(ticket(time_in_force=TimeInForce.GTD, expires_at=expiry), ACCOUNT)
    assert order.tif == "GTD" and order.goodTillDate == "20261105 20:30:00 UTC"
    assert good_till(expiry.astimezone(tz=None)) == "20261105 20:30:00 UTC"


def test_reduced_ticket_uses_the_approved_quantity() -> None:
    original = ticket(qty=10)
    amended = make_amendment(original, qty=6, price=Decimal("500.25"))
    reduced = OrderTicket(intent=original.intent, decision=amended.decision, quantity=Decimal(6),
                          broker_order_id="101", instrument=original.instrument)  # fmt: skip
    assert ib_order(reduced, ACCOUNT).totalQuantity == 6.0


def test_ticket_for_another_account_is_refused() -> None:
    with pytest.raises(AccountMismatchError):
        ib_order(ticket(), "DU7654321")


@pytest.mark.parametrize("oid", ["perm-9", "0", "abc", "-3"])
def test_order_id_must_be_a_positive_integer(oid: str) -> None:
    with pytest.raises(MappingError, match="正整数"):
        ib_order(ticket(oid=oid), ACCOUNT)


def test_inexact_decimal_is_refused_not_rounded() -> None:
    assert _to_float(Decimal("500.25"), "限价") == 500.25
    with pytest.raises(MappingError, match="精确"):
        _to_float(Decimal("0.1000000000000000000001"), "限价")


# ---------- 下单 ----------


@pytest.mark.asyncio
async def test_next_order_id_comes_from_the_connection() -> None:
    adapter, _ = await connected()
    assert await adapter.next_order_id() == "501"
    assert await adapter.next_order_id() == "502"


@pytest.mark.asyncio
async def test_place_order_sends_once_and_refuses_duplicates() -> None:
    adapter, ib = await connected()
    await adapter.place_order(ticket())
    assert [o.orderId for o in ib.placed] == [101]
    with pytest.raises(DuplicateOrderError):
        await adapter.place_order(ticket())
    assert len(ib.placed) == 1


@pytest.mark.asyncio
async def test_failed_send_is_not_retried() -> None:
    adapter, ib = await connected()
    ib.fail_place = True
    with pytest.raises(BrokerUnavailableError, match="先查单"):
        await adapter.place_order(ticket())
    ib.fail_place = False
    with pytest.raises(DuplicateOrderError):  # 同一订单号不会再发：OMS 进入 UNKNOWN 后查单
        await adapter.place_order(ticket())
    assert ib.placed == []


@pytest.mark.asyncio
async def test_status_reports_flow_through_the_read_only_event_path() -> None:
    adapter, ib = await connected()
    stream = adapter.events()
    await anext(stream)  # CONNECTED
    await adapter.place_order(ticket())
    ib.ack(101, filled=4.0)
    update = await asyncio.wait_for(anext(stream), 1)
    assert isinstance(update, OrderStatusUpdate) and update.broker_order_id == "101"
    assert update.state is BrokerOrderState.PARTIALLY_FILLED and update.order_ref == "i-1"


@pytest.mark.asyncio
async def test_unconfirmed_contract_is_refused_before_sending() -> None:
    adapter, ib = await connected()
    ib.qualify = ib_async.Option("SPY", conId=5)
    with pytest.raises(MappingError):
        await adapter.place_order(ticket())
    assert ib.placed == []


# ---------- 改单 ----------


@pytest.mark.asyncio
async def test_modify_sends_a_copy_with_new_price_and_quantity() -> None:
    adapter, ib = await connected()
    original = ticket()
    await adapter.place_order(original)
    ib.ack(101, filled=2.0)
    await adapter.modify_order(make_amendment(original, qty=8, price=Decimal("500.10")))
    sent = ib.placed[-1]
    assert (sent.orderId, sent.totalQuantity, sent.lmtPrice) == (101, 8.0, 500.10)
    assert ib.placed[0].totalQuantity == 10.0  # 原订单对象没有被改动


@pytest.mark.asyncio
async def test_modify_refuses_unknown_done_cancelling_and_overfilled() -> None:
    adapter, ib = await connected()
    original = ticket()
    with pytest.raises(UnknownOrderError):
        await adapter.modify_order(make_amendment(original, qty=8, price=Decimal("500.10")))
    await adapter.place_order(original)
    ib.ack(101, filled=6.0)
    with pytest.raises(AmendmentRejectedError, match="已成交"):
        await adapter.modify_order(make_amendment(original, qty=6, price=Decimal("500.10")))
    await adapter.cancel_order("101")
    with pytest.raises(AmendmentRejectedError, match="撤销"):
        await adapter.modify_order(make_amendment(original, qty=8, price=Decimal("500.10")))
    ib.ack(101, filled=6.0, status="Cancelled")
    with pytest.raises(UnknownOrderError, match="已结束"):
        await adapter.modify_order(make_amendment(original, qty=8, price=Decimal("500.10")))


# ---------- 撤单 ----------


@pytest.mark.asyncio
async def test_cancel_limit_order_reports_cancel_pending() -> None:
    adapter, ib = await connected()
    stream = adapter.events()
    await anext(stream)
    await adapter.place_order(ticket())
    ib.ack(101)
    await anext(stream)  # ACKNOWLEDGED
    await adapter.cancel_order("101")
    assert ib.cancelled == [101]
    update = await asyncio.wait_for(anext(stream), 1)
    assert isinstance(update, OrderStatusUpdate) and update.state is BrokerOrderState.CANCEL_PENDING


@pytest.mark.asyncio
async def test_protective_stop_cancel_needs_explicit_confirmation() -> None:
    adapter, ib = await connected()
    await adapter.place_order(stop_ticket())
    ib.ack(102, status="PreSubmitted")
    with pytest.raises(ProtectiveStopCancelError):
        await adapter.cancel_order("102")
    assert ib.cancelled == []
    await adapter.cancel_order("102", allow_protective_stop=True)
    assert ib.cancelled == [102]


@pytest.mark.asyncio
async def test_cancel_refuses_unknown_done_and_foreign_orders() -> None:
    adapter, ib = await connected()
    with pytest.raises(UnknownOrderError):
        await adapter.cancel_order("101")
    await adapter.place_order(ticket())
    ib.ack(101, filled=10.0, status="Filled")
    with pytest.raises(UnknownOrderError, match="已结束"):
        await adapter.cancel_order("101")
    ib.foreign(333, client_id=9)  # 别的 clientId 的同号订单不是本连接的
    with pytest.raises(UnknownOrderError):
        await adapter.cancel_order("333")
    with pytest.raises(BrokerUnavailableError, match="不是本连接"):
        await adapter.cancel_order("perm-9000")
    assert ib.cancelled == []


@pytest.mark.asyncio
async def test_foreign_order_with_the_same_id_does_not_block_placing() -> None:
    adapter, ib = await connected()
    ib.foreign(101, client_id=9)
    await adapter.place_order(ticket())
    assert [o.orderId for o in ib.placed] == [101]


def test_expiry_helper_handles_offsets() -> None:
    eastern = datetime(2026, 11, 5, 15, 30, tzinfo=ZoneInfo("America/New_York"))
    assert good_till(eastern) == "20261105 20:30:00 UTC"  # 美东 15:30 = UTC 20:30（标准时间）


# ---------- 评审补充 ----------


@pytest.mark.asyncio
async def test_concurrent_submissions_with_the_same_id_send_only_once() -> None:
    adapter, ib = await connected()
    results = await asyncio.gather(adapter.place_order(ticket()), adapter.place_order(ticket()),
                                   return_exceptions=True)  # fmt: skip
    assert sum(isinstance(r, DuplicateOrderError) for r in results) == 1
    assert len(ib.placed) == 1


@pytest.mark.asyncio
async def test_failed_contract_check_also_burns_the_id() -> None:
    adapter, ib = await connected()
    ib.qualify = ib_async.Option("SPY", conId=5)
    with pytest.raises(MappingError):
        await adapter.place_order(ticket())
    ib.qualify = None
    with pytest.raises(DuplicateOrderError):
        await adapter.place_order(ticket())
    assert ib.placed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["no_con_id", "cad"])
async def test_contract_without_con_id_or_in_other_currency_is_refused(bad: str) -> None:
    adapter, ib = await connected()
    found = ib_async.Stock("SPY", "SMART", "CAD" if bad == "cad" else "USD")
    found.conId, found.secType = (0 if bad == "no_con_id" else 7), "STK"
    ib.qualify = found
    with pytest.raises(MappingError):
        await adapter.place_order(ticket())
    assert ib.placed == []


@pytest.mark.asyncio
async def test_modify_refuses_a_different_intent_behind_the_same_id() -> None:
    adapter, ib = await connected()
    await adapter.place_order(ticket())
    ib.ack(101)
    other = ticket(intent_id="i-other")
    with pytest.raises(UnknownOrderError, match="orderRef"):
        await adapter.modify_order(make_amendment(other, qty=8, price=Decimal("500.10")))
    assert len(ib.placed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("broker_side", ["STP", "conditional"])
async def test_modify_refuses_when_the_broker_side_type_differs(broker_side: str) -> None:
    adapter, ib = await connected()
    original = ticket()
    await adapter.place_order(original)
    ib.ack(101)
    trade = ib.trades()[0]
    if broker_side == "STP":
        trade.order.orderType = "STP"
    else:
        trade.order.conditions = [ib_async.PriceCondition()]
    with pytest.raises(AmendmentRejectedError, match="类型"):
        await adapter.modify_order(make_amendment(original, qty=8, price=Decimal("500.10")))
    assert len(ib.placed) == 1


@pytest.mark.asyncio
async def test_modifying_a_stop_changes_the_stop_price() -> None:
    adapter, ib = await connected()
    original = stop_ticket()
    await adapter.place_order(original)
    ib.ack(102, status="PreSubmitted")
    await adapter.modify_order(make_amendment(original, qty=10, price=Decimal("440.00")))
    sent = ib.placed[-1]
    assert (sent.orderType, sent.auxPrice, sent.totalQuantity) == ("STP", 440.0, 10.0)


@pytest.mark.asyncio
async def test_modify_for_another_account_is_refused() -> None:
    adapter, _ = await connected()
    original = ticket()
    await adapter.place_order(original)
    foreign = make_ticket(DRIVER, "101", "i-1", account_id="DU7654321")
    with pytest.raises(AccountMismatchError):
        await adapter.modify_order(make_amendment(foreign, qty=8, price=Decimal("500.10")))


@pytest.mark.asyncio
async def test_failed_modify_send_is_reported_as_unknown() -> None:
    adapter, ib = await connected()
    original = ticket()
    await adapter.place_order(original)
    ib.ack(101)
    ib.fail_place = True
    with pytest.raises(BrokerUnavailableError, match="先查单"):
        await adapter.modify_order(make_amendment(original, qty=8, price=Decimal("500.10")))


@pytest.mark.asyncio
async def test_inactive_orders_are_not_modified_or_cancelled() -> None:
    adapter, ib = await connected()
    original = ticket()
    await adapter.place_order(original)
    # 只改状态、不发事件：Inactive 的状态回报本身会让适配器回到未核验（含义不确定，先查单）
    ib.trades()[0].orderStatus.status = "Inactive"
    with pytest.raises(UnknownOrderError):
        await adapter.modify_order(make_amendment(original, qty=8, price=Decimal("500.10")))
    with pytest.raises(UnknownOrderError):
        await adapter.cancel_order("101")
    assert ib.cancelled == []


@pytest.mark.asyncio
async def test_cancel_in_progress_is_not_sent_again() -> None:
    adapter, ib = await connected()
    await adapter.place_order(ticket())
    ib.ack(101)
    await adapter.cancel_order("101")
    await adapter.cancel_order("101")
    assert ib.cancelled == [101]


@pytest.mark.asyncio
async def test_cancelling_an_unrecognised_type_needs_confirmation() -> None:
    adapter, ib = await connected()
    await adapter.place_order(ticket())
    ib.ack(101)
    ib.trades()[0].order.adjustedOrderType = "STP"  # 可调整止损：可能起保护作用
    with pytest.raises(ProtectiveStopCancelError):
        await adapter.cancel_order("101")
    assert ib.cancelled == []


@pytest.mark.asyncio
async def test_own_order_without_order_ref_is_not_cancelled() -> None:
    adapter, ib = await connected()
    await adapter.place_order(ticket())
    ib.ack(101)
    ib.trades()[0].order.orderRef = ""
    with pytest.raises(UnknownOrderError, match="orderRef"):
        await adapter.cancel_order("101")


def test_allow_orders_cannot_be_switched_on_later() -> None:
    client = IbAsyncClient(FakeIB())
    with pytest.raises(AttributeError):
        client.allow_orders = True  # type: ignore[misc]
