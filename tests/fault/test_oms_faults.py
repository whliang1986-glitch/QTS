"""O-04：OMS 故障注入套件（V4.1 11.2 中属于券商侧、由 OMS 处理的场景）+ 随机故障组合的属性测试。

每个场景对应矩阵 docs/gates/G1-fault-matrix.md 的一行；断言的是 OMS 的行为（不重发、不重复记账、
不确定时失败即关闭），券商侧事实由 FakeBroker 注入（B-02）。真实 IBKR 的行为差异见 B-02 待实测清单。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from server.broker.base import (
    BrokerOrderState,
    BrokerUnavailableError,
    ErrorCategory,
    OrderLookup,
)
from server.broker.fake.broker import FakeBroker
from server.domain.enums import Side
from server.domain.order_state import EventKind, OrderStatus
from server.oms.manager import SubmissionRefusedError

from tests.support.oms import Env, status_update

S = OrderStatus
EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
LIVE = {
    BrokerOrderState.ACKNOWLEDGED,
    BrokerOrderState.PARTIALLY_FILLED,
    BrokerOrderState.CANCEL_PENDING,
}
EXPECTED = {
    BrokerOrderState.ACKNOWLEDGED: {S.SUBMITTED},
    BrokerOrderState.PARTIALLY_FILLED: {S.PARTIALLY_FILLED},
    BrokerOrderState.CANCEL_PENDING: {S.CANCEL_PENDING},
    BrokerOrderState.FILLED: {S.FILLED},
    BrokerOrderState.CANCELLED: {S.CANCELLED},
    BrokerOrderState.REJECTED: {S.REJECTED},
    BrokerOrderState.EXPIRED: {S.EXPIRED},
}


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> Env:
    e = Env(tmp_path)
    await e.broker.connect()
    return e


def broker_orders(env: Env, intent_id: str) -> list[Any]:
    """券商侧事实：直接读 FakeBroker 服务端（断线时也能查）；OMS 自己的查单仍走客户端。"""
    return list(env.server._lookup(intent_id).orders)


# ---------- 场景 1：已接单但响应丢失 ----------


@pytest.mark.asyncio
async def test_s01_lost_ack_unknown_then_lookup_never_resends(env: Env) -> None:
    env.server.lose_next_ack()
    await env.submit()
    env.clock.advance(11)
    assert env.om.check_timeouts() == ["i-1"]
    await env.om.submit_approved_intent(
        env.repo.get_intent("i-1"),  # type: ignore[arg-type]
        env.repo.risk_decisions_for("i-1")[0],
    )  # 上游重试也不会再下单
    assert await env.om.resolve_unknown("i-1") is S.SUBMITTED
    assert len(broker_orders(env, "i-1")) == 1


@pytest.mark.asyncio
async def test_s01_lost_request_stays_unknown_and_escalates(env: Env) -> None:
    env.server.lose_next_request()
    await env.submit()
    env.clock.advance(11)
    env.om.check_timeouts()
    for _ in range(3):
        assert await env.om.resolve_unknown("i-1") is S.UNKNOWN
    assert env.unresolved == ["i-1"] and broker_orders(env, "i-1") == []


class LookupDownBroker(FakeBroker):
    """查单接口不可用（回报通道仍在）。"""

    async def lookup_order(self, order_ref: str) -> OrderLookup:
        raise BrokerUnavailableError("lookup down")


@pytest.mark.asyncio
async def test_s01_status_report_alone_does_not_resolve_unknown(tmp_path: Path) -> None:
    """UNKNOWN 时收到状态回报：回报只是线索，必须查单核实；查不了就继续 UNKNOWN，不照抄回报。"""
    env = Env(tmp_path)
    env.use(LookupDownBroker(env.server))
    await env.broker.connect()
    env.server.lose_next_ack()
    await env.submit()
    env.clock.advance(11)
    assert env.om.check_timeouts() == ["i-1"]
    await env.om.on_broker_event(status_update(env, BrokerOrderState.ACKNOWLEDGED))
    assert env.om.status("i-1") is S.UNKNOWN
    assert "unknown_unresolved" in env.discrepancies()


# ---------- 场景 2、3：重复与乱序回报 ----------


@pytest.mark.asyncio
async def test_s02_duplicate_execution_booked_once(env: Env) -> None:
    await env.submit()
    await env.pump()
    ex = env.server.fill(env.oid(), Decimal(10), Decimal(100))
    for _ in range(3):
        env.server.duplicate_execution(ex.execution_id)
    await env.pump()
    assert env.om.filled_quantity("i-1") == Decimal(10)
    assert len(env.repo.executions_for("i-1")) == 1


@pytest.mark.asyncio
async def test_s03_fill_before_ack(env: Env) -> None:
    env.server.hold_events()
    await env.submit()
    env.server.fill(env.oid(), Decimal(10), Decimal(100))
    env.server.release_events(reverse=True)
    await env.pump()
    assert env.om.status("i-1") is S.FILLED
    assert env.om.filled_quantity("i-1") == Decimal(10)
    kinds = [e.kind for e, _ in env.repo.order_history("i-1")]
    assert kinds.index(EventKind.BROKER_ACK) < kinds.index(EventKind.FULL_FILL)  # 先补 ACK
    assert "illegal_transition" not in env.discrepancies()


# ---------- 场景 4：撤单中成交、部分成交后撤单 ----------


@pytest.mark.asyncio
async def test_s04_fills_during_cancel(env: Env) -> None:
    env.server.manual_cancel_confirm = True
    await env.submit()
    await env.pump()
    env.server.fill(env.oid(), Decimal(3), Decimal(100))
    await env.pump()
    await env.om.request_cancel("i-1")
    env.server.fill(env.oid(), Decimal(2), Decimal(100))
    await env.pump()
    assert env.om.status("i-1") is S.CANCEL_PENDING
    env.server.confirm_cancel(env.oid())
    await env.pump()
    assert env.om.status("i-1") is S.CANCELLED
    assert env.om.filled_quantity("i-1") == Decimal(5)


@pytest.mark.asyncio
async def test_s04_fill_reported_after_cancel_confirmation_is_still_booked(env: Env) -> None:
    await env.submit()
    await env.pump()
    env.server.hold_events()
    env.server.fill(env.oid(), Decimal(3), Decimal(100))
    await env.om.request_cancel("i-1")
    env.server.release_events(reverse=True)  # 撤单确认先到，成交后到
    await env.pump()
    assert env.om.status("i-1") is S.CANCELLED
    assert env.om.filled_quantity("i-1") == Decimal(3)  # 终态之后到的成交照样入账
    assert "fill_after_terminal" in env.discrepancies()


# ---------- 场景 5：拒单 ----------


@pytest.mark.parametrize(
    "category",
    [ErrorCategory.PRICE_REJECTED, ErrorCategory.NO_PERMISSION, ErrorCategory.CONTRACT_NOT_FOUND],
)
@pytest.mark.asyncio
async def test_s05_rejections(env: Env, category: ErrorCategory) -> None:
    env.server.reject_next(category, "注入")
    await env.submit()
    await env.pump()
    assert env.om.status("i-1") is S.REJECTED
    intent = env.repo.get_intent("i-1")
    assert intent is not None
    # 上游用同一决定重试：不重下；换新决定：拒绝签发（拒单后要再下只能走新意图）
    decision = env.repo.risk_decisions_for("i-1")[0]
    assert await env.om.submit_approved_intent(intent, decision) is S.REJECTED
    with pytest.raises(SubmissionRefusedError):
        await env.om.submit_approved_intent(intent, env.allow(intent, "d-retry"))
    assert len(broker_orders(env, "i-1")) == 1  # 没有重下


# ---------- 场景 7（移交 O-05）：重启窗口吞掉在途回报 ----------


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="O-05：重连后需对账补回丢失的回报；O-05 完成后去掉 xfail",
)
@pytest.mark.asyncio
async def test_s07_inflight_reports_lost_across_restart_need_reconciliation(env: Env) -> None:
    await env.submit()
    await env.pump()
    env.server.hold_events()
    env.server.fill(env.oid(), Decimal(10), Decimal(100))  # 回报在途
    env.server.enter_restart_window()
    env.server.release_events()  # 客户端已断开：在途回报丢失
    env.server.exit_restart_window()
    await env.broker.connect()
    await env.pump()  # 没有重连对账
    assert env.om.filled_quantity("i-1") == Decimal(10)
    assert env.om.status("i-1") is S.FILLED


# ---------- 场景 8：券商重启窗口 ----------


@pytest.mark.asyncio
async def test_s08_restart_window_refuses_new_orders(env: Env) -> None:
    env.server.enter_restart_window()
    with pytest.raises(BrokerUnavailableError):
        await env.submit()
    assert env.repo.issuance_for("i-1") is None  # 没有签发，也就没有遗留的 SUBMITTING
    env.server.exit_restart_window()
    await env.broker.connect()
    await env.om.submit_approved_intent(
        env.repo.get_intent("i-1"),  # type: ignore[arg-type]
        env.repo.risk_decisions_for("i-1")[0],
    )
    await env.pump()
    assert env.om.status("i-1") is S.SUBMITTED


# ---------- 外来成交（B-01 / C-03 移交） ----------


@pytest.mark.asyncio
async def test_foreign_execution_is_kept_unmatched(env: Env) -> None:
    await env.submit()
    await env.pump()
    env.server.inject_foreign_execution("SPY", Side.SELL, Decimal("0.5"), Decimal(100))
    await env.pump()
    assert [r for _, r, _ in env.repo.unmatched_executions()] == ["no_order"]
    assert env.om.filled_quantity("i-1") == 0


# ---------- 随机故障组合 ----------

FAULT_NAMES = [
    "submit", "retry", "retry_new_decision", "lose_ack", "lose_request", "reject_next",
    "fill1", "fill4", "dup", "hold", "release", "release_rev", "cancel", "confirm",
    "reject_cancel", "manual_cancel", "timeout", "resolve", "foreign", "restart", "expire",
]  # fmt: skip
# 断线（券商重启窗口）期间不执行会改变券商侧事实、或要求 OMS 与券商通信的故障：
# 断线期间的事实漂移需要重连对账（场景 7，O-05），这里只验证断线本身不破坏不变量
BROKER_SIDE = {
    "fill1", "fill4", "dup", "release", "release_rev", "confirm", "reject_cancel",
    "foreign", "expire", "cancel", "resolve",
}  # fmt: skip
FAULTS = st.lists(
    st.tuples(st.sampled_from(FAULT_NAMES), st.integers(min_value=0, max_value=7)),
    min_size=1,
    max_size=25,
)
# 固定用例（评审要求）：重启窗口内超时；暂存回报后进入重启窗口（回报在重连后才放出）；
# 窗口内下单被拒与重试；撤单被拒后再撤；提交中 / UNKNOWN 时撤单（挂起，确认后补发）；
# 回报暂存期间券商已到终态再撤单；窗口内超时转 UNKNOWN 后重连查证；在途订单的重复成交
EXAMPLES: list[list[tuple[str, int]]] = [
    [("submit", 0), ("restart", 0), ("timeout", 0)],
    [("submit", 0), ("hold", 0), ("fill1", 0), ("restart", 0)],
    [("submit", 0), ("restart", 0), ("submit", 0), ("retry", 0), ("restart", 0)],
    [("manual_cancel", 0), ("submit", 0), ("cancel", 0), ("reject_cancel", 0), ("cancel", 0)],
    [("hold", 0), ("submit", 0), ("cancel", 0), ("release", 0)],
    [("lose_ack", 0), ("submit", 0), ("cancel", 0)],
    [("submit", 0), ("hold", 0), ("expire", 0), ("cancel", 0)],
    [("submit", 0), ("hold", 0), ("fill4", 0), ("fill4", 0), ("fill4", 0), ("cancel", 0)],
    [("lose_ack", 0), ("submit", 0), ("restart", 0), ("timeout", 0), ("retry", 0)],
    [("submit", 0), ("fill1", 0), ("dup", 0), ("dup", 0)],
]
# 随机故障下允许出现的差异种类，每一种都对应注入的故障；其余（非法转移、超额成交、成交冲突、
# permId 冲突、同一 orderRef 下多张单、查单事实不符等）出现即失败
ALLOWED_DISCREPANCIES = {
    "submit_raised",  # 丢请求 / 丢 ACK / 拒单时 place_order 抛错
    "unknown_unresolved",  # 查无此单（丢请求）
    "unmatched_execution",  # 外来成交
    "fill_after_terminal",  # 逆序放出：终态回报先于成交到达
    "status_after_terminal",  # 逆序放出：终态之后又到一条不同的终态回报
}
# 只在暂存过回报时允许：OMS 还没看到终态就撤单，券商报“订单不存在”（真实的撤单竞态）
ALLOWED_WHEN_HELD = {"cancel_unknown_order"}


class Run:
    """一次随机运行的记录：已签发的意图、被拒签发的意图、撤单请求、外来成交数。"""

    def __init__(self) -> None:
        self.intents: list[str] = []
        self.refused: list[str] = []
        self.cancel: dict[str, str] = {}  # intent_id → "requested" / "rejected"
        self.foreign = 0
        self.held = False  # 是否暂存过回报


async def settle(env: Env, run: Run) -> None:
    """收尾，让 OMS 看到全部券商事实。

    1. 退出重启窗口并重连（先连上再放出暂存回报：暂存时已连接的客户端重连后才收得到）；
    2. 放出暂存的回报并处理；
    3. 时钟前进 60 秒，超时转 UNKNOWN；每个意图查证到升级所需轮数（查到即转出，挂起的撤单补发）；
    4. 再处理一次回报。
    乐观假设：重启窗口前已发出、尚未送达的回报，在重连后送达（FakeBroker 按客户端对象投递）。
    真实 TWS 重启时这些在途回报会丢，只能靠重连对账补回，归 O-05（见
    test_s07_inflight_reports_lost_across_restart_need_reconciliation）。
    不做：重连对账、补发断线期间丢失的事件（O-05）；resolve_amendments（本测试不改单）。
    """
    if not env.broker.connected:
        env.server.exit_restart_window()
        await env.broker.connect()
    env.server.release_events()
    await env.pump()
    env.clock.advance(60)
    env.om.check_timeouts()
    for intent_id in run.intents:
        for _ in range(env.om._max_lookup_rounds):  # 与升级阈值一致：查无此单的意图到此必已升级
            await env.om.resolve_unknown(intent_id)
    await env.pump()


async def retry(env: Env, target: str, fault: str, n: int) -> None:
    intent = env.repo.get_intent(target)
    assert intent is not None
    before = (env.om.status(target), len(broker_orders(env, target)))
    if fault == "retry":  # 上游用同一决定重试：幂等，不下单、不改状态
        decision = env.repo.risk_decisions_for(target)[0]
        assert await env.om.submit_approved_intent(intent, decision) is before[0]
    else:  # 换新决定再签发：拒绝
        with pytest.raises(SubmissionRefusedError):
            await env.om.submit_approved_intent(intent, env.allow(intent, f"d-{target}-{n}"))
    assert (env.om.status(target), len(broker_orders(env, target))) == before


async def submit(env: Env, run: Run, intent_id: str) -> None:
    if env.broker.connected:
        run.intents.append(intent_id)
        await env.submit(intent_id)
        return
    run.refused.append(intent_id)  # 重启窗口内：拒绝，不留签发
    with pytest.raises(BrokerUnavailableError):
        await env.submit(intent_id)
    assert env.repo.issuance_for(intent_id) is None


async def cancel(env: Env, run: Run, target: str) -> None:
    before = env.om.status(target)
    after = await env.om.request_cancel(target)
    # 真正发出或挂起的撤单才要求最终生效；撤单中、终态时的请求被忽略
    if before in {S.SUBMITTED, S.PARTIALLY_FILLED, S.SUBMITTING, S.UNKNOWN}:
        assert after in {S.CANCEL_PENDING, S.SUBMITTING, S.UNKNOWN}, after
        run.cancel[target] = "requested"


async def apply_fault(env: Env, fault: str, pick: int, run: Run, n: int) -> None:  # noqa: PLR0912, PLR0915
    connected = env.broker.connected
    if not connected and fault in BROKER_SIDE:
        return
    target = run.intents[pick % len(run.intents)] if run.intents else None
    found = broker_orders(env, target) if target else []
    order = found[0] if found else None
    live = order is not None and order.state in LIVE
    if fault == "submit":
        await submit(env, run, f"i-{n}")
    elif fault in {"retry", "retry_new_decision"} and target:
        await retry(env, target, fault, n)
    elif fault == "lose_ack":
        env.server.lose_next_ack()
    elif fault == "lose_request":
        env.server.lose_next_request()
    elif fault == "reject_next":
        env.server.reject_next(ErrorCategory.PRICE_REJECTED, "注入")
    elif fault.startswith("fill") and live and order is not None and order.remaining_quantity > 0:
        qty = min(Decimal(fault[4:]), order.remaining_quantity)
        env.server.fill(order.broker_order_id, qty, Decimal(100))
    elif fault == "dup" and (booked := env.server.executions(EPOCH)):
        env.server.duplicate_execution(booked[pick % len(booked)].execution_id)
    elif fault == "hold":
        env.server.hold_events()
        run.held = True
    elif fault in {"release", "release_rev"}:
        env.server.release_events(reverse=fault == "release_rev")
    elif fault == "cancel" and target:
        await cancel(env, run, target)
    elif fault in {"confirm", "reject_cancel"} and order is not None and target:
        if order.state is BrokerOrderState.CANCEL_PENDING:
            if fault == "confirm":
                env.server.confirm_cancel(order.broker_order_id)
            else:
                env.server.reject_cancel(order.broker_order_id, "拒绝")
                run.cancel[target] = "rejected"
    elif fault == "manual_cancel":
        env.server.manual_cancel_confirm = not env.server.manual_cancel_confirm
    elif fault == "timeout":
        env.clock.advance(11)
        env.om.check_timeouts()
    elif fault == "resolve" and target:
        await env.om.resolve_unknown(target)
    elif fault == "foreign":
        env.server.inject_foreign_execution("SPY", Side.SELL, Decimal("0.5"), Decimal(100))
        run.foreign += 1
    elif fault == "restart":
        if connected:
            env.server.enter_restart_window()
        else:
            env.server.exit_restart_window()
            await env.broker.connect()
    elif fault == "expire":
        env.server.expire_day_orders()
    await env.pump()  # 断线时队列里只有连接事件


def check_intent(env: Env, run: Run, intent_id: str, faults: Any) -> None:
    orders = broker_orders(env, intent_id)
    status = env.om.status(intent_id)
    history = env.repo.order_history(intent_id)
    assert len(orders) <= 1, (intent_id, faults)
    sent = [e for e, _ in history if e.kind is EventKind.SUBMIT_SENT]
    assert len(sent) == 1, ("一条订单链只发送一次", intent_id, faults)
    if not orders:
        # 券商没有这张单：只可能停在 UNKNOWN（查无此单不映射为 REJECTED），并已升级
        assert status is S.UNKNOWN, (intent_id, status, faults)
        assert S.REJECTED not in [s for _, s in history]
        assert intent_id in env.unresolved, ("查无此单必须升级", intent_id, faults)
        return
    order = orders[0]
    filled = env.om.filled_quantity(intent_id)
    assert filled == order.filled_quantity, (intent_id, faults)
    assert status in EXPECTED[order.state], (intent_id, status, order.state, faults)
    if filled > 0:
        assert status is not S.SUBMITTED, (intent_id, faults)
    if run.cancel.get(intent_id) == "requested":  # 撤单已发出（或挂起后补发）且未被拒：不能还挂着
        assert order.state not in {
            BrokerOrderState.ACKNOWLEDGED,
            BrokerOrderState.PARTIALLY_FILLED,
        }, ("撤单没有生效", intent_id, faults)


def check_invariants(env: Env, run: Run, faults: Any) -> None:
    for intent_id in run.intents:
        check_intent(env, run, intent_id, faults)
    # 券商侧每张单都来自本系统的一次签发，每个 orderRef 一张
    refs = [o.order_ref for o in env.server._open_orders() + env.server._completed_orders()]
    issued = {i for i in run.intents if env.repo.issuance_for(i) is not None}
    assert len(refs) == len(set(refs)) and set(refs) <= issued, (refs, faults)
    for intent_id in run.refused:
        assert env.repo.issuance_for(intent_id) is None and not broker_orders(env, intent_id)
    unmatched = env.repo.unmatched_executions()
    assert {r for _, r, _ in unmatched} <= {"no_order"}, faults
    assert len(unmatched) == run.foreign, ("外来成交全部且只进未匹配表", faults)
    allowed = ALLOWED_DISCREPANCIES | (ALLOWED_WHEN_HELD if run.held else set())
    unexpected = set(env.discrepancies()) - allowed
    assert not unexpected, (unexpected, faults)


def _with_examples(test: Any) -> Any:
    for faults in EXAMPLES:
        test = example(faults=faults)(test)
    return test


@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@_with_examples
@given(faults=FAULTS)
def test_random_fault_sequences_keep_oms_invariants(
    tmp_path_factory: pytest.TempPathFactory, faults: list[tuple[str, int]]
) -> None:
    """随机组合 11.2 的券商侧故障（目标意图随机选取）后，check_invariants 的各项始终成立。"""

    async def run_faults() -> None:
        env = Env(tmp_path_factory.mktemp("faults"))
        await env.broker.connect()
        run = Run()
        for n, (fault, pick) in enumerate(faults):
            await apply_fault(env, fault, pick, run, n)
        await settle(env, run)
        check_invariants(env, run, faults)

    asyncio.run(run_faults())
