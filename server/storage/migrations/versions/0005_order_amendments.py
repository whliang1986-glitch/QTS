"""改单记录（O-03）。

Revision ID: 0005_order_amendments
Revises: 0004_execution_corrections

- order_amendments：改单请求在调用券商**之前**落账，amendment_id 是幂等键；用掉的风控决定唯一
  （一个决定只能用于一次改单，B-01 移交：按 amendment_id 消费）。触发器
  order_amendments_no_cross_intent：决定必须属于同一意图，且不能是已用于签发的决定。
- order_amendment_results：改单结果是**事件**（只追加，按 seq 排序，最后一条为当前结果）：
  - 第一条可以是 ACCEPTED / REJECTED / UNKNOWN；
  - UNKNOWN 之后只能查证转为 ACCEPTED 或 REJECTED；
  - ACCEPTED 之后只能被券商异步拒绝转为 REJECTED（B-05）；
  - REJECTED 是终结，之后不能再追加。
  触发器 order_amendment_results_no_bad_result 强制以上先后关系与取值。
有效下单数量 = 当前结果为 ACCEPTED 的最后一次改单的数量，没有则为签发数量。

冻结快照：不得 import 当前的 schema 模块。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_order_amendments"
down_revision: str | None = "0004_execution_corrections"
branch_labels: str | None = None
depends_on: str | None = None

UNIQUE_KEYS: dict[str, tuple[str, ...]] = {
    "order_amendments": ("seq", "amendment_id", "decision_id"),
    "order_amendment_results": ("seq",),
}


def _text(name: str) -> sa.Column[str]:
    return sa.Column(name, sa.Text, nullable=False)


def upgrade() -> None:
    op.create_table(
        "order_amendments",
        sa.Column("seq", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("amendment_id", sa.Text, nullable=False, unique=True),
        sa.Column("intent_id", sa.Text, sa.ForeignKey("orders.intent_id"), nullable=False),
        sa.Column(
            "decision_id",
            sa.Text,
            sa.ForeignKey("risk_decisions.decision_id"),
            nullable=False,
            unique=True,
        ),
        _text("new_price"),
        _text("new_quantity"),
        _text("requested_at"),
    )
    op.create_table(
        "order_amendment_results",
        sa.Column("seq", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "amendment_id",
            sa.Text,
            sa.ForeignKey("order_amendments.amendment_id"),
            nullable=False,
            index=True,
        ),
        _text("result"),
        _text("recorded_at"),
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
        "CREATE TRIGGER order_amendments_no_cross_intent BEFORE INSERT ON order_amendments "
        "WHEN NOT EXISTS (SELECT 1 FROM risk_decisions "
        "WHERE decision_id = NEW.decision_id AND intent_id = NEW.intent_id) "
        "OR EXISTS (SELECT 1 FROM ticket_issuance WHERE decision_id = NEW.decision_id) "
        "BEGIN SELECT RAISE(ABORT, 'amendment decision must be fresh and of the same intent'); END"
    )
    last = (
        "(SELECT result FROM order_amendment_results "
        "WHERE amendment_id = NEW.amendment_id ORDER BY seq DESC LIMIT 1)"
    )
    op.execute(
        "CREATE TRIGGER order_amendment_results_no_bad_result "
        "BEFORE INSERT ON order_amendment_results "
        "WHEN NEW.result NOT IN ('ACCEPTED', 'REJECTED', 'UNKNOWN') "
        f"OR {last} = 'REJECTED' "
        f"OR ({last} = 'UNKNOWN' AND NEW.result = 'UNKNOWN') "
        f"OR ({last} = 'ACCEPTED' AND NEW.result <> 'REJECTED') "
        "BEGIN SELECT RAISE(ABORT, 'invalid amendment result transition'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS order_amendment_results_no_bad_result")
    op.execute("DROP TRIGGER IF EXISTS order_amendments_no_cross_intent")
    for table in reversed(list(UNIQUE_KEYS)):
        for verb in ("update", "delete", "replace"):
            op.execute(f"DROP TRIGGER IF EXISTS {table}_no_{verb}")
        op.drop_table(table)
