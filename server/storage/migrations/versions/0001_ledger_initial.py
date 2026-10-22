"""账本初始表结构与只追加触发器（C-03）。

Revision ID: 0001_ledger_initial
Revises:

冻结快照：不得 import 当前的 schema 模块。以后任何改动账本表的迁移（尤其是 SQLite 的 batch 重建表）
都必须重建该表的三个触发器，否则打开账本时会因缺少触发器被拒绝。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_ledger_initial"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

# 表 → 唯一键（用于防 REPLACE 的 BEFORE INSERT 触发器）
UNIQUE_KEYS: dict[str, tuple[str, ...]] = {
    "orders": ("intent_id",),
    "broker_orders": ("broker_order_id", "intent_id"),
    "perm_ids": ("perm_id", "intent_id"),
    "order_events": ("seq", "event_id"),
    "executions": ("execution_id",),
    "risk_decisions": ("decision_id",),
    "command_log": ("request_id",),
    "audit_events": ("seq", "event_id"),
    "mode_changes": ("seq", "change_id"),
}


def _text(name: str, *, unique: bool = False) -> sa.Column[str]:
    return sa.Column(name, sa.Text, nullable=False, unique=unique)


def _fk_intent(*, unique: bool = False) -> sa.Column[str]:
    return sa.Column(
        "intent_id", sa.Text, sa.ForeignKey("orders.intent_id"), nullable=False, unique=unique
    )


def _seq() -> sa.Column[int]:
    return sa.Column("seq", sa.Integer, primary_key=True, autoincrement=True)


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column("intent_id", sa.Text, primary_key=True),
        _text("account_id"),
        _text("symbol"),
        _text("side"),
        _text("order_class"),
        _text("created_at"),
        _text("payload"),
    )
    op.create_table(
        "broker_orders",
        sa.Column("broker_order_id", sa.Text, primary_key=True),
        _fk_intent(unique=True),
        _text("order_class"),
        _text("submitted_at"),
        _text("payload"),
    )
    op.create_table(
        "perm_ids",
        sa.Column("perm_id", sa.Text, primary_key=True),
        _fk_intent(unique=True),
        _text("recorded_at"),
    )
    op.create_table(
        "order_events",
        _seq(),
        _text("event_id", unique=True),
        _fk_intent(),
        _text("kind"),
        _text("status"),
        _text("occurred_at"),
        _text("correlation_id"),
        _text("payload"),
    )
    op.create_index("ix_order_events_intent_id", "order_events", ["intent_id"])
    op.create_table(
        "executions",
        sa.Column("execution_id", sa.Text, primary_key=True),
        _fk_intent(),
        _text("broker_order_id"),
        _text("symbol"),
        _text("side"),
        _text("quantity"),
        _text("price"),
        _text("commission"),
        _text("executed_at"),
        _text("payload"),
    )
    op.create_index("ix_executions_intent_id", "executions", ["intent_id"])
    op.create_table(
        "risk_decisions",
        sa.Column("decision_id", sa.Text, primary_key=True),
        _fk_intent(),
        _text("verdict"),
        _text("decided_at"),
        _text("payload"),
    )
    op.create_index("ix_risk_decisions_intent_id", "risk_decisions", ["intent_id"])
    op.create_table(
        "command_log",
        sa.Column("request_id", sa.Text, primary_key=True),
        _text("command"),
        _text("result"),
        _text("recorded_at"),
    )
    op.create_table(
        "audit_events",
        _seq(),
        _text("event_id", unique=True),
        _text("occurred_at"),
        _text("actor"),
        _text("action"),
        _text("correlation_id"),
        _text("payload"),
    )
    op.create_table(
        "mode_changes",
        _seq(),
        _text("change_id", unique=True),
        _text("mode"),
        _text("halt"),
        _text("changed_at"),
        _text("actor"),
    )
    for table, keys in UNIQUE_KEYS.items():
        abort = f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
        for verb in ("UPDATE", "DELETE"):
            op.execute(f"CREATE TRIGGER {table}_no_{verb.lower()} BEFORE {verb} ON {table} {abort}")
        # 自增主键在未指定时 NEW.seq 为 -1 或 NULL，不会误拦正常插入
        clash = " OR ".join(f"{k} = NEW.{k}" for k in keys)
        op.execute(  # 表名与列名都来自本文件的常量，没有外部输入
            f"CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table} "  # noqa: S608
            f"WHEN EXISTS (SELECT 1 FROM {table} WHERE {clash}) {abort}"
        )


def downgrade() -> None:
    for table in reversed(list(UNIQUE_KEYS)):
        for verb in ("update", "delete", "replace"):
            op.execute(f"DROP TRIGGER IF EXISTS {table}_no_{verb}")
        op.drop_table(table)
