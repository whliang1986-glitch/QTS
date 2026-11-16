"""AI 回放缓存与研究知识库只追加存储（A-05 / A-06）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from server.ai.orchestrator import KnowledgeNote

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ai_replay_cache (
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    content_json TEXT NOT NULL,
    tokens_used INTEGER NOT NULL,
    cached_at TEXT NOT NULL,
    PRIMARY KEY (provider, model, prompt_hash, schema_version)
);

CREATE TABLE IF NOT EXISTS ai_knowledge_notes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id TEXT NOT NULL UNIQUE,
    topic TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    content TEXT NOT NULL,
    source_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS ai_knowledge_no_update
BEFORE UPDATE ON ai_knowledge_notes
BEGIN
    SELECT RAISE(ABORT, 'ai_knowledge_notes is append-only: UPDATE forbidden');
END;

CREATE TRIGGER IF NOT EXISTS ai_knowledge_no_delete
BEFORE DELETE ON ai_knowledge_notes
BEGIN
    SELECT RAISE(ABORT, 'ai_knowledge_notes is append-only: DELETE forbidden');
END;
"""


class AiStorage:
    """meta.db 中的 AI 确定性回放缓存与只追加研究知识库。"""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA recursive_triggers = ON")
        return conn

    # ---------- A-05 ReplayCache ----------

    def get_cached(
        self, *, provider: str, model: str, prompt_hash: str, schema_version: str
    ) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT content_json FROM ai_replay_cache
                WHERE provider = ? AND model = ? AND prompt_hash = ? AND schema_version = ?
                """,
                (provider, model, prompt_hash, schema_version),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def put_cached(  # noqa: PLR0913
        self,
        *,
        provider: str,
        model: str,
        prompt_hash: str,
        schema_version: str,
        content_json: str,
        tokens_used: int,
        at: datetime,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO ai_replay_cache (
                    provider, model, prompt_hash, schema_version,
                    content_json, tokens_used, cached_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    provider,
                    model,
                    prompt_hash,
                    schema_version,
                    content_json,
                    int(tokens_used),
                    at.astimezone(UTC).isoformat(timespec="microseconds"),
                ),
            )

    # ---------- A-06 KnowledgeStore ----------

    def append_note(self, note: KnowledgeNote) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO ai_knowledge_notes (
                    note_id, topic, tags_json, content, source_id, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    note.note_id,
                    note.topic,
                    json.dumps(list(note.tags), ensure_ascii=False),
                    note.content,
                    note.source_id,
                    note.recorded_at.astimezone(UTC).isoformat(timespec="microseconds"),
                ),
            )

    def search_notes(self, query: str, *, limit: int = 5) -> list[KnowledgeNote]:
        terms = [t.strip().lower() for t in query.split() if t.strip()]
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT note_id, topic, tags_json, content, source_id, recorded_at
                FROM ai_knowledge_notes
                ORDER BY seq DESC
                LIMIT 200
                """
            ).fetchall()
        matched: list[KnowledgeNote] = []
        for r in rows:
            note = KnowledgeNote(
                note_id=str(r[0]),
                topic=str(r[1]),
                tags=tuple(str(x) for x in json.loads(str(r[2]))),
                content=str(r[3]),
                source_id=str(r[4]),
                recorded_at=datetime.fromisoformat(str(r[5])),
            )
            haystack = f"{note.topic} {note.content} {' '.join(note.tags)}".lower()
            if not terms or any(t in haystack for t in terms):
                matched.append(note)
                if len(matched) >= limit:
                    break
        return matched


__all__ = ("AiStorage",)
