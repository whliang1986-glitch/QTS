"""策略生命周期规则（T-09a）。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from server.domain.enums import Mode
from server.domain.strategy_lifecycle import (
    Actor,
    ActorKind,
    Evidence,
    LifecycleError,
    Stage,
    StrategyRecord,
    archive,
    create,
    promote,
    resume,
    retire,
    stop,
    trading_allowed,
)

AT = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)
AI = Actor(ActorKind.AI, "research-agent")
SYSTEM = Actor(ActorKind.SYSTEM, "workerd")
OWNER = Actor(ActorKind.OWNER, "dylan")
EVIDENCE = Evidence(
    data_version="lake-v3",
    commit="abc123",
    code_hash="c1",
    params_hash="p1",
    cost_model="ibkr-tiered",
    evaluation_id="exp-7",
)
GATED = replace(EVIDENCE, gate_report_id="gate-1", exit_conditions="回撤超过阈值即退出")


def new(actor: Actor = OWNER, **kw: Any) -> StrategyRecord:
    args: dict[str, object] = {
        "strategy_id": "momentum",
        "version": "v1",
        "code_hash": "c1",
        "params_hash": "p1",
        "actor": actor,
        "at": AT,
        "existing_versions": frozenset(),
        "archived_ids": frozenset(),
    }
    args.update(kw)
    return create(**args)[0]  # type: ignore[arg-type]


def at_stage(stage: Stage) -> StrategyRecord:
    return replace(new(), stage=stage)


# ---------- 新建与版本 ----------


def test_new_versions_start_at_idea_and_are_enabled() -> None:
    record, transition = create(
        strategy_id="momentum",
        version="v1",
        code_hash="c1",
        params_hash="p1",
        actor=AI,
        at=AT,
        existing_versions=frozenset(),
        archived_ids=frozenset(),
    )
    assert (record.stage, record.enabled, record.archived) == (Stage.IDEA, True, False)
    assert (transition.action, transition.from_stage, transition.to_stage) == (
        "create",
        None,
        Stage.IDEA,
    )


def test_versions_are_immutable_and_archived_ids_never_return() -> None:
    with pytest.raises(LifecycleError, match="不可变"):
        new(existing_versions=frozenset({"v1"}))
    with pytest.raises(LifecycleError, match="不能复用"):
        new(archived_ids=frozenset({"momentum"}))


@pytest.mark.parametrize("field", ["strategy_id", "version", "code_hash", "params_hash"])
def test_blank_fields_are_refused(field: str) -> None:
    with pytest.raises(LifecycleError, match="不能为空"):
        blank: dict[str, Any] = {field: " "}
        new(OWNER, **blank)


def test_naive_time_is_refused() -> None:
    with pytest.raises(LifecycleError, match="时区"):
        new(at=datetime(2026, 11, 5, 15, 0))  # noqa: DTZ001


# ---------- 晋级 ----------


def test_promotion_is_one_step_at_a_time() -> None:
    record = new()
    record, transition = promote(record, actor=AI, at=AT)
    assert record.stage is Stage.HYPOTHESIS and transition.from_stage is Stage.IDEA


def test_ai_cannot_go_past_hypothesis() -> None:
    with pytest.raises(LifecycleError, match="所有者审批"):
        promote(at_stage(Stage.HYPOTHESIS), actor=AI, at=AT)


def test_system_can_reach_robustness_with_evidence_but_not_paper() -> None:
    record, _ = promote(at_stage(Stage.BASELINE), actor=SYSTEM, at=AT, evidence=EVIDENCE)
    assert record.stage is Stage.BACKTEST
    record, _ = promote(record, actor=SYSTEM, at=AT, evidence=EVIDENCE)
    assert record.stage is Stage.ROBUSTNESS
    with pytest.raises(LifecycleError, match="所有者审批"):
        promote(record, actor=SYSTEM, at=AT, evidence=GATED)


def test_evidence_is_required_from_backtest_and_gate_report_from_paper() -> None:
    with pytest.raises(LifecycleError, match="必须附证据"):
        promote(at_stage(Stage.BASELINE), actor=SYSTEM, at=AT)
    with pytest.raises(LifecycleError, match="commit"):
        promote(
            at_stage(Stage.BASELINE), actor=SYSTEM, at=AT, evidence=replace(EVIDENCE, commit="")
        )
    with pytest.raises(LifecycleError, match="gate_report_id"):
        promote(at_stage(Stage.ROBUSTNESS), actor=OWNER, at=AT, evidence=EVIDENCE)
    record, _ = promote(at_stage(Stage.ROBUSTNESS), actor=OWNER, at=AT, evidence=GATED)
    assert record.stage is Stage.PAPER


def test_last_working_stage_can_only_retire() -> None:
    with pytest.raises(LifecycleError, match="退役"):
        promote(at_stage(Stage.MONITORED_PRODUCTION), actor=OWNER, at=AT, evidence=GATED)


def test_stopped_strategies_cannot_be_promoted() -> None:
    stopped, _ = stop(new(), actor=OWNER, at=AT, reason="暂停观察")
    with pytest.raises(LifecycleError, match="已停用"):
        promote(stopped, actor=OWNER, at=AT)


# ---------- 停用、恢复、退役、删除 ----------


@pytest.mark.parametrize("actor", [AI, SYSTEM])
def test_only_the_owner_can_stop_resume_retire_or_delete(actor: Actor) -> None:
    record = new()
    with pytest.raises(LifecycleError, match="只有所有者"):
        stop(record, actor=actor, at=AT, reason="x")
    stopped, _ = stop(record, actor=OWNER, at=AT, reason="x")
    with pytest.raises(LifecycleError, match="只有所有者"):
        resume(stopped, actor=actor, at=AT)
    with pytest.raises(LifecycleError, match="只有所有者"):
        retire(record, actor=actor, at=AT, reason="x")
    with pytest.raises(LifecycleError, match="只有所有者"):
        archive(stopped, actor=actor, at=AT, reason="x")


def test_stop_is_immediate_and_needs_a_reason() -> None:
    with pytest.raises(LifecycleError, match="原因"):
        stop(new(), actor=OWNER, at=AT, reason=" ")
    stopped, transition = stop(at_stage(Stage.PAPER), actor=OWNER, at=AT, reason="回撤偏大")
    assert not stopped.enabled and transition.action == "stop"
    assert not trading_allowed(stopped, Mode.PAPER)


def test_resuming_a_paper_strategy_needs_a_fresh_gate_report() -> None:
    stopped, _ = stop(at_stage(Stage.PAPER), actor=OWNER, at=AT, reason="x")
    with pytest.raises(LifecycleError, match="证据"):
        resume(stopped, actor=OWNER, at=AT)
    resumed, _ = resume(stopped, actor=OWNER, at=AT, evidence=GATED)
    assert resumed.enabled
    early, _ = stop(new(), actor=OWNER, at=AT, reason="x")
    assert resume(early, actor=OWNER, at=AT)[0].enabled  # 候选阶段恢复不需要门禁
    with pytest.raises(LifecycleError, match="没有停用"):
        resume(new(), actor=OWNER, at=AT)


def test_delete_requires_stop_and_is_an_archive() -> None:
    with pytest.raises(LifecycleError, match="先停用"):
        archive(new(), actor=OWNER, at=AT, reason="不再需要")
    stopped, _ = stop(new(), actor=OWNER, at=AT, reason="x")
    archived, transition = archive(stopped, actor=OWNER, at=AT, reason="不再需要")
    assert archived.archived and transition.action == "archive"
    assert (archived.code_hash, archived.params_hash) == ("c1", "p1")  # 记录保留，只是归档
    changes: list[Callable[[StrategyRecord], object]] = [
        lambda r: promote(r, actor=OWNER, at=AT),
        lambda r: resume(r, actor=OWNER, at=AT),
        lambda r: retire(r, actor=OWNER, at=AT, reason="x"),
        lambda r: archive(r, actor=OWNER, at=AT, reason="x"),
    ]
    for change in changes:
        with pytest.raises(LifecycleError):
            change(archived)


def test_retire_is_final_and_stops_trading() -> None:
    retired, _ = retire(at_stage(Stage.PAPER), actor=OWNER, at=AT, reason="策略失效")
    assert retired.stage is Stage.RETIRE and not retired.enabled
    with pytest.raises(LifecycleError, match="已退役"):
        stop(retired, actor=OWNER, at=AT, reason="x")


# ---------- 能否交易 ----------


@pytest.mark.parametrize(
    ("stage", "mode", "allowed"),
    [
        (Stage.IDEA, Mode.BACKTEST, True),
        (Stage.ROBUSTNESS, Mode.PAPER, False),
        (Stage.PAPER, Mode.PAPER, True),
        (Stage.MONITORED_PRODUCTION, Mode.PAPER, True),
        (Stage.MONITORED_PRODUCTION, Mode.LIMITED_AUTO_LIVE, False),  # G7 之前没有实盘
        (Stage.LIMITED_LIVE, Mode.ASSISTED_LIVE, False),
        (Stage.PAPER, Mode.RESEARCH, False),
        (Stage.PAPER, Mode.HALT, False),
    ],
)
def test_trading_allowed(stage: Stage, mode: Mode, allowed: bool) -> None:
    assert trading_allowed(at_stage(stage), mode) is allowed


def test_archived_or_retired_never_trade_even_in_backtest() -> None:
    stopped, _ = stop(new(), actor=OWNER, at=AT, reason="x")
    archived, _ = archive(stopped, actor=OWNER, at=AT, reason="x")
    retired, _ = retire(new(), actor=OWNER, at=AT, reason="x")
    assert not trading_allowed(archived, Mode.BACKTEST)
    assert not trading_allowed(retired, Mode.BACKTEST)


@pytest.mark.parametrize("field", ["code_hash", "params_hash"])
def test_evidence_from_another_version_is_refused(field: str) -> None:
    other = replace(GATED, **{field: "other"})
    with pytest.raises(LifecycleError, match="不属于这个版本"):
        promote(at_stage(Stage.BASELINE), actor=SYSTEM, at=AT, evidence=other)
    with pytest.raises(LifecycleError, match="不属于这个版本"):
        promote(at_stage(Stage.ROBUSTNESS), actor=OWNER, at=AT, evidence=other)


def test_live_stages_cannot_be_entered_before_g7() -> None:
    with pytest.raises(LifecycleError, match="G7"):
        promote(at_stage(Stage.REVIEW), actor=OWNER, at=AT, evidence=GATED)
    record, _ = promote(at_stage(Stage.PAPER), actor=OWNER, at=AT, evidence=GATED)
    assert record.stage is Stage.REVIEW


def test_stopping_twice_is_refused() -> None:
    stopped, _ = stop(new(), actor=OWNER, at=AT, reason="x")
    with pytest.raises(LifecycleError, match="已经停用"):
        stop(stopped, actor=OWNER, at=AT, reason="x")


def test_stage_order_follows_the_plan() -> None:
    assert [s.name for s in Stage] == [
        "IDEA", "HYPOTHESIS", "DATA_AUDIT", "BASELINE", "BACKTEST", "ROBUSTNESS", "PAPER",
        "REVIEW", "LIMITED_LIVE", "MONITORED_PRODUCTION", "RETIRE",
    ]  # fmt: skip
    assert Stage.DATA_AUDIT < Stage.REVIEW
    assert new(AI).created_by == AI


def test_blank_actor_names_are_refused() -> None:
    with pytest.raises(LifecycleError, match="操作者"):
        Actor(ActorKind.OWNER, " ")
