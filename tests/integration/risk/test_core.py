"""S-01a：交易核心（启动顺序、意图入口、命令去重、串行化、例行任务）。真实账本 + FakeBroker。"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from server.domain.enums import Mode, OrderClass, OrderType, RiskVerdict, Side, TimeInForce
from server.domain.orders import EMERGENCY_FLATTEN_SOURCE, PROTECTIVE_STOP_SOURCE
from server.risk.config import parse_health_config
from server.risk.context import LedgerContextProvider
from server.risk.core import IntakeError, TradingCore
from server.risk.emergency import EmergencyControls
from server.risk.engine import RiskEngine
from server.risk.mode_manager import ModeConfig, ModeManager, StartupRefusedError
from server.risk.reconcile import ADOPT_PHRASE, Reconciler, ReconKind
from server.risk.rulesets import build_rule_set
from server.risk.run_state import RunStateMachine
from server.storage.config import ConfigError, load_toml
from server.storage.ledger import Amendment
from tests.integration.risk.test_context import Quotes
from tests.integration.risk.test_limit_rules_engine import PARAMS
from tests.integration.risk.test_reconcile import Actions
from tests.support.oms import ACCOUNT, Env

ROOT = Path(__file__).resolve().parents[3]
HEALTH = ROOT / "config/runtime/health.toml"
V = RiskVerdict


def config(**kw: Any) -> ModeConfig:
    data: dict[str, Any] = {
        "requested_mode": Mode.PAPER, "account_id": ACCOUNT.account_id,
        "expected_account_type": ACCOUNT.account_type, "risk_regime": ACCOUNT.account_type,
        "allow_delayed_data": False, "config_version": "core-test",
    }  # fmt: skip
    data.update(kw)
    return ModeConfig.model_validate(data)


class Rig:
    def __init__(self, env: Env, mode_config: ModeConfig | None = None) -> None:
        self.env = env
        self.modes = ModeManager(env.repo, clock=env.clock)
        self.quotes = Quotes(env)
        self.untrusted: list[str] = []
        self.provider = LedgerContextProvider(
            env.repo, env.om, account=env.broker, broker_orders=env.broker, quotes=self.quotes,
            instruments={"SPY": env.om._instruments["SPY"]},
            account_synced=lambda: True, clock_skew_ms=self._skew,
            start_of_day_nav=lambda _a: Decimal(200000),
            strategy_drawdown=lambda: {"s": Decimal(0)},
            stop_cooldown=dict, delayed_data_allowed=self.modes.delayed_data_allowed,
            on_untrusted=self.untrusted.append, clock=env.clock,
        )  # fmt: skip
        self.recon = Reconciler(
            env.repo, env.om, self.modes, account=env.broker, broker_orders=env.broker,
            executions=env.broker, corporate_actions=Actions(), clock=env.clock,
        )  # fmt: skip
        components = parse_health_config(load_toml(HEALTH, name="health"))
        self.run_state = RunStateMachine(
            env.repo, env.om, self.modes, components=components,
            rule_set=lambda: build_rule_set(PARAMS), reconciled=self.recon.last_clean,
            broker_window=lambda _now: False, degraded_allowed={}, clock=env.clock,
        )  # fmt: skip
        self.engine = RiskEngine(
            env.repo, env.om, self.modes, rule_set=lambda: build_rule_set(PARAMS),
            context=self.provider, clock=env.clock, run_gate=self.run_state.gate,
        )  # fmt: skip
        self.emergency = EmergencyControls(
            env.repo, env.om, self.modes, self.engine, context=self.provider,
            parameters=lambda: PARAMS, clock=env.clock,
        )  # fmt: skip
        self.core = TradingCore(
            env.repo, env.om, self.modes, mode_config=mode_config or config(),
            broker_account=env.broker.account, engine=self.engine, reconciler=self.recon,
            run_state=self.run_state, emergency=self.emergency, context=self.provider,
            clock=env.clock,
        )  # fmt: skip

    async def _skew(self) -> int | None:
        return 0

    def healthy(self) -> None:
        for name in ("broker", "account_sync", "quotes", "news", "model"):
            self.run_state.report(name, ok=True)

    async def bring_up(self) -> None:
        """首次启动：对账发现无基准 → SAFE HALT；人工采用基准、对账、恢复。"""
        self.healthy()
        report = await self.core.start()
        assert report.recovered and not report.reconciled
        await self.recon.adopt_broker_positions(actor="dylan", confirmation=ADOPT_PHRASE)
        assert (await self.core.command("r-recon", "reconcile", {}, actor="dylan")).detail[
            "settled"
        ]
        resumed = await self.core.command("r-resume", "resume", {}, actor="dylan")
        assert resumed.ok, resumed.detail


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> Env:
    e = Env(tmp_path)
    await e.broker.connect()
    await e.pump()
    return e


def reduce_intent(env: Env, intent_id: str = "r-1", **kw: Any) -> Any:
    data: dict[str, Any] = {"side": Side.SELL, "order_class": OrderClass.REDUCE,
                            "limit_price": Decimal(100)}  # fmt: skip
    data.update(kw)
    return env.make_intent(intent_id, **data)


# ---------- 启动 ----------


@pytest.mark.asyncio
async def test_nothing_is_accepted_before_start(env: Env) -> None:
    rig = Rig(env)
    with pytest.raises(IntakeError, match="尚未完成启动"):
        await rig.core.submit_intent(reduce_intent(env))
    with pytest.raises(IntakeError, match="尚未完成启动"):
        await rig.core.command("r-1", "cancel_open_orders", {}, actor="dylan")
    # 急停例外：启动之前也能执行
    stopped = await rig.core.command("r-0", "stop_new_orders", {"reason": "启动前"}, actor="dylan")
    assert stopped.ok
    state = rig.modes.current()
    assert state is not None and state.halt.halted


@pytest.mark.asyncio
async def test_full_bring_up_then_orders_flow(env: Env) -> None:
    rig = Rig(env)
    await rig.bring_up()
    outcome = await rig.core.submit_intent(reduce_intent(env))
    assert outcome.decision.verdict is V.ALLOW, outcome.decision.rule_hits


@pytest.mark.asyncio
async def test_startup_refusal_propagates_and_core_stays_closed(env: Env) -> None:
    rig = Rig(env, mode_config=config(account_id="DU0000002"))
    with pytest.raises(StartupRefusedError):
        await rig.core.start()
    assert not rig.core.ready


@pytest.mark.asyncio
async def test_persisted_halt_survives_restart(env: Env) -> None:
    rig = Rig(env)
    await rig.bring_up()
    await rig.core.command("r-halt", "stop_new_orders", {"reason": "收盘前急停"}, actor="dylan")
    again = Rig(env)  # 进程重启：新的内存状态，同一账本
    again.healthy()
    await again.core.start()
    state = again.modes.current()
    assert state is not None and state.halt.halted
    outcome = await again.core.submit_intent(reduce_intent(env, "r-2"))
    assert outcome.decision.verdict is V.REJECT


# ---------- 意图入口 ----------


@pytest.mark.asyncio
async def test_reserved_identities_and_foreign_accounts_are_refused(env: Env) -> None:
    rig = Rig(env)
    await rig.bring_up()
    for strategy in (PROTECTIVE_STOP_SOURCE, EMERGENCY_FLATTEN_SOURCE):
        with pytest.raises(IntakeError, match="保护性止损或紧急平仓"):
            await rig.core.submit_intent(reduce_intent(env, f"x-{strategy}", strategy_id=strategy))
    stop = env.make_intent(
        "stop", side=Side.SELL, order_class=OrderClass.PROTECTIVE_STOP, order_type=OrderType.STOP,
        limit_price=None, stop_price=Decimal(95), quantity=Decimal(1000),
        time_in_force=TimeInForce.GTC, strategy_id="momentum",
    )  # fmt: skip
    with pytest.raises(IntakeError, match="保护性止损或紧急平仓"):
        await rig.core.submit_intent(stop)
    with pytest.raises(IntakeError, match="账户"):
        await rig.core.submit_intent(reduce_intent(env, "x-acct", account_id="DU0000002"))
    assert env.repo.submitted_intents() == []


@pytest.mark.asyncio
async def test_intents_are_serialized(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(env)
    await rig.bring_up()
    active = 0
    peak = 0
    real = rig.engine.submit

    async def tracked(intent: Any) -> Any:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        try:
            return await real(intent)
        finally:
            active -= 1

    monkeypatch.setattr(rig.engine, "submit", tracked)
    await asyncio.gather(*(rig.core.submit_intent(reduce_intent(env, f"r-{i}")) for i in range(4)))
    assert peak == 1


# ---------- 命令 ----------


@pytest.mark.asyncio
async def test_commands_are_deduplicated_by_request_id(env: Env) -> None:
    rig = Rig(env)
    await rig.bring_up()
    first = await rig.core.command("r-h", "stop_new_orders", {"reason": "x"}, actor="dylan")
    again = await rig.core.command("r-h", "stop_new_orders", {"reason": "x"}, actor="dylan")
    assert first.ok and not first.replayed and again.replayed
    assert len(env.repo.audits("halt")) == 2  # 启动对账的 SAFE HALT + 这一次；重试没有再写
    with pytest.raises(IntakeError, match="命令内容或操作者不同"):
        await rig.core.command("r-h", "stop_new_orders", {"reason": "y"}, actor="dylan")
    with pytest.raises(IntakeError, match="未知命令"):
        await rig.core.command("r-x", "self_destruct", {}, actor="dylan")


@pytest.mark.asyncio
async def test_refused_command_result_is_recorded_and_replayed(env: Env) -> None:
    rig = Rig(env)
    rig.healthy()
    await rig.core.start()  # 没有基准：SAFE HALT，对账未通过
    refused = await rig.core.command("r-resume", "resume", {}, actor="dylan")
    assert not refused.ok and "reconciled" in refused.detail["error"]
    replay = await rig.core.command("r-resume", "resume", {}, actor="dylan")
    assert replay.replayed and not replay.ok
    stored = env.repo.command_result("r-resume")
    assert stored is not None and json.loads(stored.result)["ok"] is False


@pytest.mark.asyncio
async def test_emergency_commands_available_through_core(env: Env) -> None:
    rig = Rig(env)
    await rig.bring_up()
    preview = await rig.core.command("r-p", "preview_flatten", {}, actor="dylan")
    assert preview.ok and preview.detail["lines"][0]["symbol"] == "SPY"
    cancel = await rig.core.command("r-c", "cancel_open_orders", {}, actor="dylan")
    assert cancel.ok and cancel.detail["orders"] == []


@pytest.mark.asyncio
async def test_resume_resets_untrusted_reports(env: Env) -> None:
    rig = Rig(env)
    await rig.bring_up()
    rig.provider._reported.add(("x", ()))
    await rig.core.command("r-h2", "stop_new_orders", {}, actor="dylan")
    await rig.core.command("r-recon2", "reconcile", {}, actor="dylan")
    resumed = await rig.core.command("r-resume2", "resume", {}, actor="dylan")
    assert resumed.ok, resumed.detail
    assert rig.provider._reported == set()


# ---------- 例行任务 ----------


@pytest.mark.asyncio
async def test_events_are_processed_under_the_lock(env: Env) -> None:
    rig = Rig(env)
    await rig.bring_up()
    outcome = await rig.core.submit_intent(reduce_intent(env))
    assert outcome.decision.verdict is V.ALLOW

    async def stream() -> Any:
        while not env.broker._queue.empty():
            yield env.broker._queue.get_nowait()

    await rig.core.handle_events(stream())
    assert env.om.status("r-1").value == "SUBMITTED"


@pytest.mark.asyncio
async def test_assess_errors_become_safe_halt(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(env)
    await rig.bring_up()

    def broken() -> Any:
        raise RuntimeError("x")

    monkeypatch.setattr(rig.run_state, "assess", broken)
    assert rig.core.assess() == "SAFE_HALT"
    assert not rig.modes.risk_reducing_allowed()


@pytest.mark.asyncio
async def test_intraday_reconcile_resolves_pending_amendments_first(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:

    rig = Rig(env)
    await rig.bring_up()
    await rig.core.submit_intent(reduce_intent(env))
    await env.pump()
    pending = [Amendment("a1", "r-1", "d", Decimal(99), Decimal(10), "t", "UNKNOWN")]
    monkeypatch.setattr(env.repo, "amendments_for", lambda _i: pending)
    resolved: list[str] = []

    async def resolve(intent_id: str, **_kw: Any) -> None:
        resolved.append(intent_id)

    monkeypatch.setattr(env.om, "resolve_amendments", resolve)
    await rig.core.reconcile(ReconKind.INTRADAY)
    assert resolved == ["r-1"]


# ---------- 组件登记配置 ----------


def test_repository_health_config_loads_as_agreed() -> None:
    components = parse_health_config(load_toml(ROOT / "config/runtime/health.toml", name="health"))
    critical = {c.name: (c.critical, c.stale_after.total_seconds()) for c in components}
    assert critical == {
        "broker": (True, 30), "account_sync": (True, 30), "quotes": (True, 30),
        "news": (False, 300), "model": (False, 300),
    }  # fmt: skip


@pytest.mark.parametrize(
    "body",
    [
        '[components.x]\nkind = "DATA"\ncritical = true\n',  # 缺时效
        '[components.x]\nkind = "NOPE"\ncritical = true\nstale_after_seconds = 30\n',
        '[components.x]\nkind = "DATA"\ncritical = 1\nstale_after_seconds = 30\n',
        '[components.x]\nkind = "DATA"\ncritical = true\nstale_after_seconds = 0\n',
        '[components.x]\nkind = "DATA"\ncritical = true\nstale_after_seconds = 1.5\n',
        '[components.x]\nkind = "DATA"\ncritical = true\nstale_after_seconds = 30\nextra = 1\n',
        "[components]\n",
        "[other]\n",
    ],
)
def test_bad_health_config_is_refused(tmp_path: Path, body: str) -> None:
    path = tmp_path / "health.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError):
        parse_health_config(load_toml(path, name="health"))


@pytest.mark.asyncio
async def test_startup_reports_oms_recovery_failure(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    rig.healthy()

    async def failed() -> bool:
        return False

    monkeypatch.setattr(env.om, "recover", failed)
    report = await rig.core.start()
    assert not report.recovered  # 查单没完成：OMS 拒绝新单，报告如实反映


# ---------- 评审补充 ----------


@pytest.mark.asyncio
async def test_event_handling_failure_halts_before_any_new_order(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    await rig.bring_up()

    async def broken(_event: Any) -> None:
        raise RuntimeError("ledger write failed")

    async def stream() -> Any:
        yield object()

    monkeypatch.setattr(env.om, "on_broker_event", broken)
    with pytest.raises(RuntimeError):
        await rig.core.handle_events(stream())
    assert not rig.modes.risk_reducing_allowed()  # SAFE HALT
    outcome = await rig.core.submit_intent(reduce_intent(env, "r-after"))
    assert outcome.decision.verdict is V.REJECT


@pytest.mark.asyncio
async def test_stop_is_not_queued_behind_other_commands(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    await rig.bring_up()
    gate = asyncio.Event()
    real = rig.recon.reconcile

    async def slow(kind: Any) -> Any:
        await gate.wait()
        return await real(kind)

    monkeypatch.setattr(rig.recon, "reconcile", slow)
    busy = asyncio.create_task(rig.core.command("r-slow", "reconcile", {}, actor="dylan"))
    await asyncio.sleep(0)
    stopped = await rig.core.command("r-stop", "stop_new_orders", {}, actor="dylan")
    assert stopped.ok and not busy.done()
    state = rig.modes.current()
    assert state is not None and state.halt.halted  # 对账还卡着，急停已经落盘
    gate.set()
    await busy


@pytest.mark.asyncio
async def test_stop_retry_does_not_write_another_halt(env: Env) -> None:
    rig = Rig(env)
    await rig.bring_up()
    await rig.core.command("r-s", "stop_new_orders", {}, actor="dylan")
    halts = len(env.repo.audits("halt"))
    replay = await rig.core.command("r-s", "stop_new_orders", {}, actor="dylan")
    assert replay.replayed and len(env.repo.audits("halt")) == halts


@pytest.mark.parametrize(
    ("request_id", "name", "params", "actor"),
    [
        ("", "reconcile", {}, "dylan"),
        ("r", 1, {}, "dylan"),
        ("r", "stop_new_orders", {"reason": 123}, "dylan"),
        ("r", "reconcile", {1: "x"}, "dylan"),
        ("r", "reconcile", [], "dylan"),
        ("r", "reconcile", {}, None),
    ],
)
@pytest.mark.asyncio
async def test_malformed_commands_are_refused(
    env: Env, request_id: Any, name: Any, params: Any, actor: Any
) -> None:
    rig = Rig(env)
    await rig.bring_up()
    with pytest.raises(IntakeError):
        await rig.core.command(request_id, name, params, actor=actor)


@pytest.mark.asyncio
async def test_same_request_by_another_actor_conflicts(env: Env) -> None:
    rig = Rig(env)
    await rig.bring_up()
    await rig.core.command("r-x", "reconcile", {}, actor="dylan")
    with pytest.raises(IntakeError, match="操作者"):
        await rig.core.command("r-x", "reconcile", {}, actor="someone")


@pytest.mark.asyncio
async def test_unexpected_errors_are_not_recorded(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    await rig.bring_up()

    async def boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("broker down")

    monkeypatch.setattr(rig.emergency, "cancel_open_orders", boom)
    with pytest.raises(RuntimeError):
        await rig.core.command("r-c", "cancel_open_orders", {}, actor="dylan")
    assert env.repo.command_result("r-c") is None  # 允许重试


@pytest.mark.asyncio
async def test_resume_needs_a_human_through_the_core(env: Env) -> None:
    rig = Rig(env)
    rig.healthy()
    await rig.core.start()
    await rig.recon.adopt_broker_positions(actor="dylan", confirmation=ADOPT_PHRASE)
    await rig.core.command("r-recon", "reconcile", {}, actor="dylan")
    refused = await rig.core.command("r-res", "resume", {}, actor="tradingd")
    assert not refused.ok and "人工" in refused.detail["error"]


@pytest.mark.asyncio
async def test_start_twice_is_refused(env: Env) -> None:
    rig = Rig(env)
    rig.healthy()
    await rig.core.start()
    with pytest.raises(IntakeError, match="已启动"):
        await rig.core.start()


def test_core_requires_a_run_gated_engine(env: Env) -> None:
    rig = Rig(env)
    bare = RiskEngine(
        env.repo, env.om, rig.modes, rule_set=lambda: build_rule_set(PARAMS),
        context=rig.provider, clock=env.clock,
    )  # fmt: skip
    with pytest.raises(ValueError, match="运行状态门禁"):
        TradingCore(
            env.repo, env.om, rig.modes, mode_config=config(),
            broker_account=env.broker.account, engine=bare, reconciler=rig.recon,
            run_state=rig.run_state, emergency=rig.emergency, clock=env.clock,
        )  # fmt: skip


@pytest.mark.asyncio
async def test_reserved_intent_id_prefix_is_refused(env: Env) -> None:
    rig = Rig(env)
    await rig.bring_up()
    with pytest.raises(IntakeError):
        await rig.core.submit_intent(reduce_intent(env, "flatten-abc-SPY"))


@pytest.mark.asyncio
async def test_intraday_preprocessing_errors_still_reconcile(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    await rig.bring_up()
    called: list[Any] = []
    real = rig.recon.reconcile

    async def spy(kind: Any) -> Any:
        called.append(kind)
        return await real(kind)

    def broken(_i: str) -> Any:
        raise RuntimeError("ledger")

    monkeypatch.setattr(rig.recon, "reconcile", spy)
    await rig.core.submit_intent(reduce_intent(env))
    monkeypatch.setattr(env.repo, "amendments_for", broken)
    await rig.core.reconcile(ReconKind.INTRADAY)
    assert called == [ReconKind.INTRADAY]


def test_health_config_must_keep_required_critical_components(tmp_path: Path) -> None:
    body = HEALTH.read_text(encoding="utf-8").replace(
        '[components.quotes]        # 实时行情\nkind = "DATA"\ncritical = true',
        '[components.quotes]        # 实时行情\nkind = "DATA"\ncritical = false',
    )
    assert "critical = false\nstale_after_seconds = 30" in body
    path = tmp_path / "health.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError, match="quotes"):
        parse_health_config(load_toml(path, name="health"))
    bad_kind = tmp_path / "kind.toml"
    bad_kind.write_text(
        HEALTH.read_text(encoding="utf-8").replace('kind = "OTHER"', "kind = [1]"), encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        parse_health_config(load_toml(bad_kind, name="health"))


def test_critical_flag_must_be_a_real_boolean(tmp_path: Path) -> None:
    body = HEALTH.read_text(encoding="utf-8").replace(
        '[components.broker]        # 券商连接\nkind = "ACCOUNT"\ncritical = true',
        '[components.broker]        # 券商连接\nkind = "ACCOUNT"\ncritical = 1',
    )
    assert "critical = 1" in body
    path = tmp_path / "health.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError, match="broker"):
        parse_health_config(load_toml(path, name="health"))


@pytest.mark.asyncio
async def test_resume_waits_for_in_flight_work(env: Env) -> None:
    rig = Rig(env)
    rig.healthy()
    await rig.core.start()
    await rig.recon.adopt_broker_positions(actor="dylan", confirmation=ADOPT_PHRASE)
    await rig.core.command("r-recon", "reconcile", {}, actor="dylan")
    await rig.core._lock.acquire()
    task = asyncio.create_task(rig.core.command("r-res", "resume", {}, actor="dylan"))
    await asyncio.sleep(0.01)
    assert not task.done()  # 恢复在锁外不会抢先执行
    rig.core._lock.release()
    assert (await task).ok


@pytest.mark.asyncio
async def test_expired_intents_are_refused_by_tradingd_itself(env: Env) -> None:
    """不信任 workerd 的过期判断：tradingd 按自己的时钟拒绝已过有效期的意图（S-01b 评审 P1-4）。"""
    rig = Rig(env)
    await rig.bring_up()
    until = env.clock() + timedelta(seconds=30)
    late = reduce_intent(env, "late", time_in_force=TimeInForce.GTD, expires_at=until)
    on_time = reduce_intent(env, "ok", time_in_force=TimeInForce.GTD, expires_at=until, quantity=1)
    assert (await rig.core.submit_intent(on_time)).decision.verdict is V.ALLOW
    env.clock.advance(30)
    with pytest.raises(IntakeError, match="有效期"):
        await rig.core.submit_intent(late)
    assert env.repo.risk_decisions_for("late") == []
