"""实验注册表（T-07，V4.1 8.3 & economic-gate.md 第 0–1 节）：只追加存储于 meta.db。

核心职责：
1. **每次回测必登记**：绑定 `experiment_id`、`strategy_id`、`strategy_version`、`data_version`、
   `code_commit`、`params_hash`、`cost_model`、样本内外区间、核心绩效与产物目录；
2. **只追加防篡改**：通过 SQLite 触发器禁止 UPDATE / DELETE / REPLACE，严防删改失败实验以刷高
   Deflated Sharpe Ratio；
3. **累计试验次数统计**：`trial_count(strategy_id)` 返回该策略累计运行过的回测次数（含已归档版本），
   供 T-06 / T-08 计算 Deflated Sharpe 校正；
4. **样本外封存单次使用锁**：同一 `(strategy_id, strategy_version)` 的封存样本外
   （`is_out_of_sample=True`）只允许评估一次；重复评估抛 `ConflictError`（幂等重放除外）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from server.storage.errors import ConflictError, StorageError

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    start_day TEXT NOT NULL,
    end_day TEXT NOT NULL,
    is_out_of_sample INTEGER NOT NULL CHECK (is_out_of_sample IN (0, 1)),
    data_version TEXT NOT NULL,
    code_commit TEXT NOT NULL,
    params_hash TEXT NOT NULL,
    cost_model TEXT NOT NULL,
    sharpe TEXT NOT NULL,
    annualized_return TEXT NOT NULL,
    max_drawdown TEXT NOT NULL,
    rebalance_count INTEGER NOT NULL CHECK (rebalance_count >= 0),
    survivorship_bias_free INTEGER NOT NULL CHECK (survivorship_bias_free IN (0, 1)),
    artifact_dir TEXT NOT NULL,
    manifest_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_experiments_strategy
    ON experiments (strategy_id, strategy_version);

CREATE TRIGGER IF NOT EXISTS experiments_no_update
BEFORE UPDATE ON experiments
BEGIN
    SELECT RAISE(ABORT, 'experiments is append-only: UPDATE forbidden');
END;

CREATE TRIGGER IF NOT EXISTS experiments_no_delete
BEFORE DELETE ON experiments
BEGIN
    SELECT RAISE(ABORT, 'experiments is append-only: DELETE forbidden');
END;

CREATE TRIGGER IF NOT EXISTS experiments_no_replace
BEFORE INSERT ON experiments
WHEN EXISTS (SELECT 1 FROM experiments WHERE experiment_id = NEW.experiment_id)
BEGIN
    SELECT RAISE(ABORT, 'experiments is append-only: REPLACE forbidden');
END;
"""


@dataclass(frozen=True)
class ExperimentRecord:
    experiment_id: str
    strategy_id: str
    strategy_version: str
    recorded_at: datetime
    start_day: date
    end_day: date
    is_out_of_sample: bool
    data_version: str
    code_commit: str
    params_hash: str
    cost_model: str
    sharpe: Decimal
    annualized_return: Decimal
    max_drawdown: Decimal
    rebalance_count: int
    survivorship_bias_free: bool
    artifact_dir: str
    manifest: Mapping[str, Any]


def _iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("recorded_at 必须带时区")
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


class ExperimentRegistry:
    """meta.db 中的回测实验只追加注册表。"""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
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

    def register(self, record: ExperimentRecord) -> bool:
        """登记回测实验。同 ID 同内容幂等返回 False；不同内容或二次用样本外抛 ConflictError。"""
        existing = self.get(record.experiment_id)
        if existing is not None:
            if existing == record:
                return False
            raise ConflictError(f"实验 {record.experiment_id} 已存在且内容不一致")

        if record.is_out_of_sample:
            prior_oos = [
                r
                for r in self.list_for_strategy(record.strategy_id, version=record.strategy_version)
                if r.is_out_of_sample
            ]
            if prior_oos:
                raise ConflictError(
                    f"策略版本 {record.strategy_id}:{record.strategy_version} "
                    f"已使用过封存样本外（实验 {prior_oos[0].experiment_id}），禁止二次窥探"
                )

        manifest_str = json.dumps(dict(record.manifest), ensure_ascii=False, sort_keys=True)
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO experiments (
                        experiment_id, strategy_id, strategy_version, recorded_at,
                        start_day, end_day, is_out_of_sample, data_version, code_commit,
                        params_hash, cost_model, sharpe, annualized_return, max_drawdown,
                        rebalance_count, survivorship_bias_free, artifact_dir, manifest_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.experiment_id,
                        record.strategy_id,
                        record.strategy_version,
                        _iso_utc(record.recorded_at),
                        record.start_day.isoformat(),
                        record.end_day.isoformat(),
                        1 if record.is_out_of_sample else 0,
                        record.data_version,
                        record.code_commit,
                        record.params_hash,
                        record.cost_model,
                        str(record.sharpe),
                        str(record.annualized_return),
                        str(record.max_drawdown),
                        int(record.rebalance_count),
                        1 if record.survivorship_bias_free else 0,
                        record.artifact_dir,
                        manifest_str,
                    ),
                )
        except sqlite3.Error as exc:
            raise StorageError(f"写入实验注册表失败：{exc}") from exc
        return True

    def get(self, experiment_id: str) -> ExperimentRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT experiment_id, strategy_id, strategy_version, recorded_at,
                       start_day, end_day, is_out_of_sample, data_version, code_commit,
                       params_hash, cost_model, sharpe, annualized_return, max_drawdown,
                       rebalance_count, survivorship_bias_free, artifact_dir, manifest_json
                FROM experiments WHERE experiment_id = ?
                """,
                (experiment_id,),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def list_for_strategy(
        self, strategy_id: str, *, version: str | None = None
    ) -> list[ExperimentRecord]:
        cols = (
            "experiment_id, strategy_id, strategy_version, recorded_at, "
            "start_day, end_day, is_out_of_sample, data_version, code_commit, "
            "params_hash, cost_model, sharpe, annualized_return, max_drawdown, "
            "rebalance_count, survivorship_bias_free, artifact_dir, manifest_json"
        )
        with self._connect() as conn:
            if version is None:
                rows = conn.execute(
                    f"SELECT {cols} FROM experiments "  # noqa: S608
                    "WHERE strategy_id = ? ORDER BY recorded_at ASC",
                    (strategy_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {cols} FROM experiments "  # noqa: S608
                    "WHERE strategy_id = ? AND strategy_version = ? ORDER BY recorded_at ASC",
                    (strategy_id, version),
                ).fetchall()
        return [_from_row(r) for r in rows]

    def trial_count(self, strategy_id: str) -> int:
        """返回该策略下所有版本累计运行过的回测次数（用于 Deflated Sharpe 多重检验校正）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM experiments WHERE strategy_id = ?",
                (strategy_id,),
            ).fetchone()
        return int(row[0]) if row is not None else 0


def _from_row(row: tuple[Any, ...]) -> ExperimentRecord:
    return ExperimentRecord(
        experiment_id=str(row[0]),
        strategy_id=str(row[1]),
        strategy_version=str(row[2]),
        recorded_at=datetime.fromisoformat(str(row[3])),
        start_day=date.fromisoformat(str(row[4])),
        end_day=date.fromisoformat(str(row[5])),
        is_out_of_sample=bool(row[6]),
        data_version=str(row[7]),
        code_commit=str(row[8]),
        params_hash=str(row[9]),
        cost_model=str(row[10]),
        sharpe=Decimal(str(row[11])),
        annualized_return=Decimal(str(row[12])),
        max_drawdown=Decimal(str(row[13])),
        rebalance_count=int(row[14]),
        survivorship_bias_free=bool(row[15]),
        artifact_dir=str(row[16]),
        manifest=json.loads(str(row[17])),
    )


__all__ = ("ExperimentRecord", "ExperimentRegistry")
