"""持仓调整记录（R-07 对账）。

Revision ID: 0006_position_adjustments
Revises: 0005_order_amendments

对账的预期持仓 = 本表的调整合计 + 本系统全部有效成交（买入为正、卖出为负）。
本表记录不来自本系统成交的持仓变化：
- BASELINE：人工确认以券商持仓为基准（首次启动、备份恢复、人工处理差异之后），
  只能在 HALT 下由人工发起；reference 为“采用批次:标的”；
- CORPORATE_ACTION：与公司行动数据匹配上的拆股、合股（reference 为公司行动事件号与标的，
  同一事件只能调整一次）；
- ACKNOWLEDGE：人工确认一笔外来成交（reference 为券商成交号，数量变化记 0，
  持仓差异由 BASELINE 处理）。
只追加；adjustment_id 与 (kind, reference) 唯一；
kind 取值由触发器 position_adjustments_no_bad_kind 强制。

冻结快照：不得 import 当前的 schema 模块。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_position_adjustments"
down_revision: str | None = "0005_order_amendments"
branch_labels: str | None = None
depends_on: str | None = None

TABLE = "position_adjustments"
KEYS = ("seq", "adjustment_id")


def _text(name: str) -> sa.Column[str]:
    return sa.Column(name, sa.Text, nullable=False)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("seq", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("adjustment_id", sa.Text, nullable=False, unique=True),
        _text("account_id"),
        _text("symbol"),
        _text("quantity_delta"),
        _text("kind"),
        _text("reference"),
        _text("actor"),
        _text("recorded_at"),
        _text("payload"),
        sa.UniqueConstraint("kind", "reference", name="position_adjustments_once"),
    )
    abort = f"BEGIN SELECT RAISE(ABORT, '{TABLE} is append-only'); END"
    for verb in ("UPDATE", "DELETE"):
        op.execute(f"CREATE TRIGGER {TABLE}_no_{verb.lower()} BEFORE {verb} ON {TABLE} {abort}")
    clash = " OR ".join(f"{k} = NEW.{k}" for k in KEYS)
    op.execute(  # 表名与列名都来自本文件的常量，没有外部输入
        f"CREATE TRIGGER {TABLE}_no_replace BEFORE INSERT ON {TABLE} "  # noqa: S608
        f"WHEN EXISTS (SELECT 1 FROM {TABLE} WHERE {clash} "
        f"OR (kind = NEW.kind AND reference = NEW.reference)) {abort}"
    )
    op.execute(
        f"CREATE TRIGGER {TABLE}_no_bad_kind BEFORE INSERT ON {TABLE} "
        "WHEN NEW.kind NOT IN ('BASELINE', 'CORPORATE_ACTION', 'ACKNOWLEDGE') "
        "OR (NEW.kind = 'ACKNOWLEDGE' AND NEW.quantity_delta <> '0') "
        "BEGIN SELECT RAISE(ABORT, 'invalid position adjustment'); END"
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {TABLE}_no_bad_kind")
    for verb in ("update", "delete", "replace"):
        op.execute(f"DROP TRIGGER IF EXISTS {TABLE}_no_{verb}")
    op.drop_table(TABLE)
