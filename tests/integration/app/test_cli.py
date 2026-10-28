"""S-01c：进程配置与命令行入口。"""

from __future__ import annotations

import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from server.app.__main__ import DEFAULT_CONFIG, ROOT, main
from server.app.settings import ProcessSettings, parse_process_settings
from server.app.supervisor import EXIT_REFUSED
from server.ipc.protocol import Role
from server.storage.config import ConfigError, load_toml

GOOD = """
[processes]
runtime_dir = "data/run"
ledger = "data/ledger.db"
meta = "data/meta.db"
instance = "qts"
heartbeat_seconds = 15
guard_check_seconds = 15
guard_timeout_seconds = 90
workerd_max_age_seconds = 300

[processes.accounts]
tradingd = "self"
apid = "self"
workerd = "uid:2001"
"""


def parse(tmp_path: Path, text: str, me: str = "S-1-5-me") -> ProcessSettings:
    path = tmp_path / "processes.toml"
    path.write_text(text, encoding="utf-8")
    return parse_process_settings(load_toml(path, name="processes"), base=tmp_path, me=me)


def test_repository_config_loads() -> None:
    settings = parse_process_settings(
        load_toml(DEFAULT_CONFIG, name="processes"), base=ROOT, me="me"
    )
    assert settings.guard_timeout == timedelta(seconds=90)
    assert settings.workerd_max_age == timedelta(minutes=5)
    assert settings.runtime_dir == ROOT / "data" / "run"
    assert set(settings.accounts.values()) == {"me"}


def test_self_resolves_to_the_running_account(tmp_path: Path) -> None:
    settings = parse(tmp_path, GOOD)
    assert settings.accounts == {"tradingd": "S-1-5-me", "apid": "S-1-5-me", "workerd": "uid:2001"}
    assert settings.allowed() == {
        Role.WORKERD: frozenset({"uid:2001"}),
        Role.APID: frozenset({"S-1-5-me"}),
    }
    assert settings.ledger == tmp_path / "data" / "ledger.db"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("heartbeat_seconds = 15", "heartbeat_seconds = 0"),
        ("heartbeat_seconds = 15", "heartbeat_seconds = 1.5"),
        ("heartbeat_seconds = 15", 'heartbeat_seconds = "15"'),
        ("heartbeat_seconds = 15", "heartbeat_seconds = true"),
        ('ledger = "data/ledger.db"', 'ledger = "data\\\\ledger.db"'),
        ('ledger = "data/ledger.db"', 'ledger = ""'),
        ('instance = "qts"', 'instance = ""'),
        ('workerd = "uid:2001"', 'workerd = " "'),
        ('workerd = "uid:2001"', 'workerdd = "uid:2001"'),
        ("guard_timeout_seconds = 90", "guard_timeout_secs = 90"),
        ("[processes]", "[processes]\nextra = 1"),
    ],
)
def test_bad_settings_are_refused(tmp_path: Path, old: str, new: str) -> None:
    with pytest.raises(ConfigError):
        parse(tmp_path, GOOD.replace(old, new))


def test_extra_top_level_tables_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        parse(tmp_path, GOOD + "\n[other]\nx = 1\n")


def test_bad_config_makes_every_command_refuse(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("[processes]\n", encoding="utf-8")
    for command in ("supervise", "tradingd", "workerd", "guardd"):
        assert main([command, "--config", str(bad)]) == EXIT_REFUSED


def test_production_tradingd_refuses_to_start(tmp_path: Path) -> None:
    """生产入口：B-03 之前没有 IBKR 适配器，tradingd 以 78 退出（守护进程不会反复拉起）。"""
    config = tmp_path / "processes.toml"
    config.write_text(GOOD.replace('"uid:2001"', '"self"'), encoding="utf-8")
    from server.storage.migrate import upgrade  # noqa: PLC0415

    (tmp_path / "data").mkdir()
    upgrade(tmp_path / "data" / "ledger.db")
    result = subprocess.run(
        [sys.executable, "-m", "server.app", "tradingd", "--config", str(config),
         "--root", str(tmp_path)],
        cwd=ROOT, capture_output=True, timeout=60, check=False,
    )  # fmt: skip
    assert result.returncode == EXIT_REFUSED, result.stderr.decode("utf-8", "replace")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("guard_timeout_seconds = 90", "guard_timeout_seconds = 91"),  # 不得放宽 V4.1 附录的 90 秒
        ("guard_timeout_seconds = 90", "guard_timeout_seconds = 44"),  # 小于心跳间隔的 3 倍
        ("guard_check_seconds = 15", "guard_check_seconds = 90"),  # 检查间隔不小于超时
        ("workerd_max_age_seconds = 300", "workerd_max_age_seconds = 301"),  # 不得放宽限龄
        ('ledger = "data/ledger.db"', 'ledger = "/abs/ledger.db"'),
        ('ledger = "data/ledger.db"', 'ledger = "C:/x/ledger.db"'),
        ('ledger = "data/ledger.db"', 'ledger = "../ledger.db"'),
        ('ledger = "data/ledger.db"', 'ledger = "data//ledger.db"'),
    ],
)
def test_safety_limits_and_portable_paths(tmp_path: Path, old: str, new: str) -> None:
    with pytest.raises(ConfigError):
        parse(tmp_path, GOOD.replace(old, new))


def test_tighter_values_are_accepted(tmp_path: Path) -> None:
    text = GOOD.replace("guard_timeout_seconds = 90", "guard_timeout_seconds = 60").replace(
        "workerd_max_age_seconds = 300", "workerd_max_age_seconds = 120"
    )
    settings = parse(tmp_path, text)
    assert settings.guard_timeout == timedelta(seconds=60)
    assert settings.workerd_max_age == timedelta(seconds=120)
