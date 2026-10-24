"""R-01：预交易风控引擎——四种结论、失败即关闭、决定绑定评估对象（真实账本 + FakeBroker）。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from server.domain.enums import Mode, RiskVerdict
from server.domain.order_state import OrderStatus
from server.domain.orders import RiskDecision
from server.oms.binding import amendment_digest, intent_digest
from server.oms.manager import AmendRefusedError, SubmissionRefusedError
from server.risk.engine import (
    RiskContext,
    RiskEngine,
    RuleResult,
    RuleSet,
    Subject,
)
from server.risk.mode_manager import ModeConfig, ModeManager
from server.storage.ledger import LedgerRepository, open_ledger
from tests.support.oms import ACCOUNT, Env

V, S = RiskVerdict, OrderStatus


@dataclass
class FixedRule:
    """测试用规则：对所有订单给出固定结论（None 表示放行）。"""

    result: RuleResult | None = None
    rule_id: str = "test_rule"
    version: str = "7"
    raises: bool = False

    def check(self, subject: Subject, context: RiskContext) -> RuleResult | None:
        if self.raises:
            raise RuntimeError("boom")
        return self.result


class Context:
    def __init__(self, env: Env, *, fail: bool = False, delay: float = 0) -> None:
        self.env, self.fail, self.delay = env, fail, delay

    async def snapshot(self, subject: Subject) -> RiskContext:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("positions unavailable")
        return RiskContext(now=self.env.clock())


def reject(msg: str = "超限") -> RuleResult:
    return RuleResult(verdict=V.REJECT, message=msg)


def reduce(qty: str) -> RuleResult:
    return RuleResult(verdict=V.REDUCE, message="缩量", max_quantity=Decimal(qty))


def modes(env: Env, *, start: bool = True) -> ModeManager:
    manager = ModeManager(env.repo, clock=env.clock)
    if start:
        manager.start(
            ModeConfig(
                requested_mode=Mode.PAPER,
                account_id=ACCOUNT.account_id,
                expected_account_type=ACCOUNT.account_type,
                risk_regime=ACCOUNT.account_type,
                allow_delayed_data=False,
                config_version="risk-test",
            ),
            ACCOUNT,
        )
    return manager


def engine(
    env: Env,
    *rules: FixedRule,
    manager: ModeManager | None = None,
    context: Context | None = None,
    rule_set: RuleSet | str | None = "default",
    timeout: float = 1,
) -> RiskEngine:
    if isinstance(rule_set, str):
        rule_set = RuleSet(version="rs-1", rules=rules or (FixedRule(),))
    return RiskEngine(
        env.repo,
        env.om,
        manager or modes(env),
        rule_set=rule_set,
        context=context or Context(env),
        clock=env.clock,
        timeout=timedelta(seconds=timeout),
    )


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> Env:
    e = Env(tmp_path)
    await e.broker.connect()
    await e.pump()
    return e


# ---------- 四种结论 ----------


@pytest.mark.asyncio
async def test_allow_submits_with_bound_decision(env: Env) -> None:
    intent = env.make_intent()
    outcome = await engine(env).submit(intent)
    assert outcome.decision.verdict is V.ALLOW and outcome.status is S.SUBMITTING
    assert outcome.decision.subject_digest == intent_digest(intent)
    assert env.repo.risk_decisions_for("i-1") == [outcome.decision]
    issued = env.repo.issuance_for("i-1")
    assert issued is not None and issued.decision_id == outcome.decision.decision_id


@pytest.mark.asyncio
async def test_reject_records_hits_and_does_not_submit(env: Env) -> None:
    outcome = await engine(env, FixedRule(reject("单笔超限"))).submit(env.make_intent())
    assert outcome.decision.verdict is V.REJECT and outcome.status is None
    (hit,) = outcome.decision.rule_hits
    assert (hit.rule_id, hit.rule_version, hit.message) == ("test_rule", "7", "单笔超限")
    assert outcome.decision.rule_set_version == "rs-1"
    assert env.repo.issuance_for("i-1") is None


@pytest.mark.asyncio
async def test_reduce_takes_the_smallest_whole_quantity(env: Env) -> None:
    rules = (FixedRule(reduce("7.6"), rule_id="a"), FixedRule(reduce("8"), rule_id="b"))
    outcome = await engine(env, *rules).submit(env.make_intent())
    assert outcome.decision.verdict is V.REDUCE
    assert outcome.decision.approved_quantity == Decimal(7)
    issued = env.repo.issuance_for("i-1")
    assert issued is not None and issued.quantity == Decimal(7)
    assert len(outcome.decision.rule_hits) == 2


@pytest.mark.parametrize(
    "result",
    [
        reduce("10"),  # 不小于原数量：结论矛盾
        reduce("0.5"),  # 取整后为 0
        RuleResult(verdict=V.REDUCE, message="没给数量"),
    ],
)
@pytest.mark.asyncio
async def test_unusable_reduce_becomes_reject(env: Env, result: RuleResult) -> None:
    outcome = await engine(env, FixedRule(result)).submit(env.make_intent())
    assert outcome.decision.verdict is V.REJECT and env.repo.issuance_for("i-1") is None


@pytest.mark.asyncio
async def test_reject_beats_reduce_and_halt_beats_all(env: Env) -> None:
    mixed = (FixedRule(reduce("5"), rule_id="a"), FixedRule(reject(), rule_id="b"))
    assert (await engine(env, *mixed).submit(env.make_intent())).decision.verdict is V.REJECT
    manager = modes(env)
    halting = (*mixed, FixedRule(RuleResult(verdict=V.HALT, message="账户不可信"), rule_id="c"))
    outcome = await engine(env, *halting, manager=manager).submit(env.make_intent("i-2"))
    assert outcome.decision.verdict is V.HALT and outcome.status is None
    state = manager.current()
    assert state is not None and state.halt.halted  # HALT 已持久化
    after = await engine(env, manager=manager).submit(env.make_intent("i-3"))
    assert after.decision.rule_hits[0].rule_id == "mode_gate"


# ---------- 失败即关闭 ----------


@pytest.mark.asyncio
async def test_missing_rule_set_rejects_everything(env: Env) -> None:
    for rule_set in (None, RuleSet(version="empty", rules=())):
        outcome = await engine(env, rule_set=rule_set).submit(env.make_intent(f"i-{id(rule_set)}"))
        assert outcome.decision.verdict is V.REJECT
        assert outcome.decision.rule_hits[0].rule_id == "rule_set_missing"
    assert env.repo.submitted_intents() == []


@pytest.mark.asyncio
async def test_mode_not_verified_rejects(env: Env) -> None:
    outcome = await engine(env, manager=modes(env, start=False)).submit(env.make_intent())
    assert outcome.decision.verdict is V.REJECT
    assert outcome.decision.rule_hits[0].rule_id == "mode_gate"


@pytest.mark.asyncio
async def test_rule_error_rejects(env: Env) -> None:
    outcome = await engine(env, FixedRule(raises=True)).submit(env.make_intent())
    assert outcome.decision.rule_hits[0].rule_id == "rule_error"
    assert env.repo.issuance_for("i-1") is None


@pytest.mark.parametrize(
    ("context", "rule_id"),
    [({"fail": True}, "context_unavailable"), ({"delay": 0.2}, "risk_timeout")],
)
@pytest.mark.asyncio
async def test_context_failure_or_timeout_rejects(
    env: Env, context: dict[str, object], rule_id: str
) -> None:
    risk = engine(env, context=Context(env, **context), timeout=0.05)  # type: ignore[arg-type]
    outcome = await risk.submit(env.make_intent())
    assert outcome.decision.verdict is V.REJECT
    assert outcome.decision.rule_hits[0].rule_id == rule_id


class FailingDecisionRepo(LedgerRepository):
    def add_risk_decision(self, decision: RiskDecision) -> bool:
        raise OSError("disk full")


@pytest.mark.asyncio
async def test_decision_not_persisted_means_no_order(env: Env, tmp_path: Path) -> None:
    env.repo = FailingDecisionRepo(open_ledger(tmp_path / "ledger.db"))
    env.use(env.broker)
    await env.broker.connect()
    with pytest.raises(OSError):
        await engine(env).submit(env.make_intent())
    assert env.server._lookup("i-1").empty


# ---------- 幂等 ----------


@pytest.mark.asyncio
async def test_retry_reuses_decision_and_never_resubmits(env: Env) -> None:
    risk = engine(env)
    intent = env.make_intent()
    first = await risk.submit(intent)
    await env.pump()
    second = await risk.submit(intent)
    assert second.decision == first.decision and second.status is S.SUBMITTED
    assert len(env.repo.risk_decisions_for("i-1")) == 1
    assert len(env.server._lookup("i-1").orders) == 1


@pytest.mark.asyncio
async def test_conflicting_intent_content_is_rejected(env: Env) -> None:
    risk = engine(env)
    await risk.submit(env.make_intent())
    other = env.make_intent(quantity=Decimal(20))
    outcome = await risk.submit(other)
    assert outcome.decision.verdict is V.REJECT
    assert outcome.decision.rule_hits[0].rule_id == "intent_conflict"


# ---------- 决定绑定评估对象（G3 前硬前置） ----------


@pytest.mark.asyncio
async def test_oms_refuses_decision_bound_to_other_content(env: Env) -> None:
    intent = env.intent()
    other = env.make_intent("x", quantity=Decimal(20))
    wrong = env.allow(intent, "d-wrong", subject_digest=intent_digest(other))
    with pytest.raises(SubmissionRefusedError, match="绑定"):
        await env.om.submit_approved_intent(intent, wrong)
    unbound = env.allow(intent, "d-none", subject_digest=None)
    with pytest.raises(SubmissionRefusedError, match="绑定"):
        await env.om.submit_approved_intent(intent, unbound)
    assert env.repo.issuance_for("i-1") is None


@pytest.mark.asyncio
async def test_amend_through_engine_binds_price_and_quantity(env: Env) -> None:
    risk = engine(env)
    await risk.submit(env.make_intent())
    await env.pump()
    env.clock.advance(1)
    outcome = await risk.amend(
        "i-1", amendment_id="am-1", new_price=Decimal("99.50"), new_quantity=Decimal(10)
    )
    assert outcome.decision.verdict is V.ALLOW and outcome.status is S.SUBMITTED
    intent = env.repo.get_intent("i-1")
    assert intent is not None
    expected = amendment_digest(
        intent, amendment_id="am-1", new_price=Decimal("99.50"), new_quantity=Decimal(10)
    )
    assert outcome.decision.subject_digest == expected
    (order,) = await env.broker.open_orders()
    assert order.price == Decimal("99.50")


@pytest.mark.asyncio
async def test_amend_decision_cannot_approve_a_different_price(env: Env) -> None:
    risk = engine(env)
    await risk.submit(env.make_intent())
    await env.pump()
    env.clock.advance(1)
    decision = await risk.decide(
        Subject(
            intent=env.repo.get_intent("i-1"),  # type: ignore[arg-type]
            amendment_id="am-1",
            price=Decimal("99.50"),
            quantity=Decimal(10),
        )
    )
    with pytest.raises(AmendRefusedError, match="绑定"):
        await env.om.request_amend(
            "i-1",
            amendment_id="am-1",
            new_price=Decimal("120"),  # 拿批准 99.50 的决定去改成 120
            new_quantity=Decimal(10),
            decision=decision,
        )


@pytest.mark.asyncio
async def test_amend_reduce_is_rejected(env: Env) -> None:
    risk = engine(env)
    await risk.submit(env.make_intent())
    await env.pump()
    env.clock.advance(1)
    reducing = engine(env, FixedRule(reduce("5")))
    outcome = await reducing.amend(
        "i-1", amendment_id="am-1", new_price=Decimal("99.50"), new_quantity=Decimal(10)
    )
    assert outcome.decision.verdict is V.REJECT and outcome.status is None
    assert env.repo.amendments_for("i-1") == []


# ---------- R-01 评审补充 ----------


@pytest.mark.asyncio
async def test_amend_to_smaller_quantity_is_reduce_bound_to_new_quantity(env: Env) -> None:
    risk = engine(env)
    await risk.submit(env.make_intent())
    await env.pump()
    env.clock.advance(1)
    outcome = await risk.amend(
        "i-1", amendment_id="am-1", new_price=Decimal("99.50"), new_quantity=Decimal(6)
    )
    assert outcome.decision.verdict is V.REDUCE
    assert outcome.decision.approved_quantity == Decimal(6)
    assert outcome.status is S.SUBMITTED
    (order,) = await env.broker.open_orders()
    assert order.total_quantity == Decimal(6)


@pytest.mark.asyncio
async def test_amend_above_intent_quantity_is_rejected_before_rules(env: Env) -> None:
    risk = engine(env)
    await risk.submit(env.make_intent())
    await env.pump()
    outcome = await risk.amend(
        "i-1", amendment_id="am-1", new_price=Decimal("99.50"), new_quantity=Decimal(11)
    )
    assert outcome.decision.verdict is V.REJECT
    assert outcome.decision.rule_hits[0].rule_id == "amend_quantity"
    assert env.repo.amendments_for("i-1") == []


@pytest.mark.parametrize(
    ("amendment_id", "quantity"),
    [("am-1", Decimal(8)), ("am-2", Decimal(6))],  # 批准 am-1 改成 6：换数量、换改单都不行
)
@pytest.mark.asyncio
async def test_amend_decision_bound_to_quantity_and_amendment_id(
    env: Env, amendment_id: str, quantity: Decimal
) -> None:
    risk = engine(env)
    await risk.submit(env.make_intent())
    await env.pump()
    env.clock.advance(1)
    intent = env.repo.get_intent("i-1")
    assert intent is not None
    decision = await risk.decide(
        Subject(intent=intent, amendment_id="am-1", price=Decimal("99.50"), quantity=Decimal(6))
    )
    with pytest.raises(AmendRefusedError, match="绑定"):  # 只能由摘要核对拦下
        await env.om.request_amend(
            "i-1",
            amendment_id=amendment_id,
            new_price=Decimal("99.50"),
            new_quantity=quantity,
            decision=decision,
        )
    assert env.repo.amendments_for("i-1") == []


@pytest.mark.asyncio
async def test_halt_write_failure_still_halts_and_latches(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = modes(env)

    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(env.repo, "commit_mode_and_audits", broken)
    halting = FixedRule(RuleResult(verdict=V.HALT, message="账户不可信"))
    outcome = await engine(env, halting, manager=manager).submit(env.make_intent())
    assert outcome.decision.verdict is V.HALT and outcome.status is None
    after = await engine(env, manager=manager).submit(env.make_intent("i-2"))
    assert after.decision.rule_hits[0].rule_id == "mode_gate"  # 本进程已闩锁为拒绝
    assert env.repo.submitted_intents() == []


@pytest.mark.asyncio
async def test_unreadable_mode_state_rejects(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = modes(env)

    def broken() -> None:
        raise OSError("ledger unreadable")

    monkeypatch.setattr(manager, "current", broken)
    outcome = await engine(env, manager=manager).submit(env.make_intent())
    assert outcome.decision.rule_hits[0].rule_id == "mode_gate"


@dataclass
class BadRule:
    rule_id: str = "bad"
    version: str = "1"

    def check(self, subject: Subject, context: RiskContext) -> RuleResult | None:
        return "ALLOW"  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_rule_returning_garbage_is_rule_error(env: Env) -> None:
    risk = RiskEngine(
        env.repo,
        env.om,
        modes(env),
        rule_set=RuleSet(version="rs", rules=(BadRule(),)),
        context=Context(env),
        clock=env.clock,
    )
    outcome = await risk.submit(env.make_intent())
    assert outcome.decision.rule_hits[0].rule_id == "rule_error"


@pytest.mark.asyncio
async def test_fractional_amend_quantity_is_rejected_before_rules(env: Env) -> None:
    risk = engine(env)
    await risk.submit(env.make_intent())
    await env.pump()
    outcome = await risk.amend(
        "i-1", amendment_id="am-1", new_price=Decimal("99.50"), new_quantity=Decimal("6.5")
    )
    assert outcome.decision.rule_hits[0].rule_id == "amend_quantity"
    assert env.repo.amendments_for("i-1") == []
