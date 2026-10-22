"""G7 之前禁止在账本中写入实盘模式（C-04）。

Revision ID: 0002_no_live_modes
Revises: 0001_ledger_initial

ModeManager 已在代码层拒绝实盘模式；这是数据库层的最后一道防线：任何路径（包括直接写库）
往 mode_changes 写入 ASSISTED_LIVE / LIMITED_AUTO_LIVE 都会被拒绝。解除只能通过一个
经 Dylan 批准的新迁移（G7 任务），不能靠改代码里的常量。
"""

from __future__ import annotations

from alembic import op

revision = "0002_no_live_modes"
down_revision: str | None = "0001_ledger_initial"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        "CREATE TRIGGER mode_changes_no_live BEFORE INSERT ON mode_changes "
        "WHEN NEW.mode IN ('ASSISTED_LIVE', 'LIMITED_AUTO_LIVE') "
        "BEGIN SELECT RAISE(ABORT, 'live modes are locked before G7'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS mode_changes_no_live")
