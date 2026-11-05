"""策略生命周期（T-09a，V4.1 8.4 与第 9 章）：纯领域规则，不做 I/O。

阶段：Idea → Hypothesis → Data Audit → Baseline → Backtest → Robustness → Paper → Review
→ Limited Live → Monitored Production → Retire。

规则（失败即关闭，一律抛 LifecycleError）：
- **版本不可变**：同一策略的版本号不能重复；改参数或代码就是新版本，新版本一律从 IDEA 开始。
- **只能逐级晋级**，不能跳级、不能后退；RETIRE 可以从任何阶段进入，进入后不再变化；
  G7 之前不能进入 LIMITED_LIVE 及之后的实盘阶段。
- **谁能做什么**：
  - AI 只能新建版本（进入 IDEA），并把 IDEA 推到 HYPOTHESIS；
    不能再往后晋级，不能停用、恢复、退役或删除；
  - 系统（自动流程）可以推进到 ROBUSTNESS 为止，必须附证据；
  - 进入 PAPER 及之后的阶段，必须是人工所有者，并附上 T-08 发布门禁的报告编号与审批记录；
  - 停用、恢复、退役、删除只有人工所有者能做。
- **证据**：从 BACKTEST 起每次晋级都要附数据版本、commit、代码与参数哈希、成本模型、评测编号，
  代码与参数哈希必须与被晋级的版本一致（别的版本的证据不能拿来用）；
  PAPER 起还要附发布门禁报告编号。
- **停用**：立即生效，不再产生新意图；恢复到 PAPER 及之后的阶段，需要所有者重新审批（附门禁报告）。
- **删除**：必须先停用；删除就是归档隐藏，不物理删除，记录全部保留；策略 ID 不再复用。
- **能否交易**（`trading_allowed`）：
  - 已停用、已归档、已退役：一律不能；
  - BACKTEST 模式：任何未退役、未停用的版本都可以；
  - PAPER 模式：阶段至少是 PAPER；
  - 实盘模式：G7 之前一律不能（红线）；
  - 其他模式：不能。

不改变仓位、不下单：晋级、停用只改变阶段；交易仍经交易名单与风控（S-14 的三层定义）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import IntEnum, StrEnum

from server.domain.enums import LIVE_MODES, Mode


class Stage(IntEnum):
    IDEA = 1
    HYPOTHESIS = 2
    DATA_AUDIT = 3
    BASELINE = 4
    BACKTEST = 5
    ROBUSTNESS = 6
    PAPER = 7
    REVIEW = 8
    LIMITED_LIVE = 9
    MONITORED_PRODUCTION = 10
    RETIRE = 11


class ActorKind(StrEnum):
    AI = "AI"
    SYSTEM = "SYSTEM"
    OWNER = "OWNER"  # 人工所有者（首期只有 Dylan）：身份取自令牌，由 API 层核验


AI_MAX_STAGE = Stage.HYPOTHESIS
SYSTEM_MAX_STAGE = Stage.ROBUSTNESS
EVIDENCE_FROM = Stage.BACKTEST
GATE_FROM = Stage.PAPER
FIRST_LIVE_STAGE = (
    Stage.LIMITED_LIVE
)  # G7 之前不允许进入（纵深防御：trading_allowed 也拒绝实盘模式）


Change = tuple["StrategyRecord", "Transition"]  # 新状态与对应的变化记录


class LifecycleError(ValueError):
    """不允许的生命周期操作。"""


@dataclass(frozen=True)
class Actor:
    kind: ActorKind
    name: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise LifecycleError("操作者名不能为空")


@dataclass(frozen=True)
class Evidence:
    """晋级证据（V4.1 8.4：数据版本、commit、参数、成本模型、评测结果、审批人、退出条件）。"""

    data_version: str
    commit: str
    code_hash: str  # 证据所评测的代码与参数：必须与被晋级的版本一致
    params_hash: str
    cost_model: str
    evaluation_id: str
    gate_report_id: str | None = None  # T-08 发布门禁报告；进入 PAPER 及之后必填
    exit_conditions: str | None = None

    def missing(self, *, need_gate: bool) -> list[str]:
        fields = {"data_version": self.data_version, "commit": self.commit,
                  "code_hash": self.code_hash, "params_hash": self.params_hash,
                  "cost_model": self.cost_model, "evaluation_id": self.evaluation_id}  # fmt: skip
        if need_gate:
            fields["gate_report_id"] = self.gate_report_id or ""
            fields["exit_conditions"] = self.exit_conditions or ""
        return sorted(k for k, v in fields.items() if not str(v or "").strip())


@dataclass(frozen=True)
class StrategyRecord:
    """一个策略版本的当前状态。变化只能经本模块的函数产生新对象，并由存储层写只追加记录。"""

    strategy_id: str
    version: str
    code_hash: str
    params_hash: str
    stage: Stage
    enabled: bool
    archived: bool
    created_by: Actor
    updated_at: datetime

    @property
    def key(self) -> tuple[str, str]:
        return (self.strategy_id, self.version)


@dataclass(frozen=True)
class Transition:
    """一次状态变化的记录（存储层原样写入只追加表与审计）。"""

    strategy_id: str
    version: str
    action: str  # create / promote / stop / resume / retire / archive
    from_stage: Stage | None
    to_stage: Stage
    actor: Actor
    at: datetime
    evidence: Evidence | None = None
    reason: str = ""


def _aware(at: datetime) -> None:
    if at.tzinfo is None or at.utcoffset() is None:
        raise LifecycleError("时间必须带时区")


def create(  # noqa: PLR0913 - 新版本的字段全部显式给出
    *,
    strategy_id: str,
    version: str,
    code_hash: str,
    params_hash: str,
    actor: Actor,
    at: datetime,
    existing_versions: frozenset[str],
    archived_ids: frozenset[str],
) -> tuple[StrategyRecord, Transition]:
    """新建版本：一律从 IDEA 开始。已归档（删除过）的策略 ID 不能再用；版本号不能重复。"""
    _aware(at)
    if strategy_id in archived_ids:
        raise LifecycleError(f"策略 {strategy_id} 已删除（归档），ID 不能复用")
    if version in existing_versions:
        raise LifecycleError(f"策略 {strategy_id} 的版本 {version} 已存在：版本不可变，请新建版本")
    for name, value in (("策略 ID", strategy_id), ("版本", version), ("代码哈希", code_hash),
                        ("参数哈希", params_hash)):  # fmt: skip
        if not value.strip():
            raise LifecycleError(f"{name}不能为空")
    record = StrategyRecord(strategy_id, version, code_hash, params_hash, Stage.IDEA,
                            enabled=True, archived=False, created_by=actor,
                            updated_at=at)  # fmt: skip
    return record, Transition(strategy_id, version, "create", None, Stage.IDEA, actor, at)


def promote(
    record: StrategyRecord, *, actor: Actor, at: datetime, evidence: Evidence | None = None
) -> tuple[StrategyRecord, Transition]:
    """晋级到下一阶段（不能跳级）。RETIRE 走 retire()。"""
    _aware(at)
    _require_live(record)
    if not record.enabled:
        raise LifecycleError("已停用的策略不能晋级：先恢复")
    target = Stage(record.stage + 1)
    if target is Stage.RETIRE:
        raise LifecycleError("最后一个工作阶段之后只能退役（retire）")
    limit = {ActorKind.AI: AI_MAX_STAGE, ActorKind.SYSTEM: SYSTEM_MAX_STAGE}.get(actor.kind)
    if limit is not None and target > limit:
        raise LifecycleError(f"{actor.kind.value} 不能把策略晋级到 {target.name}：需要所有者审批")
    if target >= FIRST_LIVE_STAGE:
        raise LifecycleError(
            f"G7 之前不能晋级到 {target.name}：实盘阶段须 Dylan 本人按 G7 批准后另行放开"
        )
    _check_evidence(record, target, evidence)
    updated = replace(record, stage=target, updated_at=at)
    return updated, Transition(record.strategy_id, record.version, "promote", record.stage,
                               target, actor, at, evidence)  # fmt: skip


def stop(
    record: StrategyRecord, *, actor: Actor, at: datetime, reason: str
) -> tuple[StrategyRecord, Transition]:
    """停用：立即生效，不再产生新意图。只有所有者能做。"""
    _aware(at)
    _require_owner(actor, "停用")
    _require_live(record)
    if not record.enabled:
        raise LifecycleError("策略已经停用")
    if not reason.strip():
        raise LifecycleError("停用要写明原因")
    updated = replace(record, enabled=False, updated_at=at)
    return updated, Transition(record.strategy_id, record.version, "stop", record.stage,
                               record.stage, actor, at, reason=reason)  # fmt: skip


def resume(
    record: StrategyRecord, *, actor: Actor, at: datetime, evidence: Evidence | None = None
) -> tuple[StrategyRecord, Transition]:
    """恢复：只有所有者能做；PAPER 及之后的阶段要附门禁报告重新审批。"""
    _aware(at)
    _require_owner(actor, "恢复")
    _require_live(record)
    if record.enabled:
        raise LifecycleError("策略没有停用，不需要恢复")
    if record.stage >= GATE_FROM:
        _check_evidence(record, record.stage, evidence)
    updated = replace(record, enabled=True, updated_at=at)
    return updated, Transition(record.strategy_id, record.version, "resume", record.stage,
                               record.stage, actor, at, evidence)  # fmt: skip


def retire(
    record: StrategyRecord, *, actor: Actor, at: datetime, reason: str
) -> tuple[StrategyRecord, Transition]:
    """退役：任何阶段都可以，之后不再变化。只有所有者能做；会同时停用。"""
    _aware(at)
    _require_owner(actor, "退役")
    _require_live(record)
    if not reason.strip():
        raise LifecycleError("退役要写明原因")
    updated = replace(record, stage=Stage.RETIRE, enabled=False, updated_at=at)
    return updated, Transition(record.strategy_id, record.version, "retire", record.stage,
                               Stage.RETIRE, actor, at, reason=reason)  # fmt: skip


def archive(
    record: StrategyRecord, *, actor: Actor, at: datetime, reason: str
) -> tuple[StrategyRecord, Transition]:
    """删除 = 归档隐藏：必须先停用；记录全部保留，策略 ID 不再复用（由存储层登记）。"""
    _aware(at)
    _require_owner(actor, "删除")
    if record.archived:
        raise LifecycleError("已经归档")
    if record.enabled:
        raise LifecycleError("未停用的策略不能删除：先停用")
    if not reason.strip():
        raise LifecycleError("删除要写明原因")
    updated = replace(record, archived=True, updated_at=at)
    return updated, Transition(record.strategy_id, record.version, "archive", record.stage,
                               record.stage, actor, at, reason=reason)  # fmt: skip


def trading_allowed(record: StrategyRecord, mode: Mode) -> bool:
    """交易核心用：这个版本在这个模式下能否产生新意图。拿不准一律不能。"""
    if record.archived or not record.enabled or record.stage is Stage.RETIRE:
        return False
    if mode in LIVE_MODES:
        return False  # G7 之前不存在实盘交易（红线）；放开须 Dylan 本人批准
    if mode is Mode.BACKTEST:
        return True
    if mode is Mode.PAPER:
        return record.stage >= Stage.PAPER
    return False


def _require_owner(actor: Actor, what: str) -> None:
    if actor.kind is not ActorKind.OWNER:
        raise LifecycleError(f"只有所有者能{what}策略（{actor.kind.value} 不行）")


def _require_live(record: StrategyRecord) -> None:
    if record.archived:
        raise LifecycleError("已归档（删除）的策略不能再变化")
    if record.stage is Stage.RETIRE:
        raise LifecycleError("已退役的策略不能再变化")


def _check_evidence(record: StrategyRecord, target: Stage, evidence: Evidence | None) -> None:
    """形式检查：字段齐全、并且属于这个版本（代码与参数哈希一致）。

    证据是否真实存在、门禁报告是否为新出且属于这个版本，由存储层与 T-08 核验（T-09b）。
    """
    if target < EVIDENCE_FROM:
        return
    if evidence is None:
        raise LifecycleError(f"晋级到 {target.name} 必须附证据")
    missing = evidence.missing(need_gate=target >= GATE_FROM)
    if missing:
        raise LifecycleError(f"晋级到 {target.name} 缺少证据：{', '.join(missing)}")
    if (evidence.code_hash, evidence.params_hash) != (record.code_hash, record.params_hash):
        raise LifecycleError("证据不属于这个版本（代码或参数哈希不一致）：改参数或代码要重新验证")


__all__ = (
    "AI_MAX_STAGE",
    "SYSTEM_MAX_STAGE",
    "Actor",
    "ActorKind",
    "Change",
    "Evidence",
    "LifecycleError",
    "Stage",
    "StrategyRecord",
    "Transition",
    "archive",
    "create",
    "promote",
    "resume",
    "retire",
    "stop",
    "trading_allowed",
)
