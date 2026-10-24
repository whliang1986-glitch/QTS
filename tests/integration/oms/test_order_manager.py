"""O-01：订单管理——真实 SQLite 账本 + FakeBroker，不用 mock。"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from server.broker.base import (
    AmendmentRejectedError,
    BrokerOrderState,
    BrokerUnavailableError,
    ErrorCategory,
    OrderAmendment,
    OrderLookup,
    OrderRecord,
    OrderStatusUpdate,
    OrderTicket,
)
from server.broker.fake.broker import FakeBroker
from server.domain.enums import (
    OrderClass,
    OrderType,
    RiskVerdict,
    Side,
    TimeInForce,
)
from server.domain.order_state import EventKind, OrderStatus
from server.domain.orders import BrokerOrder, OrderIntent, RiskDecision, RuleHit
from server.oms.manager import (
    DISCREPANCY,
    AmendRefusedError,
    CancelRefusedError,
    OrderManager,
    SubmissionRefusedError,
    canonical,
)
from server.storage.errors import StorageError
from server.storage.ledger import LedgerRepository
from tests.support.oms import SPY, T0, Env, execution, status_update

S = OrderStatus


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
async def test_foreign_execution_is_unmatched_and_correction_supersedes(env: Env) -> None:
    await env.submit()
    await env.pump()
    env.server.inject_foreign_execution("SPY", Side.SELL, Decimal("0.5"), Decimal(100))
    ex = env.server.fill(env.oid(), Decimal(4), Decimal(100))
    env.server.correct_execution(ex.execution_id, Decimal(3), Decimal(100))
    await env.pump()
    reasons = sorted(r for _, r, _ in env.repo.unmatched_executions())
    assert reasons == ["no_order"]
    assert env.om.filled_quantity("i-1") == Decimal(3)  # 更正取代原成交（O-02）


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


# ---------- O-02：成交更正、数值规范化、重复提交与重复回报 ----------


@pytest.mark.asyncio
async def test_correction_after_filled_reduces_quantity_and_flags_missing_fills(env: Env) -> None:
    await env.submit()
    await env.pump()
    ex = env.server.fill(env.oid(), Decimal(10), Decimal(100))
    await env.pump()
    assert env.om.status("i-1") is S.FILLED
    env.server.correct_execution(ex.execution_id, Decimal(8), Decimal(100))
    await env.pump()
    assert env.om.filled_quantity("i-1") == Decimal(8)
    assert env.om.status("i-1") is S.FILLED  # 终态不变，差异交对账
    assert {"execution_corrected", "fills_missing"} <= set(env.discrepancies())


@pytest.mark.asyncio
async def test_correction_chain_and_replays(env: Env) -> None:
    await env.submit()
    await env.pump()
    ex = env.server.fill(env.oid(), Decimal(4), Decimal(100))
    first = env.server.correct_execution(ex.execution_id, Decimal(3), Decimal(100))
    second = env.server.correct_execution(first.execution_id, Decimal(5), Decimal(100))
    env.server.duplicate_execution(second.execution_id)  # 更正重放
    await env.pump()
    assert env.om.filled_quantity("i-1") == Decimal(5)
    assert env.repo.superseded_executions("i-1") == {ex.execution_id, first.execution_id}
    # 重放不写任何新记录：不进未匹配表，不记“更正已取代成交”的差异
    assert env.repo.unmatched_executions() == []
    assert "correction_of_superseded" not in env.discrepancies()
    audits_before = len(env.repo.audits(DISCREPANCY))
    for fid in (ex.execution_id, first.execution_id, second.execution_id):
        env.server.duplicate_execution(fid)
    await env.pump()
    assert len(env.repo.audits(DISCREPANCY)) == audits_before
    # 再拿已被取代的那笔做更正：不计入，照实进未匹配表
    await env.om.on_broker_event(execution(env, "late-fix", "1", corrects=ex.execution_id))
    assert env.om.filled_quantity("i-1") == Decimal(5)
    assert [r for e, r, _ in env.repo.unmatched_executions() if e == "late-fix"] == [
        "correction_of_superseded"
    ]


@pytest.mark.asyncio
async def test_correction_before_its_target_is_kept_unmatched(env: Env) -> None:
    await env.submit()
    await env.pump()
    await env.om.on_broker_event(execution(env, "fix", "3", corrects="not-yet"))
    assert env.om.filled_quantity("i-1") == 0
    assert "correction_unknown_target" in env.discrepancies()


@pytest.mark.asyncio
async def test_replay_with_different_decimal_representation_is_not_a_conflict(env: Env) -> None:
    await env.submit()
    await env.pump()
    await env.om.on_broker_event(execution(env, "e-1", "4", price=Decimal("99.50")))
    await env.om.on_broker_event(execution(env, "e-1", "4.0", price=Decimal("99.500")))
    assert env.om.filled_quantity("i-1") == Decimal(4)
    assert "execution_conflict" not in env.discrepancies()


@pytest.mark.asyncio
async def test_resubmission_after_restart_never_places_again(env: Env) -> None:
    intent, _ = await env.submit()
    decision = env.repo.risk_decisions_for("i-1")[0]
    restarted = OrderManager(
        env.repo, env.broker, instruments={"SPY": SPY}, clock=env.clock, on_unresolved=print
    )
    for _ in range(3):
        await restarted.submit_approved_intent(intent, decision)
    assert len(await env.broker.open_orders()) == 1


@pytest.mark.asyncio
async def test_repeated_status_reports_write_no_duplicate_events(env: Env) -> None:
    await env.submit()
    await env.pump()
    for _ in range(5):
        await env.om.on_broker_event(status_update(env, BrokerOrderState.ACKNOWLEDGED))
    kinds = [e.kind for e, _ in env.repo.order_history("i-1")]
    assert kinds.count(EventKind.BROKER_ACK) == 1


FILL_PLANS = st.lists(st.integers(min_value=1, max_value=4), min_size=1, max_size=6).filter(
    lambda xs: sum(xs) <= 10
)


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    plan=FILL_PLANS,
    duplicates=st.lists(st.integers(min_value=0, max_value=5), max_size=6),
    seed=st.randoms(use_true_random=False),
)
def test_duplicated_and_shuffled_reports_book_each_fill_once(
    tmp_path_factory: pytest.TempPathFactory,
    plan: list[int],
    duplicates: list[int],
    seed: Any,
) -> None:
    async def run() -> None:
        env = Env(tmp_path_factory.mktemp("prop"))
        await env.broker.connect()
        await env.submit()
        await env.pump()
        reports = [execution(env, f"e-{i}", str(q)) for i, q in enumerate(plan)]
        stream = reports + [reports[i % len(reports)] for i in duplicates]
        seed.shuffle(stream)
        for report in stream:
            await env.om.on_broker_event(report)
        assert env.om.filled_quantity("i-1") == Decimal(sum(plan))
        expected = S.FILLED if sum(plan) == 10 else S.PARTIALLY_FILLED
        assert env.om.status("i-1") is expected
        assert len(env.repo.executions_for("i-1")) == len(plan)

    asyncio.run(run())


# ---------- O-02 第一轮评审补充 ----------


@pytest.mark.asyncio
async def test_correction_arriving_first_is_paired_when_target_arrives(env: Env) -> None:
    await env.submit()
    await env.pump()
    # 更正链整体先到，原成交最后到
    await env.om.on_broker_event(execution(env, "e-1.02", "6", corrects="e-1.01"))
    await env.om.on_broker_event(execution(env, "e-1.01", "5", corrects="e-1"))
    assert env.om.filled_quantity("i-1") == 0
    (_, _, payload), *_ = env.repo.unmatched_executions()
    assert json.loads(payload)["corrects"]  # 存的是券商原文（含 corrects），便于对账配对
    await env.om.on_broker_event(execution(env, "e-1", "4"))
    assert env.om.filled_quantity("i-1") == Decimal(6)  # 逐级配对，只剩最后一笔有效
    assert env.repo.superseded_executions("i-1") == {"e-1", "e-1.01"}
    assert env.om.status("i-1") is S.PARTIALLY_FILLED


@pytest.mark.asyncio
async def test_lookup_books_plain_executions_before_corrections(env: Env) -> None:
    env.server.lose_next_ack()
    await env.submit()
    env.clock.advance(11)
    env.om.check_timeouts()
    ex = env.server.fill(env.oid(), Decimal(10), Decimal(100))
    env.server.correct_execution(ex.execution_id, Decimal(9), Decimal(100))
    env.broker._queue = type(env.broker._queue)()  # 丢掉推送，只靠查单
    assert await env.om.resolve_unknown("i-1") is S.FILLED  # 券商仍报 FILLED
    assert env.om.filled_quantity("i-1") == Decimal(9)  # 先入账原成交、再入账更正
    assert "fills_missing" in env.discrepancies()
    assert env.repo.unmatched_executions() == []


@pytest.mark.asyncio
async def test_correction_overfilling_a_filled_order_is_flagged(env: Env) -> None:
    await env.submit()
    await env.pump()
    ex = env.server.fill(env.oid(), Decimal(10), Decimal(100))
    await env.pump()
    await env.om.on_broker_event(execution(env, "fix", "12", corrects=ex.execution_id))
    assert env.om.filled_quantity("i-1") == Decimal(12)
    assert "overfill" in env.discrepancies()


def test_canonical_is_exact_and_context_independent() -> None:
    assert canonical(Decimal("500.050")) == Decimal("500.05")
    assert str(canonical(Decimal("100.00"))) == "100"
    assert str(canonical(Decimal("-0.00"))) == "0"
    big = Decimal("123456789012345.1234567891")
    with localcontext() as ctx:
        ctx.prec = 6  # 调用方把精度调低也不影响
        assert canonical(big) == big


CHAINS = st.lists(
    st.lists(st.integers(min_value=1, max_value=3), min_size=1, max_size=3),
    min_size=1,
    max_size=3,
)


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(chains=CHAINS, duplicates=st.lists(st.integers(0, 20), max_size=6), seed=st.randoms())
def test_corrections_duplicated_and_shuffled_end_with_final_values(
    tmp_path_factory: pytest.TempPathFactory,
    chains: list[list[int]],
    duplicates: list[int],
    seed: Any,
) -> None:
    """每笔成交带一条更正链（第一个值是原成交，之后是逐次更正）；随机重复、打乱后有效量等于各链最后的值。"""

    async def run() -> None:
        env = Env(tmp_path_factory.mktemp("prop-fix"))
        await env.broker.connect()
        await env.submit()
        await env.pump()
        reports = []
        for i, chain in enumerate(chains):
            ids = [f"e-{i}"] + [f"e-{i}.{k:02d}" for k in range(1, len(chain))]
            for k, qty in enumerate(chain):
                corrects = ids[k - 1] if k else None
                reports.append(execution(env, ids[k], str(qty), corrects=corrects))
        stream = reports + [reports[d % len(reports)] for d in duplicates]
        seed.shuffle(stream)
        for report in stream:
            await env.om.on_broker_event(report)
        final = sum(chain[-1] for chain in chains)
        assert env.om.filled_quantity("i-1") == Decimal(final)
        assert "correction_of_superseded" not in env.discrepancies()
        # 未匹配表里只可能有等待过配对的更正，且最后都已配对入账
        assert all(env.repo.has_execution(e) for e, _, _ in env.repo.unmatched_executions())

    asyncio.run(run())


# ---------- O-02 第二轮评审补充 ----------


@pytest.mark.asyncio
async def test_correction_replay_with_changed_content_is_flagged(env: Env) -> None:
    await env.submit()
    await env.pump()
    await env.om.on_broker_event(execution(env, "e-1", "4"))
    await env.om.on_broker_event(execution(env, "e-1.01", "3", corrects="e-1"))
    await env.om.on_broker_event(execution(env, "e-1.01", "2", corrects="e-1"))  # 内容变了
    assert env.om.filled_quantity("i-1") == Decimal(3)  # 保留第一次的事实
    assert "correction_conflict" in env.discrepancies()


@pytest.mark.asyncio
async def test_two_waiting_corrections_for_one_execution_are_flagged(env: Env) -> None:
    await env.submit()
    await env.pump()
    await env.om.on_broker_event(execution(env, "fix-a", "3", corrects="e-1"))
    await env.om.on_broker_event(execution(env, "fix-b", "2", corrects="e-1"))
    await env.om.on_broker_event(execution(env, "e-1", "4"))
    assert env.om.filled_quantity("i-1") in {Decimal(3), Decimal(2)}  # 只有一笔取代成功
    assert "correction_conflict" in env.discrepancies()


# ---------- O-03：改单与撤单竞态 ----------


async def amend(
    env: Env, amendment_id: str, price: str, qty: int, *, decision_id: str | None = None
) -> OrderStatus:
    intent = env.repo.get_intent("i-1")
    assert intent is not None
    did = decision_id or f"d-{amendment_id}"
    existing = [d for d in env.repo.risk_decisions_for("i-1") if d.decision_id == did]
    # 改单的决定必须晚于本订单已用过的全部决定：每个新决定往后推一秒
    later = T0 + timedelta(seconds=len(env.repo.risk_decisions_for("i-1")))
    bound = (amendment_id, Decimal(price), Decimal(qty))  # R-01：决定绑定这次改单的价格与数量
    if existing:
        decision = existing[0]
    elif qty == intent.quantity:
        decision = env.allow(intent, did, decided_at=later, amendment=bound)
    else:
        hit = (RuleHit(rule_id="r", rule_version="1", message="缩量"),)
        decision = env.allow(
            intent, did, verdict=RiskVerdict.REDUCE, approved_quantity=Decimal(qty),
            rule_hits=hit, decided_at=later, amendment=bound,
        )  # fmt: skip
    return await env.om.request_amend(
        "i-1", amendment_id=amendment_id, new_price=Decimal(price), new_quantity=Decimal(qty),
        decision=decision,
    )  # fmt: skip


@pytest.mark.asyncio
async def test_amend_price_and_quantity_with_new_decision(env: Env) -> None:
    await env.submit()
    await env.pump()
    assert await amend(env, "am-1", "99.50", 6) is S.SUBMITTED
    (order,) = await env.broker.open_orders()
    assert (order.price, order.total_quantity) == (Decimal("99.50"), Decimal(6))
    env.server.fill(env.oid(), Decimal(6), Decimal("99.50"))
    await env.pump()
    assert env.om.status("i-1") is S.FILLED  # 按改单后的数量判定全部成交
    assert [a.result for a in env.repo.amendments_for("i-1")] == ["ACCEPTED"]


@pytest.mark.asyncio
async def test_amend_is_idempotent_and_needs_a_fresh_decision(env: Env) -> None:
    await env.submit()
    await env.pump()
    await amend(env, "am-1", "99.50", 10)
    await amend(env, "am-1", "99.50", 10)  # 同一 amendment_id 不再调用券商
    assert len(env.repo.amendments_for("i-1")) == 1
    with pytest.raises(AmendRefusedError, match="用过"):  # 原签发的决定
        await env.om.request_amend(
            "i-1", amendment_id="am-2", new_price=Decimal(99), new_quantity=Decimal(10),
            decision=env.repo.risk_decisions_for("i-1")[0],
        )  # fmt: skip
    used = next(d for d in env.repo.risk_decisions_for("i-1") if d.decision_id == "d-am-1")
    with pytest.raises(AmendRefusedError, match="用过"):  # 上一次改单的决定
        await env.om.request_amend(
            "i-1", amendment_id="am-3", new_price=Decimal(99), new_quantity=Decimal(10),
            decision=used,
        )  # fmt: skip
    hit = (RuleHit(rule_id="r", rule_version="1", message="x"),)
    unpersisted = RiskDecision(
        decision_id="d-x", intent_id="i-1", verdict=RiskVerdict.REDUCE,
        approved_quantity=Decimal(5), rule_hits=hit, rule_set_version="1",
        decided_at=T0 + timedelta(hours=1),
    )  # fmt: skip
    with pytest.raises(AmendRefusedError, match="未落账"):
        await env.om.request_amend(
            "i-1", amendment_id="am-4", new_price=Decimal(99), new_quantity=Decimal(5),
            decision=unpersisted,
        )  # fmt: skip


@pytest.mark.asyncio
async def test_amend_refused_in_wrong_states(env: Env) -> None:
    env.server.hold_events()
    await env.submit()
    with pytest.raises(AmendRefusedError, match="SUBMITTING"):
        await amend(env, "am-1", "99", 10)
    env.server.release_events()
    await env.pump()
    env.server.fill(env.oid(), Decimal(4), Decimal(100))
    await env.pump()
    with pytest.raises(AmendRefusedError, match="已成交"):
        await amend(env, "am-2", "99", 4)
    env.server.manual_cancel_confirm = True
    await env.om.request_cancel("i-1")
    with pytest.raises(AmendRefusedError, match="CANCEL_PENDING"):
        await amend(env, "am-3", "99", 10)
    assert env.repo.amendments_for("i-1") == []  # 被拒的改单不落账、不调用券商


class AmendFailBroker(FakeBroker):
    async def modify_order(self, amendment: OrderAmendment) -> None:
        raise RuntimeError("socket closed")


@pytest.mark.asyncio
async def test_amend_rejected_or_unknown_keeps_quantity(tmp_path: Path, env: Env) -> None:
    await env.submit()
    await env.pump()
    env.server.broker_side_cancel(env.oid())  # 券商侧已撤：改单被拒
    await amend(env, "am-1", "99", 6)
    assert env.repo.amendments_for("i-1")[0].result == "REJECTED"
    assert "amend_rejected" in env.discrepancies()
    other = Env(tmp_path / "b")
    other.use(AmendFailBroker(other.server))
    await other.broker.connect()
    await other.submit()
    await other.pump()
    await amend(other, "am-1", "99", 6)
    assert other.repo.amendments_for("i-1")[0].result == "UNKNOWN"
    assert other.om._ordered("i-1") == Decimal(10)  # 结果未知：有效数量保持原值
    assert "amend_unknown" in other.discrepancies()


ACTIONS = st.lists(
    st.sampled_from(["fill1", "fill3", "cancel", "confirm", "reject", "amend_up", "amend_down"]),
    max_size=12,
)


@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(actions=ACTIONS)
def test_random_cancel_amend_fill_sequences_stay_consistent(
    tmp_path_factory: pytest.TempPathFactory, actions: list[str]
) -> None:
    """随机交错成交、撤单、确认 / 拒绝撤单、改单后：成交量、剩余量、终态与券商事实一致。"""

    async def run() -> None:
        env = Env(tmp_path_factory.mktemp("race"))
        env.server.manual_cancel_confirm = True
        await env.broker.connect()
        await env.submit()
        await env.pump()
        oid = env.oid()
        for n, action in enumerate(actions):
            (order,) = (await env.broker.lookup_order("i-1")).orders
            active = order.state in {
                BrokerOrderState.ACKNOWLEDGED, BrokerOrderState.PARTIALLY_FILLED,
                BrokerOrderState.CANCEL_PENDING,
            }  # fmt: skip
            remaining = order.remaining_quantity
            if action.startswith("fill") and active and remaining > 0:
                env.server.fill(oid, min(Decimal(action[4:]), remaining), Decimal(100))
            elif action == "cancel":
                await env.om.request_cancel("i-1")
            elif action in {"confirm", "reject"} and order.state is BrokerOrderState.CANCEL_PENDING:
                if action == "confirm":
                    env.server.confirm_cancel(oid)
                else:
                    env.server.reject_cancel(oid, "拒绝")
            elif action.startswith("amend") and env.om.status("i-1") in {
                S.SUBMITTED,
                S.PARTIALLY_FILLED,
            }:
                qty = 10 if action == "amend_up" else int(env.om.filled_quantity("i-1")) + 1
                if qty <= 10:
                    await amend(env, f"am-{n}", "100", qty)
            await env.pump()
        (order,) = (await env.broker.lookup_order("i-1")).orders
        status = env.om.status("i-1")
        assert env.om.filled_quantity("i-1") == order.filled_quantity
        assert env.om._ordered("i-1") == order.total_quantity
        expected = {
            BrokerOrderState.FILLED: {S.FILLED},
            BrokerOrderState.CANCELLED: {S.CANCELLED},
            BrokerOrderState.CANCEL_PENDING: {S.CANCEL_PENDING},
            BrokerOrderState.PARTIALLY_FILLED: {S.PARTIALLY_FILLED},
            BrokerOrderState.ACKNOWLEDGED: {S.SUBMITTED},
        }[order.state]
        assert status in expected, (status, order.state, actions)
        assert "illegal_transition" not in env.discrepancies()

    asyncio.run(run())


# ---------- O-03 第一轮评审补充 ----------


class InFlightBroker(FakeBroker):
    """真实适配器在 await modify_order 期间会处理回报：改单生效后、返回前先送达一笔成交。"""

    om: OrderManager | None = None
    fill_during_modify: Decimal = Decimal(0)
    raise_after_apply: bool = False

    async def modify_order(self, amendment: OrderAmendment) -> None:
        await super().modify_order(amendment)
        if self.fill_during_modify:
            self.server.fill(amendment.broker_order_id, self.fill_during_modify, Decimal(100))
            assert self.om is not None
            while not self._queue.empty():
                await self.om.on_broker_event(self._queue.get_nowait())
        if self.raise_after_apply:
            raise RuntimeError("connection reset after send")


def in_flight_env(tmp_path: Path) -> tuple[Env, InFlightBroker]:
    env = Env(tmp_path)
    broker = InFlightBroker(env.server)
    env.use(broker)
    broker.om = env.om
    return env, broker


@pytest.mark.asyncio
async def test_fill_while_increase_amendment_in_flight_is_not_full(tmp_path: Path) -> None:
    env, broker = in_flight_env(tmp_path)
    await env.broker.connect()
    await env.submit()
    await env.pump()
    await amend(env, "am-1", "100", 6)  # 先减到 6
    broker.fill_during_modify = Decimal(6)
    await amend(env, "am-2", "100", 10)  # 加回 10，在途时成交 6 股
    assert env.om.status("i-1") is S.PARTIALLY_FILLED  # 没有按旧数量 6 提前判全部成交
    assert env.om._ordered("i-1") == Decimal(10)


@pytest.mark.asyncio
async def test_unknown_amendment_blocks_new_ones_and_is_resolved_by_lookup(tmp_path: Path) -> None:
    env, broker = in_flight_env(tmp_path)
    await env.broker.connect()
    await env.submit()
    await env.pump()
    await amend(env, "am-1", "100", 6)
    broker.raise_after_apply = True
    await amend(env, "am-2", "100", 10)  # 券商实际改成了 10，但调用抛异常 → UNKNOWN
    assert env.repo.amendments_for("i-1")[-1].result == "UNKNOWN"
    assert env.unresolved == ["i-1"]
    broker.raise_after_apply = False
    with pytest.raises(AmendRefusedError, match="结果未定"):
        await amend(env, "am-3", "100", 8)
    env.server.fill(env.oid(), Decimal(6), Decimal(100))
    await env.pump()
    assert env.om.status("i-1") is S.PARTIALLY_FILLED  # 未定期间按较大数量 10 判定
    await env.om.resolve_amendments("i-1")
    assert env.repo.amendments_for("i-1")[-1].result == "ACCEPTED"
    assert env.om._ordered("i-1") == Decimal(10)


@pytest.mark.asyncio
async def test_unknown_amendment_not_applied_on_live_order_stays_unknown(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.use(AmendFailBroker(env.server))
    await env.broker.connect()
    await env.submit()
    await env.pump()
    await amend(env, "am-1", "99", 6)
    await env.om.resolve_amendments("i-1")
    # 订单还活着：券商可能还没处理完，不能提前记成终结的 REJECTED
    assert env.repo.amendments_for("i-1")[-1].result == "UNKNOWN"
    assert env.om._ordered("i-1") == Decimal(10)


@pytest.mark.asyncio
async def test_amendment_replay_with_different_content_is_refused(env: Env) -> None:
    await env.submit()
    await env.pump()
    await amend(env, "am-1", "99.50", 10)
    with pytest.raises(AmendRefusedError, match="不同的改单内容"):
        await amend(env, "am-1", "99.40", 10)
    env.intent("i-2")
    with pytest.raises(AmendRefusedError, match="别的意图"):
        await env.om.request_amend(
            "i-2", amendment_id="am-1", new_price=Decimal("99.50"), new_quantity=Decimal(10),
            decision=env.repo.risk_decisions_for("i-1")[-1],
        )  # fmt: skip


@pytest.mark.asyncio
async def test_protective_stop_amendment_is_refused(env: Env) -> None:
    intent = env.intent(
        "stop", side=Side.SELL, order_type=OrderType.STOP, limit_price=None,
        stop_price=Decimal(90), order_class=OrderClass.PROTECTIVE_STOP,
        time_in_force=TimeInForce.GTC,
    )  # fmt: skip
    await env.om.submit_approved_intent(intent, env.allow(intent))
    await env.pump()
    decision = env.allow(intent, "d-stop-2", decided_at=T0 + timedelta(seconds=5))
    with pytest.raises(AmendRefusedError, match="保护性止损"):
        await env.om.request_amend(
            "stop", amendment_id="am-s", new_price=Decimal(50), new_quantity=Decimal(10),
            decision=decision,
        )  # fmt: skip
    assert env.repo.amendments_for("stop") == []


@pytest.mark.parametrize("case", ["other_intent", "reject", "not_later", "off_tick"])
@pytest.mark.asyncio
async def test_amendment_decision_checks(env: Env, case: str) -> None:
    intent, _ = await env.submit()
    await env.pump()
    later = T0 + timedelta(seconds=30)
    hit = (RuleHit(rule_id="r", rule_version="1", message="x"),)
    price = Decimal("99.50")
    if case == "other_intent":
        other = env.intent("i-2")
        decision = env.allow(other, "d-o", decided_at=later)
    elif case == "reject":
        decision = env.allow(
            intent, "d-r", verdict=RiskVerdict.REJECT, rule_hits=hit, decided_at=later
        )
    elif case == "not_later":
        decision = env.allow(intent, "d-n", decided_at=T0)  # 与签发决定同一时刻
    else:
        decision = env.allow(intent, "d-t", decided_at=later)
        price = Decimal("99.505")
    with pytest.raises(AmendRefusedError):
        await env.om.request_amend(
            "i-1", amendment_id=f"am-{case}", new_price=price, new_quantity=Decimal(10),
            decision=decision,
        )  # fmt: skip
    assert env.repo.amendments_for("i-1") == []  # 不落账、不调用券商
    (order,) = await env.broker.open_orders()
    assert order.price == Decimal("100.00")


class RaceBroker(FakeBroker):
    """随机测试用：可让改单在途时送达回报、可让改单抛错，并统计撤单调用次数。"""

    om: OrderManager | None = None
    deliver_during_modify: bool = False
    modify_mode: str = "ok"  # ok / reject / raise_after_apply / raise_before_apply
    cancel_calls: int = 0

    async def cancel_order(
        self, broker_order_id: str, *, allow_protective_stop: bool = False
    ) -> None:
        self.cancel_calls += 1
        await super().cancel_order(broker_order_id, allow_protective_stop=allow_protective_stop)

    async def modify_order(self, amendment: OrderAmendment) -> None:
        if self.modify_mode == "reject":
            raise AmendmentRejectedError("random reject")
        if self.modify_mode == "raise_before_apply":
            raise RuntimeError("lost before send")
        await super().modify_order(amendment)
        if self.deliver_during_modify and self.om is not None:
            while not self._queue.empty():
                await self.om.on_broker_event(self._queue.get_nowait())
        if self.modify_mode == "raise_after_apply":
            raise RuntimeError("lost after send")


RACE_ACTIONS = st.lists(
    st.sampled_from(
        ["fill1", "fill3", "cancel", "confirm", "reject_cancel", "amend_up", "amend_down",
         "hold", "release", "mode_ok", "mode_reject", "mode_raise_after", "mode_raise_before",
         "resolve", "inflight"]
    ),
    max_size=16,
)  # fmt: skip


@settings(
    max_examples=80, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(actions=RACE_ACTIONS)
def test_random_race_sequences_with_delayed_reports_stay_consistent(  # noqa: PLR0915
    tmp_path_factory: pytest.TempPathFactory, actions: list[str]
) -> None:
    """回报可被暂存、改单可在途送达回报或随机失败；查证之后本地与券商事实一致。"""

    async def run() -> None:  # noqa: PLR0912, PLR0915 - 随机动作的分派
        env = Env(tmp_path_factory.mktemp("race2"))
        broker = RaceBroker(env.server)
        env.use(broker)
        broker.om = env.om
        env.server.manual_cancel_confirm = True
        await env.broker.connect()
        await env.submit()
        await env.pump()
        oid = env.oid()
        cancel_requests = 0
        for n, action in enumerate(actions):
            (order,) = (await env.broker.lookup_order("i-1")).orders
            live = order.state in {
                BrokerOrderState.ACKNOWLEDGED, BrokerOrderState.PARTIALLY_FILLED,
                BrokerOrderState.CANCEL_PENDING,
            }  # fmt: skip
            if action.startswith("fill") and live and order.remaining_quantity > 0:
                env.server.fill(
                    oid, min(Decimal(action[4:]), order.remaining_quantity), Decimal(100)
                )
            elif action == "cancel":
                before = env.om.status("i-1")
                await env.om.request_cancel("i-1")
                cancel_requests += before in {S.SUBMITTED, S.PARTIALLY_FILLED}
            elif (
                action in {"confirm", "reject_cancel"}
                and order.state is BrokerOrderState.CANCEL_PENDING
            ):
                if action == "confirm":
                    env.server.confirm_cancel(oid)
                else:
                    env.server.reject_cancel(oid, "拒绝")
            elif action.startswith("amend"):
                qty = 10 if action == "amend_up" else int(env.om.filled_quantity("i-1")) + 1
                if qty <= 10:
                    recorded = len(env.repo.amendments_for("i-1"))
                    try:
                        await amend(env, f"am-{n}", "100", qty)
                    except AmendRefusedError:
                        assert len(env.repo.amendments_for("i-1")) == recorded  # 被拒不落账
            elif action == "hold":
                env.server.hold_events()
            elif action == "release":
                env.server.release_events(reverse=n % 2 == 0)
            elif action.startswith("mode_"):
                broker.modify_mode = {
                    "mode_ok": "ok", "mode_reject": "reject",
                    "mode_raise_after": "raise_after_apply",
                    "mode_raise_before": "raise_before_apply",
                }[action]  # fmt: skip
            elif action == "resolve":
                await env.om.resolve_amendments("i-1")
            elif action == "inflight":
                broker.deliver_during_modify = not broker.deliver_during_modify
            if action not in {"hold"}:
                await env.pump()
        # 收尾：放出暂存的回报、查证未定的改单，再与券商事实比对
        env.server.release_events()
        await env.pump()
        await env.om.resolve_amendments("i-1")
        await env.pump()
        (order,) = (await env.broker.lookup_order("i-1")).orders
        assert env.om.filled_quantity("i-1") == order.filled_quantity
        assert env.om._ordered("i-1") == order.total_quantity
        assert broker.cancel_calls == cancel_requests  # 每次真正的撤单请求只调用券商一次
        expected = {
            BrokerOrderState.FILLED: {S.FILLED},
            BrokerOrderState.CANCELLED: {S.CANCELLED},
            BrokerOrderState.CANCEL_PENDING: {S.CANCEL_PENDING},
            BrokerOrderState.PARTIALLY_FILLED: {S.PARTIALLY_FILLED},
            BrokerOrderState.ACKNOWLEDGED: {S.SUBMITTED},
        }[order.state]
        assert env.om.status("i-1") in expected, (env.om.status("i-1"), order.state, actions)
        assert "illegal_transition" not in env.discrepancies()

    asyncio.run(run())


# ---------- O-03 第二轮评审补充 ----------


class DelayedApplyBroker(FakeBroker):
    """改单请求发出后连接中断，券商稍后才应用改单（IBKR 改单是异步的）。"""

    held: OrderAmendment | None = None

    async def modify_order(self, amendment: OrderAmendment) -> None:
        self.held = amendment
        raise RuntimeError("connection reset; broker will apply later")

    def apply_later(self) -> None:
        assert self.held is not None
        self.server._modify(self.client_id, self.held)


@pytest.mark.asyncio
async def test_unknown_amendment_not_yet_applied_stays_unknown_until_applied(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    broker = DelayedApplyBroker(env.server)
    env.use(broker)
    await env.broker.connect()
    await env.submit()
    await env.pump()
    await amend(env, "am-1", "99.50", 10)
    await env.om.resolve_amendments("i-1")  # 券商还没处理：订单仍活着，保持 UNKNOWN
    assert env.repo.amendments_for("i-1")[-1].result == "UNKNOWN"
    assert "amend_still_pending" in env.discrepancies()
    broker.apply_later()
    await env.pump()
    await env.om.resolve_amendments("i-1")
    assert env.repo.amendments_for("i-1")[-1].result == "ACCEPTED"


@pytest.mark.asyncio
async def test_unknown_amendment_on_terminal_order_resolves_rejected(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.use(DelayedApplyBroker(env.server))
    await env.broker.connect()
    await env.submit()
    await env.pump()
    await amend(env, "am-1", "99.50", 10)
    env.server.broker_side_cancel(env.oid())  # 订单已终结，改单不可能再生效
    await env.pump()
    await env.om.resolve_amendments("i-1")
    assert env.repo.amendments_for("i-1")[-1].result == "REJECTED"


class ResolveDuringModifyBroker(FakeBroker):
    """改单在途时，另一路协程（对账循环）调用了 resolve_amendments。"""

    om: OrderManager | None = None

    async def modify_order(self, amendment: OrderAmendment) -> None:
        assert self.om is not None
        await self.om.resolve_amendments(amendment.order_ref)  # 在途改单必须被跳过
        await super().modify_order(amendment)


@pytest.mark.asyncio
async def test_resolve_skips_amendments_in_flight(tmp_path: Path) -> None:
    env = Env(tmp_path)
    broker = ResolveDuringModifyBroker(env.server)
    env.use(broker)
    broker.om = env.om
    await env.broker.connect()
    await env.submit()
    await env.pump()
    await amend(env, "am-1", "99.50", 6)
    assert env.repo.amendments_for("i-1")[-1].result == "ACCEPTED"
    assert "amend_result_conflict" not in env.discrepancies()
    assert env.om._ordered("i-1") == Decimal(6)


# ---------- O-03 第三轮评审补充 ----------


class CancelledModifyBroker(FakeBroker):
    async def modify_order(self, amendment: OrderAmendment) -> None:
        raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_cancelled_amendment_is_recorded_unknown_and_not_stuck(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.use(CancelledModifyBroker(env.server))
    await env.broker.connect()
    await env.submit()
    await env.pump()
    with pytest.raises(asyncio.CancelledError):
        await amend(env, "am-1", "99.50", 10)
    assert env.repo.amendments_for("i-1")[-1].result == "UNKNOWN"
    assert env.om._amending == set()  # 不会卡在在途状态
    assert env.unresolved == ["i-1"]


class FailingLookupBroker(DelayedApplyBroker):
    failing = False  # 下单之后才让查单失败（签发前的查重照常，O-05）

    async def lookup_order(self, order_ref: str) -> OrderLookup:
        if self.failing:
            raise RuntimeError("lookup timed out")
        return await super().lookup_order(order_ref)


@pytest.mark.asyncio
async def test_amendment_lookup_failure_keeps_it_pending(tmp_path: Path) -> None:
    env = Env(tmp_path)
    broker = FailingLookupBroker(env.server)
    env.use(broker)
    await env.broker.connect()
    await env.submit()
    await env.pump()
    broker.failing = True
    await amend(env, "am-1", "99.50", 10)
    await env.om.resolve_amendments("i-1")
    assert env.repo.amendments_for("i-1")[-1].result == "UNKNOWN"
    assert "amend_lookup_failed" in env.discrepancies()


class ConflictingResultBroker(FakeBroker):
    """改单在途时账本里已被别处记成 REJECTED（模拟另一路写入），随后券商报接受。"""

    repo: LedgerRepository | None = None

    async def modify_order(self, amendment: OrderAmendment) -> None:
        assert self.repo is not None
        self.repo.record_amendment_result(amendment.amendment_id, "REJECTED", T0)
        await super().modify_order(amendment)


@pytest.mark.asyncio
async def test_amendment_result_conflict_is_flagged_not_raised(tmp_path: Path) -> None:
    env = Env(tmp_path)
    broker = ConflictingResultBroker(env.server)
    env.use(broker)
    broker.repo = env.repo
    await env.broker.connect()
    await env.submit()
    await env.pump()
    await amend(env, "am-1", "99.50", 10)  # 不抛异常
    assert "amend_result_conflict" in env.discrepancies()
    assert env.unresolved == ["i-1"]


@pytest.mark.asyncio
async def test_long_pending_amendment_escalates_again(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.use(DelayedApplyBroker(env.server))
    await env.broker.connect()
    await env.submit()
    await env.pump()
    await amend(env, "am-1", "99.50", 10)  # UNKNOWN 时通知一次
    for _ in range(3):
        await env.om.resolve_amendments("i-1")
    assert env.unresolved == ["i-1", "i-1"]  # 第 3 轮仍对不上，再次通知
