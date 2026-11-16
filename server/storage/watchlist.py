"""自选标的存储与行情订阅额度管理（S-14 第二节）。

特点：
1. 独立只追加事件表 `watchlist_events`（存 api.db 同目录或专用 SQLite），重放得到当前自选；
2. 增删、分组、标签、备注每一步均写审计，只有 owner 可改；
3. 行情订阅额度上限（`max_subscriptions`）：超出额度部分标明 `UNSUBSCRIBED`，
   延迟行情标明 `DELAYED`（绝不伪装成实时），不可用标明 `UNAVAILABLE`；
4. 自选永远只是候选，绝不直接赋予交易权限（三层隔离）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path


class QuoteSubscriptionStatus(StrEnum):
    REALTIME = "REALTIME"
    DELAYED = "DELAYED"
    UNSUBSCRIBED = "UNSUBSCRIBED"
    UNAVAILABLE = "UNAVAILABLE"


class WatchlistError(ValueError):
    """自选操作违约。"""


@dataclass(frozen=True)
class WatchlistEntry:
    symbol: str
    group_name: str
    tags: tuple[str, ...]
    note: str
    updated_at: datetime
    subscription_status: QuoteSubscriptionStatus = QuoteSubscriptionStatus.UNAVAILABLE


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS watchlist_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('upsert', 'remove')),
    group_name TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    note TEXT NOT NULL,
    actor TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS watchlist_events_no_update
BEFORE UPDATE ON watchlist_events
BEGIN
    SELECT RAISE(ABORT, 'watchlist_events is append-only: UPDATE forbidden');
END;

CREATE TRIGGER IF NOT EXISTS watchlist_events_no_delete
BEFORE DELETE ON watchlist_events
BEGIN
    SELECT RAISE(ABORT, 'watchlist_events is append-only: DELETE forbidden');
END;
"""


class WatchlistStore:
    """只追加自选标的存储（含行情订阅额度裁剪）。"""

    def __init__(self, db_path: Path, *, max_subscriptions: int = 50) -> None:
        if max_subscriptions < 1:
            raise ValueError("max_subscriptions 必须为正整数")
        self._path = db_path
        self._max_subscriptions = max_subscriptions
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA recursive_triggers = ON")
        return conn

    def upsert(  # noqa: PLR0913
        self,
        symbol: str,
        *,
        group_name: str = "default",
        tags: tuple[str, ...] = (),
        note: str = "",
        actor: str,
        at: datetime,
    ) -> WatchlistEntry:
        sym = symbol.strip().upper()
        if not sym:
            raise WatchlistError("标的代码不能为空")
        if not actor.strip():
            raise WatchlistError("操作者不能为空")
        if at.tzinfo is None or at.utcoffset() is None:
            raise WatchlistError("时间必须带时区")
        clean_tags = tuple(dict.fromkeys(t.strip() for t in tags if t.strip()))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO watchlist_events (
                    symbol, action, group_name, tags_json, note, actor, occurred_at
                ) VALUES (?, 'upsert', ?, ?, ?, ?, ?)
                """,
                (
                    sym,
                    group_name.strip() or "default",
                    json.dumps(list(clean_tags), ensure_ascii=False),
                    note.strip(),
                    actor,
                    at.astimezone(UTC).isoformat(timespec="microseconds"),
                ),
            )
        entries = {e.symbol: e for e in self.list_entries()}
        return entries[sym]

    def remove(self, symbol: str, *, actor: str, at: datetime) -> None:
        sym = symbol.strip().upper()
        if not sym:
            raise WatchlistError("标的代码不能为空")
        if not actor.strip():
            raise WatchlistError("操作者不能为空")
        if at.tzinfo is None or at.utcoffset() is None:
            raise WatchlistError("时间必须带时区")
        current = {e.symbol for e in self.list_entries()}
        if sym not in current:
            raise WatchlistError(f"{sym} 不在自选列表中")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO watchlist_events (
                    symbol, action, group_name, tags_json, note, actor, occurred_at
                ) VALUES (?, 'remove', '', '[]', '', ?, ?)
                """,
                (sym, actor, at.astimezone(UTC).isoformat(timespec="microseconds")),
            )

    def list_entries(
        self,
        *,
        quote_delayed_map: dict[str, bool] | None = None,
    ) -> list[WatchlistEntry]:
        """按加入先后重放自选列表，前 max_subscriptions 只分配行情订阅，超出标明 UNSUBSCRIBED。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT symbol, action, group_name, tags_json, note, occurred_at
                FROM watchlist_events
                ORDER BY seq ASC
                """
            ).fetchall()

        ordered: dict[str, tuple[str, tuple[str, ...], str, datetime]] = {}
        for r in rows:
            sym, act = str(r[0]), str(r[1])
            if act == "upsert":
                ordered[sym] = (
                    str(r[2]),
                    tuple(str(x) for x in json.loads(str(r[3]))),
                    str(r[4]),
                    datetime.fromisoformat(str(r[5])),
                )
            elif act == "remove":
                ordered.pop(sym, None)

        out: list[WatchlistEntry] = []
        delayed_map = quote_delayed_map or {}
        for idx, (sym, (grp, tags, note, ts)) in enumerate(ordered.items()):
            if idx >= self._max_subscriptions:
                q_status = QuoteSubscriptionStatus.UNSUBSCRIBED
            elif sym not in delayed_map:
                q_status = QuoteSubscriptionStatus.UNAVAILABLE
            elif delayed_map[sym]:
                q_status = QuoteSubscriptionStatus.DELAYED
            else:
                q_status = QuoteSubscriptionStatus.REALTIME
            out.append(
                WatchlistEntry(
                    symbol=sym,
                    group_name=grp,
                    tags=tags,
                    note=note,
                    updated_at=ts,
                    subscription_status=q_status,
                )
            )
        return out


def quote_status_label(status: QuoteSubscriptionStatus) -> str:
    return {
        QuoteSubscriptionStatus.REALTIME: "实时",
        QuoteSubscriptionStatus.DELAYED: "延迟",
        QuoteSubscriptionStatus.UNSUBSCRIBED: "未订阅（超出额度）",
        QuoteSubscriptionStatus.UNAVAILABLE: "不可用",
    }[status]


__all__ = (
    "QuoteSubscriptionStatus",
    "WatchlistEntry",
    "WatchlistError",
    "WatchlistStore",
    "quote_status_label",
)
