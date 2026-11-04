"""alerts.db 与 api.db v2（S-05a）：只追加、按发生编号确认、只读读取、失败即关闭。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from server.storage.alertsdb import (
    AlertsDbError,
    AlertsReader,
    AlertStore,
    alerts_db_path,
    read_ack_requests,
    read_alert,
    read_alerts,
)
from server.storage.apidb import ApiDb, ApiDbError


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "alerts.db"


def test_lifecycle_is_derived_from_events(path: Path) -> None:
    store = AlertStore(path)
    first = store.raised("k1", "P0", "alert_test", {"detail": "一"})
    second = store.raised("k2", "P1", "alert_test", {"detail": "二"})
    store.acked(first, "k1", actor="dylan")
    store.resolved(second, "k2")
    store.close()
    alerts = read_alerts(path)
    assert [a.alert_id for a in alerts] == [second, first]  # 从新到旧
    one, two = alerts[1], alerts[0]
    assert (one.acked_by, one.resolved_at) == ("dylan", None)
    assert two.acked_by is None
    assert two.resolved_at is not None
    assert dict(one.params) == {"detail": "一"}
    store_id, found = read_alert(path, first)
    assert found == one
    assert store_id
    assert read_alert(path, 999)[1] is None
    reopened = AlertStore(path)
    assert [a.alert_id for a in reopened.unresolved()] == [first]


def test_events_are_append_only(path: Path) -> None:
    store = AlertStore(path)
    store.raised("k", "P0", "alert_test", {"detail": "x"})
    store.close()
    conn = sqlite3.connect(path)
    for sql in ("UPDATE alert_events SET level = 'P3'", "DELETE FROM alert_events"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute(sql)
    conn.close()


def test_ack_and_resolve_only_once_per_occurrence(path: Path) -> None:
    store = AlertStore(path)
    alert_id = store.raised("k", "P0", "alert_test", {"detail": "x"})
    store.acked(alert_id, "k", actor="a")
    with pytest.raises(AlertsDbError):
        store.acked(alert_id, "k", actor="b")
    with pytest.raises(AlertsDbError):
        store.acked(12345, "k", actor="b")  # 引用不存在的发生：外键拒绝
    with pytest.raises(AlertsDbError):
        store.raised("k", "PX", "alert_test", {})


def test_missing_trigger_or_unknown_version_refused(path: Path) -> None:
    AlertStore(path).close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER alert_events_no_delete")
    conn.commit()
    conn.close()
    with pytest.raises(AlertsDbError, match="触发器"):
        AlertStore(path)
    with pytest.raises(AlertsDbError, match="触发器"):
        read_alerts(path)
    other = path.with_name("other.db")
    conn = sqlite3.connect(other)
    conn.execute("PRAGMA user_version = 7")
    conn.close()
    with pytest.raises(AlertsDbError, match="版本"):
        AlertStore(other)


def test_reader_fails_closed(path: Path) -> None:
    with pytest.raises(AlertsDbError):
        read_alerts(path)  # 文件不存在：不是“没有告警”
    assert not path.exists()  # 只读读取不会创建文件
    path.write_bytes(b"not a database" * 100)
    with pytest.raises(AlertsDbError):
        AlertsReader(path).recent(10)


def test_reader_is_read_only(path: Path) -> None:
    AlertStore(path).close()
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO alert_events (at, kind, alert_key) VALUES ('t', 'resolved', 'k')")
    conn.close()


def test_alerts_db_sits_next_to_api_db(tmp_path: Path) -> None:
    assert alerts_db_path(tmp_path / "data" / "api.db") == tmp_path / "data" / "alerts.db"


# ---------- api.db v2 ----------


def test_ack_request_and_audit_written_together(tmp_path: Path) -> None:
    db = ApiDb(tmp_path / "api.db")
    assert not db.ack_requested(7, "s1")
    db.request_ack(7, "k", "s1", actor="dylan")
    [req] = read_ack_requests(tmp_path / "api.db", 0)
    assert (req.seq, req.alert_id, req.alert_key, req.store_id, req.actor) == (
        1,
        7,
        "k",
        "s1",
        "dylan",
    )
    assert req.at.tzinfo is not None
    assert db.ack_requested(7, "s1")
    assert not db.ack_requested(7, "other-store")
    assert read_ack_requests(tmp_path / "api.db", 1) == []
    audits = db.audits("alert_ack_requested")
    assert [(a[2], a[3]) for a in audits] == [("dylan", {"alert_id": "7", "alert_key": "k"})]
    with pytest.raises(ApiDbError):
        db.request_ack(0, "k", "s1", actor="dylan")
    with pytest.raises(ApiDbError):
        db.request_ack(1, " ", "s1", actor="dylan")
    with pytest.raises(ApiDbError):
        db.request_ack(1, "k", " ", actor="dylan")
    conn = sqlite3.connect(tmp_path / "api.db")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM alert_acks")
    conn.close()


def test_ack_request_rolled_back_when_audit_fails(tmp_path: Path) -> None:
    db = ApiDb(tmp_path / "api.db")
    conn = sqlite3.connect(tmp_path / "api.db")
    conn.execute(
        "CREATE TRIGGER audit_breaks BEFORE INSERT ON api_audit"
        " BEGIN SELECT RAISE(ABORT, 'broken'); END"
    )
    conn.commit()
    conn.close()
    with pytest.raises(ApiDbError):
        db.request_ack(3, "k", "s1", actor="dylan")
    assert read_ack_requests(tmp_path / "api.db", 0) == []  # 审计写不进去，确认请求也不落盘


def test_v1_database_is_upgraded_in_place(tmp_path: Path) -> None:
    file = tmp_path / "api.db"
    db = ApiDb(file)
    token_id, _ = db.issue("dylan", "owner", days=30, actor="t")
    db.close()
    conn = sqlite3.connect(file)  # 退回到 v1 的样子：去掉 v2 的表
    conn.executescript(
        "DROP TRIGGER alert_acks_no_update; DROP TRIGGER alert_acks_no_delete;"
        " DROP TABLE alert_acks; PRAGMA user_version = 1;"
    )
    conn.close()
    upgraded = ApiDb(file)
    assert [t.token_id for t in upgraded.tokens()] == [token_id]  # 旧数据还在
    upgraded.request_ack(1, "k", "s1", actor="dylan")
    assert [r.alert_id for r in read_ack_requests(file, 0)] == [1]
    backup = tmp_path / "api.db.v1.bak"  # 升级前自动留了 v1 备份（回滚用）
    conn = sqlite3.connect(backup)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM token_events").fetchone()[0] == 1
    conn.close()


def test_ack_reader_refuses_old_schema(tmp_path: Path) -> None:
    file = tmp_path / "api.db"
    conn = sqlite3.connect(file)
    conn.execute("PRAGMA user_version = 1")
    conn.close()
    with pytest.raises(AlertsDbError, match="版本"):
        read_ack_requests(file, 0)
    with pytest.raises(AlertsDbError):
        read_ack_requests(tmp_path / "missing.db", 0)


def test_recent_audits_newest_first_with_paging(tmp_path: Path) -> None:
    db = ApiDb(tmp_path / "api.db")
    for i in range(5):
        db.audit("a", "x", {"i": str(i)})
    page = db.recent_audits(2)
    assert [r[4]["i"] for r in page] == ["4", "3"]
    older = db.recent_audits(2, before=page[-1][0])
    assert [r[4]["i"] for r in older] == ["2", "1"]
    with pytest.raises(ApiDbError):
        db.recent_audits(0)


# ---------- 评审修复（S-05a） ----------


def test_rebuilt_store_gets_a_new_store_id(path: Path) -> None:
    first = AlertStore(path)
    old_id = first.store_id
    first.close()
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    assert AlertStore(path).store_id != old_id


def test_unresolved_alerts_are_never_cut_by_the_limit(path: Path) -> None:
    store = AlertStore(path)
    old = store.raised("still-open", "P0", "alert_test", {"detail": "old"})
    for i in range(5):  # 反复发生又解除
        store.resolved(store.raised(f"flap-{i}", "P1", "alert_test", {"detail": "x"}), f"flap-{i}")
    ids = [a.alert_id for a in read_alerts(path, limit=2)]
    assert old in ids  # 仍未解除的旧告警不会被挤出
    assert len(ids) == 3


def test_parent_that_is_a_file_is_an_alerts_db_error(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    with pytest.raises(AlertsDbError):
        AlertStore(blocker / "alerts.db")


def test_status_marker_gates_the_reader(path: Path) -> None:
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from server.storage.alertsdb import write_status  # noqa: PLC0415

    now = datetime(2026, 11, 4, 14, 0, tzinfo=UTC)
    AlertStore(path).close()
    reader = AlertsReader(path, clock=lambda: now)
    with pytest.raises(AlertsDbError, match="健康标记"):
        reader.recent(10)  # 没有标记：guardd 可能没在运行
    write_status(path, store_ok=True, at=now - timedelta(seconds=5))
    assert reader.recent(10) == []
    write_status(path, store_ok=True, at=now - timedelta(minutes=5))
    with pytest.raises(AlertsDbError, match="过期"):
        reader.recent(10)
    write_status(path, store_ok=False, at=now)
    with pytest.raises(AlertsDbError, match="降级"):
        reader.one(1)


def test_reader_works_after_writer_closed(path: Path) -> None:
    store = AlertStore(path)
    store.raised("k", "P0", "alert_test", {"detail": "x"})
    store.close()  # guardd 正常退出后 -wal、-shm 可能已被清掉
    assert [a.key for a in read_alerts(path)] == ["k"]
