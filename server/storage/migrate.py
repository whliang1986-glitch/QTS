"""账本迁移（C-03，ADR-05）：Alembic 以编程方式运行，迁移前自动备份并校验备份。

- 每次迁移整体在一个事务里：pysqlite 默认对 DDL 自动提交，这里关闭其隐式事务并手动发 BEGIN，
  失败时表与触发器全部回滚，不会留下半套结构。
- 迁移前若数据库已存在且非空，先用 SQLite 的 VACUUM INTO 生成一致性快照（V4.1 5.5、5.6），
  并对快照做 integrity_check 与逐表行数核对，不通过即中止。
- 降级会删除账本表：库内有数据时必须显式 allow_data_loss=True。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import NullPool

from server.storage.errors import StorageError

_SCRIPTS = Path(__file__).resolve().parent / "migrations"


def _config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPTS))
    return cfg


def _transactional_engine(path: Path) -> Engine:
    """DDL 也在事务内的引擎（SQLAlchemy 文档中 pysqlite 的推荐做法）。"""

    def connect() -> sqlite3.Connection:
        conn = sqlite3.connect(path, isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    engine = create_engine("sqlite+pysqlite://", creator=connect, poolclass=NullPool)

    @event.listens_for(engine, "begin")
    def _begin(conn: Any) -> None:
        conn.exec_driver_sql("BEGIN")

    return engine


def head_revision() -> str:
    head = ScriptDirectory.from_config(_config()).get_current_head()
    if head is None:
        raise StorageError("没有任何迁移版本")
    return head


def current_revision(path: Path) -> str | None:
    if not path.exists():
        return None
    with closing(sqlite3.connect(f"file:{_uri_path(path)}?mode=ro", uri=True)) as conn:
        engine = create_engine("sqlite+pysqlite://", creator=lambda: conn, poolclass=NullPool)
        with engine.connect() as sa_conn:
            return MigrationContext.configure(sa_conn).get_current_revision()


def _uri_path(path: Path) -> str:
    return quote(path.resolve().as_posix(), safe="/:")


def _table_counts(path: Path) -> dict[str, int]:
    with closing(sqlite3.connect(path)) as conn:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {n: conn.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in names}  # noqa: S608


def _has_tables(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0 and bool(_table_counts(path))


def backup(path: Path) -> Path:
    """生成一致性快照并校验；返回快照路径。同一秒内多次调用也不会覆盖。"""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for n in range(1000):
        target = path.with_name(f"{path.stem}.pre-migrate-{stamp}-{n:03d}{path.suffix}")
        if not target.exists():
            break
    else:  # pragma: no cover - 1000 次同名几乎不可能
        raise StorageError("无法生成备份文件名")
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("VACUUM INTO ?", (str(target),))
    with closing(sqlite3.connect(target)) as conn:
        check = conn.execute("PRAGMA integrity_check").fetchone()
    if not check or check[0] != "ok":
        raise StorageError(f"备份 {target} 未通过完整性检查：{check}")
    if _table_counts(target) != _table_counts(path):
        raise StorageError(f"备份 {target} 的行数与原库不一致")
    return target


def _run(path: Path, action: str, revision: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = _config()
    engine = _transactional_engine(path)
    try:
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            if action == "upgrade":
                command.upgrade(cfg, revision)
            else:
                command.downgrade(cfg, revision)
    except StorageError:
        raise
    except Exception as exc:
        raise StorageError(f"迁移失败，已整体回滚：{exc}") from exc
    finally:
        engine.dispose()


def upgrade(path: Path, revision: str = "head") -> Path | None:
    """升级到指定版本。已有非空数据库先备份并校验，返回备份路径；新库返回 None。"""
    snapshot = backup(path) if _has_tables(path) else None
    _run(path, "upgrade", revision)
    return snapshot


def downgrade(path: Path, revision: str, *, allow_data_loss: bool = False) -> Path:
    """降级前先备份；降级会删表，账本有数据时必须显式 allow_data_loss=True。"""
    if not _has_tables(path):
        raise StorageError(f"{path} 不是已迁移的账本")
    rows = sum(n for name, n in _table_counts(path).items() if name != "alembic_version")
    if rows and not allow_data_loss:
        raise StorageError(f"账本中有 {rows} 行数据，降级会物理删除；如确需降级请显式确认")
    snapshot = backup(path)
    _run(path, "downgrade", revision)
    return snapshot
