"""api.db（S-03，V4.1 5.1 / 10.2）：apid 的令牌与审计。不含任何交易数据。

- 单写者：只有 apid（或在 apid 停止时持同一把锁的令牌管理命令）写；锁由调用方负责（server.app）。
- 只追加：令牌事件（签发、吊销）与审计两张表，UPDATE、DELETE 被触发器拒绝；
  令牌的当前状态由事件推出。结构版本或触发器不对即拒绝打开（失败即关闭）。
- 令牌：`qts_<token_id>_<secret>`；库里只存 secret 的 SHA-256，比对用 hmac.compare_digest；
  明文只在签发时返回一次，不进日志、审计。secret 是 32 字节随机数，SHA-256 足够（不是口令）。
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA_VERSION = 2  # v2（S-05a）：告警确认请求 alert_acks
MAX_TOKEN_DAYS = 365
TOKEN_PREFIX = "qts"  # noqa: S105 - 令牌格式前缀，不是密钥
ROLES = frozenset({"owner", "viewer"})
_TOKEN = re.compile(r"^qts_([0-9a-f]{16})_([A-Za-z0-9_-]{43})$")
_NAME = re.compile(r"^\S{1,128}$")
_SCHEMA = """
CREATE TABLE token_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('issued', 'revoked')),
    name TEXT,
    role TEXT CHECK (role IS NULL OR role IN ('owner', 'viewer')),
    digest TEXT,
    expires_at TEXT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT,
    CHECK ((kind = 'issued') = (name IS NOT NULL AND role IS NOT NULL AND digest IS NOT NULL
                                AND expires_at IS NOT NULL))
);
CREATE UNIQUE INDEX token_events_once ON token_events (token_id, kind);
CREATE TABLE api_audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""
# v2：告警确认请求（S-05a）。apid 写；guardd 只用 SQL 只读，按 seq 续读后执行确认。
# alert_id 是 alerts.db 里那次告警发生的编号：确认只对那一次发生有效，旧请求不会误确认新告警。
_SCHEMA_V2 = """
CREATE TABLE alert_acks (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    alert_id INTEGER NOT NULL,
    alert_key TEXT NOT NULL,
    store_id TEXT NOT NULL,
    actor TEXT NOT NULL
);
"""
_TABLES = ("token_events", "api_audit", "alert_acks")


class ApiDbError(Exception):
    """api.db 不可用、结构不对或写入失败（失败即关闭）。"""


class TokenNotFoundError(ApiDbError):
    """没有这个令牌。"""


@dataclass(frozen=True)
class TokenInfo:
    token_id: str
    name: str
    role: str
    issued_at: str
    expires_at: str
    revoked_at: str | None

    def state(self, now: datetime) -> str:
        if self.revoked_at is not None:
            return "已吊销"
        return "已过期" if datetime.fromisoformat(self.expires_at) <= now else "有效"


@dataclass(frozen=True)
class Identity:
    token_id: str
    name: str
    role: str


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ApiDbError("时间必须带时区")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


class ApiDb:
    def __init__(
        self, path: Path, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._clock = clock
        self._lock = threading.Lock()  # 一条连接被线程池里的多个请求共用
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA recursive_triggers = ON")
            mode = self._conn.execute("PRAGMA journal_mode = WAL").fetchone()
            if not mode or str(mode[0]).lower() != "wal":
                raise ApiDbError(f"无法启用 WAL（得到 {mode}）")
            self._conn.execute("PRAGMA synchronous = FULL")
            self._migrate()
        except sqlite3.Error as exc:
            raise ApiDbError(f"无法打开 api.db：{exc}") from exc

    def close(self) -> None:
        self._conn.close()

    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            self._conn.executescript(
                "BEGIN;" + _SCHEMA + _SCHEMA_V2 + _append_only_triggers(_TABLES)
                + f"PRAGMA user_version = {SCHEMA_VERSION};COMMIT;"
            )  # fmt: skip
        elif version == 1:  # 升级：先留一份 v1 备份（回滚用），再只新增表与触发器，不动已有数据
            self._backup_before_upgrade(1)
            self._conn.executescript(
                "BEGIN;" + _SCHEMA_V2 + _append_only_triggers(("alert_acks",))
                + f"PRAGMA user_version = {SCHEMA_VERSION};COMMIT;"
            )  # fmt: skip
        elif version != SCHEMA_VERSION:
            raise ApiDbError(f"api.db 结构版本 {version} 不认识")
        rows = self._conn.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='trigger'"
        )
        triggers = {n for n, t, sql in rows if n.rsplit("_no_", 1)[0] == t and "RAISE(ABORT" in sql}
        missing = {f"{t}_no_{op}" for t in _TABLES for op in ("update", "delete")} - triggers
        if missing:
            raise ApiDbError(f"api.db 只追加触发器缺失：{sorted(missing)}")

    def _backup_before_upgrade(self, version: int) -> None:
        main = self._conn.execute("PRAGMA database_list").fetchone()[2]
        target = Path(main).with_name(Path(main).name + f".v{version}.bak")
        if target.exists():
            return  # 已有备份：不覆盖（第一次升级前的那份最有用）
        backup = sqlite3.connect(target)
        try:
            self._conn.backup(backup)
        finally:
            backup.close()

    # ---------- 令牌 ----------

    def issue(self, name: str, role: str, *, days: int, actor: str) -> tuple[str, str]:
        """签发令牌，返回 (token_id, 明文令牌)。明文只在这里出现一次。"""
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ApiDbError("使用者名称必须是 1 到 128 个非空白字符")
        if role not in ROLES:
            raise ApiDbError(f"角色只能是：{sorted(ROLES)}")
        if type(days) is not int or not 1 <= days <= MAX_TOKEN_DAYS:
            raise ApiDbError(f"有效期必须是 1 到 {MAX_TOKEN_DAYS} 天")
        token_id = secrets.token_hex(8)
        secret = secrets.token_urlsafe(32)
        now = self._clock()
        self._write(  # 令牌与审计同一事务：要么都落盘，要么都不落盘
            (
                "INSERT INTO token_events (token_id, kind, name, role, digest, expires_at, at,"
                " actor) VALUES (?, 'issued', ?, ?, ?, ?, ?, ?)",
                (token_id, name, role, _digest(secret), _iso(now + timedelta(days=days)),
                 _iso(now), actor),
            ),
            self._audit_row("token_issued", actor,
                            {"token_id": token_id, "name": name, "role": role}),
        )  # fmt: skip
        return token_id, f"{TOKEN_PREFIX}_{token_id}_{secret}"

    def revoke(self, token_id: str, *, actor: str, reason: str) -> None:
        if not reason.strip():
            raise ApiDbError("吊销必须写明原因")
        info = self._info(token_id)
        if info is None:
            raise TokenNotFoundError(f"没有这个令牌：{token_id}")
        if info.revoked_at is not None:
            return  # 已吊销：幂等
        self._write(
            (
                "INSERT INTO token_events (token_id, kind, at, actor, reason)"
                " VALUES (?, 'revoked', ?, ?, ?)",
                (token_id, _iso(self._clock()), actor, reason),
            ),
            self._audit_row("token_revoked", actor, {"token_id": token_id, "reason": reason}),
        )

    def now(self) -> datetime:
        return self._clock()

    def tokens(self) -> list[TokenInfo]:
        ids = [r[0] for r in self._read(
            "SELECT token_id FROM token_events WHERE kind = 'issued' ORDER BY seq", ()
        )]  # fmt: skip
        return [info for i in ids if (info := self._info(i)) is not None]

    def _info(self, token_id: str) -> TokenInfo | None:
        rows = self._read(
            "SELECT kind, name, role, expires_at, at FROM token_events WHERE token_id = ?",
            (token_id,),
        )
        issued = next((r for r in rows if r[0] == "issued"), None)
        if issued is None:
            return None
        revoked = next((r[4] for r in rows if r[0] == "revoked"), None)
        return TokenInfo(token_id, issued[1], issued[2], issued[4], issued[3], revoked)

    def verify(self, token: str) -> Identity | None:
        """令牌有效（存在、哈希相符、未过期、未吊销）返回身份，否则 None。"""
        match = _TOKEN.fullmatch(token) if isinstance(token, str) else None
        if match is None:
            return None
        token_id, secret = match.groups()
        rows = self._read(
            "SELECT kind, name, role, digest, expires_at FROM token_events WHERE token_id = ?",
            (token_id,),
        )
        issued = next((r for r in rows if r[0] == "issued"), None)
        if issued is None or any(r[0] == "revoked" for r in rows):
            return None
        if not hmac.compare_digest(issued[3], _digest(secret)):
            return None
        if datetime.fromisoformat(issued[4]) <= self._clock():
            return None
        return Identity(token_id, issued[1], issued[2])

    # ---------- 告警确认请求（S-05a） ----------

    def request_ack(self, alert_id: int, alert_key: str, store_id: str, *, actor: str) -> None:
        """记录一条确认请求，同一事务写审计。重复确认无害：guardd 只确认仍未确认的那次发生。

        store_id 是 alerts.db 的库编号：库被重建后，旧请求不会作用于新库里同编号的告警。
        """
        if type(alert_id) is not int or alert_id <= 0 or not alert_key.strip():
            raise ApiDbError("告警编号或键不合法")
        if not store_id.strip():
            raise ApiDbError("告警库编号不能为空")
        now = _iso(self._clock())
        self._write(
            (
                "INSERT INTO alert_acks (at, alert_id, alert_key, store_id, actor)"
                " VALUES (?, ?, ?, ?, ?)",
                (now, alert_id, alert_key, store_id, actor),
            ),
            self._audit_row("alert_ack_requested", actor,
                            {"alert_id": str(alert_id), "alert_key": alert_key}),
        )  # fmt: skip

    def ack_requested(self, alert_id: int, store_id: str) -> bool:
        """这次发生是否已有确认请求（guardd 还没执行时再点确认，不重复记录）。"""
        rows = self._read(
            "SELECT 1 FROM alert_acks WHERE alert_id = ? AND store_id = ? LIMIT 1",
            (alert_id, store_id),
        )
        return bool(rows)

    # ---------- 审计 ----------

    def recent_audits(
        self, limit: int, before: int | None = None
    ) -> list[tuple[int, str, str, str, dict[str, str]]]:
        """最新的审计记录（从新到旧）：(seq, 时间, 动作, 操作者, 详情)。before 用于翻页。"""
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ApiDbError("limit 必须是 1 到 500 的整数")
        sql = "SELECT seq, at, action, actor, detail FROM api_audit"
        args: tuple[object, ...] = (limit,)
        if before is not None:
            sql, args = sql + " WHERE seq < ?", (before, limit)
        rows = self._read(sql + " ORDER BY seq DESC LIMIT ?", args)
        return [(int(s), a, b, c, json.loads(d)) for s, a, b, c, d in rows]

    def audit(self, action: str, actor: str, detail: Mapping[str, str]) -> None:
        self._write(self._audit_row(action, actor, detail))

    def _audit_row(
        self, action: str, actor: str, detail: Mapping[str, str]
    ) -> tuple[str, tuple[object, ...]]:
        body = json.dumps(dict(detail), ensure_ascii=False, sort_keys=True)
        return (
            "INSERT INTO api_audit (at, action, actor, detail) VALUES (?, ?, ?, ?)",
            (_iso(self._clock()), action, actor, body),
        )

    def audits(self, action: str | None = None) -> list[tuple[str, str, str, dict[str, str]]]:
        """审计记录，按写入顺序：(时间, 动作, 操作者, 详情)。"""
        sql = "SELECT at, action, actor, detail FROM api_audit"
        args: tuple[str, ...] = ()
        if action is not None:
            sql, args = sql + " WHERE action = ?", (action,)
        return [(a, b, c, json.loads(d)) for a, b, c, d in self._read(sql + " ORDER BY seq", args)]

    # ---------- 基础 ----------

    def _write(self, *statements: tuple[str, tuple[object, ...]]) -> None:
        """在同一个事务里执行几条写入：任何一条失败整体回滚。"""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                for sql, args in statements:
                    self._conn.execute(sql, args)
                self._conn.execute("COMMIT")
            except sqlite3.Error as exc:
                if self._conn.in_transaction:
                    with contextlib.suppress(sqlite3.Error):
                        self._conn.execute("ROLLBACK")
                raise ApiDbError(f"写入 api.db 失败，已回滚：{exc}") from exc

    def _read(self, sql: str, args: tuple[object, ...]) -> list[tuple[str, ...]]:
        try:
            with self._lock:
                return self._conn.execute(sql, args).fetchall()
        except sqlite3.Error as exc:
            raise ApiDbError(f"读取 api.db 失败：{exc}") from exc


def _append_only_triggers(tables: tuple[str, ...]) -> str:
    return "".join(
        f"CREATE TRIGGER {table}_no_{verb.lower()} BEFORE {verb} ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;"
        for table in tables
        for verb in ("UPDATE", "DELETE")
    )


__all__ = (
    "MAX_TOKEN_DAYS",
    "ApiDb",
    "ApiDbError",
    "Identity",
    "TokenInfo",
    "TokenNotFoundError",
)
