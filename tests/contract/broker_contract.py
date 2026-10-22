"""BrokerAdapter 契约测试套件（B-01）。

任何适配器都要通过同一套用例：子类提供 `adapter`（未连接的适配器）与 `driver`（市场驱动器）
两个夹具。
为了能原样跑在 IBKR 模拟盘上（B-05）：
- 每次运行的 orderRef 带随机前缀，断言只看本用例自己的订单（模拟盘上本来就有别的挂单）；
- 用例结束时逐单撤掉本用例留下的挂单（不使用全局撤单）；
- 事件完整记录，按订单断言先后顺序（例如先 ACK、先 CANCEL_PENDING 再 CANCELLED）；
- 依赖精确撮合的用例由驱动器的能力开关控制（IBKR 无法精确制造部分成交或无持仓的止损单）。
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

import pytest
from server.broker.base import (
    AccountMismatchError,
    AmendmentRejectedError,
    BrokerAdapter,
    BrokerEvent,
    BrokerOrderState,
    BrokerOrderType,
    BrokerUnavailableError,
    ConnectionEvent,
    ConnectionState,
    DuplicateOrderError,
    ExecutionReport,
    OrderAmendment,
    OrderStatusUpdate,
    OrderTicket,
    ProtectiveStopCancelError,
    UnknownOrderError,
)
from server.domain.enums import OrderClass, OrderType, RiskVerdict, Side, TimeInForce
from server.domain.instrument import Instrument
from server.domain.orders import OrderIntent, RiskDecision, RuleHit

EVENT_TIMEOUT = 5.0
S = BrokerOrderState


class MarketDriver(Protocol):
    """让契约用例在不同适配器上都能制造“挂单”与“成交”。"""

    account_id: str
    supports_partial_fills: bool  # 能否精确制造部分成交
    supports_unbacked_stops: bool  # 能否在无持仓时挂卖出止损单（只用于验证撤单保护）
    supports_foreign_injection: bool  # 能否注入非本系统下的订单与成交（IBKR 上需人工在 TWS 下单）

    def inject_foreign(self, adapter: BrokerAdapter) -> None:
        """注入外来非限价挂单（如 TRAIL）与外来成交（数量可为小数），两者都没有 orderRef。"""

    def instrument(self) -> Instrument: ...

    def resting_price(self) -> Decimal:
        """不会成交的买入限价（远低于市价）。"""

    def marketable_price(self) -> Decimal:
        """会立即成交的买入限价。"""

    async def fill(self, adapter: BrokerAdapter, ticket: OrderTicket, quantity: Decimal) -> None:
        """让该订单成交 quantity（IBKR 驱动器只支持全部成交：用可成交价下单即可）。"""


class EventLog:
    """记录全部事件，支持按条件等待与按订单查看先后顺序。"""

    def __init__(self, stream: AsyncIterator[BrokerEvent]) -> None:
        self._stream = stream
        self.seen: list[BrokerEvent] = []

    async def wait_for(
        self, predicate: Callable[[BrokerEvent], bool], *, timeout: float = EVENT_TIMEOUT
    ) -> BrokerEvent:
        for ev in self.seen:
            if predicate(ev):
                return ev
        async with asyncio.timeout(timeout):
            while True:
                ev = await anext(self._stream)
                self.seen.append(ev)
                if predicate(ev):
                    return ev

    async def status(
        self, oid: str, state: BrokerOrderState, *, remaining: Decimal | None = None
    ) -> OrderStatusUpdate:
        ev = await self.wait_for(
            lambda e: (
                isinstance(e, OrderStatusUpdate)
                and e.broker_order_id == oid
                and e.state is state
                and (remaining is None or e.remaining_quantity == remaining)
            )
        )
        assert isinstance(ev, OrderStatusUpdate)
        return ev

    async def execution(self, order_ref: str) -> ExecutionReport:
        ev = await self.wait_for(
            lambda e: isinstance(e, ExecutionReport) and e.execution.order_ref == order_ref
        )
        assert isinstance(ev, ExecutionReport)
        return ev

    def states(self, oid: str) -> list[BrokerOrderState]:
        return [
            e.state
            for e in self.seen
            if isinstance(e, OrderStatusUpdate) and e.broker_order_id == oid
        ]


def _intent(
    driver: MarketDriver, intent_id: str, qty: int, price: Decimal, **kw: object
) -> OrderIntent:
    data: dict[str, object] = {
        "intent_id": intent_id,
        "created_at": datetime.now(UTC),
        "strategy_id": "contract",
        "strategy_version": "1",
        "account_id": driver.account_id,
        "symbol": driver.instrument().symbol,
        "side": Side.BUY,
        "quantity": Decimal(qty),
        "order_type": OrderType.LIMIT,
        "limit_price": price,
        "order_class": OrderClass.OPEN,
        "time_in_force": TimeInForce.DAY,
    }
    data.update(kw)
    return OrderIntent.model_validate(data)


def _allow(intent: OrderIntent) -> RiskDecision:
    return RiskDecision(
        decision_id=f"risk-{intent.intent_id}",
        intent_id=intent.intent_id,
        verdict=RiskVerdict.ALLOW,
        rule_set_version="contract",
        decided_at=datetime.now(UTC),
    )


def make_ticket(
    driver: MarketDriver,
    oid: str,
    intent_id: str,
    *,
    qty: int = 10,
    price: Decimal | None = None,
    **intent_kw: object,
) -> OrderTicket:
    intent = _intent(driver, intent_id, qty, price or driver.resting_price(), **intent_kw)
    return OrderTicket(
        intent=intent,
        decision=_allow(intent),
        quantity=intent.quantity,
        broker_order_id=oid,
        instrument=driver.instrument(),
    )


def make_amendment(ticket: OrderTicket, *, qty: int, price: Decimal) -> OrderAmendment:
    """改单附带针对新数量的风控决定：等于意图数量为 ALLOW，小于则为 REDUCE。"""
    if qty == ticket.intent.quantity:
        # 改单必须附带新的风控决定（不同的 decision_id），不能复用原票据的决定
        decision = RiskDecision(
            decision_id=f"risk-amend-{uuid.uuid4().hex[:8]}",
            intent_id=ticket.order_ref,
            verdict=RiskVerdict.ALLOW,
            rule_set_version="contract",
            decided_at=datetime.now(UTC),
        )
    else:
        decision = RiskDecision(
            decision_id=f"risk-amend-{ticket.order_ref}",
            intent_id=ticket.order_ref,
            verdict=RiskVerdict.REDUCE,
            approved_quantity=Decimal(qty),
            rule_hits=(RuleHit(rule_id="contract", rule_version="1", message="缩量"),),
            rule_set_version="contract",
            decided_at=datetime.now(UTC),
        )
    return OrderAmendment(
        amendment_id=f"amend-{uuid.uuid4().hex[:8]}",
        original=ticket,
        new_price=price,
        new_quantity=Decimal(qty),
        decision=decision,
    )


class BrokerContract:
    """子类必须提供 adapter 与 driver 夹具。"""

    @pytest.fixture
    def adapter(self) -> BrokerAdapter:  # pragma: no cover - 由子类覆盖
        raise NotImplementedError

    @pytest.fixture
    def driver(self) -> MarketDriver:  # pragma: no cover - 由子类覆盖
        raise NotImplementedError

    @pytest.fixture
    def ref(self) -> Callable[[str], str]:
        run = uuid.uuid4().hex[:8]
        return lambda name: f"ct-{run}-{name}"

    @asynccontextmanager
    async def session(self, adapter: BrokerAdapter, prefix: str = "ct-") -> AsyncIterator[EventLog]:
        await adapter.connect()
        log = EventLog(adapter.events())
        try:
            yield log
        finally:
            if adapter.connected:
                for order in await adapter.open_orders():  # 只清理本套件留下的挂单，逐单撤
                    mine = order.order_ref is not None and order.order_ref.startswith(prefix)
                    if mine and order.order_type is BrokerOrderType.LIMIT:
                        # 收尾撤单时订单可能刚好成交：不得掩盖用例本身的失败
                        with contextlib.suppress(UnknownOrderError):
                            await adapter.cancel_order(order.broker_order_id)
                await adapter.disconnect()

    # ---------- 1. 连接 ----------

    @pytest.mark.asyncio
    async def test_calls_fail_when_disconnected(
        self, adapter: BrokerAdapter, driver: MarketDriver, ref: Callable[[str], str]
    ) -> None:
        assert not adapter.connected
        ticket = make_ticket(driver, "1", ref("disc"))
        calls = [
            adapter.account(),
            adapter.positions(),
            adapter.cash(),
            adapter.open_orders(),
            adapter.completed_orders(),
            adapter.executions(datetime.now(UTC) - timedelta(days=1)),
            adapter.next_order_id(),
            adapter.place_order(ticket),
            adapter.modify_order(make_amendment(ticket, qty=10, price=driver.resting_price())),
            adapter.cancel_order("1"),
            adapter.lookup_order(ref("disc")),
        ]
        for call in calls:
            with pytest.raises(BrokerUnavailableError):
                await call

    @pytest.mark.asyncio
    async def test_connection_events(self, adapter: BrokerAdapter) -> None:
        async with self.session(adapter) as log:
            ev = await log.wait_for(lambda e: isinstance(e, ConnectionEvent))
            assert isinstance(ev, ConnectionEvent) and ev.state is ConnectionState.CONNECTED
            await adapter.disconnect()
            down = await log.wait_for(
                lambda e: isinstance(e, ConnectionEvent) and e.state is ConnectionState.DISCONNECTED
            )
            assert isinstance(down, ConnectionEvent)

    @pytest.mark.asyncio
    async def test_foreign_orders_and_executions_are_reported(
        self, adapter: BrokerAdapter, driver: MarketDriver
    ) -> None:
        # 非本系统下的订单与成交（TWS 手工单、券商强平等）必须如实报出，不得抛异常或被丢弃
        if not driver.supports_foreign_injection:
            pytest.skip("该适配器无法自动注入外来订单与成交")
        since = datetime.now(UTC) - timedelta(minutes=1)
        async with self.session(adapter) as log:
            driver.inject_foreign(adapter)
            foreign = [o for o in await adapter.open_orders() if o.order_ref is None]
            assert foreign and all(o.order_type is not BrokerOrderType.LIMIT for o in foreign)
            # 外来非限价单可能是别处设的保护性止损：不经确认不得撤
            with pytest.raises(ProtectiveStopCancelError):
                await adapter.cancel_order(foreign[0].broker_order_id)
            ev = await log.wait_for(
                lambda e: isinstance(e, ExecutionReport) and e.execution.order_ref is None
            )
            assert isinstance(ev, ExecutionReport)
            assert any(e.order_ref is None for e in await adapter.executions(since))

    @pytest.mark.asyncio
    async def test_snapshots(self, adapter: BrokerAdapter, driver: MarketDriver) -> None:
        async with self.session(adapter):
            assert (await adapter.account()).account_id == driver.account_id
            assert (await adapter.cash()).currency == "USD"
            assert isinstance(await adapter.positions(), list)

    # ---------- 2. 订单号 ----------

    @pytest.mark.asyncio
    async def test_order_ids_increase_across_reconnect(self, adapter: BrokerAdapter) -> None:
        async with self.session(adapter):
            first = [int(await adapter.next_order_id()) for _ in range(3)]
        async with self.session(adapter):
            second = [int(await adapter.next_order_id()) for _ in range(3)]
        ids = first + second
        assert ids == sorted(ids) and len(set(ids)) == 6

    # ---------- 3. 下单、重复、账户 ----------

    @pytest.mark.asyncio
    async def test_place_is_acknowledged_first_with_ref_and_perm_id(
        self, adapter: BrokerAdapter, driver: MarketDriver, ref: Callable[[str], str]
    ) -> None:
        async with self.session(adapter) as log:
            oid = await adapter.next_order_id()
            await adapter.place_order(make_ticket(driver, oid, ref("ack")))
            ack = await log.status(oid, S.ACKNOWLEDGED)
            assert log.states(oid)[0] is S.ACKNOWLEDGED
            assert ack.order_ref == ref("ack") and ack.remaining_quantity == Decimal(10)
            perm = await log.wait_for(
                lambda e: (
                    isinstance(e, OrderStatusUpdate)
                    and e.broker_order_id == oid
                    and e.perm_id is not None
                )
            )
            assert isinstance(perm, OrderStatusUpdate) and perm.perm_id
            mine = [o for o in await adapter.open_orders() if o.order_ref == ref("ack")]
            assert [o.broker_order_id for o in mine] == [oid]
            assert mine[0].symbol == driver.instrument().symbol and mine[0].side is Side.BUY

    @pytest.mark.asyncio
    async def test_duplicate_order_id_is_rejected(
        self, adapter: BrokerAdapter, driver: MarketDriver, ref: Callable[[str], str]
    ) -> None:
        async with self.session(adapter) as log:
            oid = await adapter.next_order_id()
            ticket = make_ticket(driver, oid, ref("dup"))
            await adapter.place_order(ticket)
            await log.status(oid, S.ACKNOWLEDGED)
            with pytest.raises(DuplicateOrderError):
                await adapter.place_order(ticket)
            assert len([o for o in await adapter.open_orders() if o.order_ref == ref("dup")]) == 1

    @pytest.mark.asyncio
    async def test_account_mismatch_is_rejected(
        self, adapter: BrokerAdapter, driver: MarketDriver, ref: Callable[[str], str]
    ) -> None:
        async with self.session(adapter):
            oid = await adapter.next_order_id()
            ticket = make_ticket(driver, oid, ref("acct"), account_id="DU9999999")
            with pytest.raises(AccountMismatchError):
                await adapter.place_order(ticket)

    # ---------- 4. 查单 ----------

    @pytest.mark.asyncio
    async def test_lookup_finds_open_and_completed_orders(
        self, adapter: BrokerAdapter, driver: MarketDriver, ref: Callable[[str], str]
    ) -> None:
        async with self.session(adapter) as log:
            assert (await adapter.lookup_order(ref("never"))).empty  # 暂未查到，不是异常
            oid = await adapter.next_order_id()
            await adapter.place_order(make_ticket(driver, oid, ref("look")))
            await log.status(oid, S.ACKNOWLEDGED)
            found = await adapter.lookup_order(ref("look"))
            assert [o.broker_order_id for o in found.orders] == [oid]
            await adapter.cancel_order(oid)
            await log.status(oid, S.CANCELLED)
            done = await adapter.lookup_order(ref("look"))
            assert [o.state for o in done.orders] == [S.CANCELLED]
            assert oid in {o.broker_order_id for o in await adapter.completed_orders()}

    # ---------- 5. 成交 ----------

    @pytest.mark.asyncio
    async def test_fill_is_reported_and_queryable(
        self, adapter: BrokerAdapter, driver: MarketDriver, ref: Callable[[str], str]
    ) -> None:
        since = datetime.now(UTC) - timedelta(minutes=1)
        async with self.session(adapter) as log:
            oid = await adapter.next_order_id()
            ticket = make_ticket(driver, oid, ref("fill"), price=driver.marketable_price())
            await adapter.place_order(ticket)
            await driver.fill(adapter, ticket, Decimal(10))
            report = await log.execution(ref("fill"))
            assert report.execution.broker_order_id == oid
            assert report.execution.quantity == Decimal(10)
            assert report.execution.price <= ticket.price  # 买入成交价不劣于限价
            filled = await log.status(oid, S.FILLED)
            assert filled.perm_id == report.execution.perm_id  # 状态与成交的 permId 一致
            booked = await adapter.executions(since, order_ref=ref("fill"))
            assert [e.execution_id for e in booked] == [report.execution.execution_id]
            assert not [o for o in await adapter.open_orders() if o.order_ref == ref("fill")]

    @pytest.mark.asyncio
    async def test_partial_fills_conserve_quantity(
        self, adapter: BrokerAdapter, driver: MarketDriver, ref: Callable[[str], str]
    ) -> None:
        if not driver.supports_partial_fills:
            pytest.skip("该适配器的驱动器无法精确制造部分成交")
        async with self.session(adapter) as log:
            oid = await adapter.next_order_id()
            ticket = make_ticket(driver, oid, ref("part"))
            await adapter.place_order(ticket)
            await driver.fill(adapter, ticket, Decimal(3))
            part = await log.status(oid, S.PARTIALLY_FILLED)
            assert (part.filled_quantity, part.remaining_quantity) == (Decimal(3), Decimal(7))
            [rec] = [o for o in await adapter.open_orders() if o.order_ref == ref("part")]
            assert rec.filled_quantity + rec.remaining_quantity == rec.total_quantity
            with pytest.raises(AmendmentRejectedError):  # 改单数量不能不大于已成交量
                await adapter.modify_order(make_amendment(ticket, qty=3, price=ticket.price))
            await driver.fill(adapter, ticket, Decimal(7))
            await log.status(oid, S.FILLED)

    # ---------- 6. 撤单与改单 ----------

    @pytest.mark.asyncio
    async def test_cancel_reports_pending_then_cancelled(
        self, adapter: BrokerAdapter, driver: MarketDriver, ref: Callable[[str], str]
    ) -> None:
        async with self.session(adapter) as log:
            oid = await adapter.next_order_id()
            await adapter.place_order(make_ticket(driver, oid, ref("cxl")))
            await log.status(oid, S.ACKNOWLEDGED)
            await adapter.cancel_order(oid)
            await log.status(oid, S.CANCELLED)
            states = log.states(oid)
            assert states.index(S.CANCEL_PENDING) < states.index(S.CANCELLED)

    @pytest.mark.asyncio
    async def test_cancel_and_modify_unknown_order_raise(
        self, adapter: BrokerAdapter, driver: MarketDriver, ref: Callable[[str], str]
    ) -> None:
        async with self.session(adapter):
            with pytest.raises(UnknownOrderError):
                await adapter.cancel_order("999999")
            ticket = make_ticket(driver, "999999", ref("mod-x"))
            with pytest.raises(UnknownOrderError):
                await adapter.modify_order(
                    make_amendment(ticket, qty=10, price=driver.resting_price())
                )

    @pytest.mark.asyncio
    async def test_modify_changes_only_price_and_quantity(
        self, adapter: BrokerAdapter, driver: MarketDriver, ref: Callable[[str], str]
    ) -> None:
        async with self.session(adapter) as log:
            oid = await adapter.next_order_id()
            ticket = make_ticket(driver, oid, ref("mod"), qty=10)
            await adapter.place_order(ticket)
            await log.status(oid, S.ACKNOWLEDGED, remaining=Decimal(10))
            await adapter.modify_order(make_amendment(ticket, qty=8, price=driver.resting_price()))
            await log.status(oid, S.ACKNOWLEDGED, remaining=Decimal(8))
            [rec] = [o for o in await adapter.open_orders() if o.order_ref == ref("mod")]
            assert rec.total_quantity == Decimal(8) and rec.side is Side.BUY
            assert rec.order_type is BrokerOrderType.LIMIT

    @pytest.mark.asyncio
    async def test_protective_stop_needs_explicit_confirmation(
        self, adapter: BrokerAdapter, driver: MarketDriver, ref: Callable[[str], str]
    ) -> None:
        if not driver.supports_unbacked_stops:
            pytest.skip("该适配器无法在无持仓时挂卖出止损单")
        async with self.session(adapter) as log:
            oid = await adapter.next_order_id()
            stop = make_ticket(
                driver, oid, ref("stop"), price=driver.resting_price(),
                side=Side.SELL, order_type=OrderType.STOP, limit_price=None,
                stop_price=driver.resting_price(), order_class=OrderClass.PROTECTIVE_STOP,
                time_in_force=TimeInForce.GTC,
            )  # fmt: skip
            await adapter.place_order(stop)
            await log.status(oid, S.ACKNOWLEDGED)
            with pytest.raises(ProtectiveStopCancelError):
                await adapter.cancel_order(oid)
            await adapter.cancel_order(oid, allow_protective_stop=True)
            await log.status(oid, S.CANCELLED)

    # ---------- 禁止项 ----------

    def test_has_no_global_cancel(self, adapter: BrokerAdapter) -> None:
        for name in ("reqGlobalCancel", "global_cancel", "cancel_all_orders", "cancel_all"):
            assert not hasattr(adapter, name), name
