"""alerts.db（S-05a）：告警的发生、确认、解除，只追加。guardd 是唯一写者，apid 只读。

- 一次“发生”是一条 raised 事件，它的 seq 就是告警编号（alert_id）；确认、解除各追加一条事件，
  引用那个编号。当前状态由事件推出；UPDATE、DELETE 被触发器拒绝。
- 结构版本或触发器不对即拒绝打开（失败即关闭）。
- 只读读取（apid 与 guardd 读 api.db 的确认请求）用 `mode=ro` 打开，每次读完即关，
  不持有写锁，也不会创建或迁移文件；文件不存在、结构不对一律抛出（界面显示“无法获取”）。
- 库编号 store_id（S-05a 评审 P1-1）：建库时生成的随机串。网页确认请求带上它，
  guardd 只认同一个库的请求：alerts.db 被删掉重建后编号从 1 重新开始，
  旧请求不会误确认新库里同编号的告警。
- 健康标记（S-05a 评审 P2-2）：guardd 定期写 alerts.status.json（store_ok 与时间）。
  apid 读告警前先看它：标记缺失、过期或告警库已降级，一律当作“无法获取告警”
  （库里的数据可能不全，不能显示成“没有告警”）。
- 这里只有 SQL 与文件读写，不依赖 server.api 与 server.guard。
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA_VERSION = 1
ALERTS_DB = "alerts.db"
STATUS_FILE = "alerts.status.json"
STATUS_MAX_AGE = timedelta(seconds=30)  # guardd 每 5 秒写一次；超过 30 秒没更新即视为不可信
API_DB_ACKS_VERSION = 2  # api.db 从 v2 起有 alert_acks（server.storage.apidb）
LEVELS = ("P0", "P1", "P2", "P3")
_SCHEMA = """
CREATE TABLE alert_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('raised', 'acked', 'resolved')),
    alert_id INTEGER REFERENCES alert_events (seq),
    alert_key TEXT NOT NULL,
    level TEXT CHECK (level IS NULL OR level IN ('P0', 'P1', 'P2', 'P3')),
    code TEXT,
    params TEXT,
    actor TEXT,
    CHECK ((kind = 'raised') = (alert_id IS NULL)),
    CHECK ((kind = 'raised') = (level IS NOT NULL AND code IS NOT NULL AND params IS NOT NULL)),
    CHECK ((kind = 'acked') = (actor IS NOT NULL))
);
CREATE UNIQUE INDEX alert_events_once ON alert_events (alert_id, kind) WHERE alert_id IS NOT NULL;
CREATE TABLE store_meta (store_id TEXT NOT NULL);
CREATE TRIGGER store_meta_no_update BEFORE UPDATE ON store_meta
BEGIN SELECT RAISE(ABORT, 'store_meta is append-only'); END;
CREATE TRIGGER store_meta_no_delete BEFORE DELETE ON store_meta
BEGIN SELECT RAISE(ABORT, 'store_meta is append-only'); END;
CREATE TRIGGER alert_events_no_update BEFORE UPDATE ON alert_events
BEGIN SELECT RAISE(ABORT, 'alert_events is append-only'); END;
CREATE TRIGGER alert_events_no_delete BEFORE DELETE ON alert_events
BEGIN SELECT RAISE(ABORT, 'alert_events is append-only'); END;
"""
_TRIGGERS = frozenset({
    "alert_events_no_update", "alert_events_no_delete",
    "store_meta_no_update", "store_meta_no_delete",
})  # fmt: skip


class AlertsDbError(Exception):
    """alerts.db 不可用、结构不对或写入失败。"""


@dataclass(frozen=True)
class StoredAlert:
    alert_id: int
    key: str
    level: str
    code: str
    params: Mapping[str, str]
    raised_at: str
    acked_by: str | None = None
    acked_at: str | None = None
    resolved_at: str | None = None


@dataclass(frozen=True)
class AckRequest:
    """api.db 里的一条网页确认请求。"""

    seq: int
    alert_id: int
    alert_key: str
    store_id: str
    actor: str
    at: datetime


def alerts_db_path(api_db: Path) -> Path:
    """alerts.db 与 api.db 放在同一目录（apid 与 guardd 共同的数据目录）。"""
    return api_db.with_name(ALERTS_DB)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AlertsDbError("时间必须带时区")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class AlertStore:
    """guardd 的写入端。所有写入都是单条 INSERT，失败抛 AlertsDbError（调用方降级到内存）。"""

    def __init__(
        self, path: Path, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 等锁最多 0.5 秒：guardd 在事件循环里写，不能被别的连接拖住太久
            self._conn = sqlite3.connect(
                path, isolation_level=None, check_same_thread=False, timeout=0.5
            )
            self._conn.execute("PRAGMA foreign_keys = ON")
            mode = self._conn.execute("PRAGMA journal_mode = WAL").fetchone()
            if not mode or str(mode[0]).lower() != "wal":
                raise AlertsDbError(f"无法启用 WAL（得到 {mode}）")
            self._conn.execute("PRAGMA synchronous = FULL")
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                store_id = os.urandom(16).hex()  # 只有 0-9a-f，可以直接拼进脚本
                self._conn.executescript(
                    f"BEGIN;{_SCHEMA}INSERT INTO store_meta (store_id) VALUES ('{store_id}');"  # noqa: S608
                    f"PRAGMA user_version = {SCHEMA_VERSION};COMMIT;"
                )
            self.store_id = _check_schema(self._conn)
        except (sqlite3.Error, OSError) as exc:  # 目录建不出来也算打不开（调用方降级到内存）
            raise AlertsDbError(f"无法打开 alerts.db：{type(exc).__name__}: {exc}") from exc

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self._conn.close()

    def raised(self, key: str, level: str, code: str, params: Mapping[str, str]) -> int:
        if level not in LEVELS:
            raise AlertsDbError(f"告警级别不认识：{level}")
        body = json.dumps(dict(params), ensure_ascii=False, sort_keys=True)
        return self._insert(
            "INSERT INTO alert_events (at, kind, alert_key, level, code, params)"
            " VALUES (?, 'raised', ?, ?, ?, ?)",
            (_iso(self._clock()), key, level, code, body),
        )

    def acked(self, alert_id: int, key: str, *, actor: str) -> None:
        self._insert(
            "INSERT INTO alert_events (at, kind, alert_id, alert_key, actor)"
            " VALUES (?, 'acked', ?, ?, ?)",
            (_iso(self._clock()), alert_id, key, actor),
        )

    def resolved(self, alert_id: int, key: str) -> None:
        self._insert(
            "INSERT INTO alert_events (at, kind, alert_id, alert_key) VALUES (?, 'resolved', ?, ?)",
            (_iso(self._clock()), alert_id, key),
        )

    def unresolved(self) -> list[StoredAlert]:
        """尚未解除的告警（guardd 重启后据此恢复，不会忘掉没解除的 P0）。"""
        with self._lock:
            try:
                return _alerts(self._conn, None)
            except (sqlite3.Error, ValueError) as exc:
                raise AlertsDbError(f"读取 alerts.db 失败：{exc}") from exc

    def _insert(self, sql: str, args: tuple[object, ...]) -> int:
        with self._lock:
            try:
                cursor = self._conn.execute(sql, args)
            except sqlite3.Error as exc:
                raise AlertsDbError(f"写入 alerts.db 失败：{exc}") from exc
            seq = cursor.lastrowid
            if seq is None:
                raise AlertsDbError("写入 alerts.db 后没有得到编号")
            return seq


def _check_schema(conn: sqlite3.Connection) -> str:
    """结构不对就抛出；返回库编号。"""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        raise AlertsDbError(f"alerts.db 结构版本 {version} 不认识")
    rows = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger'").fetchall()
    found = {name for name, sql in rows if "RAISE(ABORT" in (sql or "")}
    if not found >= _TRIGGERS:
        raise AlertsDbError(f"alerts.db 只追加触发器缺失：{sorted(_TRIGGERS - found)}")
    ids = conn.execute("SELECT store_id FROM store_meta").fetchall()
    if len(ids) != 1 or not str(ids[0][0]):
        raise AlertsDbError("alerts.db 库编号缺失或不唯一")
    return str(ids[0][0])


_RAISED = "SELECT seq, at, alert_key, level, code, params FROM alert_events WHERE kind = 'raised'"
_UNRESOLVED = (
    " AND seq NOT IN (SELECT alert_id FROM alert_events WHERE kind = 'resolved'"
    " AND alert_id IS NOT NULL)"
)


def _alerts(conn: sqlite3.Connection, limit: int | None) -> list[StoredAlert]:
    """按编号从新到旧。None：只取未解除的；给了 limit：全部未解除的，加上最近 limit 次发生。

    未解除的一律返回（评审 P2-1）：反复发生又解除的告警再多，也不能把仍未解除的旧告警挤出列表。
    """
    if limit is None:
        raised = conn.execute(_RAISED + _UNRESOLVED + " ORDER BY seq DESC").fetchall()
    else:
        recent = "SELECT seq FROM alert_events WHERE kind = 'raised' ORDER BY seq DESC LIMIT ?"
        raised = conn.execute(
            _RAISED + f" AND (seq IN ({recent}) OR (1 = 1{_UNRESOLVED})) ORDER BY seq DESC",
            (limit,),
        ).fetchall()
    return _build(conn, raised)


def _one(conn: sqlite3.Connection, alert_id: int) -> StoredAlert | None:
    raised = conn.execute(_RAISED + " AND seq = ?", (alert_id,)).fetchall()
    found = _build(conn, raised)
    return found[0] if found else None


def _build(conn: sqlite3.Connection, raised: list[tuple[object, ...]]) -> list[StoredAlert]:
    if not raised:
        return []
    ids = [int(str(r[0])) for r in raised]
    marks = ",".join("?" * len(ids))
    follow = conn.execute(
        f"SELECT alert_id, kind, at, actor FROM alert_events WHERE alert_id IN ({marks})",  # noqa: S608 - 只有占位符
        ids,
    ).fetchall()
    acked = {a: (at, actor) for a, kind, at, actor in follow if kind == "acked"}
    resolved = {a: at for a, kind, at, _ in follow if kind == "resolved"}
    result = []
    for seq, at, key, level, code, params in raised:
        loaded = json.loads(str(params))
        if not isinstance(loaded, dict):
            raise ValueError("告警参数不是对象")
        ack = acked.get(seq)
        result.append(StoredAlert(
            alert_id=int(str(seq)), key=str(key), level=str(level), code=str(code),
            params={str(k): str(v) for k, v in loaded.items()}, raised_at=str(at),
            acked_by=None if ack is None else ack[1], acked_at=None if ack is None else ack[0],
            resolved_at=resolved.get(seq),
        ))  # fmt: skip
    return result


def _read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():  # mode=ro 不会创建文件；先查一次，报错更清楚
        raise AlertsDbError(f"文件不存在：{path.name}")
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def read_alerts(path: Path, *, limit: int = 200) -> list[StoredAlert]:
    """apid 只读读取 alerts.db：全部未解除的与最近 limit 次发生，及其确认、解除。出错抛出。"""
    if type(limit) is not int or limit <= 0:
        raise AlertsDbError("limit 必须是正整数")
    try:
        conn = _read_only(path)
        try:
            _check_schema(conn)
            return _alerts(conn, limit)
        finally:
            conn.close()
    except (sqlite3.Error, ValueError) as exc:
        raise AlertsDbError(f"读取 alerts.db 失败：{exc}") from exc


def read_alert(path: Path, alert_id: int) -> tuple[str, StoredAlert | None]:
    """只读取一次发生（确认前核对它是否存在、是否已解除）。返回 (库编号, 那次发生)。"""
    try:
        conn = _read_only(path)
        try:
            return _check_schema(conn), _one(conn, alert_id)
        finally:
            conn.close()
    except (sqlite3.Error, ValueError) as exc:
        raise AlertsDbError(f"读取 alerts.db 失败：{exc}") from exc


# ---------- 健康标记 ----------


def status_path(alerts_db: Path) -> Path:
    return alerts_db.with_name(STATUS_FILE)


def write_status(alerts_db: Path, *, store_ok: bool, at: datetime) -> None:
    """guardd 写健康标记：先写临时文件再替换，读者不会读到半个文件。"""
    target = status_path(alerts_db)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(json.dumps({"store_ok": store_ok, "at": _iso(at)}), encoding="utf-8")
    os.replace(temp, target)


def check_status(alerts_db: Path, now: datetime) -> None:
    """健康标记缺失、读不出、过期或告警库已降级：抛 AlertsDbError。"""
    try:
        raw = json.loads(status_path(alerts_db).read_text(encoding="utf-8"))
        at = datetime.fromisoformat(raw["at"])
        ok = raw["store_ok"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AlertsDbError("guardd 健康标记不可读（guardd 可能没在运行）") from exc
    if at.tzinfo is None or not -STATUS_MAX_AGE <= now - at <= STATUS_MAX_AGE:
        raise AlertsDbError("guardd 健康标记过期（guardd 可能没在运行）")
    if ok is not True:
        raise AlertsDbError("告警库已降级为只在内存里保留，记录不全")


class AlertsReader:
    """apid 用的只读来源：每次读都先核对 guardd 健康标记，再以 mode=ro 打开。"""

    def __init__(
        self, path: Path, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._path = path
        self._clock = clock

    def recent(self, limit: int) -> list[StoredAlert]:
        check_status(self._path, self._clock())
        return read_alerts(self._path, limit=limit)

    def one(self, alert_id: int) -> tuple[str, StoredAlert | None]:
        check_status(self._path, self._clock())
        return read_alert(self._path, alert_id)


def read_ack_requests(api_db: Path, after: int) -> list[AckRequest]:
    """guardd 只读读取 api.db 的确认请求，seq 升序。"""
    try:
        conn = _read_only(api_db)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version != API_DB_ACKS_VERSION:
                raise AlertsDbError(f"api.db 结构版本 {version} 不支持告警确认")
            rows = conn.execute(
                "SELECT seq, alert_id, alert_key, store_id, actor, at FROM alert_acks"
                " WHERE seq > ? ORDER BY seq LIMIT 500",
                (after,),
            ).fetchall()
        finally:
            conn.close()
        return [
            AckRequest(int(a), int(b), str(c), str(d), str(e), datetime.fromisoformat(str(f)))
            for a, b, c, d, e, f in rows
        ]
    except (sqlite3.Error, ValueError) as exc:
        raise AlertsDbError(f"读取 api.db 确认请求失败：{exc}") from exc


__all__ = (
    "ALERTS_DB",
    "LEVELS",
    "STATUS_FILE",
    "STATUS_MAX_AGE",
    "AckRequest",
    "AlertStore",
    "AlertsDbError",
    "AlertsReader",
    "StoredAlert",
    "alerts_db_path",
    "check_status",
    "read_ack_requests",
    "read_alert",
    "read_alerts",
    "status_path",
    "write_status",
)
