"""待审批队列（S-10，V4.1 第 9 章 & S-02）：统一托管需 Dylan（OWNER）人工批准的事项。

涵盖提案类别：
- `ORDER_INTENT`：ASSISTED_LIVE / 事件驱动人工确认阶段的交易意图；
- `STRATEGY_PROMOTE`：AI 策略或研究策略进入 PAPER / 实盘的晋级审批；
- `FACTOR_PROMOTE`：AI 或手动因子转正审批；
- `TRADING_LIST_ADD`：交易名单加入待确认；
- `RISK_LOOSEN`：风控参数调松待确认。

安全约束：
- 只追加事件记录（`approval_events`），禁止 UPDATE / DELETE；
- 只有 `OWNER` 可以 `decide()`（批准或拒绝），AI 与 VIEWER 一律拒绝；
- 超时未批的待审批项可调用 `expire_stale()` 标记过期，过期后不可再批准（失败即关闭）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class ApprovalKind(StrEnum):
    ORDER_INTENT = "ORDER_INTENT"
    STRATEGY_PROMOTE = "STRATEGY_PROMOTE"
    FACTOR_PROMOTE = "FACTOR_PROMOTE"
    TRADING_LIST_ADD = "TRADING_LIST_ADD"
    RISK_LOOSEN = "RISK_LOOSEN"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class ApprovalError(ValueError):
    """审批队列权限、状态或过期违约。"""


@dataclass(frozen=True)
class ApprovalItem:
    approval_id: str
    kind: ApprovalKind
    title: str
    summary: str
    proposer: str
    payload: Mapping[str, Any]
    status: ApprovalStatus
    created_at: datetime
    expires_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_reason: str = ""


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS approval_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('submit', 'approve', 'reject', 'expire')),
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    proposer TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_approval_events_id ON approval_events (approval_id, seq);

CREATE TRIGGER IF NOT EXISTS approval_events_no_update
BEFORE UPDATE ON approval_events
BEGIN
    SELECT RAISE(ABORT, 'approval_events is append-only: UPDATE forbidden');
END;

CREATE TRIGGER IF NOT EXISTS approval_events_no_delete
BEFORE DELETE ON approval_events
BEGIN
    SELECT RAISE(ABORT, 'approval_events is append-only: DELETE forbidden');
END;
"""


def _iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ApprovalError("时间必须带时区")
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


class ApprovalQueue:
    """只追加待审批队列。"""

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

    def submit(  # noqa: PLR0913
        self,
        *,
        approval_id: str,
        kind: ApprovalKind,
        title: str,
        summary: str,
        proposer: str,
        payload: Mapping[str, Any],
        at: datetime,
        expires_at: datetime,
    ) -> ApprovalItem:
        if not approval_id.strip() or not title.strip() or not proposer.strip():
            raise ApprovalError("approval_id、title 与 proposer 不能为空")
        if expires_at <= at:
            raise ApprovalError("expires_at 必须晚于提交时间")
        if self.get(approval_id) is not None:
            raise ApprovalError(f"审批单 {approval_id} 已存在")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO approval_events (
                    approval_id, action, kind, title, summary, proposer,
                    payload_json, actor, occurred_at, expires_at, reason
                ) VALUES (?, 'submit', ?, ?, ?, ?, ?, ?, ?, ?, '')
                """,
                (
                    approval_id,
                    kind.value,
                    title,
                    summary,
                    proposer,
                    json.dumps(dict(payload), ensure_ascii=False, sort_keys=True),
                    proposer,
                    _iso_utc(at),
                    _iso_utc(expires_at),
                ),
            )
        item = self.get(approval_id)
        if item is None:
            raise ApprovalError("写入审批单后回查失败")
        return item

    def decide(  # noqa: PLR0913
        self,
        approval_id: str,
        *,
        approve: bool,
        actor_role: str,
        actor_name: str,
        reason: str,
        at: datetime,
    ) -> ApprovalItem:
        if actor_role != "owner":
            raise ApprovalError("只有 owner 可以审批待处理队列")
        if not approve and not reason.strip():
            raise ApprovalError("拒绝审批单必须填写原因")
        item = self.get(approval_id)
        if item is None:
            raise ApprovalError(f"审批单不存在：{approval_id}")
        if item.status is not ApprovalStatus.PENDING:
            raise ApprovalError(f"审批单 {approval_id} 已结束（{item.status.value}）")
        if at >= item.expires_at:
            self._append_status(item, "expire", "SYSTEM:expiry", at, "已过有效期")
            raise ApprovalError(f"审批单 {approval_id} 已过期，无法批准")
        action = "approve" if approve else "reject"
        self._append_status(item, action, actor_name, at, reason.strip())
        updated = self.get(approval_id)
        if updated is None:
            raise ApprovalError("更新审批单后回查失败")
        return updated

    def expire_stale(self, *, now: datetime) -> int:
        expired_count = 0
        for item in self.list_items(status=ApprovalStatus.PENDING):
            if now >= item.expires_at:
                self._append_status(item, "expire", "SYSTEM:expiry", now, "超时自动过期")
                expired_count += 1
        return expired_count

    def get(self, approval_id: str) -> ApprovalItem | None:
        all_items = {it.approval_id: it for it in self.list_items()}
        return all_items.get(approval_id)

    def list_items(self, *, status: ApprovalStatus | None = None) -> list[ApprovalItem]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT approval_id, action, kind, title, summary, proposer,
                       payload_json, actor, occurred_at, expires_at, reason
                FROM approval_events
                ORDER BY seq ASC
                """
            ).fetchall()

        items: dict[str, ApprovalItem] = {}
        for r in rows:
            aid, act = str(r[0]), str(r[1])
            if act == "submit":
                items[aid] = ApprovalItem(
                    approval_id=aid,
                    kind=ApprovalKind(str(r[2])),
                    title=str(r[3]),
                    summary=str(r[4]),
                    proposer=str(r[5]),
                    payload=json.loads(str(r[6])),
                    status=ApprovalStatus.PENDING,
                    created_at=datetime.fromisoformat(str(r[8])),
                    expires_at=datetime.fromisoformat(str(r[9])),
                )
            elif aid in items:
                cur = items[aid]
                new_status = {
                    "approve": ApprovalStatus.APPROVED,
                    "reject": ApprovalStatus.REJECTED,
                    "expire": ApprovalStatus.EXPIRED,
                }[act]
                items[aid] = ApprovalItem(
                    approval_id=cur.approval_id,
                    kind=cur.kind,
                    title=cur.title,
                    summary=cur.summary,
                    proposer=cur.proposer,
                    payload=cur.payload,
                    status=new_status,
                    created_at=cur.created_at,
                    expires_at=cur.expires_at,
                    decided_by=str(r[7]),
                    decided_at=datetime.fromisoformat(str(r[8])),
                    decision_reason=str(r[10]),
                )
        out = list(items.values())
        if status is not None:
            out = [it for it in out if it.status is status]
        return out

    def _append_status(
        self, item: ApprovalItem, action: str, actor: str, at: datetime, reason: str
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO approval_events (
                    approval_id, action, kind, title, summary, proposer,
                    payload_json, actor, occurred_at, expires_at, reason
                ) VALUES (?, ?, ?, ?, ?, ?, '{}', ?, ?, ?, ?)
                """,
                (
                    item.approval_id,
                    action,
                    item.kind.value,
                    item.title,
                    item.summary,
                    item.proposer,
                    actor,
                    _iso_utc(at),
                    _iso_utc(item.expires_at),
                    reason,
                ),
            )


__all__ = (
    "ApprovalError",
    "ApprovalItem",
    "ApprovalKind",
    "ApprovalQueue",
    "ApprovalStatus",
)
