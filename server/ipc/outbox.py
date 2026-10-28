"""意图待发队列（outbox，S-01b，ADR-14）：workerd 的意图先落 meta.db，tradingd 回执后才算已投递。

- meta.db 只由 workerd 写；与账本一样只追加（UPDATE、DELETE 被触发器拒绝），WAL + synchronous=FULL。
- 意图按 intent_id 幂等：同一 intent_id 同内容重复入队返回 False，内容不同抛 OutboxConflictError。
- 回执只追加一次：tradingd 已作出风控决定（放行、缩量、拒绝、HALT）
  或在入口明确拒绝（refused）都记回执，不再重投；
- 连接失败、tradingd 尚未就绪或内部出错不记回执，留在队列里按入队顺序重投
  （tradingd 按 intent_id 去重）；
- 意图生成后超过 max_age 仍未投出（例如 tradingd 停机很久）就不再投递，记为过期回执：
  价格与仓位都可能已变，过期意图由策略下一轮重新生成（失败即关闭）；
  意图自带的 expires_at 已过的同样作废。
- 只能有一个投递者（workerd 里一个投递循环）：并发投递会让同一意图拿到两份回答而回执冲突。
- tradingd 侧不信任这里的过期判断，入口按自己的时钟另查（S-01c 验收条件）。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from server.domain.orders import OrderIntent
from server.storage.logs import log_event

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE outbox_intents (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE outbox_receipts (
    intent_id TEXT PRIMARY KEY REFERENCES outbox_intents(intent_id),
    verdict TEXT,
    status TEXT,
    refused INTEGER NOT NULL,
    error TEXT,
    recorded_at TEXT NOT NULL
);
"""
_TABLES = ("outbox_intents", "outbox_receipts")


class OutboxError(Exception):
    """meta.db 不可用或结构不对（失败即关闭：workerd 不能提交意图）。"""


class OutboxConflictError(OutboxError):
    """同一 intent_id 已入队且内容不同。"""


@dataclass(frozen=True)
class Receipt:
    intent_id: str
    verdict: str | None
    status: str | None
    refused: bool
    error: str | None


class Outbox:
    def __init__(self, path: Path, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self._clock = clock
        try:
            self._conn = sqlite3.connect(path, isolation_level=None)
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA recursive_triggers = ON")
            mode = self._conn.execute("PRAGMA journal_mode = WAL").fetchone()
            if not mode or str(mode[0]).lower() != "wal":
                raise OutboxError(f"无法启用 WAL（得到 {mode}）")
            self._conn.execute("PRAGMA synchronous = FULL")
            self._migrate()
        except sqlite3.Error as exc:
            raise OutboxError(f"无法打开 outbox：{exc}") from exc

    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            self._conn.executescript(
                "BEGIN;"
                + _SCHEMA
                + _append_only_triggers()
                + f"PRAGMA user_version = {SCHEMA_VERSION};COMMIT;"
            )
        elif version != SCHEMA_VERSION:
            raise OutboxError(f"outbox 结构版本 {version} 不认识")
        rows = self._conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        triggers = {r[0] for r in rows}
        missing = {f"{t}_no_{op}" for t in _TABLES for op in ("update", "delete")} - triggers
        if missing:
            raise OutboxError(f"outbox 只追加触发器缺失：{sorted(missing)}")

    # ---------- 入队与查询 ----------

    def enqueue(self, intent: OrderIntent) -> bool:
        payload = intent.model_dump_json()
        try:
            self._conn.execute(
                "INSERT INTO outbox_intents (intent_id, payload, created_at) VALUES (?, ?, ?)",
                (intent.intent_id, payload, self._clock().isoformat()),
            )
        except sqlite3.IntegrityError:
            row = self._conn.execute(
                "SELECT payload FROM outbox_intents WHERE intent_id = ?", (intent.intent_id,)
            ).fetchone()
            if row is not None and row[0] == payload:
                return False
            raise OutboxConflictError(f"意图 {intent.intent_id} 已入队且内容不同") from None
        except sqlite3.Error as exc:
            raise OutboxError(f"写入 outbox 失败：{exc}") from exc
        return True

    def pending_rows(self) -> list[tuple[str, str]]:
        """还没有回执的 (intent_id, payload)，按入队顺序。"""
        rows = self._conn.execute(
            "SELECT i.intent_id, i.payload FROM outbox_intents i LEFT JOIN outbox_receipts r "
            "ON r.intent_id = i.intent_id WHERE r.intent_id IS NULL ORDER BY i.seq"
        ).fetchall()
        return [(str(i), str(p)) for i, p in rows]

    def pending(self) -> list[OrderIntent]:
        """还没有回执的意图，按入队顺序。"""
        return [OrderIntent.model_validate_json(p) for _, p in self.pending_rows()]

    def receipt(self, intent_id: str) -> Receipt | None:
        row = self._conn.execute(
            "SELECT intent_id, verdict, status, refused, error FROM outbox_receipts "
            "WHERE intent_id = ?",
            (intent_id,),
        ).fetchone()
        if row is None:
            return None
        return Receipt(row[0], row[1], row[2], bool(row[3]), row[4])

    def record_receipt(self, receipt: Receipt) -> None:
        try:
            self._conn.execute(
                "INSERT INTO outbox_receipts "
                "(intent_id, verdict, status, refused, error, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (receipt.intent_id, receipt.verdict, receipt.status, int(receipt.refused),
                 receipt.error, self._clock().isoformat()),
            )  # fmt: skip
        except sqlite3.IntegrityError:
            if self.receipt(receipt.intent_id) == receipt:
                return
            raise OutboxConflictError(f"意图 {receipt.intent_id} 已有不同的回执") from None
        except sqlite3.Error as exc:
            raise OutboxError(f"写入回执失败：{exc}") from exc

    def now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise OutboxError("时钟必须带时区")
        return now

    def close(self) -> None:
        self._conn.close()


def _append_only_triggers() -> str:
    parts = []
    for table in _TABLES:
        for verb in ("UPDATE", "DELETE"):
            parts.append(
                f"CREATE TRIGGER {table}_no_{verb.lower()} BEFORE {verb} ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;"
            )
    return "".join(parts)


class IntentChannel(Protocol):
    async def submit_intent(self, intent: OrderIntent) -> dict[str, Any]: ...


@dataclass(frozen=True)
class DeliveryReport:
    delivered: int
    refused: int
    expired: int
    remaining: int  # 还没投出去的（连接失败或 tradingd 出错后停下）


EXPIRED = "expired"
UNREADABLE = "unreadable"  # 入队后结构演进等原因读不回来：作废，不卡住后面的意图


async def deliver_pending(
    outbox: Outbox, channel: IntentChannel, *, max_age: timedelta
) -> DeliveryReport:
    """按入队顺序投递；遇到连接失败或 tradingd 未就绪、内部出错即停（保持顺序），留待下次。"""
    if max_age <= timedelta(0):
        raise ValueError("max_age 必须为正")
    delivered = refused = expired = 0
    queue = outbox.pending_rows()
    for index, (intent_id, payload) in enumerate(queue):
        try:
            intent = OrderIntent.model_validate_json(payload)
        except ValueError:
            outbox.record_receipt(Receipt(intent_id, None, None, True, UNREADABLE))
            log_event(log, logging.ERROR, "outbox 里的意图读不回来，作废", intent_id=intent_id)
            expired += 1
            continue
        now = outbox.now()
        age = now - intent.created_at
        stale = age > max_age or age < -max_age  # 太旧，或生成时间离谱地在未来
        if stale or (intent.expires_at is not None and now >= intent.expires_at):
            outbox.record_receipt(Receipt(intent.intent_id, None, None, True, EXPIRED))
            log_event(log, logging.WARNING, "意图已过期，不再投递", intent_id=intent.intent_id)
            expired += 1
            continue
        try:
            answer = await channel.submit_intent(intent)
        except Exception as exc:  # 连接断开、协议错误：意图留在队列里
            log_event(log, logging.WARNING, "意图投递失败，稍后重投", error=type(exc).__name__)
            return DeliveryReport(delivered, refused, expired, len(queue) - index)
        if answer.get("ok") is True:
            result = answer.get("result")
            if not isinstance(result, dict) or result.get("intent_id") != intent.intent_id:
                log_event(log, logging.ERROR, "tradingd 的回答与意图不对应，停止投递",
                          intent_id=intent.intent_id)  # fmt: skip
                return DeliveryReport(delivered, refused, expired, len(queue) - index)
            outbox.record_receipt(
                Receipt(intent.intent_id, result.get("verdict"), result.get("status"), False, None)
            )
            delivered += 1
        elif answer.get("refused") is True:  # 入口明确拒绝：重投也一样，记回执不再投
            outbox.record_receipt(
                Receipt(intent.intent_id, None, None, True, str(answer.get("error")))
            )
            refused += 1
        else:  # tradingd 未就绪或内部出错：可能是暂时性的，保持顺序，稍后重投
            log_event(log, logging.WARNING, "tradingd 暂未接受意图，稍后重投",
                      error=str(answer.get("error")))  # fmt: skip
            return DeliveryReport(delivered, refused, expired, len(queue) - index)
    return DeliveryReport(delivered, refused, expired, 0)


__all__ = (
    "EXPIRED",
    "UNREADABLE",
    "DeliveryReport",
    "IntentChannel",
    "Outbox",
    "OutboxConflictError",
    "OutboxError",
    "Receipt",
    "deliver_pending",
)
