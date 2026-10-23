"""成交更正的冲正记录（O-02）。

Revision ID: 0004_execution_corrections
Revises: 0003_order_manager

成交是券商事实，入账后不改不删。券商更正一笔成交时，更正后的成交作为新的一行入账，
同时在 execution_supersessions 里记“谁被谁取代”；有效成交 = 全部成交 − 被取代的成交。
- 一笔成交只能被取代一次（主键），一笔更正只能取代一笔成交（唯一）；
- 触发器 execution_supersessions_no_cross_intent：被取代与取代它的成交
  必须属于同一意图、同一券商订单；不能自取代，取代者不能是已被取代的成交（防成环）。

冻结快照：不得 import 当前的 schema 模块。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_execution_corrections"
down_revision: str | None = "0003_order_manager"
branch_labels: str | None = None
depends_on: str | None = None

TABLE = "execution_supersessions"
KEYS = ("superseded_execution_id", "superseding_execution_id")


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column(
            "superseded_execution_id",
            sa.Text,
            sa.ForeignKey("executions.execution_id"),
            primary_key=True,
        ),
        sa.Column(
            "superseding_execution_id",
            sa.Text,
            sa.ForeignKey("executions.execution_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("recorded_at", sa.Text, nullable=False),
    )
    abort = f"BEGIN SELECT RAISE(ABORT, '{TABLE} is append-only'); END"
    for verb in ("UPDATE", "DELETE"):
        op.execute(f"CREATE TRIGGER {TABLE}_no_{verb.lower()} BEFORE {verb} ON {TABLE} {abort}")
    clash = " OR ".join(f"{k} = NEW.{k}" for k in KEYS)
    op.execute(  # 表名与列名都来自本文件的常量，没有外部输入
        f"CREATE TRIGGER {TABLE}_no_replace BEFORE INSERT ON {TABLE} "  # noqa: S608
        f"WHEN EXISTS (SELECT 1 FROM {TABLE} WHERE {clash}) {abort}"
    )
    op.execute(
        "CREATE TRIGGER execution_supersessions_no_cross_intent "
        "BEFORE INSERT ON execution_supersessions "
        "WHEN NEW.superseded_execution_id = NEW.superseding_execution_id "
        # 取代者不能是已被取代的成交（防止 A→X、X→A 成环导致少算）
        "OR EXISTS (SELECT 1 FROM execution_supersessions "
        "WHERE superseded_execution_id = NEW.superseding_execution_id) "
        "OR NOT EXISTS ("
        "SELECT 1 FROM executions a JOIN executions b "
        "ON a.intent_id = b.intent_id AND a.broker_order_id = b.broker_order_id "
        "WHERE a.execution_id = NEW.superseded_execution_id "
        "AND b.execution_id = NEW.superseding_execution_id) "
        "BEGIN SELECT RAISE(ABORT, 'correction must stay within one order'); END"
    )


def downgrade() -> None:
    for verb in ("cross_intent", "update", "delete", "replace"):
        op.execute(f"DROP TRIGGER IF EXISTS {TABLE}_no_{verb}")
    op.drop_table(TABLE)
