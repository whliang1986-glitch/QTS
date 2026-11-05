"""IBKR 只读适配器（B-03b，不连 TWS）：用 ib_async 的真实数据类加替身 IB，验证转换、核验与只读。

这里的结果只说明“按文档与 ib_async 源码理解的字段能正确转换”，**不代表模拟盘联调通过**。
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import ib_async
import pytest
from ib_async.util import UNSET_DOUBLE
from server.broker.base import (
    AccountMismatchError,
    BrokerError,
    BrokerOrderState,
    BrokerOrderType,
    BrokerUnavailableError,
    ConnectionEvent,
    ConnectionState,
    ExecutionReport,
    OrderSource,
    OrderStatusUpdate,
)
from server.broker.ibkr.adapter import IbkrReadOnlyAdapter
from server.broker.ibkr.client import IbAsyncClient
from server.broker.ibkr.connection import LOCALHOST, ConnectionPolicy
from server.broker.ibkr.convert import (
    PendingOrderError,
    broker_order_id,
    cash_balance,
    decimal_of,
    execution,
    order_record,
    position,
)
from server.broker.ibkr.mapping import MappingError
from server.domain.enums import AccountType, Mode, Side

ACCOUNT = "DU1234567"
OTHER = "DU9999999"
CLIENT = 7
NOW = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)
REF = "benchmark:v1:2026-12-01:SPY:BUY:OPEN"


def policy() -> ConnectionPolicy:
    return ConnectionPolicy(mode=Mode.PAPER, host=LOCALHOST, port=7497, client_id=CLIENT,
                            readonly=True, expected_account_id=ACCOUNT,
                            expected_account_type=AccountType.CASH)  # fmt: skip


def stock(symbol: str = "SPY") -> ib_async.Contract:
    return ib_async.Stock(symbol, "SMART", "USD")


def order(
    *,
    order_id: int = 11,
    client_id: int = CLIENT,
    perm: int | None = None,
    ref: str = REF,
    kind: str = "LMT",
    total: float = 10.0,
    account: str = ACCOUNT,
    action: str = "BUY",
) -> ib_async.Order:
    result = ib_async.Order(orderId=order_id, clientId=client_id,
                            permId=900 + order_id if perm is None else perm, action=action,
                            totalQuantity=total, orderType=kind, lmtPrice=500.25, tif="DAY",
                            account=account, orderRef=ref)  # fmt: skip
    if kind in {"STP", "TRAIL"}:
        result.lmtPrice, result.auxPrice = UNSET_DOUBLE, 450.0
    return result


def open_trade(*, status: str = "Submitted", filled: float = 0.0, remaining: float = 10.0,
               **kw: Any) -> ib_async.Trade:  # fmt: skip
    o = order(total=filled + remaining, **kw)
    st = ib_async.OrderStatus(orderId=o.orderId, status=status, filled=filled, remaining=remaining)
    return ib_async.Trade(contract=stock(), order=o, orderStatus=st)


def completed_trade(*, status: str = "Filled", filled: float = 10.0, total: float = 10.0,
                    **kw: Any) -> ib_async.Trade:  # fmt: skip
    """ib_async 的 completedOrder 只给 status：成交量在 order.filledQuantity 上（未实测）。"""
    o = order(total=total, **kw)
    o.filledQuantity = filled
    return ib_async.Trade(
        contract=stock(), order=o, orderStatus=ib_async.OrderStatus(status=status)
    )


def fill(exec_id: str = "e1", *, account: str = ACCOUNT, client_id: int = CLIENT,
         commission: ib_async.CommissionReport | None = None) -> ib_async.Fill:  # fmt: skip
    ex = ib_async.Execution(execId=exec_id, time=NOW, acctNumber=account, side="BOT",
                            shares=5.0, price=500.1, permId=911, orderId=11, clientId=client_id,
                            orderRef=REF)  # fmt: skip
    report = commission or ib_async.CommissionReport(execId=exec_id, commission=1.0,
                                                     currency="USD")  # fmt: skip
    return ib_async.Fill(contract=stock(), execution=ex, commissionReport=report, time=NOW)


def value(tag: str, amount: str, currency: str = "USD", account: str = ACCOUNT) -> Any:
    return ib_async.AccountValue(account, tag, amount, currency, "")


class FakeIB:
    """替身：只实现适配器用到的方法；事件用 ib_async 自己的 Event，与真实对象行为一致。"""

    def __init__(self) -> None:
        real = ib_async.IB()
        self.orderStatusEvent = real.orderStatusEvent
        self.execDetailsEvent = real.execDetailsEvent
        self.commissionReportEvent = real.commissionReportEvent
        self.errorEvent = real.errorEvent
        self.disconnectedEvent = real.disconnectedEvent
        self.connected = False
        self.readonly: bool | None = None
        self.accounts = [ACCOUNT]
        self.summary = [
            value("AccountType", "CASH", ""),
            value("Currency", "USD"),
            value("TotalCashValue", "100000.50"),
            value("SettledCash", "90000.25"),
        ]
        self.positions = [ib_async.Position(ACCOUNT, stock(), 12.0, 480.8333333333333)]
        self.open: list[ib_async.Trade] = [open_trade()]
        self.completed: list[ib_async.Trade] = [completed_trade(order_id=12)]
        self.fills = [fill()]

    async def connectAsync(self, host: str, port: int, clientId: int, timeout: float,  # noqa: N802, N803
                           readonly: bool) -> None:  # fmt: skip
        del host, port, clientId, timeout
        self.readonly = readonly
        self.connected = True

    def disconnect(self) -> None:
        if self.connected:
            self.connected = False
            self.disconnectedEvent.emit()

    def isConnected(self) -> bool:  # noqa: N802
        return self.connected

    def managedAccounts(self) -> list[str]:  # noqa: N802
        return self.accounts

    async def accountSummaryAsync(self, account: str = "") -> list[Any]:  # noqa: N802
        return self.summary

    async def reqPositionsAsync(self) -> list[Any]:  # noqa: N802
        return self.positions

    async def reqAllOpenOrdersAsync(self) -> list[Any]:  # noqa: N802
        return self.open

    async def reqCompletedOrdersAsync(self, apiOnly: bool) -> list[Any]:  # noqa: N802, N803
        assert apiOnly is False  # 要包含 TWS 手工单与别的 clientId 的已完成订单
        return self.completed

    async def reqExecutionsAsync(self) -> list[Any]:  # noqa: N802
        return self.fills


async def connected() -> tuple[IbkrReadOnlyAdapter, FakeIB]:
    ib = FakeIB()
    adapter = IbkrReadOnlyAdapter(policy(), client=IbAsyncClient(ib), clock=lambda: NOW)
    await adapter.connect()
    return adapter, ib


# ---------- 连接与核验 ----------


@pytest.mark.asyncio
async def test_connects_read_only_and_verifies_the_account() -> None:
    adapter, ib = await connected()
    assert ib.readonly is True
    assert adapter.connected
    account = await adapter.account()
    assert (account.account_id, account.account_type) == (ACCOUNT, AccountType.CASH)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["two_accounts", "live_like", "margin", "individual", "eur"])
async def test_account_mismatch_disconnects_and_halts(case: str) -> None:
    ib = FakeIB()
    if case == "two_accounts":
        ib.accounts = [ACCOUNT, OTHER]
    elif case == "live_like":
        ib.accounts = ["U1234567"]
    else:
        kind = {"margin": "MARGIN", "individual": "INDIVIDUAL", "eur": "CASH"}[case]
        ib.summary[0] = value("AccountType", kind, "")
        if case == "eur":
            ib.summary[1] = value("Currency", "EUR", "EUR")
    adapter = IbkrReadOnlyAdapter(policy(), client=IbAsyncClient(ib), clock=lambda: NOW)
    with pytest.raises(AccountMismatchError):
        await adapter.connect()
    assert not ib.connected and not adapter.connected
    with pytest.raises(BrokerUnavailableError, match="停机"):  # 不会一次次连到错误的账户
        await adapter.connect()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("host", "port", "client_id", "readonly"),
    [("127.0.0.1", 7496, 1, True), ("127.0.0.1", 4001, 1, True), ("localhost", 7497, 1, True),
     ("192.168.1.5", 7497, 1, True), ("127.0.0.1", 7497, 0, True),
     ("127.0.0.1", 7497, 1, False)],
)  # fmt: skip
async def test_client_itself_refuses_live_ports_and_non_read_only(
    host: str, port: int, client_id: int, readonly: bool
) -> None:
    ib = FakeIB()
    with pytest.raises(ValueError):
        await IbAsyncClient(ib).connect(host, port, client_id, readonly=readonly)
    assert not ib.connected
    if not readonly:  # 显式开启 allow_orders 也只能连本机模拟盘
        with pytest.raises(ValueError, match="模拟盘端口"):
            await IbAsyncClient(ib, allow_orders=True).connect(host, 7496, client_id,
                                                               readonly=False)  # fmt: skip


@pytest.mark.asyncio
async def test_non_read_only_connection_needs_explicit_allow_orders() -> None:
    ib = FakeIB()
    await IbAsyncClient(ib, allow_orders=True).connect("127.0.0.1", 7497, 1, readonly=False)
    assert ib.connected and ib.readonly is False


def test_read_only_adapter_refuses_a_non_read_only_policy() -> None:
    with pytest.raises(ValueError, match="只读适配器"):
        IbkrReadOnlyAdapter(replace(policy(), readonly=False), client=IbAsyncClient(FakeIB()))


# ---------- 快照 ----------


@pytest.mark.asyncio
async def test_snapshots_convert_without_float_errors() -> None:
    adapter, _ = await connected()
    [pos] = await adapter.positions()
    assert (pos.symbol, pos.quantity) == ("SPY", Decimal(12))
    assert pos.average_cost == Decimal("480.8333333333")  # 平均成本按 10 位取整
    cash = await adapter.cash()
    assert (cash.settled, cash.unsettled) == (Decimal("90000.25"), Decimal("10000.25"))
    [o] = await adapter.open_orders()
    assert (o.order_type, o.price, o.state) == (BrokerOrderType.LIMIT, Decimal("500.25"),
                                                BrokerOrderState.ACKNOWLEDGED)  # fmt: skip
    assert (o.broker_order_id, o.order_ref) == ("11", REF)
    [done] = await adapter.completed_orders()
    assert done.state is BrokerOrderState.FILLED and done.filled_quantity == Decimal(10)
    [ex] = await adapter.executions(NOW.replace(hour=0))
    got = (ex.side, ex.quantity, ex.price, ex.commission)
    assert got == (Side.BUY, Decimal(5), Decimal("500.1"), Decimal(1))


def test_completed_partial_fill_then_cancel_keeps_the_filled_part() -> None:
    record = order_record(completed_trade(status="Cancelled", filled=4.0), OrderSource.COMPLETED,
                          CLIENT)  # fmt: skip
    assert record.state is BrokerOrderState.CANCELLED
    assert (record.filled_quantity, record.remaining_quantity) == (Decimal(4), Decimal(6))
    with pytest.raises(MappingError, match="成交量"):
        order_record(completed_trade(filled=UNSET_DOUBLE), OrderSource.COMPLETED, CLIENT)
    with pytest.raises(MappingError, match="大于订单数量"):
        order_record(completed_trade(filled=12.0), OrderSource.COMPLETED, CLIENT)


@pytest.mark.asyncio
async def test_lookup_merges_open_completed_and_fills() -> None:
    adapter, _ = await connected()
    lookup = await adapter.lookup_order(REF)
    assert {o.broker_order_id for o in lookup.orders} == {"11", "12"}
    assert [e.execution_id for e in lookup.executions] == ["e1"]
    assert await adapter.executions(NOW.replace(hour=16)) == []  # 早于起点的不算


@pytest.mark.asyncio
@pytest.mark.parametrize("what", ["position", "open", "completed", "fill"])
async def test_foreign_account_data_is_never_silently_dropped(what: str) -> None:
    adapter, ib = await connected()
    if what == "position":
        ib.positions.append(ib_async.Position(OTHER, stock("QQQ"), 1.0, 400.0))
        call: Coroutine[Any, Any, Any] = adapter.positions()
    elif what == "open":
        ib.open.append(open_trade(order_id=13, account=OTHER))
        call = adapter.open_orders()
    elif what == "completed":
        ib.completed.append(completed_trade(order_id=14, account=OTHER))
        call = adapter.completed_orders()
    else:
        ib.fills.append(fill("e2", account=OTHER))
        call = adapter.executions(NOW.replace(hour=0))
    with pytest.raises(MappingError, match="不属于登记账户"):
        await call


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ([value("TotalCashValue", "100")], "SettledCash"),
        ([value("TotalCashValue", "100"), value("SettledCash", "150")], "不一致"),
        ([value("TotalCashValue", "100"), value("SettledCash", "-1")], "不一致"),
        ([value("TotalCashValue", "100"), value("SettledCash", "80", "EUR")], "SettledCash"),
        ([value("TotalCashValue", "100"), value("TotalCashValue", "90", "EUR"),
          value("SettledCash", "80"), value("SettledCash", "70")], "SettledCash"),
    ],
)  # fmt: skip
def test_cash_fails_closed(values: list[Any], message: str) -> None:
    with pytest.raises(MappingError, match=message):
        cash_balance(values, ACCOUNT, NOW)


def test_cash_ignores_other_currencies_when_usd_is_unique() -> None:
    values = [value("TotalCashValue", "100"), value("TotalCashValue", "90", "EUR"),
              value("SettledCash", "80")]  # fmt: skip
    cash = cash_balance(values, ACCOUNT, NOW)
    assert (cash.settled, cash.unsettled) == (Decimal(80), Decimal(20))


# ---------- 订单号、订单类型 ----------


def test_order_ids_are_unique_across_client_ids() -> None:
    assert broker_order_id(11, CLIENT, 911, CLIENT) == "11"
    assert broker_order_id(11, 99, 811, CLIENT) == "perm-811"  # 别的 clientId 的同号单
    assert broker_order_id(0, 0, 712, CLIENT) == "perm-712"  # TWS 手工单
    with pytest.raises(MappingError, match="唯一"):
        broker_order_id(0, 0, 0, CLIENT)  # 还没有 permId 的手工单：不能撞成 perm-0
    mine = order_record(open_trade(order_id=11), OrderSource.OPEN, CLIENT)
    theirs = order_record(open_trade(order_id=11, client_id=99, perm=811), OrderSource.OPEN,
                          CLIENT)  # fmt: skip
    assert mine.broker_order_id != theirs.broker_order_id


def test_stop_trailing_and_conditional_orders_need_confirmation_to_cancel() -> None:
    stop = order_record(open_trade(kind="STP", action="SELL"), OrderSource.OPEN, CLIENT)
    assert stop.order_type is BrokerOrderType.STOP and stop.price == Decimal(450)
    trail = order_record(open_trade(kind="TRAIL", action="SELL"), OrderSource.OPEN, CLIENT)
    assert trail.order_type is BrokerOrderType.STOP and trail.price is None  # 追踪幅度不是价格
    conditional = open_trade()
    conditional.order.conditions = [ib_async.PriceCondition()]
    assert order_record(conditional, OrderSource.OPEN, CLIENT).order_type is BrokerOrderType.OTHER
    adjusted = open_trade()
    adjusted.order.adjustedOrderType = "STP"
    assert order_record(adjusted, OrderSource.OPEN, CLIENT).order_type is BrokerOrderType.OTHER


def test_unconfirmed_orders_fail_the_snapshot_instead_of_vanishing() -> None:
    for status in ("", "PendingSubmit", "ApiPending"):
        with pytest.raises(MappingError, match="还没有确认"):
            order_record(open_trade(status=status), OrderSource.OPEN, CLIENT)


def defaults_trade() -> ib_async.Trade:
    """openOrder 刚到：状态已是 Submitted，但成交量与剩余量还是默认的 0（orderStatus 还没到）。"""
    trade = open_trade()
    trade.orderStatus.remaining = 0.0
    return trade


@pytest.mark.asyncio
async def test_open_order_with_default_quantities_is_not_trusted_yet() -> None:
    with pytest.raises(PendingOrderError, match="数量还没有确认"):
        order_record(defaults_trade(), OrderSource.OPEN, CLIENT)
    adapter, ib = await connected()
    ib.open = [defaults_trade()]
    with pytest.raises(MappingError):
        await adapter.open_orders()  # 快照失败、稍后重查，不当作 10 股全部成交
    stream = adapter.events()
    await anext(stream)
    ib.orderStatusEvent.emit(defaults_trade())  # 事件路径：等后续状态，不产生事件
    assert adapter._events.empty() and adapter.connected


def test_open_terminal_orders_with_consistent_quantities_pass() -> None:
    filled = order_record(open_trade(status="Filled", filled=10, remaining=0), OrderSource.OPEN,
                          CLIENT)  # fmt: skip
    assert filled.state is BrokerOrderState.FILLED
    cancelled = order_record(open_trade(status="Cancelled", filled=4, remaining=6),
                             OrderSource.OPEN, CLIENT)  # fmt: skip
    assert cancelled.state is BrokerOrderState.CANCELLED


@pytest.mark.asyncio
async def test_terminal_status_with_default_quantities_is_reported_not_dropped() -> None:
    """终态之后不会再有回报（如收到 201 后直接改成 Cancelled、数量还是 0）：上报不可信。"""
    trade = defaults_trade()
    trade.orderStatus.status = "Cancelled"
    with pytest.raises(MappingError, match="数量对不上") as info:
        order_record(trade, OrderSource.OPEN, CLIENT)
    assert not isinstance(info.value, PendingOrderError)
    adapter, ib = await connected()
    stream = adapter.events()
    await anext(stream)
    ib.orderStatusEvent.emit(trade)
    err = await asyncio.wait_for(anext(stream), 1)
    assert isinstance(err, BrokerError) and err.code == "MAPPING"
    assert not adapter.connected


def test_non_stock_contracts_are_marked_and_unknown_types_refused() -> None:
    option = ib_async.Position(ACCOUNT, ib_async.Option("SPY", conId=123), 1.0, 5.0)
    assert position(option, NOW).symbol == "SPY:OPT:123"
    blank = ib_async.Position(ACCOUNT, ib_async.Contract(symbol="SPY"), 1.0, 5.0)
    with pytest.raises(MappingError, match="品种"):
        position(blank, NOW)


# ---------- 只读 ----------


@pytest.mark.asyncio
async def test_order_methods_are_refused_while_read_only() -> None:
    adapter, _ = await connected()
    for call in (adapter.next_order_id(), adapter.place_order(None),  # type: ignore[arg-type]
                 adapter.modify_order(None),  # type: ignore[arg-type]
                 adapter.cancel_order("11")):  # fmt: skip
        with pytest.raises(BrokerUnavailableError, match="只读"):
            await call


@pytest.mark.asyncio
async def test_queries_refused_when_not_connected() -> None:
    adapter = IbkrReadOnlyAdapter(policy(), client=IbAsyncClient(FakeIB()), clock=lambda: NOW)
    with pytest.raises(BrokerUnavailableError):
        await adapter.positions()


# ---------- 事件 ----------


@pytest.mark.asyncio
async def test_events_status_fill_error_and_disconnect() -> None:
    adapter, ib = await connected()
    stream = adapter.events()
    assert isinstance(await anext(stream), ConnectionEvent)
    ib.orderStatusEvent.emit(open_trade(status="PendingSubmit"))  # 中间状态：不产生事件
    ib.orderStatusEvent.emit(open_trade(status="Submitted", filled=4, remaining=6))
    update = await asyncio.wait_for(anext(stream), 1)
    assert isinstance(update, OrderStatusUpdate)
    assert update.state is BrokerOrderState.PARTIALLY_FILLED
    ib.execDetailsEvent.emit(open_trade(), fill("e9"))
    report = await asyncio.wait_for(anext(stream), 1)
    assert isinstance(report, ExecutionReport) and report.execution.execution_id == "e9"
    ib.errorEvent.emit(-1, 2104, "行情农场连接正常", None)  # 信息类：不上报
    ib.errorEvent.emit(11, 201, "订单被拒", None)
    err = await asyncio.wait_for(anext(stream), 1)
    assert isinstance(err, BrokerError) and err.code == "201" and err.broker_order_id == "11"
    ib.errorEvent.emit(5, 354, "没有行情订阅", None)  # 不是订单错误：reqId 不当订单号
    err = await asyncio.wait_for(anext(stream), 1)
    assert isinstance(err, BrokerError) and err.broker_order_id is None
    await adapter.disconnect()
    gone = await asyncio.wait_for(anext(stream), 1)
    assert isinstance(gone, ConnectionEvent) and gone.state is ConnectionState.DISCONNECTED
    assert adapter._events.empty()  # 自己断开只发一条 DISCONNECTED
    ib.orderStatusEvent.emit(open_trade(status="Submitted"))  # 断开后已退订：不进队列
    assert adapter._events.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["unknown_status", "foreign_fill"])
async def test_untrusted_events_reset_the_adapter(bad: str) -> None:
    adapter, ib = await connected()
    stream = adapter.events()
    await anext(stream)
    if bad == "unknown_status":
        ib.orderStatusEvent.emit(open_trade(status="Weird"))
    else:
        ib.execDetailsEvent.emit(open_trade(), fill("e8", account=OTHER))
    err = await asyncio.wait_for(anext(stream), 1)
    assert isinstance(err, BrokerError) and err.code == "MAPPING"
    assert not adapter.connected  # 回到未核验：必须重新核验与对账


@pytest.mark.asyncio
async def test_queue_overflow_resets_the_adapter() -> None:
    adapter, ib = await connected()
    adapter._events = asyncio.Queue(1)
    ib.orderStatusEvent.emit(open_trade(status="Submitted"))
    ib.orderStatusEvent.emit(open_trade(status="Submitted"))
    assert not adapter.connected


# ---------- 数值 ----------


def test_placeholder_commission_is_unknown_not_zero() -> None:
    assert execution(fill("e3", commission=ib_async.CommissionReport()), CLIENT).commission is None
    assert execution(fill("e4"), CLIENT).commission == Decimal(1)
    wrong = ib_async.CommissionReport(execId="other", commission=1.0, currency="USD")
    with pytest.raises(MappingError, match="对不上"):
        execution(fill("e5", commission=wrong), CLIENT)
    eur = ib_async.CommissionReport(execId="e6", commission=1.0, currency="EUR")
    with pytest.raises(MappingError, match="USD"):
        execution(fill("e6", commission=eur), CLIENT)
    blank = ib_async.CommissionReport(execId="e7", commission=1.0, currency="")
    with pytest.raises(MappingError, match="USD"):
        execution(fill("e7", commission=blank), CLIENT)  # 币种为空不当作 USD


def test_unset_double_and_nan_are_rejected() -> None:
    for raw in (UNSET_DOUBLE, float("nan"), float("inf")):
        with pytest.raises(MappingError):
            decimal_of(raw, "x")
        assert decimal_of(raw, "x", allow_unset=True) is None
    assert decimal_of(0.1, "x") == Decimal("0.1")  # 不带二进制误差


def test_importing_ib_async_does_not_patch_the_event_loop() -> None:
    assert not hasattr(asyncio, "_nest_patched")
    loop = asyncio.new_event_loop()
    try:
        assert not hasattr(loop, "_nest_patched")
    finally:
        loop.close()
