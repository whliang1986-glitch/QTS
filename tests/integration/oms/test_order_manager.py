"""O-01：订单管理——真实 SQLite 账本 + FakeBroker，不用 mock。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from server.broker.base import (
    BrokerEvent,
    BrokerExecution,
    BrokerOrderState,
    BrokerUnavailableError,
    ErrorCategory,
    ExecutionReport,
    OrderLookup,
    OrderRecord,
    OrderStatusUpdate,
    OrderTicket,
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
from server.domain.order_state import EventKind, OrderStatus
from server.domain.orders import BrokerOrder, OrderIntent, RiskDecision, RuleHit
from server.oms.manager import (
    DISCREPANCY,
    CancelRefusedError,
    OrderManager,
    SubmissionRefusedError,
)
from server.storage.errors import StorageError
from server.storage.ledger import LedgerRepository, open_ledger
from server.storage.migrate import upgrade

S = OrderStatus
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

    def use(self, broker: FakeBroker) -> None:
        self.broker = broker
        self.om = OrderManager(
            self.repo,
            broker,
            instruments={"SPY": SPY},
            clock=self.clock,
            on_unresolved=self.unresolved.append,
        )

    def intent(self, intent_id: str = "i-1", **kw: Any) -> OrderIntent:
        data: dict[str, Any] = {
            "intent_id": intent_id, "created_at": T0, "strategy_id": "s", "strategy_version": "1",
            "account_id": ACCOUNT.account_id, "symbol": "SPY", "side": Side.BUY,
            "quantity": Decimal(10), "order_type": OrderType.LIMIT,
            "limit_price": Decimal("100.00"), "order_class": OrderClass.OPEN,
            "time_in_force": TimeInForce.DAY,
        }  # fmt: skip
        data.update(kw)
        intent = OrderIntent.model_validate(data)
        self.repo.add_intent(intent)
        return intent

    def allow(self, intent: OrderIntent, decision_id: str | None = None, **kw: Any) -> RiskDecision:
        data: dict[str, Any] = {
            "decision_id": decision_id or f"d-{intent.intent_id}", "intent_id": intent.intent_id,
            "verdict": RiskVerdict.ALLOW, "rule_set_version": "1", "decided_at": T0,
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


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> Env:
    e = Env(tmp_path)
    await e.broker.connect()
    return e


# ---------- 主路径 ----------


@pytest.mark.asyncio
async def test_submit_ack_fill_lifecycle(env: Env) -> None:
    _, status = await env.submit()
    assert status is S.SUBMITTING
    await env.pump()
    assert env.om.status("i-1") is S.SUBMITTED
    assert env.repo.perm_id_for("i-1") is not None  # permId 先到即落账
    env.server.fill(env.oid(), Decimal(4), Decimal("99.99"))
    await env.pump()
    assert env.om.status("i-1") is S.PARTIALLY_FILLED
    env.server.fill(env.oid(), Decimal(6), Decimal("99.98"))
    await env.pump()
    assert env.om.status("i-1") is S.FILLED
    assert env.om.filled_quantity("i-1") == Decimal(10)
    kinds = [e.kind for e, _ in env.repo.order_history("i-1")]
    assert kinds == [
        EventKind.RISK_APPROVED, EventKind.SUBMIT_SENT, EventKind.BROKER_ACK,
        EventKind.PARTIAL_FILL, EventKind.FULL_FILL,
    ]  # fmt: skip
    assert env.discrepancies() == []


@pytest.mark.asyncio
async def test_order_id_is_persisted_before_place_and_never_reused(env: Env) -> None:
    other = env.intent("old")
    env.repo.add_risk_decision(
        RiskDecision(decision_id="d-old", intent_id="old", verdict=RiskVerdict.ALLOW,
                     rule_set_version="1", decided_at=T0)
    )  # fmt: skip
    env.repo.add_broker_order(
        BrokerOrder(intent_id=other.intent_id, broker_order_id="500", order_class=OrderClass.OPEN,
                    submitted_at=T0)
    )  # fmt: skip
    await env.submit()
    assert env.oid() == "501"  # max(券商 nextValidId=1, 账本最大值 + 1)
    assert [o.broker_order_id for o in await env.broker.open_orders()] == ["501"]


@pytest.mark.asyncio
async def test_reduce_decision_sets_ticket_quantity(env: Env) -> None:
    intent = env.intent()
    hit = (RuleHit(rule_id="r", rule_version="1", message="缩量"),)
    decision = env.allow(
        intent, verdict=RiskVerdict.REDUCE, approved_quantity=Decimal(6), rule_hits=hit
    )
    await env.om.submit_approved_intent(intent, decision)
    (order,) = await env.broker.open_orders()
    assert order.total_quantity == Decimal(6)


# ---------- 签发拒绝（失败即关闭） ----------


@pytest.mark.asyncio
async def test_submission_is_refused_without_valid_persisted_decision(env: Env) -> None:
    intent = env.intent()
    unpersisted = RiskDecision(
        decision_id="d-x", intent_id="i-1", verdict=RiskVerdict.ALLOW, rule_set_version="1",
        decided_at=T0,
    )  # fmt: skip
    with pytest.raises(SubmissionRefusedError, match="未落账"):
        await env.om.submit_approved_intent(intent, unpersisted)
    hit = (RuleHit(rule_id="r", rule_version="1", message="超限"),)
    reject = env.allow(intent, "d-rej", verdict=RiskVerdict.REJECT, rule_hits=hit)
    with pytest.raises(SubmissionRefusedError, match="不允许下单"):
        await env.om.submit_approved_intent(intent, reject)
    other = env.intent("i-2")
    with pytest.raises(SubmissionRefusedError, match="不属于"):
        await env.om.submit_approved_intent(other, env.allow(intent, "d-ok"))
    no_spec = env.intent("i-3", symbol="QQQ")
    with pytest.raises(SubmissionRefusedError, match="标的规格"):
        await env.om.submit_approved_intent(no_spec, env.allow(no_spec))
    assert await env.broker.open_orders() == []  # 一张单都没有下出去


@pytest.mark.asyncio
async def test_unpersisted_intent_is_refused(env: Env) -> None:
    intent = env.intent()
    changed = OrderIntent.model_validate({**intent.model_dump(), "quantity": Decimal(11)})
    with pytest.raises(SubmissionRefusedError, match="意图"):
        await env.om.submit_approved_intent(changed, env.allow(intent))


@pytest.mark.asyncio
async def test_resubmission_never_places_a_second_order(env: Env) -> None:
    intent, _ = await env.submit()
    decision = env.repo.risk_decisions_for("i-1")[0]
    assert await env.om.submit_approved_intent(intent, decision) is S.SUBMITTING
    other_decision = env.allow(intent, "d-2")
    with pytest.raises(SubmissionRefusedError, match="别的风控决定"):
        await env.om.submit_approved_intent(intent, other_decision)
    assert len(await env.broker.open_orders()) == 1


# ---------- UNKNOWN：超时、下单异常、查证 ----------


@pytest.mark.asyncio
async def test_lost_ack_times_out_into_unknown_and_lookup_resolves(env: Env) -> None:
    env.server.lose_next_ack()
    await env.submit()
    env.clock.advance(5)
    assert env.om.check_timeouts() == []
    env.clock.advance(6)
    assert env.om.check_timeouts() == ["i-1"]
    assert env.om.status("i-1") is S.UNKNOWN
    assert await env.om.resolve_unknown("i-1") is S.SUBMITTED
    kinds = [e.kind for e, _ in env.repo.order_history("i-1")][-2:]
    assert kinds == [EventKind.SUBMIT_TIMEOUT, EventKind.BROKER_STATUS_OBSERVED]
    assert len(await env.broker.open_orders()) == 1  # 没有重发


@pytest.mark.asyncio
async def test_not_found_stays_unknown_never_rejected_or_resent(env: Env) -> None:
    env.server.lose_next_request()  # 券商根本没收到
    await env.submit()
    env.clock.advance(11)
    env.om.check_timeouts()
    for _ in range(3):
        assert await env.om.resolve_unknown("i-1") is S.UNKNOWN
    assert env.unresolved == ["i-1"]  # 多轮查不到通知调用方进入 SAFE HALT
    assert await env.broker.open_orders() == []  # 从不重发
    assert S.REJECTED not in [s for _, s in env.repo.order_history("i-1")]
    assert env.discrepancies().count("unknown_unresolved") == 3


class TimeoutBroker(FakeBroker):
    """下单请求发出后连接超时：券商收没收到不得而知。"""

    async def place_order(self, ticket: OrderTicket) -> None:
        raise TimeoutError


@pytest.mark.asyncio
async def test_place_order_exception_goes_unknown_without_resend(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.use(TimeoutBroker(env.server))
    await env.broker.connect()
    _, status = await env.submit()
    assert status is S.UNKNOWN
    assert "submit_raised" in env.discrepancies()
    assert await env.om.resolve_unknown("i-1") is S.UNKNOWN  # 券商没有：仍不确定，不重下
    assert await env.broker.open_orders() == []


@pytest.mark.asyncio
async def test_broker_unavailable_before_issuance_places_nothing(env: Env) -> None:
    await env.broker.disconnect()
    with pytest.raises(BrokerUnavailableError):
        await env.submit()
    assert env.repo.issuance_for("i-1") is None  # 没有签发，也就没有下单


@pytest.mark.asyncio
async def test_late_timeout_after_ack_is_ignored(env: Env) -> None:
    await env.submit()
    await env.pump()
    env.clock.advance(60)
    assert env.om.check_timeouts() == []
    assert env.om.status("i-1") is S.SUBMITTED


@pytest.mark.asyncio
async def test_fill_during_unknown_is_booked_then_lookup_decides(env: Env) -> None:
    env.server.lose_next_ack()
    await env.submit()
    env.clock.advance(11)
    env.om.check_timeouts()
    env.server.fill(env.oid(), Decimal(10), Decimal(100))
    await env.pump()
    assert env.om.filled_quantity("i-1") == Decimal(10)
    assert env.om.status("i-1") is S.FILLED
    observed = [e.observed for e, _ in env.repo.order_history("i-1") if e.observed is not None]
    assert observed == [S.FILLED]  # 经查证转出，而不是由成交直接改状态


# ---------- 回报：乱序、重复、终态后、外来 ----------


@pytest.mark.asyncio
async def test_fill_before_ack(env: Env) -> None:
    env.server.hold_events()
    await env.submit()
    env.server.fill(env.oid(), Decimal(10), Decimal(100))
    env.server.release_events(reverse=True)
    await env.pump()
    assert env.om.status("i-1") is S.FILLED
    assert env.discrepancies() == []


@pytest.mark.asyncio
async def test_duplicate_reports_are_booked_once(env: Env) -> None:
    await env.submit()
    ex = env.server.fill(env.oid(), Decimal(4), Decimal(100))
    env.server.duplicate_execution(ex.execution_id)
    await env.pump()
    assert env.om.filled_quantity("i-1") == Decimal(4)
    statuses = [s for _, s in env.repo.order_history("i-1")]
    assert statuses.count(S.PARTIALLY_FILLED) == 1


@pytest.mark.asyncio
async def test_fill_after_cancel_is_booked_with_discrepancy(env: Env) -> None:
    await env.submit()
    await env.pump()
    await env.om.request_cancel("i-1")
    await env.pump()
    assert env.om.status("i-1") is S.CANCELLED
    env.server.late_fill(env.oid(), Decimal(2), Decimal(100))
    await env.pump()
    assert env.om.filled_quantity("i-1") == Decimal(2)
    assert env.om.status("i-1") is S.CANCELLED
    assert "fill_after_terminal" in env.discrepancies()


@pytest.mark.asyncio
async def test_foreign_and_corrected_executions_are_kept_unmatched(env: Env) -> None:
    await env.submit()
    await env.pump()
    env.server.inject_foreign_execution("SPY", Side.SELL, Decimal("0.5"), Decimal(100))
    ex = env.server.fill(env.oid(), Decimal(4), Decimal(100))
    env.server.correct_execution(ex.execution_id, Decimal(3), Decimal(100))
    await env.pump()
    reasons = sorted(r for _, r, _ in env.repo.unmatched_executions())
    assert reasons == ["correction", "no_order"]
    assert env.om.filled_quantity("i-1") == Decimal(4)  # 更正先不计入（O-02 冲正）


@pytest.mark.asyncio
async def test_broker_rejection(env: Env) -> None:
    env.server.reject_next(ErrorCategory.PRICE_REJECTED, "价格偏离")
    await env.submit()
    await env.pump()
    assert env.om.status("i-1") is S.REJECTED


@pytest.mark.asyncio
async def test_day_order_expiry(env: Env) -> None:
    await env.submit()
    await env.pump()
    env.server.expire_day_orders()
    await env.pump()
    assert env.om.status("i-1") is S.EXPIRED


@pytest.mark.asyncio
async def test_illegal_transition_is_refused_and_audited(env: Env) -> None:
    await env.submit()
    await env.pump()
    env.server.fill(env.oid(), Decimal(4), Decimal(100))
    await env.pump()
    # 部分成交后券商报 REJECTED：转移表不允许，拒绝并记差异，状态不变
    bogus = OrderStatusUpdate(
        broker_order_id=env.oid(), order_ref="i-1", state=BrokerOrderState.REJECTED,
        filled_quantity=Decimal(4), remaining_quantity=Decimal(0), at=env.clock(),
    )  # fmt: skip
    await env.om.on_broker_event(bogus)
    assert env.om.status("i-1") is S.PARTIALLY_FILLED
    assert "illegal_transition" in env.discrepancies()


# ---------- 撤单 ----------


@pytest.mark.asyncio
async def test_cancel_with_partial_fill_during_pending_and_rejection(env: Env) -> None:
    env.server.manual_cancel_confirm = True
    await env.submit()
    await env.pump()
    await env.om.request_cancel("i-1")
    await env.pump()
    assert env.om.status("i-1") is S.CANCEL_PENDING
    env.server.fill(env.oid(), Decimal(3), Decimal(100))
    await env.pump()
    assert env.om.status("i-1") is S.CANCEL_PENDING  # 撤单中的部分成交
    env.server.reject_cancel(env.oid(), "交易所拒绝")
    await env.pump()
    assert env.om.status("i-1") is S.PARTIALLY_FILLED  # has_fills 由累计成交得出
    await env.om.request_cancel("i-1")
    env.server.confirm_cancel(env.oid())
    await env.pump()
    assert env.om.status("i-1") is S.CANCELLED


@pytest.mark.asyncio
async def test_cancel_while_submitting_waits_for_ack(env: Env) -> None:
    env.server.hold_events()
    await env.submit()
    assert await env.om.request_cancel("i-1") is S.SUBMITTING  # 挂起，不直接撤
    env.server.release_events()
    await env.pump()  # ACK 到达后补发撤单
    await env.pump()
    assert env.om.status("i-1") is S.CANCELLED


@pytest.mark.asyncio
async def test_protective_stop_cannot_be_cancelled_here(env: Env) -> None:
    intent = env.intent(
        "stop", side=Side.SELL, order_type=OrderType.STOP, limit_price=None,
        stop_price=Decimal(90), order_class=OrderClass.PROTECTIVE_STOP,
        time_in_force=TimeInForce.GTC,
    )  # fmt: skip
    await env.om.submit_approved_intent(intent, env.allow(intent))
    await env.pump()
    with pytest.raises(CancelRefusedError, match="保护性止损"):
        await env.om.request_cancel("stop")
    assert env.om.status("stop") is S.SUBMITTED


# ---------- 第一轮评审补充 ----------


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


@pytest.mark.asyncio
async def test_reports_without_order_ref_or_with_other_perm_id_are_not_claimed(env: Env) -> None:
    await env.submit()
    await env.pump()
    # 别的 clientId / TWS 手工单撞了订单号、没有 orderRef：状态与成交都不得记到本系统头上
    await env.om.on_broker_event(status_update(env, BrokerOrderState.CANCELLED, order_ref=None))
    await env.om.on_broker_event(execution(env, "x-1", "10", order_ref=None))
    perm = env.repo.perm_id_for("i-1")
    await env.om.on_broker_event(
        status_update(env, BrokerOrderState.EXPIRED, perm_id=f"{perm}-other")
    )
    await env.om.on_broker_event(execution(env, "x-2", "10", perm_id=f"{perm}-other"))
    assert env.om.status("i-1") is S.SUBMITTED
    assert env.om.filled_quantity("i-1") == 0
    assert sorted(e for e, _, _ in env.repo.unmatched_executions()) == ["x-1", "x-2"]
    assert env.discrepancies().count("unmatched_status") == 2


@pytest.mark.asyncio
async def test_existing_issuance_never_places_again(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env.repo, "commit_submission", lambda *a, **k: False)
    _, status = await env.submit()
    assert status is S.RISK_APPROVED
    assert await env.broker.open_orders() == []


@pytest.mark.asyncio
async def test_ledger_failure_before_place_places_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_: object, **__: object) -> bool:
        raise StorageError("disk full")

    monkeypatch.setattr(env.repo, "commit_submission", fail)
    with pytest.raises(StorageError):
        await env.submit()
    assert await env.broker.open_orders() == []


@pytest.mark.asyncio
async def test_concurrent_double_submit_places_one_order(env: Env) -> None:
    intent = env.intent()
    decision = env.allow(intent)
    results = await asyncio.gather(
        env.om.submit_approved_intent(intent, decision),
        env.om.submit_approved_intent(intent, decision),
        return_exceptions=True,
    )
    assert len(await env.broker.open_orders()) == 1
    # 第二次调用要么看到已签发记录直接返回，要么被唯一约束或转移表挡住；都不会第二次下单
    allowed = {"OrderStatus", "ConflictError", "IllegalTransitionError"}
    assert {type(r).__name__ for r in results} <= allowed
    assert env.repo.submitted_intents() == ["i-1"]


@pytest.mark.asyncio
async def test_perm_id_owned_by_another_intent_is_not_claimed(env: Env) -> None:
    env.server.lose_next_ack()  # i-1 还没有登记 permId
    await env.submit()
    await env.submit("i-2")
    await env.pump()
    taken = env.repo.perm_id_for("i-2")
    assert env.repo.perm_id_for("i-1") is None and taken is not None
    # 认领条件都满足，但带的 permId 已属于 i-2：不得计入 i-1
    await env.om.on_broker_event(execution(env, "x-perm", "10", perm_id=taken))
    assert env.om.filled_quantity("i-1") == 0
    assert [r for e, r, _ in env.repo.unmatched_executions() if e == "x-perm"] == ["perm_conflict"]


class StrayBroker(FakeBroker):
    """查单时同一 orderRef 下多出一张别的订单（疑似重复下单或冒用）。"""

    async def lookup_order(self, order_ref: str) -> OrderLookup:
        found = await super().lookup_order(order_ref)
        if not found.orders:
            return found
        stray = OrderRecord.model_validate(
            {**found.orders[0].model_dump(), "broker_order_id": "999"}
        )
        return OrderLookup(order_ref=order_ref, orders=(*found.orders, stray), executions=())


@pytest.mark.asyncio
async def test_stray_order_under_same_ref_escalates_and_stays_unknown(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.use(StrayBroker(env.server))
    await env.broker.connect()
    env.server.lose_next_ack()
    await env.submit()
    env.clock.advance(11)
    env.om.check_timeouts()
    assert await env.om.resolve_unknown("i-1") is S.UNKNOWN
    assert env.unresolved == ["i-1"] and "lookup_foreign_order_ids" in env.discrepancies()


@pytest.mark.asyncio
async def test_conflicting_terminal_status_is_audited(env: Env) -> None:
    await env.submit()
    await env.pump()
    env.server.fill(env.oid(), Decimal(10), Decimal(100))
    await env.pump()
    await env.om.on_broker_event(status_update(env, BrokerOrderState.CANCELLED))
    assert env.om.status("i-1") is S.FILLED
    assert "status_after_terminal" in env.discrepancies()


@pytest.mark.asyncio
async def test_pending_cancel_is_sent_after_direct_resolution(env: Env) -> None:
    env.server.lose_next_ack()
    await env.submit()
    await env.om.request_cancel("i-1")
    env.clock.advance(11)
    env.om.check_timeouts()
    assert await env.om.resolve_unknown("i-1") is S.CANCEL_PENDING  # 查证转出后补发了撤单
    await env.pump()
    assert env.om.status("i-1") is S.CANCELLED


@pytest.mark.asyncio
async def test_last_execution_after_broker_says_filled(env: Env) -> None:
    await env.submit()
    await env.pump()
    await env.om.on_broker_event(execution(env, "e-1", "6"))
    await env.om.on_broker_event(status_update(env, BrokerOrderState.FILLED))
    assert env.om.status("i-1") is S.PARTIALLY_FILLED  # 成交数量只看已入账的成交
    await env.om.on_broker_event(execution(env, "e-2", "4"))
    assert env.om.status("i-1") is S.FILLED


@pytest.mark.parametrize(
    ("kw", "reason"),
    [({"symbol": "QQQ"}, "fact_mismatch"), ({"side": Side.SELL}, "fact_mismatch"),
     ({"quantity": Decimal("0.5")}, "fractional")],
)  # fmt: skip
@pytest.mark.asyncio
async def test_own_executions_with_wrong_facts_are_unmatched(
    env: Env, kw: dict[str, Any], reason: str
) -> None:
    await env.submit()
    await env.pump()
    await env.om.on_broker_event(execution(env, "bad", "1", **kw))
    assert [r for _, r, _ in env.repo.unmatched_executions()] == [reason]
    assert env.om.filled_quantity("i-1") == 0


@pytest.mark.asyncio
async def test_perm_id_is_recorded_even_after_terminal(env: Env) -> None:
    env.server.lose_next_ack()
    await env.submit()
    await env.om.on_broker_event(execution(env, "e-1", "10"))
    assert env.om.status("i-1") is S.FILLED and env.repo.perm_id_for("i-1") is None
    await env.om.on_broker_event(status_update(env, BrokerOrderState.FILLED, perm_id="p-42"))
    assert env.repo.perm_id_for("i-1") == "p-42"


class CancelFailBroker(FakeBroker):
    async def cancel_order(
        self, broker_order_id: str, *, allow_protective_stop: bool = False
    ) -> None:
        raise RuntimeError("socket closed")


@pytest.mark.asyncio
async def test_cancel_failures_keep_cancel_pending(tmp_path: Path, env: Env) -> None:
    await env.submit()
    await env.pump()
    env.server.broker_side_cancel(env.oid())  # 券商侧已撤，本地还不知道
    assert await env.om.request_cancel("i-1") is S.CANCEL_PENDING
    assert "cancel_unknown_order" in env.discrepancies()
    other = Env(tmp_path / "b")
    other.use(CancelFailBroker(other.server))
    await other.broker.connect()
    await other.submit()
    await other.pump()
    assert await other.om.request_cancel("i-1") is S.CANCEL_PENDING
    assert "cancel_raised" in other.discrepancies()


@pytest.mark.asyncio
async def test_cancel_before_submit_is_recorded(env: Env) -> None:
    env.intent()
    assert await env.om.request_cancel("i-1") is S.CREATED
    assert "cancel_before_submit" in env.discrepancies()
