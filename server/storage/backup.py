"""本机备份、校验、恢复与保留（S-08，V4.1 5.5：3-2-1）。

只备份不可替代的部分：ledger.db、meta.db、api.db、research/、config/、backtests/，
以及行情湖的 manifest（行情数据本身可以重下，不备份）。

- 一致性：SQLite 用在线备份接口生成快照（不直接拷贝正在写入的文件），快照再做 integrity_check；
  其余文件逐个拷贝。每个文件的 SHA-256 写进 BACKUP_MANIFEST.json。
- 原子：先写进临时目录，全部校验通过后再改名为正式目录；中途失败不留下半个备份。
- 密钥不入备份：任何路径段名为 secrets 的来源一律拒绝（V4.1 5.2：明文不入备份）。
- 恢复：先完整校验，目标目录必须为空（恢复到干净机器）；恢复后由装配层写入 SAFE HALT，
  先对账才允许恢复交易（备份不能代替券商对账）。
- 保留：只删除备份根目录下、名字符合备份格式的目录，最近 N 份永远保留。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from server.domain.values import to_utc

MANIFEST = "BACKUP_MANIFEST.json"
FORMAT = 1
_NAME = re.compile(r"backup-\d{8}T\d{6}Z(?:-\d{2})?")
_PART = re.compile(r"[a-z][a-z0-9_]{0,31}")
STAGING_PREFIX = ".staging-"


class BackupError(Exception):
    """备份、校验或恢复失败：备份不可用，必须告警，不得当成成功。"""


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    created_at: datetime
    files: Mapping[str, str]  # 备份内相对路径 → SHA-256


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _secret_like(parts: Iterable[str]) -> str | None:
    """来源目录内的路径段：任何包含 secret 的（大小写、前后缀变体）都按密钥处理，返回触发的段。"""
    return next((part for part in parts if "secret" in part.lower()), None)


def _check_source(path: Path) -> None:
    """来源本身：祖先路径只拦“恰好叫 secrets”的段（避免部署路径里的无关词误伤），
    来源目录自己的名字按子串规则。"""
    resolved = path.resolve()
    hit = next((part for part in resolved.parts if part.lower() == "secrets"), None)
    hit = hit or _secret_like([resolved.name])
    if hit is not None:
        raise BackupError(f"密钥目录不入备份：{path}（触发的路径段：{hit}）")


def _sqlite_uri(path: Path, params: str) -> str:
    return f"file:{quote(path.resolve().as_posix(), safe='/:')}?{params}"


def _snapshot_db(source: Path, target: Path) -> None:
    """在线备份接口：源库正在写入也能得到一致快照。"""
    if not source.is_file():
        raise BackupError(f"数据库不存在：{source}")
    try:
        with (
            closing(sqlite3.connect(_sqlite_uri(source, "mode=ro"), uri=True)) as src,
            closing(sqlite3.connect(target)) as dst,
        ):
            src.backup(dst)
        with closing(sqlite3.connect(target)) as conn:
            # 快照改回 DELETE 日志模式：备份是单个自洽文件，不带 -wal / -shm
            mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()
            check = conn.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        raise BackupError(f"{source} 快照失败：{exc}") from exc
    if not mode or str(mode[0]).lower() != "delete":
        raise BackupError(f"{source} 的快照无法切换到 DELETE 日志模式：{mode}")
    if not check or check[0] != "ok":
        raise BackupError(f"{source} 的快照未通过完整性检查：{check}")


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()  # Windows 目录联接不算符号链接，单独查


def _copy_tree(source: Path, target: Path, *, only: str | None = None) -> None:
    if not source.is_dir() or _is_link(source):
        raise BackupError(f"目录不存在或是链接：{source}")
    root = source.resolve()
    for item in sorted(source.rglob("*")):
        if _is_link(item):
            raise BackupError(f"不跟随符号链接或目录联接：{item}")
        real = item.resolve()
        if not real.is_relative_to(root):  # 任何方式指向来源目录以外的项一律拒绝
            raise BackupError(f"{item} 指向来源目录以外")
        rel = item.relative_to(source)
        hit = _secret_like(rel.parts) or _secret_like(real.relative_to(root).parts)
        if hit is not None:
            raise BackupError(f"密钥目录不入备份：{item}（触发的路径段：{hit}）")
        if item.is_file() and (only is None or item.name == only):
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dest, follow_symlinks=False)


def _unique_name(root: Path, created: datetime) -> str:
    base = f"backup-{created.strftime('%Y%m%dT%H%M%SZ')}"
    if not (root / base).exists():
        return base
    for n in range(1, 100):
        name = f"{base}-{n:02d}"
        if not (root / name).exists():
            return name
    raise BackupError("同一秒内备份太多，无法生成名字")


def _check_parts(items: Iterable[str]) -> None:
    for name in items:
        if not _PART.fullmatch(name):
            raise BackupError(f"备份项名称不合法：{name!r}")


def _check_disjoint(dest_root: Path, sources: Iterable[Path]) -> None:
    dest = dest_root.resolve()
    for source in sources:
        real = source.resolve()
        if dest.is_relative_to(real) or real.is_relative_to(dest):
            raise BackupError(f"备份目录与来源 {source} 重叠")


def create_backup(
    dest_root: Path,
    *,
    databases: Mapping[str, Path],
    directories: Mapping[str, Path],
    lake_manifests: Path | None,
    now: datetime,
) -> BackupInfo:
    """生成一份完整备份；在临时目录里校验通过后才改名为正式目录。任何一步失败都不留下备份目录。"""
    created = to_utc(now)
    if not databases:
        raise BackupError("至少要备份一个数据库（账本）")
    _check_parts([*databases, *directories])
    trees = list(directories.values()) + ([lake_manifests] if lake_manifests is not None else [])
    for path in [*databases.values(), *trees]:
        _check_source(path)
    _check_disjoint(dest_root, trees)
    dest_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=dest_root))
    try:
        for name, source in sorted(databases.items()):
            (staging / "db").mkdir(exist_ok=True)
            _snapshot_db(source, staging / "db" / f"{name}.db")
        for name, source in sorted(directories.items()):
            _copy_tree(source, staging / "files" / name)
        if lake_manifests is not None:
            _copy_tree(lake_manifests, staging / "lake_manifests", only="manifest.json")
        files = {
            p.relative_to(staging).as_posix(): _sha256(p)
            for p in sorted(staging.rglob("*"))
            if p.is_file()
        }
        manifest = {
            "format": FORMAT,
            "created_at": created.isoformat(),
            "databases": sorted(databases),
            "directories": sorted(directories),
            "files": files,
        }
        (staging / MANIFEST).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        verify_backup(staging)  # 先校验，再给正式名字
        target = dest_root / _unique_name(dest_root, created)
        os.rename(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return BackupInfo(target, created, files)


def verify_backup(path: Path) -> BackupInfo:
    """重新计算全部哈希并检查数据库完整性；多出或缺少文件都算损坏。不修改备份目录。"""
    try:
        manifest = json.loads((path / MANIFEST).read_text(encoding="utf-8"))
        files: dict[str, str] = dict(manifest["files"])
        created = to_utc(manifest["created_at"])
        if manifest["format"] != FORMAT:
            raise BackupError(f"不认识的备份格式：{manifest['format']}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BackupError(f"{path} 的备份清单损坏：{exc}") from exc
    present = {p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()} - {MANIFEST}
    if present != set(files):
        extra, missing = sorted(present - set(files)), sorted(set(files) - present)
        raise BackupError(f"{path} 的文件与清单不一致：多 {extra}，缺 {missing}")
    for rel, digest in files.items():
        try:
            actual = _sha256(path / rel)
        except OSError as exc:
            raise BackupError(f"{path} 中 {rel} 读不出来：{exc}") from exc
        if actual != digest:
            raise BackupError(f"{path} 中 {rel} 的哈希与清单不符")
    for rel in files:
        if rel.startswith("db/"):
            # immutable：只读校验，不在备份目录里生成任何附带文件
            try:
                uri = _sqlite_uri(path / rel, "mode=ro&immutable=1")
                with closing(sqlite3.connect(uri, uri=True)) as conn:
                    check = conn.execute("PRAGMA integrity_check").fetchone()
            except sqlite3.Error as exc:
                raise BackupError(f"{path} 中 {rel} 无法打开：{exc}") from exc
            if not check or check[0] != "ok":
                raise BackupError(f"{path} 中 {rel} 未通过完整性检查")
    return BackupInfo(path, created, files)


def restore_backup(path: Path, target_root: Path, *, prepare: Callable[[Path], None]) -> BackupInfo:
    """恢复到干净目录：先校验；目标必须不存在或为空。

    先恢复到目标旁边的临时目录，核对哈希后调用 prepare(临时目录)（装配层在这里写入 SAFE HALT
    并读回确认），全部成功才改名为目标。任何失败或中途被杀都不会在目标处留下可交易的账本。
    """
    info = verify_backup(path)
    if target_root.exists() and (not target_root.is_dir() or any(target_root.iterdir())):
        raise BackupError(f"恢复目标不是空目录（只能恢复到干净机器）：{target_root}")
    target_root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=target_root.parent))
    try:
        for rel in info.files:
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path / rel, dest)
        for rel, digest in info.files.items():
            try:
                actual = _sha256(staging / rel)
            except OSError as exc:
                raise BackupError(f"恢复后 {rel} 读不出来：{exc}") from exc
            if actual != digest:
                raise BackupError(f"恢复后 {rel} 的哈希与备份不符")
        prepare(staging)
        if target_root.exists():
            target_root.rmdir()  # 已确认为空
        os.rename(staging, target_root)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return info


def prune_backups(dest_root: Path, *, keep: int) -> list[Path]:
    """只保留最近 keep 份**通过校验**的备份；只删除名字符合备份格式、真实位于备份根下的目录。

    通过校验的不足 keep 份时一份都不删（抛错，先告警排查）。返回删除的目录。
    """
    if keep < 1:
        raise BackupError("至少保留 1 份备份")
    if not dest_root.is_dir():
        return []
    root = dest_root.resolve()
    backups = sorted(
        p for p in dest_root.iterdir()
        if p.is_dir() and not _is_link(p) and _NAME.fullmatch(p.name)
        and p.resolve().parent == root
    )  # fmt: skip
    valid = []
    for path in backups:
        try:
            verify_backup(path)
        except BackupError:
            continue
        valid.append(path)
    if len(valid) < keep:
        raise BackupError(f"通过校验的备份只有 {len(valid)} 份，少于保留数 {keep}：不清理")
    oldest_kept = valid[-keep]
    removed = [p for p in backups if p < oldest_kept]
    for path in removed:
        shutil.rmtree(path)
    return removed


def copy_offsite(path: Path, target_root: Path) -> Path:
    """异地副本（目录目标）：先校验本机备份；复制到目标的临时目录、校验通过后才改名。

    目标位置（另一台设备的挂载目录或云对象存储）由 Dylan 决定；云存储另做适配。
    """
    verify_backup(path)
    target = target_root / path.name
    if target.exists():
        raise BackupError(f"异地已有同名备份：{target}")
    target_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=target_root))
    try:
        shutil.copytree(path, staging, dirs_exist_ok=True)
        verify_backup(staging)
        os.rename(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target
