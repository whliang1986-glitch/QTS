"""R-08：运行状态机与健康检查（NORMAL / DEGRADED / SAFE HALT）与恢复门槛。"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from server.domain.enums import Mode, OrderClass, OrderType, Side, TimeInForce
from server.domain.order_state import OrderStatus
from server.domain.orders import EMERGENCY_FLATTEN_SOURCE, PROTECTIVE_STOP_SOURCE
from server.risk.engine import RiskEngine, Subject
from server.risk.mode_manager import ModeConfig, ModeError, ModeManager
from server.risk.rulesets import build_rule_set
from server.risk.run_state import Component, Kind, RunState, RunStateError, RunStateMachine
from server.storage.errors import StorageError
from server.storage.ledger import Amendment
from tests.integration.risk.test_limit_rules_engine import PARAMS, Context
from tests.support.oms import ACCOUNT, Env

S = RunState
COMPONENTS = (
    Component("broker", Kind.ACCOUNT, critical=True, stale_after=timedelta(seconds=30)),
    Component("quotes", Kind.DATA, critical=True, stale_after=timedelta(seconds=30)),
    Component("news", Kind.DATA, critical=False, stale_after=timedelta(minutes=5)),
    Component("model", Kind.OTHER, critical=False, stale_after=timedelta(minutes=5)),
)


class Rig:
    def __init__(self, env: Env, **kw: Any) -> None:
        self.env = env
        self.modes = ModeManager(env.repo, clock=env.clock)
        self.modes.start(
            ModeConfig(
                requested_mode=Mode.PAPER, account_id=ACCOUNT.account_id,
                expected_account_type=ACCOUNT.account_type, risk_regime=ACCOUNT.account_type,
                allow_delayed_data=False, config_version="risk-test",
            ),
            ACCOUNT,
        )  # fmt: skip
        self.window = False
        self.reconciled = True
        self.rules: Any = build_rule_set(PARAMS)
        options: dict[str, Any] = {
            "components": COMPONENTS,
            "rule_set": lambda: self.rules,
            "reconciled": lambda: self.reconciled,
            "broker_window": lambda _now: self.window,
            "degraded_allowed": {"index": frozenset({"quotes"}), "ai": frozenset({"model"})},
            "clock": env.clock,
        }
        options.update(kw)
        self.machine = RunStateMachine(env.repo, env.om, self.modes, **options)

    def healthy(self, *names: str) -> None:
        for name in names or [c.name for c in COMPONENTS]:
            self.machine.report(name, ok=True)

    def halts(self) -> int:
        return len(self.env.repo.audits("halt"))


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> Env:
    e = Env(tmp_path)
    await e.broker.connect()
    await e.pump()
    return e


def subject(env: Env, strategy: str = "index", **kw: Any) -> Subject:
    return Subject.new_order(env.make_intent("probe", strategy_id=strategy, **kw))


def stop_subject(env: Env) -> Subject:
    return Subject.new_order(
        env.make_intent(
            "stop", side=Side.SELL, order_class=OrderClass.PROTECTIVE_STOP,
            order_type=OrderType.STOP, limit_price=None, stop_price=Decimal(95),
            quantity=Decimal(1000), time_in_force=TimeInForce.GTC,
            strategy_id=PROTECTIVE_STOP_SOURCE,
        )
    )  # fmt: skip


# ---------- 状态 ----------


def test_all_healthy_is_normal(env: Env) -> None:
    rig = Rig(env)
    rig.healthy()
    assessment = rig.machine.assess()
    assert assessment.state is S.NORMAL and rig.machine.gate(subject(env)) is None


def test_missing_or_stale_reports_count_as_failures(env: Env) -> None:
    rig = Rig(env)
    rig.healthy("broker", "quotes", "news")  # model 从未报告
    assert rig.machine.assess().failing == ("model",)
    env.clock.advance(31)  # broker、quotes 的报告过期
    assert rig.machine.assess().state is S.SAFE_HALT


def test_non_critical_failure_degrades_and_gates_by_strategy(env: Env) -> None:
    rig = Rig(env)
    rig.healthy()
    rig.machine.report("model", ok=False, detail="模型超时")
    assessment = rig.machine.assess()
    assert assessment.state is S.DEGRADED and assessment.failing == ("model",)
    assert rig.machine.gate(subject(env, "index")) is None  # 获准且不依赖 model
    assert "依赖的组件故障：model" in (rig.machine.gate(subject(env, "ai")) or "")
    assert "未获准" in (rig.machine.gate(subject(env, "momentum")) or "")
    assert rig.machine.gate(stop_subject(env)) is None  # 降风险单不受组件故障影响
    flatten = subject(env, EMERGENCY_FLATTEN_SOURCE, side=Side.SELL, order_class=OrderClass.REDUCE)
    assert rig.machine.gate(flatten) is None
    assert rig.halts() == 0  # 非关键故障不停机


def test_broker_window_blocks_every_new_order(env: Env) -> None:
    rig = Rig(env)
    rig.healthy()
    rig.window = True
    assert rig.machine.assess().state is S.DEGRADED
    assert rig.machine.gate(subject(env)) == "券商重启窗口内不新增订单"
    assert rig.machine.gate(stop_subject(env)) == "券商重启窗口内不新增订单"
    rig.window = False
    assert rig.machine.assess().state is S.NORMAL


def test_critical_failure_is_persisted_safe_halt_written_once(env: Env) -> None:
    rig = Rig(env)
    rig.healthy()
    rig.machine.report("broker", ok=False, detail="断线")
    assert rig.machine.assess().state is S.SAFE_HALT
    assert rig.machine.assess().state is S.SAFE_HALT
    assert rig.halts() == 1  # 同一组故障只写一次
    assert not rig.modes.risk_reducing_allowed()  # 是 SAFE HALT，不是普通 HALT
    rig.machine.report("quotes", ok=False)  # 又一个关键组件故障：再写一次
    rig.machine.assess()
    assert rig.halts() == 2


def test_recovery_of_components_does_not_lift_safe_halt(env: Env) -> None:
    rig = Rig(env)
    rig.healthy()
    rig.machine.report("broker", ok=False)
    rig.machine.assess()
    rig.healthy()  # 网络恢复
    assessment = rig.machine.assess()
    assert assessment.state is S.SAFE_HALT  # 持久化的 SAFE HALT 不自动解除
    assert rig.machine.gate(subject(env)) is not None


def test_assessment_failure_is_safe_halt(env: Env) -> None:
    def broken(_now: Any) -> bool:
        raise RuntimeError("calendar")

    rig = Rig(env, broker_window=broken)
    rig.healthy()
    assessment = rig.machine.assess()
    assert assessment.state is S.SAFE_HALT and "无法评估" in assessment.reason


def test_configuration_is_validated(env: Env) -> None:
    with pytest.raises(ValueError, match="重复"):
        Rig(env, components=(*COMPONENTS, COMPONENTS[0]))
    with pytest.raises(ValueError, match="未登记"):
        Rig(env, degraded_allowed={"x": frozenset({"nope"})})
    with pytest.raises(RunStateError):
        Rig(env).machine.report("nope", ok=True)


# ---------- 恢复门槛 ----------


def _halted_rig(env: Env) -> Rig:
    rig = Rig(env)
    rig.healthy()
    rig.modes.halt("测试", actor="dylan")
    return rig


def test_resume_when_every_check_passes(env: Env) -> None:
    rig = _halted_rig(env)
    checks = rig.machine.resume_checks()
    assert all(checks.model_dump().values())
    rig.machine.resume(actor="dylan")
    state = rig.modes.current()
    assert state is not None and not state.halt.halted and state.mode is Mode.PAPER


@pytest.mark.parametrize(
    ("setup", "field"),
    [
        (lambda r: r.machine.report("news", ok=False), "data_ok"),
        (lambda r: r.machine.report("broker", ok=False), "account_ok"),
        (lambda r: setattr(r, "rules", None), "risk_ok"),
        (lambda r: setattr(r, "reconciled", False), "reconciled"),
    ],
)
def test_each_failing_check_blocks_resume(env: Env, setup: Any, field: str) -> None:
    rig = _halted_rig(env)
    setup(rig)
    checks = rig.machine.resume_checks().model_dump()
    assert [k for k, v in checks.items() if not v] == [field]
    with pytest.raises(ModeError, match=field):
        rig.machine.resume(actor="dylan")


@pytest.mark.asyncio
async def test_unresolved_orders_and_amendments_block_resume(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _halted_rig(env)
    await env.submit("o-1")
    await env.pump()
    assert rig.machine.resume_checks().orders_ok
    monkeypatch.setattr(env.om, "status", lambda _i: OrderStatus.UNKNOWN)
    assert not rig.machine.resume_checks().orders_ok
    monkeypatch.undo()
    pending = [Amendment("a1", "o-1", "d", Decimal(99), Decimal(8), "t", None)]
    monkeypatch.setattr(env.repo, "amendments_for", lambda _i: pending)
    assert not rig.machine.resume_checks().orders_ok


def test_database_and_check_errors_fail_closed(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _halted_rig(env)

    def broken() -> None:
        raise StorageError("trigger missing")

    monkeypatch.setattr(env.repo, "check_integrity", broken)
    assert not rig.machine.resume_checks().database_ok

    def boom() -> bool:
        raise RuntimeError("x")

    rig.machine._reconciled = boom
    assert not rig.machine.resume_checks().reconciled


def test_resume_needs_a_human(env: Env) -> None:
    rig = _halted_rig(env)
    with pytest.raises(ModeError, match="人工"):
        rig.machine.resume(actor="tradingd")


def test_resume_refused_before_startup_verification(env: Env) -> None:
    rig = Rig(env)
    unverified = RunStateMachine(
        env.repo, env.om, ModeManager(env.repo, clock=env.clock), components=COMPONENTS,
        rule_set=lambda: rig.rules, reconciled=lambda: True, broker_window=lambda _n: False,
        degraded_allowed={}, clock=env.clock,
    )  # fmt: skip
    with pytest.raises(RunStateError, match="启动校验"):
        unverified.resume(actor="dylan")


# ---------- 接入风控 ----------


@pytest.mark.asyncio
async def test_risk_engine_rejects_through_the_run_gate(env: Env) -> None:
    rig = Rig(env)
    rig.healthy()
    rig.machine.report("model", ok=False)
    engine = RiskEngine(
        env.repo, env.om, rig.modes, rule_set=lambda: build_rule_set(PARAMS),
        context=Context(env), clock=env.clock, run_gate=rig.machine.gate,
    )  # fmt: skip
    outcome = await engine.submit(env.make_intent("o-1", strategy_id="momentum"))
    assert [h.rule_id for h in outcome.decision.rule_hits] == ["run_state"]
    assert outcome.status is None


@pytest.mark.asyncio
async def test_broken_run_gate_fails_closed(env: Env) -> None:
    rig = Rig(env)

    def broken(_s: Subject) -> str | None:
        raise RuntimeError("gate")

    engine = RiskEngine(
        env.repo, env.om, rig.modes, rule_set=lambda: build_rule_set(PARAMS),
        context=Context(env), clock=env.clock, run_gate=broken,
    )  # fmt: skip
    outcome = await engine.submit(env.make_intent("o-1"))
    assert [h.rule_id for h in outcome.decision.rule_hits] == ["run_state"]


def test_plain_halt_is_not_safe_halt(env: Env) -> None:
    rig = Rig(env)
    rig.healthy()
    rig.modes.halt("人工急停", actor="dylan")
    # 普通 HALT 由模式门拦新增风险；运行状态仍可信，降风险单（保护性止损）照常放行
    assert rig.machine.assess().state is S.NORMAL
    assert rig.machine.gate(stop_subject(env)) is None


# ---------- 评审补充 ----------


def test_safe_halt_is_rewritten_after_an_outside_resume(env: Env) -> None:
    rig = Rig(env)
    rig.healthy()
    rig.machine.report("broker", ok=False)
    rig.machine.assess()
    rig.machine.report("model", ok=False)  # 非关键仍故障：恢复后是 DEGRADED，不会回到 NORMAL
    rig.machine.report("broker", ok=True)
    rig.modes.resume(Mode.PAPER, actor="dylan", checks=rig.machine.resume_checks().model_copy())
    assert rig.machine.assess().state is S.DEGRADED
    halts = rig.halts()
    rig.machine.report("broker", ok=False)  # 同一组件再次故障
    rig.machine.assess()
    assert rig.halts() == halts + 1  # 账本已恢复过：必须重新持久化
    state = rig.modes.current()
    assert state is not None and state.halt.halted


def test_shrinking_critical_failures_do_not_rewrite(env: Env) -> None:
    rig = Rig(env)
    rig.healthy()
    rig.machine.report("broker", ok=False)
    rig.machine.report("quotes", ok=False)
    rig.machine.assess()
    rig.machine.report("quotes", ok=True)
    rig.machine.assess()
    assert rig.halts() == 1


@pytest.mark.parametrize("answer", [None, 1, "yes"])
def test_non_boolean_window_answer_fails_closed(env: Env, answer: Any) -> None:
    rig = Rig(env, broker_window=lambda _now: answer)
    rig.healthy()
    assert rig.machine.assess().state is S.SAFE_HALT
    assert rig.machine.gate(subject(env)) is not None


def test_reports_must_be_boolean_and_not_from_the_future(env: Env) -> None:
    rig = Rig(env)
    with pytest.raises(RunStateError, match="布尔"):
        rig.machine.report("broker", ok=1)  # type: ignore[arg-type]
    rig.healthy()
    env.clock.advance(-10)  # 时钟回拨：报告落在未来，不可信
    assert "broker" in rig.machine.assess().failing


def test_critical_other_component_blocks_resume(env: Env) -> None:
    critical_model = (*COMPONENTS[:3], Component("model", Kind.OTHER, critical=True,
                                                 stale_after=timedelta(minutes=5)))  # fmt: skip
    rig = Rig(env, components=critical_model)
    rig.healthy("broker", "quotes", "news")
    rig.modes.halt("x", actor="dylan")
    assert not rig.machine.resume_checks().data_ok


def test_safe_halt_written_by_another_module_is_respected(env: Env) -> None:
    rig = Rig(env)
    rig.healthy()
    rig.modes.safe_halt("对账发现差异", actor="tradingd")
    assessment = rig.machine.assess()
    assert assessment.state is S.SAFE_HALT and rig.machine.gate(stop_subject(env)) is not None


def test_order_check_errors_fail_closed(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _halted_rig(env)

    def broken() -> list[str]:
        raise RuntimeError("ledger")

    monkeypatch.setattr(env.repo, "submitted_intents", broken)
    assert not rig.machine.resume_checks().orders_ok


@pytest.mark.asyncio
async def test_submitting_and_unknown_amendment_block_resume(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _halted_rig(env)
    await env.submit("o-1")
    await env.pump()
    monkeypatch.setattr(env.om, "status", lambda _i: OrderStatus.SUBMITTING)
    assert not rig.machine.resume_checks().orders_ok
    monkeypatch.undo()
    unknown = [Amendment("a1", "o-1", "d", Decimal(99), Decimal(8), "t", "UNKNOWN")]
    monkeypatch.setattr(env.repo, "amendments_for", lambda _i: unknown)
    assert not rig.machine.resume_checks().orders_ok
    monkeypatch.setattr(env.om, "status", lambda _i: OrderStatus.CANCELLED)
    assert rig.machine.resume_checks().orders_ok  # 终态订单的旧改单不挡恢复


def test_amendment_of_protective_stop_is_not_risk_reducing(env: Env) -> None:
    rig = Rig(env)
    rig.healthy()
    rig.machine.report("model", ok=False)
    stop = stop_subject(env).intent
    amend = Subject(intent=stop, amendment_id="am", price=Decimal(96), quantity=Decimal(1000))
    assert rig.machine.gate(amend) is not None  # 改单不享受降风险放行
