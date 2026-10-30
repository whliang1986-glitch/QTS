"""本机备份、校验、恢复、保留与异地副本（S-08）。"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from server.storage.backup import (
    MANIFEST,
    BackupError,
    copy_offsite,
    create_backup,
    prune_backups,
    restore_backup,
    verify_backup,
)

NOW = datetime(2026, 10, 29, 21, 0, tzinfo=UTC)


def _db(path: Path, rows: int = 3) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"r{i}",) for i in range(rows)])
        conn.commit()
    return path


def _root(tmp_path: Path) -> dict[str, Path]:
    data = tmp_path / "data"
    ledger = _db(data / "db" / "ledger.db")
    meta = _db(data / "db" / "meta.db", rows=1)
    config = data / "config"
    (config / "runtime").mkdir(parents=True)
    (config / "runtime" / "processes.toml").write_text("[processes]\n", encoding="utf-8")
    research = data / "research"
    research.mkdir()
    (research / "note.md").write_text("# 研究\n", encoding="utf-8")
    lake = data / "lake"
    (lake / "raw" / "x" / "date=2026-10-23" / "v=001").mkdir(parents=True)
    (lake / "raw" / "x" / "date=2026-10-23" / "v=001" / "manifest.json").write_text("{}", "utf-8")
    (lake / "raw" / "x" / "date=2026-10-23" / "v=001" / "data.parquet").write_bytes(b"PAR1")
    return {"ledger": ledger, "meta": meta, "config": config, "research": research, "lake": lake}


def _backup(tmp_path: Path, src: dict[str, Path], *, now: datetime = NOW) -> Path:
    info = create_backup(
        tmp_path / "backups", databases={"ledger": src["ledger"], "meta": src["meta"]},
        directories={"config": src["config"], "research": src["research"]},
        lake_manifests=src["lake"], now=now,
    )  # fmt: skip
    return info.path


def _rows(path: Path) -> int:
    with closing(sqlite3.connect(path)) as conn:
        return int(conn.execute("SELECT count(*) FROM t").fetchone()[0])


def test_backup_contains_snapshots_files_and_only_lake_manifests(tmp_path: Path) -> None:
    src = _root(tmp_path)
    path = _backup(tmp_path, src)
    assert path.name == "backup-20261029T210000Z"
    info = verify_backup(path)
    assert set(info.files) == {
        "db/ledger.db", "db/meta.db", "files/config/runtime/processes.toml",
        "files/research/note.md", "lake_manifests/raw/x/date=2026-10-23/v=001/manifest.json",
    }  # fmt: skip
    assert _rows(path / "db" / "ledger.db") == 3
    assert not any(p.name == "data.parquet" for p in path.rglob("*"))  # 行情数据不备份
    assert not any(p.name.startswith(".staging-") for p in (tmp_path / "backups").iterdir())


def test_snapshot_is_consistent_while_the_source_is_open(tmp_path: Path) -> None:
    src = _root(tmp_path)
    with closing(sqlite3.connect(src["ledger"])) as writer:
        writer.execute("INSERT INTO t (v) VALUES ('committed')")
        writer.commit()
        writer.execute("BEGIN")
        writer.execute("INSERT INTO t (v) VALUES ('uncommitted')")
        path = _backup(tmp_path, src)
        writer.rollback()
    assert _rows(path / "db" / "ledger.db") == 4  # 已提交的在、未提交的不在


@pytest.mark.parametrize("damage", ["modify", "extra", "missing", "manifest"])
def test_verification_detects_damage(tmp_path: Path, damage: str) -> None:
    path = _backup(tmp_path, _root(tmp_path))
    target = path / "files" / "research" / "note.md"
    if damage == "modify":
        target.write_text("# 被改过\n", encoding="utf-8")
    elif damage == "extra":
        (path / "files" / "research" / "extra.md").write_text("x", encoding="utf-8")
    elif damage == "missing":
        target.unlink()
    else:
        (path / MANIFEST).write_text("not json", encoding="utf-8")
    with pytest.raises(BackupError):
        verify_backup(path)


def test_key_directories_never_enter_a_backup(tmp_path: Path) -> None:
    src = _root(tmp_path)
    (src["config"] / "secrets").mkdir()
    (src["config"] / "secrets" / "token.txt").write_text("x", encoding="utf-8")
    with pytest.raises(BackupError, match=r"密钥.*触发的路径段：secrets）"):
        _backup(tmp_path, src)
    shutil.rmtree(src["config"] / "secrets")  # 正面对照：去掉后同样的来源可以备份
    assert _backup(tmp_path, src).name.startswith("backup-")
    shutil.rmtree(tmp_path / "backups")
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    with pytest.raises(BackupError, match="密钥"):
        create_backup(tmp_path / "backups", databases={"ledger": src["ledger"]},
                      directories={"keys": secrets_dir}, lake_manifests=None, now=NOW)  # fmt: skip
    assert not (tmp_path / "backups").exists() or not any((tmp_path / "backups").iterdir())


def test_failed_backup_leaves_nothing(tmp_path: Path) -> None:
    src = _root(tmp_path)
    with pytest.raises(BackupError, match="不存在"):
        create_backup(tmp_path / "backups", databases={"ledger": tmp_path / "nope.db"},
                      directories={}, lake_manifests=None, now=NOW)  # fmt: skip
    assert list((tmp_path / "backups").iterdir()) == []
    with pytest.raises(BackupError, match="至少要备份一个数据库"):
        create_backup(tmp_path / "backups", databases={}, directories={"config": src["config"]},
                      lake_manifests=None, now=NOW)  # fmt: skip
    with pytest.raises(BackupError, match="名称不合法"):
        create_backup(tmp_path / "backups", databases={"Bad-Name": src["ledger"]},
                      directories={}, lake_manifests=None, now=NOW)  # fmt: skip


def test_restore_only_into_an_empty_directory_and_hashes_match(tmp_path: Path) -> None:
    path = _backup(tmp_path, _root(tmp_path))
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "old.db").write_bytes(b"x")
    with pytest.raises(BackupError, match="不是空目录"):
        restore_backup(path, busy, prepare=lambda _p: None)
    clean = tmp_path / "clean"
    info = restore_backup(path, clean, prepare=lambda _p: None)
    assert _rows(clean / "db" / "ledger.db") == 3
    assert (clean / "files" / "research" / "note.md").read_text(encoding="utf-8") == "# 研究\n"
    assert set(info.files) == {
        p.relative_to(clean).as_posix() for p in clean.rglob("*") if p.is_file()
    }


def test_restore_refuses_a_damaged_backup(tmp_path: Path) -> None:
    path = _backup(tmp_path, _root(tmp_path))
    (path / "db" / "meta.db").write_bytes(b"garbage")
    with pytest.raises(BackupError):
        restore_backup(path, tmp_path / "clean", prepare=lambda _p: None)
    assert not (tmp_path / "clean").exists()


def test_prune_keeps_the_newest_and_touches_nothing_else(tmp_path: Path) -> None:
    src = _root(tmp_path)
    made = [_backup(tmp_path, src, now=NOW.replace(day=d)) for d in (25, 26, 27, 28)]
    (tmp_path / "backups" / "README.txt").write_text("别删我", encoding="utf-8")
    (tmp_path / "backups" / "notes").mkdir()
    removed = prune_backups(tmp_path / "backups", keep=2)
    assert removed == made[:2]
    left = sorted(p.name for p in (tmp_path / "backups").iterdir())
    assert left == ["README.txt", made[2].name, made[3].name, "notes"]
    with pytest.raises(BackupError, match="至少保留 1 份"):
        prune_backups(tmp_path / "backups", keep=0)


def test_offsite_copy_is_verified_and_never_overwrites(tmp_path: Path) -> None:
    path = _backup(tmp_path, _root(tmp_path))
    offsite = tmp_path / "offsite"
    copied = copy_offsite(path, offsite)
    assert verify_backup(copied).files == verify_backup(path).files
    with pytest.raises(BackupError, match="同名"):
        copy_offsite(path, offsite)
    manifest = json.loads((copied / MANIFEST).read_text(encoding="utf-8"))
    assert manifest["databases"] == ["ledger", "meta"]


def test_two_backups_in_the_same_second_get_distinct_names(tmp_path: Path) -> None:
    src = _root(tmp_path)
    first, second = _backup(tmp_path, src), _backup(tmp_path, src)
    assert (first.name, second.name) == ("backup-20261029T210000Z", "backup-20261029T210000Z-01")


# ---------- 评审补充 ----------


def test_restore_failure_in_prepare_leaves_no_target(tmp_path: Path) -> None:
    path = _backup(tmp_path, _root(tmp_path))

    def fail(_staging: Path) -> None:
        raise RuntimeError("写 HALT 失败")

    with pytest.raises(RuntimeError, match="写 HALT 失败"):
        restore_backup(path, tmp_path / "clean", prepare=fail)
    assert not (tmp_path / "clean").exists()
    assert not any(p.name.startswith(".staging-") for p in tmp_path.iterdir())


def test_prepare_sees_the_restored_files_before_the_rename(tmp_path: Path) -> None:
    path = _backup(tmp_path, _root(tmp_path))
    seen: list[Path] = []

    def prepare(staging: Path) -> None:
        assert not (tmp_path / "clean").exists()  # 目标此时还不存在
        assert (staging / "db" / "ledger.db").is_file()
        seen.append(staging)

    restore_backup(path, tmp_path / "clean", prepare=prepare)
    assert seen
    assert (tmp_path / "clean" / "db" / "ledger.db").is_file()


@pytest.mark.parametrize("name", ["Secrets", ".secrets", "secrets_old", "my-secret.bak"])
def test_key_name_variants_are_refused(tmp_path: Path, name: str) -> None:
    src = _root(tmp_path)
    (src["research"] / name).mkdir()
    (src["research"] / name / "k.txt").write_text("x", encoding="utf-8")
    with pytest.raises(BackupError, match=rf"密钥.*触发的路径段：{re.escape(name)}）"):
        _backup(tmp_path, src)
    shutil.rmtree(src["research"] / name)  # 去掉以后同样的来源可以正常备份
    assert _backup(tmp_path, src).name.startswith("backup-")


def test_windows_junction_is_refused(tmp_path: Path) -> None:
    # 目录联接（junction）只存在于 Windows；其他系统没有 _winapi，跳过
    winapi = pytest.importorskip("_winapi", reason="目录联接（junction）只存在于 Windows")

    src = _root(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "token.txt").write_text("x", encoding="utf-8")
    winapi.CreateJunction(str(vault), str(src["research"] / "link"))
    with pytest.raises(BackupError, match=r"链接|来源目录以外"):
        _backup(tmp_path, src)


def test_symlink_is_refused(tmp_path: Path) -> None:
    src = _root(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    try:
        (src["research"] / "link.txt").symlink_to(outside)
    except OSError:
        pytest.skip("当前系统不允许创建符号链接（Windows 需开发者模式）")
    with pytest.raises(BackupError, match="链接"):
        _backup(tmp_path, src)


def test_corrupt_database_with_a_matching_hash_is_detected(tmp_path: Path) -> None:
    import hashlib  # noqa: PLC0415

    path = _backup(tmp_path, _root(tmp_path))
    bad = path / "db" / "meta.db"
    bad.write_bytes(b"not a database at all" * 100)
    manifest = json.loads((path / MANIFEST).read_text(encoding="utf-8"))
    manifest["files"]["db/meta.db"] = hashlib.sha256(bad.read_bytes()).hexdigest()
    (path / MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BackupError, match=r"meta\.db"):
        verify_backup(path)


def test_verification_does_not_modify_the_backup(tmp_path: Path) -> None:
    path = _backup(tmp_path, _root(tmp_path))
    verify_backup(path)
    verify_backup(path)
    assert sorted(p.name for p in (path / "db").iterdir()) == ["ledger.db", "meta.db"]


def test_prune_never_deletes_when_too_few_valid_backups(tmp_path: Path) -> None:
    src = _root(tmp_path)
    made = [_backup(tmp_path, src, now=NOW.replace(day=d)) for d in (25, 26, 27)]
    (made[2] / "files" / "research" / "note.md").write_text(
        "坏了", encoding="utf-8"
    )  # 最新一份损坏
    removed = prune_backups(tmp_path / "backups", keep=2)
    assert removed == []  # 好的只有 25、26 两份：都要保留，坏的 27 不算数、也不删
    assert made[1].exists()
    with pytest.raises(BackupError, match="少于保留数"):
        prune_backups(tmp_path / "backups", keep=3)


def test_backup_root_inside_a_source_is_refused(tmp_path: Path) -> None:
    src = _root(tmp_path)
    with pytest.raises(BackupError, match="重叠"):
        create_backup(
            src["research"] / "backups", databases={"ledger": src["ledger"]},
            directories={"research": src["research"]}, lake_manifests=None, now=NOW,
        )  # fmt: skip


def test_restore_into_an_existing_empty_directory(tmp_path: Path) -> None:
    path = _backup(tmp_path, _root(tmp_path))
    (tmp_path / "clean").mkdir()
    restore_backup(path, tmp_path / "clean", prepare=lambda _p: None)
    assert (tmp_path / "clean" / "db" / "ledger.db").is_file()
    target_file = tmp_path / "afile"
    target_file.write_text("x", encoding="utf-8")
    with pytest.raises(BackupError, match="不是空目录"):
        restore_backup(path, target_file, prepare=lambda _p: None)


def test_prune_with_a_damaged_backup_in_the_middle(tmp_path: Path) -> None:
    src = _root(tmp_path)
    made = [_backup(tmp_path, src, now=NOW.replace(day=d)) for d in (25, 26, 27, 28)]
    (made[1] / "files" / "research" / "note.md").write_text("坏了", encoding="utf-8")
    removed = prune_backups(tmp_path / "backups", keep=2)
    assert removed == [made[0], made[1]]  # 保留最近两份好的（27、28）
    assert made[2].exists()
    assert made[3].exists()
