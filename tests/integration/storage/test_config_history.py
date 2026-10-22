"""C-05：配置版本轨迹写入真实 SQLite 账本（只追加）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from server.storage.config import CONFIG_ACTION, ConfigHistory, load_toml
from server.storage.ledger import LedgerRepository, open_ledger
from server.storage.migrate import upgrade

T0 = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
NL = chr(10)


@pytest.fixture
def repo(tmp_path: Path) -> LedgerRepository:
    path = tmp_path / "ledger.db"
    upgrade(path)
    return LedgerRepository(open_ledger(path))


def test_history_records_changes_and_rollbacks(tmp_path: Path, repo: LedgerRepository) -> None:
    cfg = tmp_path / "mode.toml"
    history = ConfigHistory(repo)

    def load(text: str, at: datetime) -> bool:
        cfg.write_text(text, encoding="utf-8")
        return history.record(
            load_toml(cfg, name="mode"), actor="tradingd", correlation_id="boot", at=at
        )

    assert load("[mode]" + NL + "a = 1" + NL, T0) is True
    assert load("[mode]" + NL + "a = 1" + NL, T0 + timedelta(minutes=1)) is False  # 重复加载
    assert load("[mode]" + NL + "a = 2" + NL, T0 + timedelta(minutes=2)) is True
    assert load("[mode]" + NL + "a = 1" + NL, T0 + timedelta(minutes=3)) is True  # 回滚也留痕
    versions = history.versions("mode")
    assert len(versions) == 3
    assert versions[0].previous == versions[1].sha256
    assert versions[1].previous == versions[2].sha256 and versions[2].previous is None
    assert versions[0].sha256 == versions[2].sha256
    assert all(v.actor == "tradingd" for v in versions)
    assert len(repo.audits(CONFIG_ACTION)) == 3


def test_histories_are_per_config_name(tmp_path: Path, repo: LedgerRepository) -> None:
    history = ConfigHistory(repo)
    for name in ("mode", "calendar"):
        path = tmp_path / f"{name}.toml"
        path.write_text(f"[{name}]" + NL, encoding="utf-8")
        assert history.record(load_toml(path, name=name), actor="t", correlation_id="c", at=T0)
    assert len(history.versions("mode")) == 1 and len(history.versions("calendar")) == 1
