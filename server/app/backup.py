"""备份与恢复的装配（S-08）：恢复到干净机器后，账本立刻进入 SAFE HALT。

恢复出来的账本不能直接交易：备份时刻之后券商侧可能已有成交、撤单或公司行动。
所以恢复后写入持久化的 SAFE HALT（重启不自动恢复），之后必须完成券商对账、人工恢复，才允许交易
（V4.1 5.5、7.5：备份不能代替券商对账）。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from server.risk.mode_manager import ModeManager
from server.storage.backup import BackupError, BackupInfo, restore_backup
from server.storage.ledger import LedgerRepository, open_ledger

RESTORE_ACTOR = "system"
RESTORE_REASON = "从备份恢复：账本可能落后于券商，必须先完成券商对账并人工恢复"


def restore_and_halt(
    backup: Path,
    target_root: Path,
    *,
    ledger: str = "ledger",
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> BackupInfo:
    """恢复备份，并在改名为目标之前于临时目录的账本上写入 SAFE HALT、读回确认。

    任何一步失败（含 HALT 写入失败、中途被杀）都不会在目标处留下可交易的账本。
    """
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", ledger):
        raise BackupError(f"账本名称不合法：{ledger!r}")

    def halt(staging: Path) -> None:
        path = staging / "db" / f"{ledger}.db"
        if not path.is_file():
            raise BackupError(f"备份里没有账本 {ledger}.db")
        engine = open_ledger(path)
        try:
            modes = ModeManager(LedgerRepository(engine), clock=clock)
            modes.safe_halt(f"{RESTORE_REASON}（备份 {backup.name}）", actor=RESTORE_ACTOR)
            check = ModeManager(LedgerRepository(engine), clock=clock).current()
            if check is None or not check.halt.halted:
                raise BackupError("恢复后未能写入 SAFE HALT")
        finally:
            engine.dispose()

    return restore_backup(backup, target_root, prepare=halt)
