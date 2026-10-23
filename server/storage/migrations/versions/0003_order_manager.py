"""OMS 签发记录、未匹配成交与成交归属约束（O-01）。

Revision ID: 0003_order_manager
Revises: 0002_no_live_modes

- ticket_issuance：一张券商订单对应一次签发，记录用掉的风控决定
  （decision_id 唯一：一个决定只能用一次）与实际下单数量；
  与 broker_orders、SUBMIT_SENT 事件在同一事务里写入。
- unmatched_executions：无法关联到本系统订单的成交（外来单、零股、归属不符、成交更正等）
  照实入账，不丢弃。
- executions_no_foreign_order：成交必须挂在同一意图的券商订单上（等价于 (intent_id, broker_order_id)
  → broker_orders 的复合外键；用触发器实现，避免重建 executions 表）。

冻结快照：不得 import 当前的 schema 模块。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_order_manager"
down_revision: str | None = "0002_no_live_modes"
branch_labels: str | None = None
depends_on: str | None = None

UNIQUE_KEYS: dict[str, tuple[str, ...]] = {
    "ticket_issuance": ("broker_order_id", "intent_id", "decision_id"),
    "unmatched_executions": ("execution_id",),
}


def _text(name: str, *, unique: bool = False) -> sa.Column[str]:
    return sa.Column(name, sa.Text, nullable=False, unique=unique)


def upgrade() -> None:
    op.create_table(
        "ticket_issuance",
        sa.Column(
            "broker_order_id",
            sa.Text,
            sa.ForeignKey("broker_orders.broker_order_id"),
            primary_key=True,
        ),
        sa.Column(
            "intent_id", sa.Text, sa.ForeignKey("orders.intent_id"), nullable=False, unique=True
        ),
        sa.Column(
            "decision_id",
            sa.Text,
            sa.ForeignKey("risk_decisions.decision_id"),
            nullable=False,
            unique=True,
        ),
        _text("quantity"),
        _text("issued_at"),
    )
    op.create_table(
        "unmatched_executions",
        sa.Column("execution_id", sa.Text, primary_key=True),
        _text("reason"),
        _text("recorded_at"),
        _text("payload"),
    )
    for table, keys in UNIQUE_KEYS.items():
        abort = f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
        for verb in ("UPDATE", "DELETE"):
            op.execute(f"CREATE TRIGGER {table}_no_{verb.lower()} BEFORE {verb} ON {table} {abort}")
        clash = " OR ".join(f"{k} = NEW.{k}" for k in keys)
        op.execute(  # 表名与列名都来自本文件的常量，没有外部输入
            f"CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table} "  # noqa: S608
            f"WHEN EXISTS (SELECT 1 FROM {table} WHERE {clash}) {abort}"
        )
    op.execute(
        "CREATE TRIGGER executions_no_foreign_order BEFORE INSERT ON executions "
        "WHEN NOT EXISTS (SELECT 1 FROM broker_orders WHERE broker_order_id = NEW.broker_order_id "
        "AND intent_id = NEW.intent_id) "
        "BEGIN SELECT RAISE(ABORT, 'execution must belong to an order of the same intent'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS executions_no_foreign_order")
    for table in reversed(list(UNIQUE_KEYS)):
        for verb in ("update", "delete", "replace"):
            op.execute(f"DROP TRIGGER IF EXISTS {table}_no_{verb}")
        op.drop_table(table)
