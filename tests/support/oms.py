"""OMS 测试环境：真实 SQLite 账本 + FakeBroker（集成测试与 O-04 故障注入套件共用）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from server.broker.base import (
    BrokerEvent,
    BrokerExecution,
    BrokerOrderState,
    ExecutionReport,
    OrderStatusUpdate,
)
from server.broker.fake.broker import FakeBroker, FakeBrokerServer
from server.domain.account import Account
from server.domain.enums import (
    AccountType,
    AssetClass,
    OrderClass,
    OrderType,
    RiskVerdict,
    Side,
    TimeInForce,
)
from server.domain.instrument import Instrument
from server.domain.order_state import OrderStatus
from server.domain.orders import OrderIntent, RiskDecision
from server.oms.binding import amendment_digest, intent_digest
from server.oms.manager import DISCREPANCY, OrderManager
from server.storage.ledger import LedgerRepository, open_ledger
from server.storage.migrate import upgrade

T0 = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
ACCOUNT = Account(account_id="DU0000001", account_type=AccountType.CASH, base_currency="USD")
SPY = Instrument(
    symbol="SPY",
    asset_class=AssetClass.ETF,
    exchange="ARCA",
    currency="USD",
    tick_size=Decimal("0.01"),
    trading_calendar="XNYS",
)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Env:
    def __init__(self, tmp_path: Path, **server_kw: Any) -> None:
        path = tmp_path / "ledger.db"
        upgrade(path)
        self.repo = LedgerRepository(open_ledger(path))
        self.clock = Clock()
        server_kw.setdefault("positions", {"SPY": (Decimal(1000), Decimal(100))})
        self.server = FakeBrokerServer(ACCOUNT, clock=self.clock, **server_kw)
        self.broker = FakeBroker(self.server)
        self.unresolved: list[str] = []
        self.om = OrderManager(
            self.repo,
            self.broker,
            instruments={"SPY": SPY},
            clock=self.clock,
            on_unresolved=self.unresolved.append,
        )

    def restart(self) -> None:
        """进程崩溃重启：旧连接断开；同一账本上新建客户端与 OrderManager（内存状态全部丢失）。"""
        self.server.drop_client(self.broker.client_id)
        self.use(FakeBroker(self.server, client_id=self.broker.client_id))

    def use(self, broker: FakeBroker) -> None:
        self.broker = broker
        self.om = OrderManager(
            self.repo,
            broker,
            instruments={"SPY": SPY},
            clock=self.clock,
            on_unresolved=self.unresolved.append,
        )

    def make_intent(self, intent_id: str = "i-1", **kw: Any) -> OrderIntent:
        """构造意图但不落账（风控入口自己落账）。"""
        data: dict[str, Any] = {
            "intent_id": intent_id, "created_at": T0, "strategy_id": "s", "strategy_version": "1",
            "account_id": ACCOUNT.account_id, "symbol": "SPY", "side": Side.BUY,
            "quantity": Decimal(10), "order_type": OrderType.LIMIT,
            "limit_price": Decimal("100.00"), "order_class": OrderClass.OPEN,
            "time_in_force": TimeInForce.DAY,
        }  # fmt: skip
        data.update(kw)
        return OrderIntent.model_validate(data)

    def intent(self, intent_id: str = "i-1", **kw: Any) -> OrderIntent:
        intent = self.make_intent(intent_id, **kw)
        self.repo.add_intent(intent)
        return intent

    def allow(self, intent: OrderIntent, decision_id: str | None = None, **kw: Any) -> RiskDecision:
        """落账一个风控决定；默认绑定这份意图（R-01），改单用 amendment 参数绑定改单内容。"""
        amendment: tuple[str, Decimal, Decimal] | None = kw.pop("amendment", None)
        if amendment is None:
            digest = intent_digest(intent)
        else:
            aid, price, qty = amendment
            digest = amendment_digest(intent, amendment_id=aid, new_price=price, new_quantity=qty)
        data: dict[str, Any] = {
            "decision_id": decision_id or f"d-{intent.intent_id}", "intent_id": intent.intent_id,
            "verdict": RiskVerdict.ALLOW, "rule_set_version": "1", "decided_at": T0,
            "subject_digest": digest,
        }  # fmt: skip
        data.update(kw)
        decision = RiskDecision.model_validate(data)
        self.repo.add_risk_decision(decision)
        return decision

    async def submit(self, intent_id: str = "i-1", **kw: Any) -> tuple[OrderIntent, OrderStatus]:
        intent = self.intent(intent_id, **kw)
        status = await self.om.submit_approved_intent(intent, self.allow(intent))
        return intent, status

    async def pump(self) -> list[BrokerEvent]:
        events: list[BrokerEvent] = []
        while not self.broker._queue.empty():
            event = self.broker._queue.get_nowait()
            events.append(event)
            await self.om.on_broker_event(event)
        return events

    def oid(self, intent_id: str = "i-1") -> str:
        issued = self.repo.issuance_for(intent_id)
        assert issued is not None
        return issued.broker_order.broker_order_id

    def discrepancies(self) -> list[str]:
        return [json.loads(dict(e.detail)["kind"]) for e in reversed(self.repo.audits(DISCREPANCY))]


def status_update(env: Env, state: BrokerOrderState, **kw: Any) -> OrderStatusUpdate:
    data: dict[str, Any] = {
        "broker_order_id": env.oid(), "order_ref": "i-1", "state": state,
        "filled_quantity": Decimal(0), "remaining_quantity": Decimal(10), "at": env.clock(),
    }  # fmt: skip
    data.update(kw)
    return OrderStatusUpdate.model_validate(data)


def execution(env: Env, eid: str, qty: str, **kw: Any) -> ExecutionReport:
    data: dict[str, Any] = {
        "execution_id": eid, "order_ref": "i-1", "broker_order_id": env.oid(), "perm_id": None,
        "account_id": ACCOUNT.account_id, "symbol": "SPY", "side": Side.BUY,
        "quantity": Decimal(qty), "price": Decimal(100), "commission": Decimal(0),
        "executed_at": env.clock(),
    }  # fmt: skip
    data.update(kw)
    return ExecutionReport(execution=BrokerExecution.model_validate(data))
