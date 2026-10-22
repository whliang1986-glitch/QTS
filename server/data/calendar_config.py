"""从受保护配置加载交易日历的临时覆盖（C-05 → D-03）。

    [calendar]
    adhoc_closures = [2026-12-24]          # TOML 本地日期

    [[calendar.adhoc_early_closes]]
    date = 2026-11-27
    close = 13:00:00                        # TOML 本地时间，按美东时间解释

类型必须严格：日期只能是本地日期（不能是日期时间），收市时间只能是不带时区的本地时间；
未知字段、重复日期一律拒绝（失败即关闭）。覆盖只能减少交易时间，由 TradingCalendar 构造时校验。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from server.data.calendar import TradingCalendar
from server.storage.config import ConfigError, LoadedConfig

NEW_YORK = ZoneInfo("America/New_York")
_TABLE_KEYS = frozenset({"adhoc_closures", "adhoc_early_closes"})
_EARLY_KEYS = frozenset({"date", "close"})


def _local_date(value: Any, where: str) -> date:
    if type(value) is not date:
        raise ConfigError(f"{where} 必须是 TOML 本地日期（如 2026-12-24）")
    return value


def parse_calendar_overrides(
    loaded: LoadedConfig,
) -> tuple[frozenset[date], dict[date, datetime]]:
    if loaded.name != "calendar":
        raise ConfigError(f"日历覆盖只能从名为 calendar 的配置加载（收到 {loaded.name}）")
    table: Mapping[str, Any] = loaded.only_table("calendar")
    unknown = set(table) - _TABLE_KEYS
    if unknown:
        raise ConfigError(f"[calendar] 有未知字段：{', '.join(sorted(unknown))}")
    raw_closures = table.get("adhoc_closures", ())
    if not isinstance(raw_closures, tuple):
        raise ConfigError("adhoc_closures 必须是日期数组")
    closures = [_local_date(v, "adhoc_closures") for v in raw_closures]
    if len(set(closures)) != len(closures):
        raise ConfigError("adhoc_closures 有重复日期")
    raw_early = table.get("adhoc_early_closes", ())
    if not isinstance(raw_early, tuple):
        raise ConfigError("adhoc_early_closes 必须是表数组")
    early: dict[date, datetime] = {}
    for i, entry in enumerate(raw_early):
        where = f"adhoc_early_closes[{i}]"
        if not isinstance(entry, Mapping) or set(entry) != _EARLY_KEYS:
            raise ConfigError(f"{where} 必须且只能有 date 与 close 两个字段")
        day = _local_date(entry["date"], f"{where}.date")
        close = entry["close"]
        if type(close) is not time or close.tzinfo is not None:
            raise ConfigError(
                f"{where}.close 必须是不带时区的本地时间（如 13:00:00），按美东时间解释"
            )
        if day in early:
            raise ConfigError(f"{where} 的日期重复：{day.isoformat()}")
        early[day] = datetime.combine(day, close, tzinfo=NEW_YORK).astimezone(UTC)
    return frozenset(closures), early


def nyse_from_config(loaded: LoadedConfig, *, start: date, end: date) -> TradingCalendar:
    closures, early = parse_calendar_overrides(loaded)
    return TradingCalendar.nyse(
        start=start, end=end, adhoc_closures=closures, adhoc_early_closes=early
    )
