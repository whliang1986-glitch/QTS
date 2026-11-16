"""T-09b 集成测试：策略库只追加存储、重放校验、证据核验、整策略归档、交易核心准入与 API。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from server.api.app import ApiDeps, create_app
from server.api.models import StrategyArchiveResponse
from server.api.security import MemoryAudit
from server.backtest import BACKTEST_COMPONENTS
from server.data.calendar import TradingCalendar
from server.domain.enums import Mode
from server.domain.strategy_lifecycle import Actor, ActorKind, Evidence, LifecycleError, Stage
from server.risk.assembly import CoreInputs, build_trading_core
from server.risk.core import IntakeError
from server.risk.mode_manager import ModeConfig
from server.storage.experiments import ExperimentRecord, ExperimentRegistry
from server.storage.strategy_registry import StrategyRegistry
from tests.integration.risk.test_context import Quotes
from tests.integration.risk.test_limit_rules_engine import PARAMS
from tests.integration.risk.test_reconcile import Actions
from tests.support.oms import ACCOUNT, SPY, Env
from tests.unit.api.test_app import Gateway, Tokens

T0 = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
CALENDAR = TradingCalendar.nyse(start=date(2026, 10, 1), end=date(2026, 11, 30))


def test_strategy_registry_lifecycle_evidence_and_whole_id_archive(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    meta_db = tmp_path / "meta.db"
    gates_dir = tmp_path / "gates"
    gates_dir.mkdir()
    exp_reg = ExperimentRegistry(meta_db)
    audits: list[tuple[str, str, dict[str, object]]] = []
    reg = StrategyRegistry(
        meta_db,
        experiments=exp_reg,
        gates_dir=gates_dir,
        audit_sink=lambda act, who, det: audits.append((act, who, det)),
    )

    owner = Actor(ActorKind.OWNER, "dylan")
    ai = Actor(ActorKind.AI, "research_agent")
    sys_actor = Actor(ActorKind.SYSTEM, "auto_pipeline")

    # 1. AI 创建 v1 并推进到 HYPOTHESIS（AI 上限）
    r = reg.create_version(
        strategy_id="mom_rot",
        version="v1",
        code_hash="code-1",
        params_hash="param-1",
        actor=ai,
        at=T0,
    )
    assert r.stage is Stage.IDEA
    r = reg.promote_version(strategy_id="mom_rot", version="v1", actor=ai, at=T0)
    assert r.stage is Stage.HYPOTHESIS
    with pytest.raises(LifecycleError, match="AI 不能"):
        reg.promote_version(strategy_id="mom_rot", version="v1", actor=ai, at=T0)

    # 2. SYSTEM 推进到 DATA_AUDIT -> BASELINE
    r = reg.promote_version(strategy_id="mom_rot", version="v1", actor=sys_actor, at=T0)
    assert r.stage is Stage.DATA_AUDIT
    r = reg.promote_version(strategy_id="mom_rot", version="v1", actor=sys_actor, at=T0)
    assert r.stage is Stage.BASELINE

    # 3. 推进到 BACKTEST 必须核验 ExperimentRegistry 中真实存在的实验记录
    bad_ev = Evidence(
        data_version="dv1",
        commit="c1",
        code_hash="code-1",
        params_hash="param-1",
        cost_model="us_etf_full_v1",
        evaluation_id="exp-missing",
    )
    with pytest.raises(LifecycleError, match="评测编号不存在"):
        reg.promote_version(
            strategy_id="mom_rot", version="v1", actor=sys_actor, at=T0, evidence=bad_ev
        )

    exp_reg.register(
        ExperimentRecord(
            experiment_id="exp-ok",
            strategy_id="mom_rot",
            strategy_version="v1",
            recorded_at=T0,
            start_day=date(2010, 1, 1),
            end_day=date(2025, 12, 31),
            is_out_of_sample=False,
            data_version="dv1",
            code_commit="c1",
            params_hash="param-1",
            cost_model="us_etf_full_v1",
            sharpe=Decimal("1.10"),
            annualized_return=Decimal("0.11"),
            max_drawdown=Decimal("0.14"),
            rebalance_count=180,
            survivorship_bias_free=False,
            artifact_dir="backtests/exp-ok",
            manifest={},
        )
    )
    good_ev = Evidence(
        data_version="dv1",
        commit="c1",
        code_hash="code-1",
        params_hash="param-1",
        cost_model="us_etf_full_v1",
        evaluation_id="exp-ok",
        gate_report_id="gate-mom-v1",
        exit_conditions="MDD > 10% 退回",
    )
    r = reg.promote_version(
        strategy_id="mom_rot", version="v1", actor=sys_actor, at=T0, evidence=good_ev
    )
    assert r.stage is Stage.BACKTEST
    r = reg.promote_version(
        strategy_id="mom_rot", version="v1", actor=sys_actor, at=T0, evidence=good_ev
    )
    assert r.stage is Stage.ROBUSTNESS

    # 4. 进入 PAPER 必须由 OWNER 审批，且门禁报告文件存在并通过
    with pytest.raises(LifecycleError, match="门禁报告文件不存在"):
        reg.promote_version(
            strategy_id="mom_rot", version="v1", actor=owner, at=T0, evidence=good_ev
        )
    (gates_dir / "gate-mom-v1.md").write_text(
        "# 门禁\n- 策略版本：`mom_rot:v1`\n- 总体结论：**PASS（通过）**\n", encoding="utf-8"
    )
    r = reg.promote_version(
        strategy_id="mom_rot", version="v1", actor=owner, at=T0, evidence=good_ev
    )
    assert r.stage is Stage.PAPER
    assert reg.is_trading_allowed("mom_rot", "v1", Mode.PAPER) is True

    # 5. 未停用的策略不能归档删除；停用后整策略归档，历史与试验次数保留，ID 永久不可复用
    with pytest.raises(LifecycleError, match="未停用的策略不能删除"):
        reg.archive_strategy(strategy_id="mom_rot", actor=owner, at=T0, reason="尝试直接删")

    reg.stop_version(strategy_id="mom_rot", version="v1", actor=owner, at=T0, reason="停用准备归档")
    assert reg.is_trading_allowed("mom_rot", "v1", Mode.PAPER) is False

    archived = reg.archive_strategy(strategy_id="mom_rot", actor=owner, at=T0, reason="归档退役")
    assert len(archived) == 1
    assert reg.list_records(include_archived=False) == []
    assert len(reg.list_records(include_archived=True)) == 1
    assert len(reg.list_transitions("mom_rot")) == 9
    assert exp_reg.trial_count("mom_rot") == 1  # 试验次数绝不因归档清零

    with pytest.raises(LifecycleError, match="ID 不能复用"):
        reg.create_version(
            strategy_id="mom_rot",
            version="v2",
            code_hash="code-2",
            params_hash="param-2",
            actor=owner,
            at=T0,
        )

    # 另一个策略测试 retire_version
    reg.create_version(
        strategy_id="other_strat",
        version="v1",
        code_hash="c1",
        params_hash="p1",
        actor=owner,
        at=T0,
    )
    retired = reg.retire_version(
        strategy_id="other_strat", version="v1", actor=owner, at=T0, reason="退役下线"
    )
    assert retired.stage is Stage.RETIRE

    # SQLite 只追加触发器校验
    conn = sqlite3.connect(meta_db)
    with pytest.raises(sqlite3.IntegrityError, match="DELETE forbidden"):
        conn.execute("DELETE FROM strategy_transitions")
    conn.close()
    assert len(audits) == 11


@pytest.mark.asyncio
async def test_trading_core_enforces_strategy_registry_intake_gate(tmp_path: Path) -> None:
    env = Env(tmp_path, positions={})
    await env.broker.connect()
    await env.pump()
    reg = StrategyRegistry(tmp_path / "meta.db")
    owner = Actor(ActorKind.OWNER, "dylan")
    sys_actor = Actor(ActorKind.SYSTEM, "pipe")
    ev = Evidence(
        data_version="dv1",
        commit="c1",
        code_hash="h1",
        params_hash="p1",
        cost_model="c",
        evaluation_id="e1",
        gate_report_id="g1",
        exit_conditions="stop",
    )
    reg.create_version(
        strategy_id="s", version="1", code_hash="h1", params_hash="p1", actor=owner, at=T0
    )

    async def zero_skew() -> int | None:
        return 0

    inputs = CoreInputs(
        mode_config=ModeConfig(
            requested_mode=Mode.PAPER,
            account_id=ACCOUNT.account_id,
            expected_account_type=ACCOUNT.account_type,
            risk_regime=ACCOUNT.account_type,
            allow_delayed_data=False,
            config_version="v1",
        ),
        instruments={"SPY": SPY},
        components=BACKTEST_COMPONENTS,
        parameters=lambda: PARAMS,
        quotes=Quotes(env),
        account_synced=lambda: True,
        clock_skew_ms=zero_skew,
        start_of_day_nav=lambda _a: Decimal(100_000),
        strategy_drawdown=lambda: {"s": Decimal(0)},
        stop_cooldown=dict,
        corporate_actions=Actions(),
        broker_window=lambda _now: False,
        degraded_allowed={},
        session_day=CALENDAR.session_day,
        clock=env.clock,
        strategy_allowed=reg.is_trading_allowed,
    )
    assembled = build_trading_core(env.repo, env.broker, inputs)
    for comp in BACKTEST_COMPONENTS:
        assembled.run_state.report(comp.name, ok=True)
    await assembled.core.start()

    # 1. 未注册的策略版本直接拒绝
    unregistered = env.make_intent("i-unreg", strategy_id="unknown_strat", strategy_version="v1")
    with pytest.raises(IntakeError, match="未获准在当前模式交易"):
        await assembled.core.submit_intent(unregistered)

    # 2. 已注册但还在 IDEA 阶段的版本在 PAPER 模式下被拒
    idea_intent = env.make_intent("i-idea", strategy_id="s", strategy_version="1")
    with pytest.raises(IntakeError, match="未获准在当前模式交易"):
        await assembled.core.submit_intent(idea_intent)

    # 3. 逐级晋级到 PAPER 后放行；停用后立即拦截
    for actor in (owner, sys_actor, sys_actor, sys_actor, sys_actor, owner):
        reg.promote_version(strategy_id="s", version="1", actor=actor, at=T0, evidence=ev)
    outcome = await assembled.core.submit_intent(idea_intent)
    assert outcome.decision.intent_id == "i-idea"

    reg.stop_version(strategy_id="s", version="1", actor=owner, at=T0, reason="人工停用")
    stopped_intent = env.make_intent("i-stopped", strategy_id="s", strategy_version="1")
    with pytest.raises(IntakeError, match="未获准在当前模式交易"):
        await assembled.core.submit_intent(stopped_intent)


def test_strategy_api_endpoints_and_permissions(tmp_path: Path) -> None:
    reg = StrategyRegistry(tmp_path / "meta.db")
    audit = MemoryAudit()
    app = create_app(
        ApiDeps(
            authenticator=Tokens(),
            ledger=lambda: None,  # type: ignore[arg-type, return-value]
            gateway=Gateway(),
            audit=audit,
            strategies=reg,
        )
    )
    client = TestClient(app)
    owner_hdr = {"Authorization": "Bearer owner-token"}
    viewer_hdr = {"Authorization": "Bearer viewer-token"}

    # viewer 不能新建策略版本（403）
    r_forbid = client.post(
        "/v1/strategies",
        headers=viewer_hdr,
        json={"strategy_id": "alpha", "version": "v1", "code_hash": "c1", "params_hash": "p1"},
    )
    assert r_forbid.status_code == 403

    # owner 新建、查询、晋级、停用、恢复、归档
    r_create = client.post(
        "/v1/strategies",
        headers=owner_hdr,
        json={"strategy_id": "alpha", "version": "v1", "code_hash": "c1", "params_hash": "p1"},
    )
    assert r_create.status_code == 200
    assert r_create.json()["stage"] == "IDEA"

    r_list = client.get("/v1/strategies", headers=viewer_hdr)
    assert r_list.status_code == 200
    assert len(r_list.json()["strategies"]) == 1

    r_prom = client.post("/v1/strategies/alpha/versions/v1/promote", headers=owner_hdr, json={})
    assert r_prom.status_code == 200
    assert r_prom.json()["stage"] == "HYPOTHESIS"

    r_stop = client.post(
        "/v1/strategies/alpha/versions/v1/stop",
        headers=owner_hdr,
        json={"reason": "暂停观察"},
    )
    assert r_stop.status_code == 200
    assert r_stop.json()["enabled"] is False

    r_resume = client.post(
        "/v1/strategies/alpha/versions/v1/resume",
        headers=owner_hdr,
        json={},
    )
    assert r_resume.status_code == 200
    assert r_resume.json()["enabled"] is True

    client.post(
        "/v1/strategies/alpha/versions/v1/stop",
        headers=owner_hdr,
        json={"reason": "归档前停用"},
    )
    r_arch = client.post(
        "/v1/strategies/alpha/archive",
        headers=owner_hdr,
        json={"reason": "归档下线"},
    )
    assert r_arch.status_code == 200
    parsed_arch = StrategyArchiveResponse.model_validate(r_arch.json())
    assert parsed_arch.archived_versions == ["v1"]
    assert client.get("/v1/strategies", headers=viewer_hdr).json()["strategies"] == []
    assert (
        len(
            client.get("/v1/strategies?include_archived=true", headers=viewer_hdr).json()[
                "strategies"
            ]
        )
        == 1
    )
