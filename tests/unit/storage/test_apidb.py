"""api.db（S-03）：只存哈希、只追加、吊销与过期、结构校验。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from server.storage.apidb import ApiDb, ApiDbError


@pytest.fixture
def db(tmp_path: Path) -> ApiDb:
    return ApiDb(tmp_path / "api.db")


def test_plaintext_token_is_never_stored(tmp_path: Path, db: ApiDb) -> None:
    _, token = db.issue("dylan", "owner", days=30, actor="t")
    secret = token[len("qts_") + 16 + 1 :]  # 固定格式：secret 本身可能含下划线
    assert len(secret) == 43
    db.close()
    raw = (tmp_path / "api.db").read_bytes()
    wal = tmp_path / "api.db-wal"
    raw += wal.read_bytes() if wal.exists() else b""
    assert secret.encode() not in raw
    assert token.encode() not in raw


@pytest.mark.parametrize(
    "bad",
    ["", "qts", "qts_0123456789abcdef_short", "Bearer x", "qts_ZZZZ_" + "a" * 43, "x" * 5000],
)
def test_malformed_tokens_fail(db: ApiDb, bad: str) -> None:
    db.issue("dylan", "owner", days=30, actor="t")
    assert db.verify(bad) is None


def test_wrong_secret_for_existing_id_fails(db: ApiDb) -> None:
    _, token = db.issue("dylan", "owner", days=30, actor="t")
    head, secret = token[: len("qts_") + 17], token[len("qts_") + 17 :]
    forged = head + ("A" if secret[0] != "A" else "B") + secret[1:]
    assert db.verify(forged) is None
    assert db.verify(token) is not None


def test_tables_are_append_only(tmp_path: Path, db: ApiDb) -> None:
    db.issue("dylan", "owner", days=30, actor="t")
    conn = sqlite3.connect(tmp_path / "api.db")
    for sql in ("UPDATE token_events SET role = 'viewer'", "DELETE FROM token_events",
                "DELETE FROM api_audit", "UPDATE api_audit SET actor = 'x'"):  # fmt: skip
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute(sql)
    conn.close()


def test_revoke_requires_reason_and_known_id(db: ApiDb) -> None:
    token_id, _ = db.issue("dylan", "owner", days=30, actor="t")
    with pytest.raises(ApiDbError, match="原因"):
        db.revoke(token_id, actor="t", reason=" ")
    with pytest.raises(ApiDbError, match="没有这个令牌"):
        db.revoke("0" * 16, actor="t", reason="x")
    db.revoke(token_id, actor="t", reason="x")
    db.revoke(token_id, actor="t", reason="x")  # 幂等
    assert db.tokens()[0].revoked_at is not None


@pytest.mark.parametrize(
    ("name", "role", "days"),
    [("", "owner", 1), ("a b", "owner", 1), ("dylan", "admin", 1), ("dylan", "owner", 0),
     ("dylan", "owner", 366)],
)  # fmt: skip
def test_issue_validation(db: ApiDb, name: str, role: str, days: int) -> None:
    with pytest.raises(ApiDbError):
        db.issue(name, role, days=days, actor="t")


def test_unknown_schema_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "api.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 9")
    conn.close()
    with pytest.raises(ApiDbError, match="结构版本"):
        ApiDb(path)


def test_missing_trigger_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "api.db"
    ApiDb(path).close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER api_audit_no_delete")
    conn.close()
    with pytest.raises(ApiDbError, match="触发器"):
        ApiDb(path)


def test_token_and_audit_are_written_together(tmp_path: Path, db: ApiDb) -> None:
    """签发与审计同一事务：审计写不进去，令牌也不落盘。"""
    conn = sqlite3.connect(tmp_path / "api.db")
    conn.execute(
        "CREATE TRIGGER block_audit BEFORE INSERT ON api_audit BEGIN SELECT RAISE(ABORT, 'x'); END"
    )
    conn.commit()
    conn.close()
    with pytest.raises(ApiDbError, match="回滚"):
        db.issue("dylan", "owner", days=30, actor="t")
    assert db.tokens() == []


def test_expired_tokens_are_listed_as_expired(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    now = [datetime(2026, 11, 3, tzinfo=UTC)]
    db = ApiDb(tmp_path / "x.db", clock=lambda: now[0])
    db.issue("dylan", "owner", days=1, actor="t")
    assert db.tokens()[0].state(db.now()) == "有效"
    now[0] += timedelta(days=2)
    assert db.tokens()[0].state(db.now()) == "已过期"
