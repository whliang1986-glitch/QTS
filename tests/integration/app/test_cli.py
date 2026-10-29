"""S-01c：进程配置与命令行入口。"""

from __future__ import annotations

import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

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


# ---------- S-01d：调度配置 ----------

SCHEDULE = """
[schedule]
assess_seconds = 15
intraday_reconcile_minutes = 5
end_of_day_delay_minutes = 10

[schedule.broker_restart]
timezone = "America/New_York"
start = "23:45"
minutes = 15
"""


def parse_schedule(tmp_path: Path, text: str) -> object:
    from server.app.settings import parse_schedule_settings  # noqa: PLC0415

    path = tmp_path / "schedule.toml"
    path.write_text(text, encoding="utf-8")
    return parse_schedule_settings(load_toml(path, name="schedule"))


def test_repository_schedule_loads() -> None:
    from server.app.__main__ import DEFAULT_SCHEDULE  # noqa: PLC0415
    from server.app.settings import parse_schedule_settings  # noqa: PLC0415

    schedule = parse_schedule_settings(load_toml(DEFAULT_SCHEDULE, name="schedule"))
    assert schedule.intraday_every == timedelta(minutes=5)
    assert schedule.broker_window is not None


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("intraday_reconcile_minutes = 5", "intraday_reconcile_minutes = 6"),  # 不得调疏
        ("intraday_reconcile_minutes = 5", "intraday_reconcile_minutes = 0"),
        ("assess_seconds = 15", "assess_seconds = 0"),
        ("end_of_day_delay_minutes = 10", "end_of_day_delay_minutes = -1"),
        ('timezone = "America/New_York"', 'timezone = "Mars/Olympus"'),
        ('start = "23:45"', 'start = "24:00"'),
        ('start = "23:45"', 'start = "9:30"'),
        ("minutes = 15\n", "minutes = 121\n"),
        ("minutes = 15\n", "minutes = 15\nextra = 1\n"),
        ("[schedule]\n", "[schedule]\nunknown = 1\n"),
    ],
)
def test_bad_schedule_is_refused(tmp_path: Path, old: str, new: str) -> None:
    with pytest.raises(ConfigError):
        parse_schedule(tmp_path, SCHEDULE.replace(old, new))


def test_missing_schedule_makes_tradingd_refuse(tmp_path: Path) -> None:
    good = tmp_path / "processes.toml"
    good.write_text(GOOD, encoding="utf-8")
    missing = tmp_path / "nope.toml"
    assert main(["tradingd", "--config", str(good), "--schedule", str(missing)]) == EXIT_REFUSED


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("assess_seconds = 15", "assess_seconds = 16"),  # 评估不得调疏
        ("end_of_day_delay_minutes = 10", "end_of_day_delay_minutes = 61"),  # 日终不得拖过 1 小时
        ('timezone = "America/New_York"', 'timezone = "america/new_york"'),  # 大小写两系统一致
        ('timezone = "America/New_York"', 'timezone = "America"'),  # 目录名不是时区
    ],
)
def test_schedule_limits_and_strict_time_zones(tmp_path: Path, old: str, new: str) -> None:
    with pytest.raises(ConfigError):
        parse_schedule(tmp_path, SCHEDULE.replace(old, new))


def test_scheduler_and_core_share_one_calendar_and_window() -> None:
    """S-01d 评审 P1-4：调度器与交易核心的日历、券商窗口同源。"""
    from datetime import UTC, date, datetime  # noqa: PLC0415

    from server.app.__main__ import DEFAULT_SCHEDULE, production_time  # noqa: PLC0415
    from server.app.common import AdapterNotAvailableError  # noqa: PLC0415
    from server.app.settings import parse_schedule_settings  # noqa: PLC0415

    schedule = parse_schedule_settings(load_toml(DEFAULT_SCHEDULE, name="schedule"))
    time = production_time(schedule, date(2026, 10, 28))
    scheduler = time.scheduler(object())
    assert scheduler._calendar is time.calendar
    window = schedule.broker_window
    assert window is not None
    moment = datetime(2026, 10, 29, 3, 50, tzinfo=UTC)  # 23:50 ET：窗口内
    assert time.broker_window(moment) is scheduler.in_broker_window(moment) is True
    with pytest.raises(AdapterNotAvailableError, match="broker_window"):
        time.inputs()


# ---------- S-01f：临时休市 / 提前收市登记（只能更严） ----------


def _time(tmp_path: Path, text: str | None) -> object:
    from datetime import date  # noqa: PLC0415

    from server.app.__main__ import DEFAULT_SCHEDULE, production_time  # noqa: PLC0415
    from server.app.settings import parse_schedule_settings  # noqa: PLC0415

    schedule = parse_schedule_settings(load_toml(DEFAULT_SCHEDULE, name="schedule"))
    path = tmp_path / "calendar_overrides.toml"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    return production_time(schedule, date(2026, 10, 29), path)


def test_repository_overrides_file_loads_and_changes_nothing() -> None:
    from datetime import date  # noqa: PLC0415

    from server.app.__main__ import (  # noqa: PLC0415
        DEFAULT_CALENDAR_OVERRIDES,
        DEFAULT_SCHEDULE,
        production_time,
    )
    from server.app.settings import parse_schedule_settings  # noqa: PLC0415

    schedule = parse_schedule_settings(load_toml(DEFAULT_SCHEDULE, name="schedule"))
    time = production_time(schedule, date(2026, 10, 29), DEFAULT_CALENDAR_OVERRIDES)
    assert time.calendar.session(date(2026, 12, 24)) is not None  # 仓库里没有任何登记


def test_missing_overrides_file_means_no_overrides(tmp_path: Path) -> None:
    from datetime import date  # noqa: PLC0415

    time = _time(tmp_path, None)
    assert time.calendar.session(date(2026, 12, 24)) is not None  # type: ignore[attr-defined]


def test_closures_and_early_closes_are_applied_to_the_shared_calendar(tmp_path: Path) -> None:
    from datetime import UTC, date, datetime  # noqa: PLC0415

    time: Any = _time(
        tmp_path,
        "[calendar]\nadhoc_closures = [2026-12-24]\n\n"
        "[[calendar.adhoc_early_closes]]\ndate = 2026-12-23\nclose = 12:00:00\n",
    )
    assert time.calendar.session(date(2026, 12, 24)) is None  # 整天休市
    early = time.calendar.session(date(2026, 12, 23))
    assert early.close == datetime(2026, 12, 23, 17, 0, tzinfo=UTC) and early.early_close
    assert time.scheduler(object())._calendar is time.calendar  # 调度器与交易核心同源


CAL_ERR = "calendar"  # CalendarDataError（只能更严的构造校验）
CFG_ERR = "config"  # ConfigError（解析与格式）
EARLY = "[[calendar.adhoc_early_closes]]\ndate = {d}\nclose = {t}\n"
BOTH = "[calendar]\nadhoc_closures = [2026-12-24]\n\n" + EARLY.format(d="2026-12-24", t="12:00:00")


@pytest.mark.parametrize(
    ("text", "kind", "match"),
    [
        ("[calendar]\nadhoc_closures = [2026-12-25]\n", CAL_ERR, "本就不是交易日"),  # 圣诞
        ("[calendar]\nadhoc_closures = [2026-11-01]\n", CAL_ERR, "本就不是交易日"),  # 周日
        (EARLY.format(d="2026-12-23", t="17:00:00"), CAL_ERR, "只能提前"),  # 推迟收盘
        (EARLY.format(d="2026-12-23", t="16:00:00"), CAL_ERR, "只能提前"),  # 等于原收盘
        (EARLY.format(d="2026-12-23", t="09:00:00"), CAL_ERR, "晚于开盘"),  # 早于开盘
        (EARLY.format(d="2026-12-26", t="12:00:00"), CAL_ERR, "不是交易日"),  # 周六＝新增开市
        (EARLY.format(d="2026-11-27", t="13:30:00"), CAL_ERR, "只能提前"),  # 已提前的日子再推迟
        ("[calendar]\nadhoc_closures = [2026-12-24, 2026-12-24]\n", CFG_ERR, "重复"),
        ("[calendar]\nadhoc_closures = [2026-12-24T00:00:00]\n", CFG_ERR, "本地日期"),
        ("[calendar]\nadhoc_openings = [2026-12-25]\n", CFG_ERR, "未知字段"),  # 想新增开市日
        (BOTH, CAL_ERR, "同时登记"),
        ("[calendar]\nadhoc_closures = [2062-12-24]\n", CFG_ERR, "晚于覆盖范围"),  # 年份笔误
        ("not toml at all", CFG_ERR, "TOML"),
    ],
)
def test_loosening_or_malformed_overrides_are_refused(
    tmp_path: Path, text: str, kind: str, match: str
) -> None:
    from server.data.calendar import CalendarDataError  # noqa: PLC0415

    error: type[Exception] = CalendarDataError if kind == CAL_ERR else ConfigError
    with pytest.raises(error, match=match):
        _time(tmp_path, text)


def test_earlier_close_on_an_already_short_day_is_accepted(tmp_path: Path) -> None:
    from datetime import UTC, date, datetime  # noqa: PLC0415

    time: Any = _time(tmp_path, "[calendar]\n" + EARLY.format(d="2026-11-27", t="12:00:00"))
    close = time.calendar.session(date(2026, 11, 27)).close
    assert close == datetime(2026, 11, 27, 17, 0, tzinfo=UTC)


def test_early_close_during_daylight_saving_time(tmp_path: Path) -> None:
    """夏令时（EDT，UTC-4）期间的提前收市：13:00 ET = 17:00 UTC。"""
    from datetime import UTC, date, datetime  # noqa: PLC0415

    time: Any = _time(tmp_path, "[calendar]\n" + EARLY.format(d="2027-06-15", t="13:00:00"))
    close = time.calendar.session(date(2027, 6, 15)).close
    assert close == datetime(2027, 6, 15, 17, 0, tzinfo=UTC)


def test_stale_entries_before_the_coverage_are_ignored(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from datetime import date  # noqa: PLC0415

    caplog.set_level("WARNING")
    time: Any = _time(tmp_path, "[calendar]\nadhoc_closures = [2025-12-24, 2026-12-24]\n")
    assert time.calendar.session(date(2026, 12, 24)) is None  # 范围内的照常生效
    assert any("早于覆盖范围" in r.getMessage() for r in caplog.records)


def test_unreadable_overrides_are_refused(tmp_path: Path) -> None:
    from datetime import date  # noqa: PLC0415

    from server.app.__main__ import DEFAULT_SCHEDULE, production_time  # noqa: PLC0415
    from server.app.settings import parse_schedule_settings  # noqa: PLC0415

    schedule = parse_schedule_settings(load_toml(DEFAULT_SCHEDULE, name="schedule"))
    folder = tmp_path / "calendar_overrides.toml"
    folder.mkdir()  # 是目录而不是文件
    with pytest.raises(ConfigError):
        production_time(schedule, date(2026, 10, 29), folder)
    bom = tmp_path / "bom.toml"
    bom.write_bytes(b"\xef\xbb\xbf[calendar]\nadhoc_closures = []\n")
    with pytest.raises(ConfigError, match="BOM"):
        production_time(schedule, date(2026, 10, 29), bom)


def test_explicit_missing_path_is_refused_default_missing_is_not(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from datetime import date  # noqa: PLC0415

    from server.app.__main__ import DEFAULT_SCHEDULE, production_time  # noqa: PLC0415
    from server.app.settings import parse_schedule_settings  # noqa: PLC0415

    schedule = parse_schedule_settings(load_toml(DEFAULT_SCHEDULE, name="schedule"))
    missing = tmp_path / "typo.toml"
    with pytest.raises(ConfigError, match="不存在"):
        production_time(schedule, date(2026, 10, 29), missing, required=True)
    caplog.set_level("WARNING")
    production_time(schedule, date(2026, 10, 29), missing)
    assert any("没有临时日历登记文件" in r.getMessage() for r in caplog.records)


def test_children_receive_the_same_validated_paths(tmp_path: Path) -> None:
    """守护进程把校验过的 --schedule 与 --calendar-overrides 透传给子进程（绝对路径）。"""
    from server.app.__main__ import child_arguments  # noqa: PLC0415

    args = child_arguments(Path("c.toml"), Path("."), Path("s.toml"), tmp_path / "o.toml")
    pairs = dict(zip(args[::2], args[1::2], strict=True))
    assert set(pairs) == {"--config", "--root", "--schedule", "--calendar-overrides"}
    assert all(Path(v).is_absolute() for v in pairs.values())
    assert pairs["--calendar-overrides"] == str((tmp_path / "o.toml").resolve())


def test_bad_overrides_make_tradingd_refuse(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """登记有误：启动校验阶段就以 78 拒绝，日志写明是日历登记的问题（不是适配器缺失）。"""
    good = tmp_path / "processes.toml"
    good.write_text(GOOD, encoding="utf-8")
    bad = tmp_path / "calendar_overrides.toml"
    bad.write_text("[calendar]\nadhoc_closures = [2026-12-25]\n", encoding="utf-8")
    caplog.set_level("ERROR")
    assert main(["tradingd", "--config", str(good), "--calendar-overrides", str(bad)]) == (
        EXIT_REFUSED
    )
    assert any(
        "配置或平台不可用" in r.getMessage() and "不是交易日" in r.getMessage()
        for r in caplog.records
    )
