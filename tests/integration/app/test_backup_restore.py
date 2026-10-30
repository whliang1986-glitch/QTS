"""恢复演练（S-08）：交易中的账本做备份，恢复到干净目录后必须处于 SAFE HALT，不可交易。"""

from __future__ import annotations

from pathlib import Path

import pytest
from server.app.backup import RESTORE_REASON, restore_and_halt
from server.risk.mode_manager import ModeManager, TradingHaltedError
from server.storage.backup import BackupError, create_backup
from server.storage.ledger import LedgerRepository, open_ledger


@pytest.mark.asyncio
async def test_restored_ledger_is_safe_halted_and_not_tradable(tmp_path: Path) -> None:
    from tests.integration.risk.test_core import Rig  # noqa: PLC0415
    from tests.support.oms import Env  # noqa: PLC0415

    env = Env(tmp_path / "live")
    await env.broker.connect()
    await env.pump()
    rig = Rig(env)
    await rig.bring_up()  # 正常交易状态：已恢复、可下单
    state = rig.modes.current()
    assert state is not None
    assert not state.halt.halted
    ledger = tmp_path / "live" / "ledger.db"  # Env 的账本位置
    info = create_backup(
        tmp_path / "backups", databases={"ledger": ledger}, directories={},
        lake_manifests=None, now=env.clock(),
    )  # fmt: skip

    restore_and_halt(info.path, tmp_path / "clean", clock=env.clock)

    engine = open_ledger(tmp_path / "clean" / "db" / "ledger.db")
    try:
        modes = ModeManager(LedgerRepository(engine), clock=env.clock)
        restored = modes.current()
        assert restored is not None
        assert restored.halt.halted
        assert restored.halt.reason is not None
        assert RESTORE_REASON in restored.halt.reason
        # 用原账本相同的登记启动：仍保持 SAFE HALT，交易与降风险通道都关闭，等对账与人工恢复
        from tests.integration.risk.test_core import config  # noqa: PLC0415
        from tests.support.oms import ACCOUNT  # noqa: PLC0415

        started = modes.start(config(), ACCOUNT)
        assert started.halt.halted
        with pytest.raises(TradingHaltedError, match="HALT"):
            modes.require_trading_allowed()
        assert not modes.risk_reducing_allowed()
        halts = LedgerRepository(engine).audits("halt")
        assert halts and dict(halts[0].detail).get("safe") == "true"
    finally:
        engine.dispose()
    original = rig.modes.current()
    assert original is not None
    assert not original.halt.halted  # 原账本不受影响


def test_restore_without_a_ledger_is_refused(tmp_path: Path) -> None:
    import sqlite3  # noqa: PLC0415
    from contextlib import closing  # noqa: PLC0415
    from datetime import UTC, datetime  # noqa: PLC0415

    meta = tmp_path / "meta.db"
    with closing(sqlite3.connect(meta)) as conn:
        conn.execute("CREATE TABLE t (x)")
        conn.commit()
    info = create_backup(tmp_path / "backups", databases={"meta": meta}, directories={},
                         lake_manifests=None, now=datetime(2026, 10, 29, tzinfo=UTC))  # fmt: skip
    with pytest.raises(BackupError, match="没有账本"):
        restore_and_halt(info.path, tmp_path / "clean")


def test_halt_failure_leaves_no_restored_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime  # noqa: PLC0415

    from server.storage.migrate import upgrade  # noqa: PLC0415

    ledger = tmp_path / "ledger.db"
    upgrade(ledger)
    info = create_backup(tmp_path / "backups", databases={"ledger": ledger}, directories={},
                         lake_manifests=None, now=datetime(2026, 10, 29, tzinfo=UTC))  # fmt: skip

    def broken(self: object, reason: str, *, actor: str) -> object:
        raise RuntimeError("写入失败")

    monkeypatch.setattr(ModeManager, "safe_halt", broken)
    with pytest.raises(RuntimeError, match="写入失败"):
        restore_and_halt(info.path, tmp_path / "clean")
    assert not (tmp_path / "clean").exists()  # 不会留下可交易的账本
