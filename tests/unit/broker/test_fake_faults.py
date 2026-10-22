"""B-02：FakeBroker 故障注入（V4.1 11.2 券商侧场景，矩阵见 docs/tasks/B-02.md）。

这里只验证注入后的**券商事实与事件**；OMS、风控、对账在这些注入下的正确行为由 O、R 组任务验证。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from server.broker.base import (
    AmendmentRejectedError,
    BrokerError,
    BrokerEvent,
    BrokerOrderState,
    BrokerUnavailableError,
    CommissionReport,
    ConnectionEvent,
    ConnectionState,
    DuplicateOrderError,
    ErrorCategory,
    ExecutionReport,
    OrderStatusUpdate,
    OrderTicket,
    ProtectiveStopCancelError,
    UnknownOrderError,
)
from server.broker.fake.broker import FakeBroker, FakeBrokerServer, next_weekday
from server.broker.sim.kernel import FixedCommission
from server.domain.account import Account
from server.domain.enums import AccountType, AssetClass, OrderClass, OrderType, Side
from server.domain.instrument import Instrument
from server.domain.market import MarketBar
from tests.contract.broker_contract import make_amendment, make_ticket

ACCOUNT = Account(account_id="DU0000001", account_type=AccountType.CASH, base_currency="USD")
T0 = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)


class Driver:
    account_id = ACCOUNT.account_id

    def instrument(self) -> Instrument:
        return Instrument(
            symbol="SPY",
            asset_class=AssetClass.ETF,
            exchange="ARCA",
            currency="USD",
            tick_size=Decimal("0.01"),
            trading_calendar="XNYS",
        )

    def resting_price(self) -> Decimal:
        return Decimal("100.00")


D = Driver()


def server(**kw: object) -> FakeBrokerServer:
    kw.setdefault("clock", lambda: T0)
    return FakeBrokerServer(ACCOUNT, **kw)  # type: ignore[arg-type]


async def client(srv: FakeBrokerServer, client_id: int = 1) -> FakeBroker:
    broker = FakeBroker(srv, client_id=client_id)
    await broker.connect()
    return broker


def drain(broker: FakeBroker) -> list[BrokerEvent]:
    out: list[BrokerEvent] = []
    while not broker._queue.empty():
        out.append(broker._queue.get_nowait())
    return out


def statuses(events: list[BrokerEvent]) -> list[BrokerOrderState]:
    return [e.state for e in events if isinstance(e, OrderStatusUpdate)]


async def place(broker: FakeBroker, tag: str, **kw: object) -> OrderTicket:
    ticket = make_ticket(D, await broker.next_order_id(), tag, **kw)  # type: ignore[arg-type]
    await broker.place_order(ticket)
    return ticket


def bar(o: str, h: str, low: str, c: str, start: datetime) -> MarketBar:
    return MarketBar(
        symbol="SPY",
        start=start,
        end=start + timedelta(minutes=1),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal(1_000_000),
        source="test",
        adjustment_version="raw",
    )


# ---------- 场景 1：已接单但响应丢失 ----------


@pytest.mark.asyncio
async def test_lost_ack_order_exists_at_broker_without_events() -> None:
    srv = server()
    b = await client(srv)
    drain(b)
    srv.lose_next_ack()
    t = await place(b, "lost-ack")
    assert drain(b) == []
    found = await b.lookup_order(t.order_ref)
    assert [o.state for o in found.orders] == [BrokerOrderState.ACKNOWLEDGED]
    # 用同一张票据重发：券商拒绝第二张，券商侧只有一张单
    with pytest.raises(DuplicateOrderError):
        await b.place_order(t)
    assert [o.order_ref for o in await b.open_orders()] == [t.order_ref]
    # 只影响一张订单
    t2 = await place(b, "next")
    assert statuses(drain(b)) == [BrokerOrderState.ACKNOWLEDGED]
    assert (await b.lookup_order(t2.order_ref)).orders


@pytest.mark.asyncio
async def test_lost_request_leaves_no_order_at_broker() -> None:
    srv = server()
    b = await client(srv)
    drain(b)
    srv.lose_next_request()
    t = await place(b, "lost-req")
    assert drain(b) == []
    assert (await b.lookup_order(t.order_ref)).empty  # 查不到：OMS 须多轮查证，不得据此重发


# ---------- 场景 2：成交回报重复 ----------


@pytest.mark.asyncio
async def test_duplicate_execution_report() -> None:
    srv = server()
    b = await client(srv)
    t = await place(b, "dup")
    ex = srv.fill(t.broker_order_id, Decimal(10), Decimal(100))
    srv.duplicate_execution(ex.execution_id)
    reports = [e for e in drain(b) if isinstance(e, ExecutionReport)]
    assert [r.execution.execution_id for r in reports] == [ex.execution_id] * 2
    assert len(srv.executions(T0, None, None)) == 1  # 券商侧只有一笔
    assert (await b.positions())[0].quantity == Decimal(10)


# ---------- 场景 3：回报乱序 ----------


@pytest.mark.asyncio
async def test_execution_before_acknowledgement() -> None:
    srv = server()
    b = await client(srv)
    drain(b)
    srv.hold_events()
    t = await place(b, "ooo")
    srv.fill(t.broker_order_id, Decimal(10), Decimal(100))
    assert drain(b) == []
    srv.release_events(reverse=True)
    events = drain(b)
    exec_at = next(i for i, e in enumerate(events) if isinstance(e, ExecutionReport))
    ack_at = next(
        i
        for i, e in enumerate(events)
        if isinstance(e, OrderStatusUpdate) and e.state is BrokerOrderState.ACKNOWLEDGED
    )
    assert exec_at < ack_at  # 成交先于确认到达
    assert statuses(events) == [BrokerOrderState.FILLED, BrokerOrderState.ACKNOWLEDGED]


@pytest.mark.asyncio
async def test_held_events_keep_order_when_not_reversed() -> None:
    srv = server()
    b = await client(srv)
    drain(b)
    srv.hold_events()
    t = await place(b, "held")
    srv.fill(t.broker_order_id, Decimal(4), Decimal(100))
    srv.release_events()
    events = drain(b)
    assert [type(e) for e in events] == [OrderStatusUpdate, ExecutionReport, OrderStatusUpdate]
    assert statuses(events) == [BrokerOrderState.ACKNOWLEDGED, BrokerOrderState.PARTIALLY_FILLED]


@pytest.mark.asyncio
async def test_held_events_are_not_delivered_to_clients_that_reconnect_later() -> None:
    srv = server()
    b = await client(srv)
    t = await place(b, "held-drop")
    drain(b)
    srv.hold_events()
    srv.fill(t.broker_order_id, Decimal(1), Decimal(100))
    srv.drop_client(1)
    b2 = await client(srv)
    drain(b2)
    srv.release_events()
    assert drain(b2) == []  # 断线期间的事件不补发


# ---------- 场景 4：撤单中成交 ----------


@pytest.mark.asyncio
async def test_partial_fill_during_cancel_then_confirm() -> None:
    srv = server()
    srv.manual_cancel_confirm = True
    b = await client(srv)
    t = await place(b, "c1")
    drain(b)
    await b.cancel_order(t.broker_order_id)
    srv.fill(t.broker_order_id, Decimal(4), Decimal(100))
    await b.cancel_order(t.broker_order_id)  # 重复撤单：券商只处理一次
    srv.confirm_cancel(t.broker_order_id)
    events = drain(b)
    assert statuses(events) == [
        BrokerOrderState.CANCEL_PENDING,
        BrokerOrderState.CANCEL_PENDING,  # 撤单期间的部分成交
        BrokerOrderState.CANCELLED,
    ]
    final = [e for e in events if isinstance(e, OrderStatusUpdate)][-1]
    assert final.filled_quantity == Decimal(4) and final.remaining_quantity == 0


@pytest.mark.asyncio
async def test_full_fill_during_cancel_makes_cancel_too_late() -> None:
    srv = server()
    srv.manual_cancel_confirm = True
    b = await client(srv)
    t = await place(b, "c2")
    await b.cancel_order(t.broker_order_id)
    srv.fill(t.broker_order_id, Decimal(10), Decimal(100))
    drain(b)
    srv.confirm_cancel(t.broker_order_id)
    (err,) = drain(b)
    assert isinstance(err, BrokerError) and err.category is ErrorCategory.CANCEL_REJECTED
    assert (await b.lookup_order(t.order_ref)).orders[0].state is BrokerOrderState.FILLED


@pytest.mark.asyncio
async def test_rejected_cancel_returns_to_active_state() -> None:
    srv = server()
    srv.manual_cancel_confirm = True
    b = await client(srv)
    t = await place(b, "c3")
    srv.fill(t.broker_order_id, Decimal(3), Decimal(100))
    await b.cancel_order(t.broker_order_id)
    drain(b)
    srv.reject_cancel(t.broker_order_id, "交易所拒绝")
    events = drain(b)
    assert statuses(events) == [BrokerOrderState.CANCEL_REJECTED, BrokerOrderState.PARTIALLY_FILLED]
    rejected = events[0]
    assert isinstance(rejected, OrderStatusUpdate) and rejected.reason == "交易所拒绝"
    assert rejected.remaining_quantity == Decimal(7)  # 订单仍然活着，剩余量照实报出
    with pytest.raises(UnknownOrderError):
        srv.confirm_cancel(t.broker_order_id)  # 没有待确认的撤单


# ---------- 场景 5：拒单 ----------


@pytest.mark.parametrize(
    "category",
    [ErrorCategory.PRICE_REJECTED, ErrorCategory.NO_PERMISSION, ErrorCategory.CONTRACT_NOT_FOUND],
)
@pytest.mark.asyncio
async def test_rejection(category: ErrorCategory) -> None:
    srv = server()
    b = await client(srv)
    drain(b)
    srv.reject_next(category, "注入的拒单")
    t = await place(b, "rej")
    err, status = drain(b)
    assert isinstance(err, BrokerError) and err.category is category and not err.retryable
    assert isinstance(status, OrderStatusUpdate) and status.state is BrokerOrderState.REJECTED
    assert status.error_code == err.code
    assert [o.state for o in await b.completed_orders()] == [BrokerOrderState.REJECTED]
    assert (await b.lookup_order(t.order_ref)).orders[0].remaining_quantity == 0
    await place(b, "after")  # 只影响一张订单
    assert statuses(drain(b)) == [BrokerOrderState.ACKNOWLEDGED]


@pytest.mark.asyncio
async def test_insufficient_funds_counts_open_buy_orders() -> None:
    srv = server(settled_cash=Decimal(1500))
    b = await client(srv)
    drain(b)
    await place(b, "a")  # 10 × 100 = 1000，占用购买力
    await place(b, "b")  # 再要 1000，只剩 500
    events = drain(b)
    assert statuses(events) == [BrokerOrderState.ACKNOWLEDGED, BrokerOrderState.REJECTED]
    err = next(e for e in events if isinstance(e, BrokerError))
    assert err.category is ErrorCategory.INSUFFICIENT_FUNDS
    # 拒单不占用资金：剩余 500 的购买力仍可用于 5 × 100 的买单
    await place(b, "c", qty=5)
    assert statuses(drain(b)) == [BrokerOrderState.ACKNOWLEDGED]
    await place(b, "d", qty=1)  # 现在才真正用尽
    assert statuses(drain(b)) == [BrokerOrderState.REJECTED]


@pytest.mark.parametrize(
    ("enabled", "expected"),
    [(False, BrokerOrderState.REJECTED), (True, BrokerOrderState.ACKNOWLEDGED)],
)
@pytest.mark.asyncio
async def test_unsettled_proceeds_only_count_when_enabled(
    enabled: bool, expected: BrokerOrderState
) -> None:
    srv = server(
        settled_cash=Decimal(0),
        positions={"SPY": (Decimal(10), Decimal(80))},
        buy_with_unsettled=enabled,
    )
    b = await client(srv)
    sell = await place(b, "sell", side=Side.SELL, order_class=OrderClass.REDUCE)
    srv.fill(sell.broker_order_id, Decimal(10), Decimal(100))  # 1000 进入未结算
    drain(b)
    await place(b, "buy")
    assert statuses(drain(b)) == [expected]


@pytest.mark.asyncio
async def test_cash_account_rejects_uncovered_sell() -> None:
    srv = server(positions={"SPY": (Decimal(5), Decimal(80))})
    b = await client(srv)
    drain(b)
    await place(b, "too-many", side=Side.SELL, order_class=OrderClass.REDUCE)  # 10 > 5
    events = drain(b)
    assert statuses(events) == [BrokerOrderState.REJECTED]
    err = next(e for e in events if isinstance(e, BrokerError))
    assert err.category is ErrorCategory.NO_PERMISSION
    await place(b, "ok", qty=5, side=Side.SELL, order_class=OrderClass.REDUCE)
    assert statuses(drain(b)) == [BrokerOrderState.ACKNOWLEDGED]


@pytest.mark.asyncio
async def test_margin_account_accepts_uncovered_sell_at_broker() -> None:
    margin = Account(
        account_id=ACCOUNT.account_id, account_type=AccountType.MARGIN, base_currency="USD"
    )
    b = await client(FakeBrokerServer(margin, clock=lambda: T0))
    drain(b)
    await place(b, "margin-sell", side=Side.SELL, order_class=OrderClass.REDUCE)
    assert statuses(drain(b)) == [BrokerOrderState.ACKNOWLEDGED]  # 卖空由本地风控禁止


@pytest.mark.asyncio
async def test_amendment_rechecks_buying_power() -> None:
    srv = server(settled_cash=Decimal(1500))
    b = await client(srv)
    t = await place(b, "amend")  # 1000
    with pytest.raises(AmendmentRejectedError, match="购买力"):
        await b.modify_order(make_amendment(t, qty=10, price=Decimal(160)))  # 1600 > 1500
    await b.modify_order(make_amendment(t, qty=10, price=Decimal(140)))  # 不重复计算自己的占用


# ---------- 场景 6：提交后进程崩溃 ----------


@pytest.mark.asyncio
async def test_crash_after_submit_new_client_can_look_up() -> None:
    srv = server()
    b = await client(srv)
    t = await place(b, "crash")
    srv.drop_client(1)  # 进程崩溃：连接消失
    with pytest.raises(BrokerUnavailableError):
        await b.open_orders()
    b2 = await client(srv)
    assert (await b2.lookup_order(t.order_ref)).orders[0].broker_order_id == t.broker_order_id
    assert int(await b2.next_order_id()) > int(t.broker_order_id)


@pytest.mark.asyncio
async def test_same_client_id_cannot_connect_twice() -> None:
    srv = server()
    await client(srv)
    with pytest.raises(BrokerUnavailableError, match="已被占用"):
        await client(srv)


# ---------- 场景 7：断线期间状态漂移 ----------


@pytest.mark.asyncio
async def test_events_during_disconnect_are_lost_and_only_queries_reveal_drift() -> None:
    srv = server()
    b = await client(srv)
    filled = await place(b, "drift-fill")
    cancelled = await place(b, "drift-cancel")
    drain(b)
    srv.drop_client(1)
    srv.fill(filled.broker_order_id, Decimal(6), Decimal(100))
    srv.broker_side_cancel(cancelled.broker_order_id)  # 券商侧（如 TWS 手工）撤单
    assert [type(e) for e in drain(b)] == [ConnectionEvent]  # 只有 DISCONNECTED
    b2 = await client(srv)
    assert [type(e) for e in drain(b2)] == [ConnectionEvent]  # 不补发断线期间的事件
    states = {o.order_ref: o.state for o in await b2.open_orders() + await b2.completed_orders()}
    assert states == {
        filled.order_ref: BrokerOrderState.PARTIALLY_FILLED,
        cancelled.order_ref: BrokerOrderState.CANCELLED,
    }
    assert len(await b2.executions(T0, order_ref=filled.order_ref)) == 1


# ---------- 场景 8：券商重启窗口 ----------


@pytest.mark.asyncio
async def test_restart_window_disconnects_and_blocks() -> None:
    srv = server()
    b = await client(srv)
    drain(b)
    srv.enter_restart_window()
    (ev,) = drain(b)
    assert isinstance(ev, ConnectionEvent) and ev.state is ConnectionState.RESTART_WINDOW
    with pytest.raises(BrokerUnavailableError):
        await b.next_order_id()
    with pytest.raises(BrokerUnavailableError, match="重启窗口"):
        await b.connect()
    srv.exit_restart_window()
    await b.connect()
    await place(b, "after-restart")


# ---------- 场景 11：持仓与券商不一致 ----------


@pytest.mark.asyncio
async def test_silent_position_change_has_no_event() -> None:
    srv = server(positions={"SPY": (Decimal(10), Decimal(90))})
    b = await client(srv)
    drain(b)
    srv.adjust_position("SPY", Decimal(-4))
    assert drain(b) == []
    (pos,) = await b.positions()
    assert pos.quantity == Decimal(6) and pos.average_cost == Decimal(90)


# ---------- 场景 15：guard 的独立连接 ----------


@pytest.mark.asyncio
async def test_guard_client_cannot_cancel_other_clients_orders_by_default() -> None:
    srv = server(positions={"SPY": (Decimal(10), Decimal(80))})
    trading = await client(srv, 1)
    guard = await client(srv, 9)
    t = await place(trading, "open")
    stop = await place(
        trading,
        "stop",
        side=Side.SELL,
        order_type=OrderType.STOP,
        limit_price=None,
        stop_price=Decimal(90),
        order_class=OrderClass.PROTECTIVE_STOP,
    )
    with pytest.raises(UnknownOrderError, match="不属于本连接"):
        await guard.cancel_order(t.broker_order_id)
    srv.cross_client_cancel = True
    await guard.cancel_order(t.broker_order_id)
    with pytest.raises(ProtectiveStopCancelError):
        await guard.cancel_order(stop.broker_order_id)  # 跨连接也不能不经确认撤止损
    drain(guard)
    assert statuses(drain(trading))[-1] is BrokerOrderState.CANCELLED  # 回报发给下单的连接


@pytest.mark.asyncio
async def test_foreign_events_reach_every_client() -> None:
    srv = server()
    a, g = await client(srv, 1), await client(srv, 9)
    drain(a)
    drain(g)
    srv.inject_foreign_execution("SPY", Side.SELL, Decimal("0.5"), Decimal(100))
    assert [type(e) for e in drain(a)] == [ExecutionReport]
    assert [type(e) for e in drain(g)] == [ExecutionReport]


# ---------- 场景 16：拆股与分红 ----------


@pytest.mark.asyncio
async def test_split_and_dividend() -> None:
    srv = server(settled_cash=Decimal(0), positions={"SPY": (Decimal(10), Decimal(300))})
    b = await client(srv)
    stop = await place(
        b,
        "stop",
        side=Side.SELL,
        order_type=OrderType.STOP,
        limit_price=None,
        stop_price=Decimal(250),
        order_class=OrderClass.PROTECTIVE_STOP,
    )
    srv.apply_split("SPY", Decimal(3), Decimal(1))
    srv.apply_dividend("SPY", Decimal("0.50"))
    (pos,) = await b.positions()
    assert pos.quantity == Decimal(30) and pos.average_cost == Decimal(100)
    assert (await b.cash()).settled == Decimal(15)
    # 券商不替本系统调整止损：数量与价格保持原样，由对账识别公司行动后同步调整
    (order,) = [o for o in await b.open_orders() if o.order_ref == stop.order_ref]
    assert order.total_quantity == Decimal(10) and order.price == Decimal(250)


# ---------- B-01 移交：到期、迟到成交、更正、佣金后到 ----------


@pytest.mark.parametrize(
    ("as_cancelled", "state"),
    [(False, BrokerOrderState.EXPIRED), (True, BrokerOrderState.CANCELLED)],
)
@pytest.mark.asyncio
async def test_day_orders_expire(as_cancelled: bool, state: BrokerOrderState) -> None:
    srv = server()
    b = await client(srv)
    t = await place(b, "day")
    drain(b)
    assert srv.expire_day_orders(as_cancelled=as_cancelled) == [t.broker_order_id]
    (ev,) = drain(b)
    assert isinstance(ev, OrderStatusUpdate) and ev.state is state and ev.reason


@pytest.mark.asyncio
async def test_late_fill_after_cancel() -> None:
    srv = server()
    b = await client(srv)
    t = await place(b, "late")
    await b.cancel_order(t.broker_order_id)
    drain(b)
    ex = srv.late_fill(t.broker_order_id, Decimal(2), Decimal(100))
    (ev,) = drain(b)
    assert isinstance(ev, ExecutionReport) and ev.execution == ex
    assert (await b.positions())[0].quantity == Decimal(2)
    with pytest.raises(ValueError, match="不超过"):
        srv.late_fill(t.broker_order_id, Decimal(9), Decimal(100))
    t2 = await place(b, "live")
    with pytest.raises(ValueError, match="已撤销或已到期"):
        srv.late_fill(t2.broker_order_id, Decimal(1), Decimal(100))


@pytest.mark.asyncio
async def test_execution_correction() -> None:
    srv = server(settled_cash=Decimal(10_000))
    b = await client(srv)
    t = await place(b, "corr")
    ex = srv.fill(t.broker_order_id, Decimal(10), Decimal(100))
    drain(b)
    fixed = srv.correct_execution(ex.execution_id, Decimal(8), Decimal("99.50"))
    (ev,) = drain(b)
    assert isinstance(ev, ExecutionReport) and ev.execution.corrects == ex.execution_id
    assert fixed.execution_id == f"{ex.execution_id}.01"
    (pos,) = await b.positions()
    assert pos.quantity == Decimal(8) and pos.average_cost == Decimal("99.50")
    assert (await b.cash()).settled == Decimal(10_000) - Decimal(8) * Decimal("99.50")
    (order,) = (await b.lookup_order(t.order_ref)).orders
    assert order.filled_quantity == Decimal(8)  # 与成交记录一致
    # 再更正一次：只能更正当前有效的那笔，编号递增，差额相对当前有效值
    with pytest.raises(ValueError, match="当前有效"):
        srv.correct_execution(ex.execution_id, Decimal(9), Decimal(99))
    again = srv.correct_execution(fixed.execution_id, Decimal(9), Decimal(99))
    assert again.execution_id == f"{ex.execution_id}.02" and again.corrects == fixed.execution_id
    assert (await b.positions())[0].quantity == Decimal(9)
    assert (await b.cash()).settled == Decimal(10_000) - Decimal(9) * Decimal(99)
    # 先校验后入账：非法更正不改账本
    with pytest.raises(ValueError, match="不得超过订单数量"):
        srv.correct_execution(again.execution_id, Decimal(11), Decimal(99))
    assert (await b.positions())[0].quantity == Decimal(9)


@pytest.mark.asyncio
async def test_sell_correction_adjusts_unsettled_proceeds() -> None:
    srv = server(settled_cash=Decimal(0), positions={"SPY": (Decimal(10), Decimal(80))})
    b = await client(srv)
    t = await place(b, "sell-corr", side=Side.SELL, order_class=OrderClass.REDUCE)
    ex = srv.fill(t.broker_order_id, Decimal(10), Decimal(100))
    srv.correct_execution(ex.execution_id, Decimal(10), Decimal(101))
    cash = await b.cash()
    assert cash.settled == 0 and cash.unsettled == Decimal(1010)


@pytest.mark.asyncio
async def test_late_commission_for_correction_is_reported() -> None:
    srv = server(settled_cash=Decimal(10_000), commission=FixedCommission())
    srv.commission_late = True
    b = await client(srv)
    t = await place(b, "corr-comm", qty=300, price=Decimal(10))
    ex = srv.fill(t.broker_order_id, Decimal(300), Decimal(10))
    fixed = srv.correct_execution(ex.execution_id, Decimal(200), Decimal(10))
    drain(b)
    srv.report_commissions()
    reports = {e.execution_id: e.commission for e in drain(b) if isinstance(e, CommissionReport)}
    assert reports == {ex.execution_id: Decimal("1.50"), fixed.execution_id: Decimal("1.00")}


@pytest.mark.asyncio
async def test_late_commission_reports() -> None:
    srv = server(settled_cash=Decimal(10_000), commission=FixedCommission())
    srv.commission_late = True
    b = await client(srv)
    t = await place(b, "comm")
    ex = srv.fill(t.broker_order_id, Decimal(10), Decimal(100))
    assert ex.commission is None
    assert (await b.cash()).settled == Decimal(10_000) - 1000 - 1  # 券商侧已扣
    drain(b)
    srv.report_commissions()
    (ev,) = drain(b)
    assert isinstance(ev, CommissionReport) and ev.commission == Decimal("1.00")
    srv.report_commissions()
    assert drain(b) == []  # 只报一次


@pytest.mark.asyncio
async def test_inline_commission() -> None:
    srv = server(settled_cash=Decimal(10_000), commission=FixedCommission())
    b = await client(srv)
    t = await place(b, "comm-inline")
    assert srv.fill(t.broker_order_id, Decimal(10), Decimal(100)).commission == Decimal("1.00")


# ---------- 撮合与现金 ----------


@pytest.mark.asyncio
async def test_match_bar_fills_eligible_orders_only() -> None:
    srv = server(settled_cash=Decimal(10_000), positions={"SPY": (Decimal(20), Decimal(80))})
    b = await client(srv)
    buy = await place(b, "buy", price=Decimal(98))
    stop = await place(
        b,
        "stop",
        side=Side.SELL,
        order_type=OrderType.STOP,
        limit_price=None,
        stop_price=Decimal(96),
        order_class=OrderClass.PROTECTIVE_STOP,
    )
    fills = srv.match_bar(bar("100", "101", "95", "97", T0 + timedelta(hours=1)))
    assert all(f.executed_at >= T0 + timedelta(hours=1) for f in fills)  # 成交不早于 K 线
    by_ref = {f.order_ref: f for f in fills}
    assert by_ref[buy.order_ref].price == Decimal(98)
    assert by_ref[stop.order_ref].price == Decimal(96)
    # K 线开始早于下单时间的订单不参与撮合（防前视）
    late = await place(b, "late", price=Decimal(99))
    assert srv.match_bar(bar("100", "101", "95", "97", T0 - timedelta(hours=1))) == []
    assert (await b.lookup_order(late.order_ref)).orders[0].filled_quantity == 0


@pytest.mark.asyncio
async def test_cash_settlement_cycle() -> None:
    srv = server(settled_cash=Decimal(0), positions={"SPY": (Decimal(10), Decimal(80))})
    b = await client(srv)
    t = await place(b, "sell", side=Side.SELL, order_class=OrderClass.REDUCE)
    srv.fill(t.broker_order_id, Decimal(10), Decimal(100))
    cash = await b.cash()
    assert cash.settled == 0 and cash.unsettled == Decimal(1000)
    settle_on = next_weekday(T0)
    srv.settle(settle_on - timedelta(days=1))
    assert (await b.cash()).unsettled == Decimal(1000)
    srv.settle(settle_on)
    assert (await b.cash()).settled == Decimal(1000)


@pytest.mark.asyncio
async def test_disconnect_twice_emits_one_event() -> None:
    b = await client(server())
    drain(b)
    await b.disconnect()
    await b.disconnect()
    assert len(drain(b)) == 1


def test_client_on_existing_server_rejects_server_parameters() -> None:
    with pytest.raises(ValueError, match="已有 server"):
        FakeBroker(server(), settled_cash=Decimal(1))


def test_next_weekday_skips_weekend() -> None:
    friday = datetime(2026, 10, 23, 20, 0, tzinfo=UTC)
    assert next_weekday(friday) == date(2026, 10, 26)
    assert next_weekday(T0) == date(2026, 10, 23)


@pytest.mark.asyncio
async def test_partially_filled_sell_can_be_amended() -> None:
    srv = server(positions={"SPY": (Decimal(10), Decimal(80))})
    b = await client(srv)
    t = await place(
        b, "sell-amend", side=Side.SELL, order_class=OrderClass.REDUCE, price=Decimal(110)
    )
    srv.fill(t.broker_order_id, Decimal(4), Decimal(110))
    await b.modify_order(make_amendment(t, qty=10, price=Decimal(109)))  # 剩余 6 ≤ 持仓 6
