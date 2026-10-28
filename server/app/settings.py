"""读 config/runtime/processes.toml（S-01c）：严格校验，未知键、类型不对、非正数一律拒绝。

安全相关的两个时长只能调严（S-01c 评审 P1-4）：
- guardd 心跳超时不超过 V4.1 附录的 90 秒，且至少是心跳间隔的 3 倍、大于检查间隔；
- workerd 意图限龄不超过 5 分钟（与 tradingd 入口的限龄一致）。
要放宽这两个上限属于风控限值，须 Dylan 本人决定（改代码里的常量并评审）。
路径只能写相对于仓库根目录的相对路径，分隔符写 /，不得含 ..：两个系统上含义相同。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from server.ipc.protocol import Role
from server.storage.config import ConfigError, LoadedConfig

SELF = "self"
_PATH_KEYS = ("runtime_dir", "ledger", "meta")
_SECONDS_KEYS = (
    "heartbeat_seconds",
    "guard_check_seconds",
    "guard_timeout_seconds",
    "workerd_max_age_seconds",
)
_ACCOUNT_KEYS = ("tradingd", "apid", "workerd")
MAX_GUARD_TIMEOUT = timedelta(seconds=90)  # V4.1 附录：tradingd 心跳超时（guardd）
MAX_INTENT_AGE = timedelta(minutes=5)
MIN_BEATS_PER_TIMEOUT = 3


@dataclass(frozen=True)
class ProcessSettings:
    runtime_dir: Path
    ledger: Path
    meta: Path
    instance: str
    heartbeat_seconds: int
    guard_check_seconds: int
    guard_timeout: timedelta
    workerd_max_age: timedelta
    accounts: Mapping[str, str]  # tradingd / apid / workerd → 系统账户（已把 self 换成实际账户）

    def allowed(self) -> dict[Role, frozenset[str]]:
        return {
            Role.WORKERD: frozenset({self.accounts["workerd"]}),
            Role.APID: frozenset({self.accounts["apid"]}),
        }


def parse_process_settings(loaded: LoadedConfig, *, base: Path, me: str) -> ProcessSettings:
    table = loaded.only_table("processes")
    accounts = table.get("accounts")
    expected = {*_PATH_KEYS, *_SECONDS_KEYS, "instance", "accounts"}
    if set(table) != expected:
        raise ConfigError(f"[processes] 的键必须恰好是：{sorted(expected)}")
    if not isinstance(accounts, Mapping) or set(accounts) != set(_ACCOUNT_KEYS):
        raise ConfigError(f"[processes.accounts] 的键必须恰好是：{list(_ACCOUNT_KEYS)}")
    paths = {k: _path(table[k], k, base) for k in _PATH_KEYS}
    seconds = {k: _positive_int(table[k], k) for k in _SECONDS_KEYS}
    instance = table["instance"]
    if not isinstance(instance, str) or not instance:
        raise ConfigError("instance 必须是非空字符串")
    resolved = {k: _account(accounts[k], k, me) for k in _ACCOUNT_KEYS}
    _check_safety(seconds)
    return ProcessSettings(
        runtime_dir=paths["runtime_dir"], ledger=paths["ledger"], meta=paths["meta"],
        instance=instance, heartbeat_seconds=seconds["heartbeat_seconds"],
        guard_check_seconds=seconds["guard_check_seconds"],
        guard_timeout=timedelta(seconds=seconds["guard_timeout_seconds"]),
        workerd_max_age=timedelta(seconds=seconds["workerd_max_age_seconds"]),
        accounts=resolved,
    )  # fmt: skip


def _check_safety(seconds: Mapping[str, int]) -> None:
    timeout = seconds["guard_timeout_seconds"]
    if timeout > MAX_GUARD_TIMEOUT.total_seconds():
        raise ConfigError(f"guard_timeout_seconds 不得超过 {MAX_GUARD_TIMEOUT.seconds} 秒")
    if timeout < MIN_BEATS_PER_TIMEOUT * seconds["heartbeat_seconds"]:
        raise ConfigError("guard_timeout_seconds 至少要是 heartbeat_seconds 的 3 倍")
    if seconds["guard_check_seconds"] >= timeout:
        raise ConfigError("guard_check_seconds 必须小于 guard_timeout_seconds")
    if seconds["workerd_max_age_seconds"] > MAX_INTENT_AGE.total_seconds():
        raise ConfigError(f"workerd_max_age_seconds 不得超过 {MAX_INTENT_AGE.seconds} 秒")


def _path(value: Any, key: str, base: Path) -> Path:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ConfigError(f"{key} 必须是非空的相对路径，分隔符写 /（两个系统通用）")
    parts = value.split("/")
    if value.startswith("/") or ".." in parts or "" in parts:
        raise ConfigError(f"{key} 只能是仓库根目录下的相对路径，不得以 / 开头或含 ..")
    return base.joinpath(*parts)


def _positive_int(value: Any, key: str) -> int:
    if type(value) is not int or value <= 0:
        raise ConfigError(f"{key} 必须是正整数")
    return value


def _account(value: Any, key: str, me: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"accounts.{key} 必须是非空字符串")
    return me if value == SELF else value


__all__ = (
    "MAX_GUARD_TIMEOUT",
    "MAX_INTENT_AGE",
    "SELF",
    "ProcessSettings",
    "parse_process_settings",
)
