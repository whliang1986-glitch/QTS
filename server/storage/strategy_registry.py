"""策略库存储层与生命周期注册表（T-09b，V4.1 8.4 & 第 9 章）。

核心保障：
1. **只追加表 + 触发器**：`strategy_transitions` 与 `archived_strategy_ids` 均带 SQLite
   `no_update` / `no_delete` / `no_replace` 触发器，历史版本、回测记录与晋级轨迹不可篡改或删除；
2. **全量重放校验（replay_all）**：读取状态时从头重放全部 Transition，逐条经
   `server.domain.strategy_lifecycle` 纯函数校验，防止绕过应用层直接往数据库插伪造状态；
3. **证据真实性核验**：晋级到 `BACKTEST` 及之后阶段时，核验 `ExperimentRegistry` 中评测记录存在且
   版本、代码/参数哈希、数据版本、成本模型一致；进入 `PAPER` 时核验 `T-08` 门禁报告存在且通过；
4. **整策略归档（删除）粒度**：删除作用于整个 `strategy_id`（要求该策略下全部版本均已停用），
   归档后列表默认隐藏但历史记录、试验次数与审计全保留，且该 `strategy_id` 永久不可复用；
5. **交易核心准入（is_trading_allowed）**：供 `TradingCore.submit_intent` 调用，未登记、已停用、
   已归档、阶段不足的版本一律拒绝（失败即关闭）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from server.domain.enums import Mode
from server.domain.strategy_lifecycle import (
    EVIDENCE_FROM,
    GATE_FROM,
    Actor,
    ActorKind,
    Evidence,
    LifecycleError,
    Stage,
    StrategyRecord,
    Transition,
    archive,
    create,
    promote,
    resume,
    retire,
    stop,
    trading_allowed,
)
from server.storage.errors import StorageError
from server.storage.experiments import ExperimentRegistry

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS strategy_transitions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    version TEXT NOT NULL,
    action TEXT NOT NULL,
    from_stage INTEGER,
    to_stage INTEGER NOT NULL,
    actor_kind TEXT NOT NULL,
    actor_name TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    evidence_json TEXT,
    reason TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    params_hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_strategy_transitions_key
    ON strategy_transitions (strategy_id, version, seq);

CREATE TRIGGER IF NOT EXISTS strategy_transitions_no_update
BEFORE UPDATE ON strategy_transitions
BEGIN
    SELECT RAISE(ABORT, 'strategy_transitions is append-only: UPDATE forbidden');
END;

CREATE TRIGGER IF NOT EXISTS strategy_transitions_no_delete
BEFORE DELETE ON strategy_transitions
BEGIN
    SELECT RAISE(ABORT, 'strategy_transitions is append-only: DELETE forbidden');
END;

CREATE TABLE IF NOT EXISTS archived_strategy_ids (
    strategy_id TEXT PRIMARY KEY,
    archived_at TEXT NOT NULL,
    actor_name TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS archived_strategy_ids_no_update
BEFORE UPDATE ON archived_strategy_ids
BEGIN
    SELECT RAISE(ABORT, 'archived_strategy_ids is append-only: UPDATE forbidden');
END;

CREATE TRIGGER IF NOT EXISTS archived_strategy_ids_no_delete
BEFORE DELETE ON archived_strategy_ids
BEGIN
    SELECT RAISE(ABORT, 'archived_strategy_ids is append-only: DELETE forbidden');
END;

CREATE TRIGGER IF NOT EXISTS archived_strategy_ids_no_replace
BEFORE INSERT ON archived_strategy_ids
WHEN EXISTS (SELECT 1 FROM archived_strategy_ids WHERE strategy_id = NEW.strategy_id)
BEGIN
    SELECT RAISE(ABORT, 'archived_strategy_ids is append-only: REPLACE forbidden');
END;
"""

AuditSink = Callable[[str, str, dict[str, Any]], None]


def _iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise LifecycleError("时间必须带时区")
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def _dump_evidence(ev: Evidence | None) -> str | None:
    if ev is None:
        return None
    return json.dumps(
        {
            "data_version": ev.data_version,
            "commit": ev.commit,
            "code_hash": ev.code_hash,
            "params_hash": ev.params_hash,
            "cost_model": ev.cost_model,
            "evaluation_id": ev.evaluation_id,
            "gate_report_id": ev.gate_report_id,
            "exit_conditions": ev.exit_conditions,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _load_evidence(raw: str | None) -> Evidence | None:
    if not raw:
        return None
    d = json.loads(raw)
    return Evidence(
        data_version=str(d["data_version"]),
        commit=str(d["commit"]),
        code_hash=str(d["code_hash"]),
        params_hash=str(d["params_hash"]),
        cost_model=str(d["cost_model"]),
        evaluation_id=str(d["evaluation_id"]),
        gate_report_id=d.get("gate_report_id"),
        exit_conditions=d.get("exit_conditions"),
    )


class StrategyRegistry:
    """meta.db 中的策略生命周期只追加注册表。"""

    def __init__(
        self,
        db_path: Path,
        *,
        experiments: ExperimentRegistry | None = None,
        gates_dir: Path | None = None,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._path = db_path
        self._experiments = experiments
        self._gates_dir = gates_dir
        self._audit = audit_sink
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA recursive_triggers = ON")
        return conn

    def archived_ids(self) -> frozenset[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT strategy_id FROM archived_strategy_ids").fetchall()
        return frozenset(str(r[0]) for r in rows)

    def replay_all(self) -> dict[tuple[str, str], StrategyRecord]:
        """从头重放全部 Transition 并逐条通过 T-09a 领域函数校验，返回最新状态表。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT strategy_id, version, action, from_stage, to_stage,
                       actor_kind, actor_name, occurred_at, evidence_json,
                       reason, code_hash, params_hash
                FROM strategy_transitions
                ORDER BY seq ASC
                """
            ).fetchall()

        records: dict[tuple[str, str], StrategyRecord] = {}
        versions_by_sid: dict[str, set[str]] = {}
        seen_archived_ids: set[str] = set()

        for row in rows:
            sid, ver, action = str(row[0]), str(row[1]), str(row[2])
            expected_from = Stage(int(row[3])) if row[3] is not None else None
            expected_to = Stage(int(row[4]))
            actor = Actor(ActorKind(str(row[5])), str(row[6]))
            at = datetime.fromisoformat(str(row[7]))
            evidence = _load_evidence(row[8])
            reason = str(row[9])
            code_hash, params_hash = str(row[10]), str(row[11])
            key = (sid, ver)

            if action == "create":
                rec, tr = create(
                    strategy_id=sid,
                    version=ver,
                    code_hash=code_hash,
                    params_hash=params_hash,
                    actor=actor,
                    at=at,
                    existing_versions=frozenset(versions_by_sid.get(sid, set())),
                    archived_ids=frozenset(seen_archived_ids),
                )
                versions_by_sid.setdefault(sid, set()).add(ver)
            else:
                cur = records.get(key)
                if cur is None:
                    raise StorageError(f"策略转移记录损坏：{sid}:{ver} 在 create 前出现 {action}")
                if action == "promote":
                    rec, tr = promote(cur, actor=actor, at=at, evidence=evidence)
                elif action == "stop":
                    rec, tr = stop(cur, actor=actor, at=at, reason=reason)
                elif action == "resume":
                    rec, tr = resume(cur, actor=actor, at=at, evidence=evidence)
                elif action == "retire":
                    rec, tr = retire(cur, actor=actor, at=at, reason=reason)
                elif action == "archive":
                    rec, tr = archive(cur, actor=actor, at=at, reason=reason)
                    seen_archived_ids.add(sid)
                else:
                    raise StorageError(f"未知的策略生命周期动作：{action}")

            if tr.from_stage != expected_from or tr.to_stage != expected_to:
                raise StorageError(f"策略转移记录阶段不一致：{sid}:{ver} {action}")
            records[key] = rec

        return records

    def get(self, strategy_id: str, version: str) -> StrategyRecord | None:
        return self.replay_all().get((strategy_id, version))

    def list_records(self, *, include_archived: bool = False) -> list[StrategyRecord]:
        all_recs = list(self.replay_all().values())
        if not include_archived:
            all_recs = [r for r in all_recs if not r.archived]
        return sorted(all_recs, key=lambda r: (r.strategy_id, r.version))

    def list_transitions(self, strategy_id: str) -> list[Transition]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT strategy_id, version, action, from_stage, to_stage,
                       actor_kind, actor_name, occurred_at, evidence_json, reason
                FROM strategy_transitions
                WHERE strategy_id = ?
                ORDER BY seq ASC
                """,
                (strategy_id,),
            ).fetchall()
        return [
            Transition(
                strategy_id=str(r[0]),
                version=str(r[1]),
                action=str(r[2]),
                from_stage=Stage(int(r[3])) if r[3] is not None else None,
                to_stage=Stage(int(r[4])),
                actor=Actor(ActorKind(str(r[5])), str(r[6])),
                at=datetime.fromisoformat(str(r[7])),
                evidence=_load_evidence(r[8]),
                reason=str(r[9]),
            )
            for r in rows
        ]

    def create_version(  # noqa: PLR0913
        self,
        *,
        strategy_id: str,
        version: str,
        code_hash: str,
        params_hash: str,
        actor: Actor,
        at: datetime,
    ) -> StrategyRecord:
        records = self.replay_all()
        existing_vers = frozenset(ver for (sid, ver) in records if sid == strategy_id)
        rec, tr = create(
            strategy_id=strategy_id,
            version=version,
            code_hash=code_hash,
            params_hash=params_hash,
            actor=actor,
            at=at,
            existing_versions=existing_vers,
            archived_ids=self.archived_ids(),
        )
        self._append_transition(rec, tr)
        return rec

    def promote_version(
        self,
        *,
        strategy_id: str,
        version: str,
        actor: Actor,
        at: datetime,
        evidence: Evidence | None = None,
    ) -> StrategyRecord:
        rec = self._require_record(strategy_id, version)
        updated, tr = promote(rec, actor=actor, at=at, evidence=evidence)
        self._verify_external_evidence(rec, updated.stage, evidence)
        self._append_transition(updated, tr)
        return updated

    def stop_version(
        self, *, strategy_id: str, version: str, actor: Actor, at: datetime, reason: str
    ) -> StrategyRecord:
        rec = self._require_record(strategy_id, version)
        updated, tr = stop(rec, actor=actor, at=at, reason=reason)
        self._append_transition(updated, tr)
        return updated

    def resume_version(
        self,
        *,
        strategy_id: str,
        version: str,
        actor: Actor,
        at: datetime,
        evidence: Evidence | None = None,
    ) -> StrategyRecord:
        rec = self._require_record(strategy_id, version)
        updated, tr = resume(rec, actor=actor, at=at, evidence=evidence)
        if updated.stage >= GATE_FROM:
            self._verify_external_evidence(rec, updated.stage, evidence)
        self._append_transition(updated, tr)
        return updated

    def retire_version(
        self, *, strategy_id: str, version: str, actor: Actor, at: datetime, reason: str
    ) -> StrategyRecord:
        rec = self._require_record(strategy_id, version)
        updated, tr = retire(rec, actor=actor, at=at, reason=reason)
        self._append_transition(updated, tr)
        return updated

    def archive_strategy(
        self, *, strategy_id: str, actor: Actor, at: datetime, reason: str
    ) -> list[StrategyRecord]:
        """整策略归档隐藏（删除）：要求该策略所有版本均已停用；归档后 ID 永久不可复用。"""
        if actor.kind is not ActorKind.OWNER:
            raise LifecycleError(f"只有所有者能删除策略（{actor.kind.value} 不行）")
        if strategy_id in self.archived_ids():
            raise LifecycleError(f"策略 {strategy_id} 已经归档")
        records = [r for r in self.replay_all().values() if r.strategy_id == strategy_id]
        if not records:
            raise LifecycleError(f"策略 {strategy_id} 不存在")
        active = [r.version for r in records if r.enabled]
        if active:
            raise LifecycleError(f"未停用的策略不能删除：请先停用版本 {', '.join(sorted(active))}")
        changes: list[tuple[StrategyRecord, Transition]] = []
        for r in sorted(records, key=lambda x: x.version):
            if not r.archived:
                changes.append(archive(r, actor=actor, at=at, reason=reason))

        try:
            with self._connect() as conn:
                for updated, tr in changes:
                    self._insert_transition(conn, updated, tr)
                conn.execute(
                    """
                    INSERT INTO archived_strategy_ids (strategy_id, archived_at, actor_name, reason)
                    VALUES (?, ?, ?, ?)
                    """,
                    (strategy_id, _iso_utc(at), actor.name, reason),
                )
        except sqlite3.Error as exc:
            raise StorageError(f"归档策略 {strategy_id} 失败：{exc}") from exc

        for _, tr in changes:
            self._emit_audit(tr)
        return [u for u, _ in changes]

    def is_trading_allowed(self, strategy_id: str, version: str, mode: Mode) -> bool:
        """供交易核心调用：未登记、已停用、已归档或阶段不足一律返回 False（失败即关闭）。"""
        try:
            rec = self.get(strategy_id, version)
        except Exception:
            return False
        if rec is None:
            return False
        return trading_allowed(rec, mode)

    def _require_record(self, strategy_id: str, version: str) -> StrategyRecord:
        rec = self.get(strategy_id, version)
        if rec is None:
            raise LifecycleError(f"策略版本不存在：{strategy_id}:{version}")
        return rec

    def _verify_external_evidence(
        self, record: StrategyRecord, target: Stage, evidence: Evidence | None
    ) -> None:
        if target < EVIDENCE_FROM or evidence is None:
            return
        if self._experiments is not None:
            exp = self._experiments.get(evidence.evaluation_id)
            if exp is None:
                raise LifecycleError(f"评测编号不存在：{evidence.evaluation_id}")
            if (exp.strategy_id, exp.strategy_version) != (record.strategy_id, record.version):
                raise LifecycleError("评测实验不属于当前策略版本")
            if (
                exp.data_version != evidence.data_version
                or exp.code_commit != evidence.commit
                or exp.params_hash != evidence.params_hash
                or exp.cost_model != evidence.cost_model
            ):
                raise LifecycleError("晋级证据与实验注册表记录不一致")
        if target >= GATE_FROM and self._gates_dir is not None:
            gate_file = self._gates_dir / f"{evidence.gate_report_id}.md"
            if not gate_file.is_file():
                raise LifecycleError(f"门禁报告文件不存在：{gate_file.name}")
            content = gate_file.read_text(encoding="utf-8")
            if (
                "PASS（通过）" not in content
                or f"{record.strategy_id}:{record.version}" not in content
            ):
                raise LifecycleError(f"门禁报告 {evidence.gate_report_id} 未通过或不属于该策略版本")

    def _append_transition(self, record: StrategyRecord, tr: Transition) -> None:
        try:
            with self._connect() as conn:
                self._insert_transition(conn, record, tr)
        except sqlite3.Error as exc:
            raise StorageError(f"写入策略转移记录失败：{exc}") from exc
        self._emit_audit(tr)

    @staticmethod
    def _insert_transition(
        conn: sqlite3.Connection, record: StrategyRecord, tr: Transition
    ) -> None:
        conn.execute(
            """
            INSERT INTO strategy_transitions (
                strategy_id, version, action, from_stage, to_stage,
                actor_kind, actor_name, occurred_at, evidence_json,
                reason, code_hash, params_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tr.strategy_id,
                tr.version,
                tr.action,
                int(tr.from_stage) if tr.from_stage is not None else None,
                int(tr.to_stage),
                tr.actor.kind.value,
                tr.actor.name,
                _iso_utc(tr.at),
                _dump_evidence(tr.evidence),
                tr.reason,
                record.code_hash,
                record.params_hash,
            ),
        )

    def _emit_audit(self, tr: Transition) -> None:
        if self._audit is not None:
            self._audit(
                f"strategy.{tr.action}",
                f"{tr.actor.kind.value}:{tr.actor.name}",
                {
                    "strategy_id": tr.strategy_id,
                    "version": tr.version,
                    "from_stage": tr.from_stage.name if tr.from_stage is not None else None,
                    "to_stage": tr.to_stage.name,
                    "reason": tr.reason,
                },
            )


__all__ = ("AuditSink", "StrategyRegistry")
