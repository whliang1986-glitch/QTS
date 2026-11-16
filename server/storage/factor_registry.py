"""因子库注册表、试验计数、引用保护与 AI 候选通道（T-10a/c/e/f，docs/tasks/T-10.md）。

核心保障：
1. **生命周期与权限**：
   - `CANDIDATE` →（自动验证通过）→ `VALIDATED` →（仅 OWNER 二次确认批准）→ `ACTIVE` → `RETIRED`；
   - 验证未通过进入 `REJECTED`，保留失败原因与试验记录；
   - AI 只能经 `submit_ai_candidate` 提交声明式候选因子（受积木复杂度上限与每日试验预算约束），
     绝不能提交代码式因子，也不能自行转正（`promote_to_active` 仅限 OWNER）。
2. **删除与试验次数不可清零（T-10 第 4 节）**：
   - `factor_trials` 与 `factor_audit_events` 为只追加表（SQLite 触发器禁止改删）；
   - `ACTIVE` 状态的因子禁止直接删除，必须先停用（`retire`）；
   - 凡被策略版本、实验或决策引用过（`factor_references`）的因子，永不物理删除，只能停用；
   - 未被引用的 `CANDIDATE` / `REJECTED` 因子删除定义后，`total_trials()` 试验计数绝不减少。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from server.strategy.factors import (
    FactorAuthorKind,
    FactorDefinition,
    FactorError,
    FactorStage,
    FactorValidationReport,
)

PROMOTE_FACTOR_PHRASE = "CONFIRM PROMOTE FACTOR"
DELETE_FACTOR_PHRASE = "CONFIRM DELETE FACTOR"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS factor_versions (
    factor_id TEXT NOT NULL,
    version TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    author_kind TEXT NOT NULL,
    author_name TEXT NOT NULL,
    definition_hash TEXT NOT NULL,
    warmup_days INTEGER NOT NULL,
    stage TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    validation_report_id TEXT,
    deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
    PRIMARY KEY (factor_id, version)
);

CREATE TABLE IF NOT EXISTS factor_trials (
    trial_id TEXT PRIMARY KEY,
    factor_id TEXT NOT NULL,
    version TEXT NOT NULL,
    author_kind TEXT NOT NULL,
    recorded_day TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    verdict TEXT NOT NULL,
    report_json TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS factor_trials_no_update
BEFORE UPDATE ON factor_trials
BEGIN
    SELECT RAISE(ABORT, 'factor_trials is append-only: UPDATE forbidden');
END;

CREATE TRIGGER IF NOT EXISTS factor_trials_no_delete
BEFORE DELETE ON factor_trials
BEGIN
    SELECT RAISE(ABORT, 'factor_trials is append-only: DELETE forbidden');
END;

CREATE TRIGGER IF NOT EXISTS factor_trials_no_replace
BEFORE INSERT ON factor_trials
WHEN EXISTS (SELECT 1 FROM factor_trials WHERE trial_id = NEW.trial_id)
BEGIN
    SELECT RAISE(ABORT, 'factor_trials is append-only: REPLACE forbidden');
END;

CREATE TABLE IF NOT EXISTS factor_references (
    factor_id TEXT NOT NULL,
    version TEXT NOT NULL,
    consumer_kind TEXT NOT NULL,
    consumer_id TEXT NOT NULL,
    linked_at TEXT NOT NULL,
    PRIMARY KEY (factor_id, version, consumer_kind, consumer_id)
);

CREATE TRIGGER IF NOT EXISTS factor_references_no_delete
BEFORE DELETE ON factor_references
BEGIN
    SELECT RAISE(ABORT, 'factor_references cannot be deleted');
END;

CREATE TABLE IF NOT EXISTS factor_audit_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_id TEXT NOT NULL,
    version TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    detail_json TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS factor_audit_no_update
BEFORE UPDATE ON factor_audit_events
BEGIN
    SELECT RAISE(ABORT, 'factor_audit_events is append-only: UPDATE forbidden');
END;

CREATE TRIGGER IF NOT EXISTS factor_audit_no_delete
BEFORE DELETE ON factor_audit_events
BEGIN
    SELECT RAISE(ABORT, 'factor_audit_events is append-only: DELETE forbidden');
END;
"""


def _iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise FactorError("时间必须带时区")
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


class FactorRegistryRecord:
    """注册表中的因子版本元数据快照。"""

    def __init__(  # noqa: PLR0913
        self,
        *,
        factor_id: str,
        version: str,
        hypothesis: str,
        author_kind: FactorAuthorKind,
        author_name: str,
        definition_hash: str,
        warmup_days: int,
        stage: FactorStage,
        created_at: datetime,
        updated_at: datetime,
        validation_report_id: str | None,
        deleted: bool,
    ) -> None:
        self.factor_id = factor_id
        self.version = version
        self.hypothesis = hypothesis
        self.author_kind = author_kind
        self.author_name = author_name
        self.definition_hash = definition_hash
        self.warmup_days = warmup_days
        self.stage = stage
        self.created_at = created_at
        self.updated_at = updated_at
        self.validation_report_id = validation_report_id
        self.deleted = deleted


class FactorRegistry:
    """meta.db 中的因子注册表（含试验计数、引用追踪、二次确认转正/删除与 AI 预算门禁）。"""

    def __init__(self, db_path: Path, *, daily_ai_budget: int = 10) -> None:
        if daily_ai_budget < 1:
            raise ValueError("daily_ai_budget 必须为正整数")
        self._path = db_path
        self._daily_ai_budget = daily_ai_budget
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

    def add_candidate(self, defn: FactorDefinition) -> FactorRegistryRecord:
        """手动添加候选因子（OWNER）。若为 AI 作者请走 submit_ai_candidate（含预算检查）。"""
        if defn.author_kind is FactorAuthorKind.AI:
            return self.submit_ai_candidate(defn)
        return self._insert_candidate(defn)

    def submit_ai_candidate(self, defn: FactorDefinition) -> FactorRegistryRecord:
        """AI 候选通道（T-10f）：强制声明式、复杂度上限与每日提交/试验预算。"""
        if defn.author_kind is not FactorAuthorKind.AI:
            raise FactorError("submit_ai_candidate 仅用于 AI 候选因子")
        day_iso = defn.created_at.astimezone(UTC).date().isoformat()
        used_today = self.ai_submissions_on_day(defn.created_at.astimezone(UTC).date())
        if used_today >= self._daily_ai_budget:
            raise FactorError(
                f"AI 在 {day_iso} 的因子提交/试验次数已达每日预算上限 {self._daily_ai_budget}"
            )
        return self._insert_candidate(defn)

    def _insert_candidate(self, defn: FactorDefinition) -> FactorRegistryRecord:
        existing = self.get(defn.factor_id, defn.version, include_deleted=True)
        if existing is not None:
            raise FactorError(
                f"因子版本 {defn.factor_id}:{defn.version} 已存在：版本不可变，修改请新建版本"
            )
        now_str = _iso_utc(defn.created_at)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO factor_versions (
                    factor_id, version, hypothesis, author_kind, author_name,
                    definition_hash, warmup_days, stage, created_at, updated_at,
                    validation_report_id, deleted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)
                """,
                (
                    defn.factor_id,
                    defn.version,
                    defn.hypothesis,
                    defn.author_kind.value,
                    defn.author_name,
                    defn.definition_hash,
                    defn.warmup_days,
                    FactorStage.CANDIDATE.value,
                    now_str,
                    now_str,
                ),
            )
            self._audit(
                conn,
                defn.factor_id,
                defn.version,
                "add_candidate",
                f"{defn.author_kind.value}:{defn.author_name}",
                now_str,
                {"hypothesis": defn.hypothesis, "definition_hash": defn.definition_hash},
            )
        return self._require(defn.factor_id, defn.version)

    def record_validation(
        self, report: FactorValidationReport, *, recorded_at: datetime
    ) -> FactorRegistryRecord:
        """登记一次自动验证结果（计入只追加 factor_trials），状态推进到 VALIDATED 或 REJECTED。"""
        rec = self._require(report.factor_id, report.version)
        if rec.stage is not FactorStage.CANDIDATE:
            raise FactorError(f"只有 CANDIDATE 状态的因子可以执行验证（当前：{rec.stage}）")
        if report.definition_hash != rec.definition_hash:
            raise FactorError("验证报告的 definition_hash 与注册的因子版本不一致")

        new_stage = FactorStage.VALIDATED if report.passed else FactorStage.REJECTED
        now_str = _iso_utc(recorded_at)
        day_str = recorded_at.astimezone(UTC).date().isoformat()
        report_payload = json.dumps(
            {
                "report_id": report.report_id,
                "verdict": report.verdict,
                "passed": report.passed,
                "sample_days": report.sample_days,
                "rank_ic_1d": str(report.rank_ic_1d),
                "oos_rank_ic": str(report.oos_rank_ic),
                "reasons": list(report.reasons),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO factor_trials (
                    trial_id, factor_id, version, author_kind,
                    recorded_day, recorded_at, verdict, report_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.report_id,
                    report.factor_id,
                    report.version,
                    rec.author_kind.value,
                    day_str,
                    now_str,
                    report.verdict,
                    report_payload,
                ),
            )
            conn.execute(
                """
                UPDATE factor_versions
                SET stage = ?, validation_report_id = ?, updated_at = ?
                WHERE factor_id = ? AND version = ?
                """,
                (new_stage.value, report.report_id, now_str, report.factor_id, report.version),
            )
            self._audit(
                conn,
                report.factor_id,
                report.version,
                "validate",
                "SYSTEM:validator",
                now_str,
                {"report_id": report.report_id, "stage": new_stage.value},
            )
        return self._require(report.factor_id, report.version)

    def promote_to_active(  # noqa: PLR0913
        self,
        factor_id: str,
        version: str,
        *,
        actor_kind: FactorAuthorKind,
        actor_name: str,
        at: datetime,
        confirmation_phrase: str,
    ) -> FactorRegistryRecord:
        """转正为正式因子（ACTIVE）：必须已通过自动验证（VALIDATED），且仅限 OWNER 二次确认批准。"""
        if actor_kind is not FactorAuthorKind.OWNER:
            raise FactorError("AI 不能批准因子转正：转正必须由 OWNER（Dylan）本人批准")
        if confirmation_phrase != PROMOTE_FACTOR_PHRASE:
            raise FactorError(f"因子转正须输入二次确认短语：{PROMOTE_FACTOR_PHRASE}")
        rec = self._require(factor_id, version)
        if rec.stage is not FactorStage.VALIDATED or not rec.validation_report_id:
            raise FactorError(f"只有已通过自动验证（VALIDATED）的因子才能转正（当前：{rec.stage}）")
        now_str = _iso_utc(at)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE factor_versions
                SET stage = ?, updated_at = ?
                WHERE factor_id = ? AND version = ?
                """,
                (FactorStage.ACTIVE.value, now_str, factor_id, version),
            )
            self._audit(
                conn,
                factor_id,
                version,
                "promote_active",
                f"OWNER:{actor_name}",
                now_str,
                {"validation_report_id": rec.validation_report_id},
            )
        return self._require(factor_id, version)

    def register_reference(
        self,
        factor_id: str,
        version: str,
        *,
        consumer_kind: str,
        consumer_id: str,
        at: datetime,
    ) -> list[str]:
        """登记策略、实验或决策对正式因子的引用。非 ACTIVE 因子不允许被策略引用。"""
        rec = self._require(factor_id, version)
        if consumer_kind == "strategy" and rec.stage is not FactorStage.ACTIVE:
            raise FactorError(
                f"策略 {consumer_id} 只能引用 ACTIVE 因子，{factor_id}:{version} 当前为 {rec.stage}"
            )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO factor_references (
                    factor_id, version, consumer_kind, consumer_id, linked_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (factor_id, version, consumer_kind, consumer_id, _iso_utc(at)),
            )
        return self.referenced_by(factor_id, version)

    def referenced_by(self, factor_id: str, version: str) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT consumer_kind, consumer_id FROM factor_references
                WHERE factor_id = ? AND version = ?
                ORDER BY consumer_kind, consumer_id
                """,
                (factor_id, version),
            ).fetchall()
        return [f"{r[0]}:{r[1]}" for r in rows]

    def retire_factor(  # noqa: PLR0913
        self,
        factor_id: str,
        version: str,
        *,
        actor_kind: FactorAuthorKind,
        actor_name: str,
        at: datetime,
        reason: str,
    ) -> tuple[FactorRegistryRecord, list[str]]:
        """停用因子（RETIRED）：仅限 OWNER，返回 (更新后的记录, 受影响的引用方列表)。"""
        if actor_kind is not FactorAuthorKind.OWNER:
            raise FactorError("只有 OWNER 能停用因子")
        if not reason.strip():
            raise FactorError("停用因子必须填写原因")
        rec = self._require(factor_id, version)
        if rec.stage is FactorStage.RETIRED:
            raise FactorError("因子已经处于停用状态")
        now_str = _iso_utc(at)
        affected = self.referenced_by(factor_id, version)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE factor_versions
                SET stage = ?, updated_at = ?
                WHERE factor_id = ? AND version = ?
                """,
                (FactorStage.RETIRED.value, now_str, factor_id, version),
            )
            self._audit(
                conn,
                factor_id,
                version,
                "retire",
                f"OWNER:{actor_name}",
                now_str,
                {"reason": reason, "affected": affected},
            )
        return self._require(factor_id, version), affected

    def delete_factor(  # noqa: PLR0913
        self,
        factor_id: str,
        version: str,
        *,
        actor_kind: FactorAuthorKind,
        actor_name: str,
        at: datetime,
        reason: str,
        confirmation_phrase: str | None = None,
    ) -> None:
        """按 T-10 第 4 节规则删除因子定义：
        - 正式（ACTIVE）因子不能直接删除，必须先停用；
        - 被策略版本、实验或决策引用过的因子，永不物理删除，只能停用；
        - 停用过的因子删除须提供二次确认短语；
        - 删除后 factor_trials 试验记录永久保留，total_trials() 不减少。
        """
        if actor_kind is not FactorAuthorKind.OWNER:
            raise FactorError("只有 OWNER 能删除因子")
        if not reason.strip():
            raise FactorError("删除因子必须填写原因")
        rec = self._require(factor_id, version)
        if rec.stage is FactorStage.ACTIVE:
            raise FactorError("正式（ACTIVE）因子不能直接删除，请先停用")
        refs = self.referenced_by(factor_id, version)
        if refs:
            raise FactorError(
                f"因子 {factor_id}:{version} 已被 {', '.join(refs)} 引用过，禁止删除，只能停用"
            )
        if rec.stage is FactorStage.RETIRED and confirmation_phrase != DELETE_FACTOR_PHRASE:
            raise FactorError(f"删除已停用的正式因子须输入二次确认短语：{DELETE_FACTOR_PHRASE}")
        now_str = _iso_utc(at)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE factor_versions
                SET deleted = 1, updated_at = ?
                WHERE factor_id = ? AND version = ?
                """,
                (now_str, factor_id, version),
            )
            self._audit(
                conn,
                factor_id,
                version,
                "delete",
                f"OWNER:{actor_name}",
                now_str,
                {"reason": reason, "previous_stage": rec.stage.value},
            )

    def get(
        self, factor_id: str, version: str, *, include_deleted: bool = False
    ) -> FactorRegistryRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT factor_id, version, hypothesis, author_kind, author_name,
                       definition_hash, warmup_days, stage, created_at, updated_at,
                       validation_report_id, deleted
                FROM factor_versions
                WHERE factor_id = ? AND version = ?
                """,
                (factor_id, version),
            ).fetchone()
        if row is None:
            return None
        rec = _row_to_record(row)
        if rec.deleted and not include_deleted:
            return None
        return rec

    def list_factors(self, *, include_deleted: bool = False) -> list[FactorRegistryRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT factor_id, version, hypothesis, author_kind, author_name,
                       definition_hash, warmup_days, stage, created_at, updated_at,
                       validation_report_id, deleted
                FROM factor_versions
                ORDER BY factor_id ASC, version ASC
                """
            ).fetchall()
        out = [_row_to_record(r) for r in rows]
        if not include_deleted:
            out = [r for r in out if not r.deleted]
        return out

    def total_trials(self) -> int:
        """返回因子库累计验证试验次数（删除候选因子后绝不减少，用于多重检验校正）。"""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM factor_trials").fetchone()
        return int(row[0]) if row is not None else 0

    def ai_submissions_on_day(self, day: date) -> int:
        prefix = day.isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM factor_audit_events
                WHERE action = 'add_candidate' AND actor LIKE 'AI:%' AND occurred_at LIKE ?
                """,
                (f"{prefix}%",),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def audit_trail(self, factor_id: str) -> list[tuple[str, str, str, dict[str, Any]]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT version, action, actor, detail_json
                FROM factor_audit_events
                WHERE factor_id = ?
                ORDER BY seq ASC
                """,
                (factor_id,),
            ).fetchall()
        return [(str(r[0]), str(r[1]), str(r[2]), json.loads(str(r[3]))) for r in rows]

    def _require(self, factor_id: str, version: str) -> FactorRegistryRecord:
        rec = self.get(factor_id, version)
        if rec is None:
            raise FactorError(f"因子版本不存在或已删除：{factor_id}:{version}")
        return rec

    @staticmethod
    def _audit(  # noqa: PLR0913, PLR0917
        conn: sqlite3.Connection,
        factor_id: str,
        version: str,
        action: str,
        actor: str,
        occurred_at: str,
        detail: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO factor_audit_events (
                factor_id, version, action, actor, occurred_at, detail_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                factor_id,
                version,
                action,
                actor,
                occurred_at,
                json.dumps(detail, ensure_ascii=False, sort_keys=True),
            ),
        )


def _row_to_record(row: tuple[Any, ...]) -> FactorRegistryRecord:
    return FactorRegistryRecord(
        factor_id=str(row[0]),
        version=str(row[1]),
        hypothesis=str(row[2]),
        author_kind=FactorAuthorKind(str(row[3])),
        author_name=str(row[4]),
        definition_hash=str(row[5]),
        warmup_days=int(row[6]),
        stage=FactorStage(str(row[7])),
        created_at=datetime.fromisoformat(str(row[8])),
        updated_at=datetime.fromisoformat(str(row[9])),
        validation_report_id=str(row[10]) if row[10] is not None else None,
        deleted=bool(row[11]),
    )


__all__ = (
    "DELETE_FACTOR_PHRASE",
    "PROMOTE_FACTOR_PHRASE",
    "FactorRegistry",
    "FactorRegistryRecord",
)
