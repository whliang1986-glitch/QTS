"""C-03：SQLite 账本存储层——真实 SQLite 文件，不用 mock。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from server.domain.enums import Mode, OrderClass, OrderType, RiskVerdict, Side, TimeInForce
from server.domain.modes import AuditEvent, HaltState
from server.domain.order_state import EventKind, OrderEvent, OrderStatus
from server.domain.orders import BrokerOrder, Execution, OrderIntent, RiskDecision, RuleHit
from server.storage import schema
from server.storage.errors import ConflictError, StorageError
from server.storage.ledger import LEDGER_TABLES, LedgerRepository, apply_pragmas, open_ledger
from server.storage.migrate import current_revision, downgrade, head_revision, upgrade
from sqlalchemy import create_engine

T0 = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
NOT_HALTED = HaltState(halted=False)


def make_intent(intent_id: str = "int-1", qty: str = "10") -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        created_at=T0,
        strategy_id="s",
        strategy_version="1",
        account_id="DU0",
        symbol="SPY",
        side=Side.BUY,
        quantity=Decimal(qty),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("500.10"),
        order_class=OrderClass.OPEN,
        time_in_force=TimeInForce.DAY,
    )


def make_execution(eid: str = "exec-1", qty: str = "4", intent_id: str = "int-1") -> Execution:
    return Execution(
        execution_id=eid,
        intent_id=intent_id,
        broker_order_id="101",
        symbol="SPY",
        side=Side.BUY,
        quantity=Decimal(qty),
        price=Decimal("500.05"),
        commission=Decimal("0.35"),
        executed_at=T0 + timedelta(seconds=5),
    )


def make_broker_order(intent_id: str = "int-1", boid: str = "101") -> BrokerOrder:
    return BrokerOrder(
        intent_id=intent_id, broker_order_id=boid, order_class=OrderClass.OPEN, submitted_at=T0
    )


def make_event(eid: str, kind: EventKind, intent_id: str = "int-1") -> OrderEvent:
    return OrderEvent(
        event_id=eid, intent_id=intent_id, kind=kind, occurred_at=T0, correlation_id="c1"
    )


def make_decision(did: str = "d-1", message: str = "超限") -> RiskDecision:
    hit = RuleHit(rule_id="max_notional", rule_version="1", message=message)
    return RiskDecision(
        decision_id=did, intent_id="int-1", verdict=RiskVerdict.REJECT, rule_hits=(hit,),
        rule_set_version="1", decided_at=T0,
    )  # fmt: skip


def make_audit(action: str = "startup") -> AuditEvent:
    return AuditEvent(
        event_id="a1", occurred_at=T0, actor="system", action=action, correlation_id="c"
    )


@contextmanager
def raw(path: Path) -> Iterator[sqlite3.Connection]:
    """绕过 Repository 的原生连接（模拟有人直接写库），默认 isolation 自动提交关闭。"""
    with closing(sqlite3.connect(path)) as conn:
        yield conn


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.db"
    upgrade(path)
    return path


@pytest.fixture
def repo(db_path: Path) -> LedgerRepository:
    return LedgerRepository(open_ledger(db_path))


@pytest.fixture
def full_repo(repo: LedgerRepository) -> LedgerRepository:
    """9 张账本表各有至少一条真实行。"""
    repo.add_intent(make_intent())
    repo.add_broker_order(make_broker_order())
    repo.record_perm_id("int-1", "9001", T0)
    repo.append_order_event(make_event("ev-1", EventKind.RISK_APPROVED), OrderStatus.RISK_APPROVED)
    repo.add_execution(make_execution())
    repo.add_risk_decision(make_decision())
    repo.record_command("req-1", "halt_new_orders", "{}", T0)
    repo.append_audit(make_audit())
    halt = HaltState(halted=True, reason="急停", since=T0, set_by="dylan")
    repo.append_mode_change("mc-1", Mode.HALT, halt, T0, "dylan")
    return repo


# ---------- 连接参数 ----------


def test_pragmas_are_enforced(db_path: Path) -> None:
    with open_ledger(db_path).connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert conn.exec_driver_sql("PRAGMA synchronous").scalar() == 2  # FULL
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert conn.exec_driver_sql("PRAGMA recursive_triggers").scalar() == 1
        assert int(conn.exec_driver_sql("PRAGMA busy_timeout").scalar() or 0) >= 1000


def test_refuses_when_wal_cannot_be_enabled() -> None:
    # 内存库的 journal_mode 只能是 memory：真实复现“WAL 不生效”，不用 mock
    with closing(sqlite3.connect(":memory:")) as conn, pytest.raises(StorageError, match="WAL"):
        apply_pragmas(conn, read_only=False, busy_timeout_ms=1000)


def test_open_refuses_unmigrated_database(tmp_path: Path) -> None:
    with pytest.raises(StorageError, match="迁移"):
        LedgerRepository(open_ledger(tmp_path / "empty.db"))


def test_open_refuses_outdated_revision(db_path: Path) -> None:
    with raw(db_path) as conn:
        conn.execute("UPDATE alembic_version SET version_num = 'old'")
        conn.commit()
    with pytest.raises(StorageError, match="不是最新"):
        LedgerRepository(open_ledger(db_path))


def test_open_refuses_when_trigger_missing(db_path: Path) -> None:
    with raw(db_path) as conn:
        conn.execute("DROP TRIGGER executions_no_replace")
        conn.commit()
    with pytest.raises(StorageError, match="触发器缺失"):
        LedgerRepository(open_ledger(db_path))


def test_open_refuses_when_trigger_is_neutered(db_path: Path) -> None:
    # 同名触发器被换成空操作，也视为缺失
    with raw(db_path) as conn:
        conn.execute("DROP TRIGGER orders_no_update")
        conn.execute("CREATE TRIGGER orders_no_update BEFORE UPDATE ON orders BEGIN SELECT 1; END")
        conn.commit()
    with pytest.raises(StorageError, match="触发器缺失"):
        LedgerRepository(open_ledger(db_path))


@pytest.mark.parametrize("folder", ["plain", "账本 目录"])
def test_read_only_connection_cannot_write(tmp_path: Path, folder: str) -> None:
    path = tmp_path / folder / "ledger.db"
    upgrade(path)
    LedgerRepository(open_ledger(path)).add_intent(make_intent())
    reader = LedgerRepository(open_ledger(path, read_only=True))
    assert reader.get_intent("int-1") == make_intent()
    with pytest.raises(StorageError):
        reader.add_intent(make_intent("int-2"))


# ---------- 迁移 ----------


def test_upgrade_downgrade_upgrade_on_empty_ledger(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    upgrade(path)
    assert current_revision(path) == head_revision()
    downgrade(path, "base")
    assert current_revision(path) is None
    with raw(path) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names.isdisjoint(LEDGER_TABLES)
    upgrade(path)
    assert current_revision(path) == head_revision()


def test_current_revision_does_not_create_files(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    assert current_revision(missing) is None
    assert not missing.exists()


def test_upgrade_backs_up_existing_database(db_path: Path, repo: LedgerRepository) -> None:
    repo.add_intent(make_intent())
    backup = upgrade(db_path)
    assert backup is not None and backup.exists()
    with raw(backup) as conn:
        assert conn.execute("SELECT count(*) FROM orders").fetchone() == (1,)


def test_upgrade_of_new_database_needs_no_backup(tmp_path: Path) -> None:
    assert upgrade(tmp_path / "new.db") is None


def test_downgrade_with_data_requires_explicit_confirmation(
    db_path: Path, full_repo: LedgerRepository
) -> None:
    with pytest.raises(StorageError, match="物理删除"):
        downgrade(db_path, "base")
    assert current_revision(db_path) == head_revision()
    backup = downgrade(db_path, "base", allow_data_loss=True)
    with raw(backup) as conn:  # 降级前的备份完整
        for table in LEDGER_TABLES:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] >= 1, table
    del full_repo


def test_failed_migration_rolls_back_completely(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    with raw(path) as conn:  # 预先占用最后一张表的名字，让迁移在中途失败
        conn.execute("CREATE TABLE mode_changes (x INTEGER)")
        conn.commit()
    with pytest.raises(StorageError, match="回滚"):
        upgrade(path)
    with raw(path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        triggers = list(conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'"))
    assert tables == {"mode_changes"}  # 前面建的 8 张表都已回滚
    assert triggers == []
    assert current_revision(path) is None


def test_schema_module_matches_migration(db_path: Path) -> None:
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), schema.metadata)
    engine.dispose()
    assert diff == []


# ---------- 只追加：禁止改、删、替换 ----------

# 每张表：(UPDATE 语句, 用已有键做 REPLACE 的 INSERT 列与值)
REPLACE_CASES: dict[str, tuple[str, str]] = {
    "orders": ("payload = '{}'", "(intent_id, account_id, symbol, side, order_class, created_at, payload) "
               "VALUES ('int-1','x','x','x','x','x','{}')"),
    "broker_orders": ("payload = '{}'", "(broker_order_id, intent_id, order_class, submitted_at, payload) "
                      "VALUES ('101','int-1','x','x','{}')"),
    "perm_ids": ("recorded_at = 'x'", "(perm_id, intent_id, recorded_at) VALUES ('9999','int-1','x')"),
    "order_events": ("status = 'FILLED'", "(seq, event_id, intent_id, kind, status, occurred_at, "
                     "correlation_id, payload) VALUES (1,'ev-x','int-1','x','FILLED','x','x','{}')"),
    "executions": ("quantity = '999'", "(execution_id, intent_id, broker_order_id, symbol, side, quantity, "
                   "price, commission, executed_at, payload) VALUES ('exec-1','int-1','101','SPY','BUY',"
                   "'999','1','0','x','{}')"),
    "risk_decisions": ("verdict = 'ALLOW'", "(decision_id, intent_id, verdict, decided_at, payload) "
                       "VALUES ('d-1','int-1','ALLOW','x','{}')"),
    "command_log": ("result = 'x'", "(request_id, command, result, recorded_at) VALUES ('req-1','x','x','x')"),
    "audit_events": ("action = 'x'", "(seq, event_id, occurred_at, actor, action, correlation_id, payload) "
                     "VALUES (1,'a-x','x','x','x','x','{}')"),
    "mode_changes": ("mode = 'PAPER'", "(seq, change_id, mode, halt, changed_at, actor) "
                     "VALUES (1,'mc-x','PAPER','{}','x','x')"),
}  # fmt: skip


def test_every_ledger_table_has_a_replace_case() -> None:
    assert set(REPLACE_CASES) == set(LEDGER_TABLES)


def _snapshot(conn: sqlite3.Connection, table: str) -> list[tuple[object, ...]]:
    return list(conn.execute(f"SELECT * FROM {table} ORDER BY rowid"))


@pytest.mark.parametrize("table", sorted(REPLACE_CASES))
@pytest.mark.parametrize("verb", ["UPDATE", "DELETE", "INSERT OR REPLACE", "REPLACE"])
def test_ledger_rows_cannot_be_changed(
    db_path: Path, full_repo: LedgerRepository, table: str, verb: str
) -> None:
    set_clause, values = REPLACE_CASES[table]
    sql = {
        "UPDATE": f"UPDATE {table} SET {set_clause}",
        "DELETE": f"DELETE FROM {table}",
        "INSERT OR REPLACE": f"INSERT OR REPLACE INTO {table} {values}",
        "REPLACE": f"REPLACE INTO {table} {values}",
    }[verb]
    with raw(db_path) as conn:
        before = _snapshot(conn, table)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(sql)
        conn.rollback()
        assert _snapshot(conn, table) == before
    del full_repo


def test_upsert_do_update_is_rejected(db_path: Path, full_repo: LedgerRepository) -> None:
    with raw(db_path) as conn, pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "INSERT INTO orders (intent_id, account_id, symbol, side, order_class, created_at, "
            "payload) VALUES ('int-1','x','x','x','x','x','{}') "
            "ON CONFLICT(intent_id) DO UPDATE SET payload = excluded.payload"
        )
    del full_repo


def test_repository_exposes_no_update_or_delete() -> None:
    public = {n for n in dir(LedgerRepository) if not n.startswith("_")}
    assert not {n for n in public if n.startswith(("update", "delete", "remove", "set_"))}


# ---------- 幂等与唯一约束 ----------


def test_intent_insert_is_idempotent_and_conflict_detected(repo: LedgerRepository) -> None:
    assert repo.add_intent(make_intent()) is True
    assert repo.add_intent(make_intent()) is False  # 完全相同：幂等
    with pytest.raises(ConflictError):
        repo.add_intent(make_intent(qty="11"))  # 同一 intent_id 内容不同：冲突


def test_duplicate_execution_is_booked_once(repo: LedgerRepository) -> None:
    repo.add_intent(make_intent())
    assert repo.add_execution(make_execution()) is True
    assert repo.add_execution(make_execution()) is False
    assert [e.execution_id for e in repo.executions_for("int-1")] == ["exec-1"]
    with pytest.raises(ConflictError):
        repo.add_execution(make_execution(qty="5"))


def test_execution_requires_known_intent(repo: LedgerRepository) -> None:
    with pytest.raises(StorageError) as info:
        repo.add_execution(make_execution(intent_id="missing"))
    assert not isinstance(info.value, ConflictError)


def test_command_log_dedupes_request_id(repo: LedgerRepository) -> None:
    first = repo.record_command("req-1", "halt_new_orders", '{"ok":true}', T0)
    again = repo.record_command("req-1", "halt_new_orders", '{"ok":false}', T0 + timedelta(1))
    assert again == first  # 返回原结果，不执行第二次
    with pytest.raises(ConflictError):
        repo.record_command("req-1", "other_command", "{}", T0)


def test_broker_order_uniqueness(repo: LedgerRepository) -> None:
    repo.add_intent(make_intent())
    repo.add_intent(make_intent("int-2"))
    assert repo.add_broker_order(make_broker_order()) is True
    assert repo.add_broker_order(make_broker_order()) is False
    with pytest.raises(ConflictError):  # 同一券商订单号挂到另一个意图
        repo.add_broker_order(make_broker_order(intent_id="int-2"))
    with pytest.raises(ConflictError):  # 同一意图换了券商订单号
        repo.add_broker_order(make_broker_order(boid="102"))
    with pytest.raises(StorageError):  # permId 必须单独登记
        repo.add_broker_order(make_broker_order("int-2", "103").evolve(perm_id="1"))


def test_perm_id_uniqueness(repo: LedgerRepository) -> None:
    repo.add_intent(make_intent())
    repo.add_intent(make_intent("int-2"))
    assert repo.record_perm_id("int-1", "9001", T0) is True
    assert repo.record_perm_id("int-1", "9001", T0 + timedelta(seconds=1)) is False
    with pytest.raises(ConflictError):  # permId 已属于另一个意图
        repo.record_perm_id("int-2", "9001", T0)
    with pytest.raises(ConflictError):  # 同一意图换了 permId
        repo.record_perm_id("int-1", "9002", T0)


def test_risk_decision_round_trip_and_conflict(repo: LedgerRepository) -> None:
    repo.add_intent(make_intent())
    assert repo.add_risk_decision(make_decision()) is True
    assert repo.add_risk_decision(make_decision()) is False
    assert repo.risk_decisions_for("int-1") == [make_decision()]
    with pytest.raises(ConflictError):
        repo.add_risk_decision(make_decision(message="另一个说明"))


# ---------- 订单事件、模式、审计 ----------


def test_order_events_are_idempotent_append_only_history(repo: LedgerRepository) -> None:
    repo.add_intent(make_intent())
    approved = make_event("ev-1", EventKind.RISK_APPROVED)
    assert repo.append_order_event(approved, OrderStatus.RISK_APPROVED) is True
    assert repo.append_order_event(approved, OrderStatus.RISK_APPROVED) is False  # 重试不重复
    with pytest.raises(ConflictError):
        repo.append_order_event(approved, OrderStatus.SUBMITTING)
    repo.append_order_event(make_event("ev-2", EventKind.SUBMIT_SENT), OrderStatus.SUBMITTING)
    assert repo.latest_status("int-1") is OrderStatus.SUBMITTING
    assert repo.latest_status("unknown") is None
    assert [s for _, s in repo.order_history("int-1")] == [
        OrderStatus.RISK_APPROVED,
        OrderStatus.SUBMITTING,
    ]


def test_mode_changes_keep_full_history(repo: LedgerRepository) -> None:
    assert repo.latest_mode() is None
    assert repo.append_mode_change("mc-1", Mode.PAPER, NOT_HALTED, T0, "system") is True
    halt = HaltState(halted=True, reason="急停", since=T0, set_by="dylan")
    later = T0 + timedelta(seconds=1)
    assert repo.append_mode_change("mc-2", Mode.HALT, halt, later, "dylan") is True
    assert repo.append_mode_change("mc-2", Mode.HALT, halt, later, "dylan") is False
    with pytest.raises(ConflictError):
        repo.append_mode_change("mc-2", Mode.PAPER, NOT_HALTED, later, "dylan")
    latest = repo.latest_mode()
    assert latest is not None and latest.mode is Mode.HALT and latest.halt == halt


def test_audit_events_dedupe_and_latest(repo: LedgerRepository) -> None:
    assert repo.append_audit(make_audit()) is True
    assert repo.append_audit(make_audit()) is False
    with pytest.raises(ConflictError):
        repo.append_audit(make_audit(action="shutdown"))
    latest = repo.latest_audit("startup")
    assert latest is not None and latest.event_id == "a1"
    assert repo.latest_audit("nothing") is None


# ---------- 数值、时间与失败即关闭 ----------


def test_money_is_stored_as_exact_text(db_path: Path, repo: LedgerRepository) -> None:
    repo.add_intent(make_intent())
    repo.add_execution(make_execution())
    with raw(db_path) as conn:
        row = conn.execute("SELECT quantity, price, commission FROM executions").fetchone()
    assert row == ("4", "500.05", "0.35")


def test_timestamps_are_fixed_width_utc(db_path: Path, repo: LedgerRepository) -> None:
    repo.add_intent(make_intent())
    with raw(db_path) as conn:
        (created,) = conn.execute("SELECT created_at FROM orders").fetchone()
    assert created == "2026-10-22T14:00:00.000000+00:00"


def test_write_failure_propagates(db_path: Path, repo: LedgerRepository) -> None:
    with raw(db_path) as conn:
        conn.execute("BEGIN EXCLUSIVE")
        with pytest.raises(StorageError):
            LedgerRepository(open_ledger(db_path, busy_timeout_ms=50)).add_intent(make_intent())
        conn.rollback()


def test_corrupt_rows_raise_storage_error(db_path: Path, repo: LedgerRepository) -> None:
    repo.add_intent(make_intent())
    with raw(db_path) as conn:
        conn.execute(
            "INSERT INTO mode_changes (change_id, mode, halt, changed_at, actor) "
            "VALUES ('bad', 'NOT_A_MODE', '{}', 'x', 'x')"
        )
        conn.execute(
            "INSERT INTO executions (execution_id, intent_id, broker_order_id, symbol, side, "
            "quantity, price, commission, executed_at, payload) "
            "VALUES ('bad', 'int-1', '1', 'SPY', 'BUY', '1', '1', '0', 'x', 'not json')"
        )
        conn.commit()
    with pytest.raises(StorageError):
        repo.latest_mode()
    with pytest.raises(StorageError):
        repo.executions_for("int-1")


def test_reads_on_broken_database_raise_storage_error(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    upgrade(path)
    repo = LedgerRepository(open_ledger(path))
    with raw(path) as conn:
        conn.execute("DROP TRIGGER order_events_no_update")
        conn.execute("DROP TRIGGER order_events_no_delete")
        conn.execute("DROP TRIGGER order_events_no_replace")
        conn.execute("DROP TABLE order_events")
        conn.commit()
    with pytest.raises(StorageError):
        repo.order_history("int-1")


# ---------- C-04：数据库层禁止实盘模式、模式与审计原子写入 ----------


@pytest.mark.parametrize("mode", ["ASSISTED_LIVE", "LIMITED_AUTO_LIVE"])
def test_live_modes_cannot_be_written_even_directly(db_path: Path, mode: str) -> None:
    with raw(db_path) as conn, pytest.raises(sqlite3.IntegrityError, match="G7"):
        conn.execute(
            "INSERT INTO mode_changes (change_id, mode, halt, changed_at, actor) "
            f"VALUES ('x', '{mode}', '{{}}', 'x', 'x')"
        )


def test_commit_mode_and_audits_is_atomic(repo: LedgerRepository) -> None:
    repo.append_audit(make_audit())  # 占用 event_id a1
    halt = HaltState(halted=True, reason="r", since=T0, set_by="dylan")
    fresh = AuditEvent(event_id="a2", occurred_at=T0, actor="x", action="halt", correlation_id="c")
    with pytest.raises(StorageError, match="回滚"):
        repo.commit_mode_and_audits([fresh, make_audit()], ("mc-9", Mode.HALT, halt, T0, "dylan"))
    assert repo.latest_mode() is None  # 模式变更随审计一起回滚
    assert repo.latest_audit("halt") is None
    repo.commit_mode_and_audits([fresh], ("mc-9", Mode.HALT, halt, T0, "dylan"))
    latest = repo.latest_mode()
    assert latest is not None and latest.mode is Mode.HALT
    assert [a.event_id for a in repo.audits("halt")] == ["a2"]
