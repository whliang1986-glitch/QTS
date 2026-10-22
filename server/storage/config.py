"""配置加载与版本留痕（C-05，ADR：TOML + 标准库 tomllib）。

- `load_toml`：读取一个 TOML 配置文件，失败即关闭（文件缺失、编码错误、语法错误都抛 ConfigError），
  内容深度冻结，版本为文件字节的 SHA-256。本模块只读配置，从不写配置文件。
- 配置里不得出现密钥类字段（password、token、api_key 等）：
  密钥只能放系统凭据存储（credentials.py）。
- `ConfigHistory`：每次加载到与上次不同的版本时，向账本追加一条审计事件（只追加、不删改），
  回滚到旧版本也会留下新的一条，形成完整的变更轨迹。
- 受保护配置（config/risk、config/live）的路径与内容由 Dylan 维护；这里只按调用方给的路径读取。
"""

from __future__ import annotations

import codecs
import hashlib
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

from server.domain.modes import AuditEvent
from server.storage.credentials import is_credential_key
from server.storage.ledger import LedgerRepository

CONFIG_ACTION = "config_version"


class ConfigError(Exception):
    """配置不可用：缺失、无法解析、含密钥字段或内容不合法。调用方必须拒绝启动或拒绝新增风险。"""


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _reject_credentials(value: Any, path: str = "") -> None:
    """递归检查任意深度的表与数组。"""
    if isinstance(value, Decimal) and not value.is_finite():
        raise ConfigError(f"配置字段 {path} 不得为 nan 或 inf")
    if isinstance(value, Mapping):
        for key, item in value.items():
            where = f"{path}.{key}" if path else str(key)
            if not str(key).isascii():
                raise ConfigError(f"配置字段 {where} 含非 ASCII 字符：键名只能用英文与数字")
            if is_credential_key(str(key)):
                raise ConfigError(
                    f"配置字段 {where} 看起来是密钥：密钥只能放系统凭据存储，不得写入配置文件"
                )
            _reject_credentials(item, where)
    elif isinstance(value, tuple | list):
        for i, item in enumerate(value):
            _reject_credentials(item, f"{path}[{i}]")


@dataclass(frozen=True)
class LoadedConfig:
    name: str  # 配置名（如 mode、calendar），用于版本轨迹
    path: str
    sha256: str
    content: Mapping[str, Any]  # 深度冻结：表为只读映射，数组为元组

    @property
    def version(self) -> str:
        """写入 ModeConfig.config_version 等处的版本标识。"""
        return f"{self.name}@{self.sha256[:12]}"

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ConfigError("sha256 必须是 64 位小写十六进制")

    def only_table(self, key: str) -> Mapping[str, Any]:
        """文件只能有这一张顶层表（拼错的表名不会被静默忽略）。"""
        extra = set(self.content) - {key}
        if extra:
            raise ConfigError(f"{self.path} 只能有 [{key}] 表，多出：{', '.join(sorted(extra))}")
        return self.table(key)

    def table(self, key: str) -> Mapping[str, Any]:
        value = self.content.get(key)
        if not isinstance(value, Mapping):
            raise ConfigError(f"{self.path} 缺少 [{key}] 表")
        return value


def load_toml(path: Path, *, name: str) -> LoadedConfig:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
        raise ConfigError(f"配置名不合法：{name!r}")
    try:
        raw = path.read_bytes()
        if raw.startswith(codecs.BOM_UTF8):
            raise ConfigError(f"配置文件 {path} 带 UTF-8 BOM：请另存为不带 BOM 的 UTF-8")
        # 浮点按 Decimal 解析：金额、比例、限额禁止 float（CLAUDE.md 交易核心不变量）
        content = tomllib.loads(raw.decode("utf-8"), parse_float=Decimal)
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在：{path}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"无法读取配置文件 {path}：{exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"配置文件 {path} 不是合法的 TOML：{exc}") from exc
    _reject_credentials(content)
    return LoadedConfig(
        name=name,
        path=str(path),
        sha256=hashlib.sha256(raw).hexdigest(),
        content=_freeze(content),
    )


@dataclass(frozen=True)
class ConfigVersion:
    name: str
    sha256: str
    previous: str | None
    path: str
    recorded_at: datetime
    actor: str


class ConfigHistory:
    """配置版本轨迹：账本审计事件（action = config_version），只追加。"""

    def __init__(self, repo: LedgerRepository) -> None:
        self._repo = repo

    def versions(self, name: str) -> list[ConfigVersion]:
        """某个配置的版本轨迹，从新到旧。"""
        out: list[ConfigVersion] = []
        for event in self._repo.audits(CONFIG_ACTION):
            detail = dict(event.detail)
            if detail.get("name") != name:
                continue
            previous = detail.get("previous")
            out.append(
                ConfigVersion(
                    name=name,
                    sha256=detail["sha256"],
                    previous=None if previous == "none" else previous,
                    path=detail["path"],
                    recorded_at=event.occurred_at,
                    actor=event.actor,
                )
            )
        return out

    def record(
        self, loaded: LoadedConfig, *, actor: str, correlation_id: str, at: datetime
    ) -> bool:
        """与上次记录的版本不同则追加一条并返回 True；相同（重复加载）返回 False。

        只应由账本的单写者（tradingd）在启动时调用。先读后写不是原子的：两个进程同时写入不同版本时，
        后写者在账本唯一约束上抛 ConflictError（失败即关闭，不会覆盖或丢失轨迹），调用方应拒绝启动。
        """
        history = self.versions(loaded.name)
        if history and history[0].sha256 == loaded.sha256:
            return False
        previous = history[0].sha256 if history else "none"
        event = AuditEvent(
            event_id=f"config-{loaded.name}-{len(history) + 1}",
            occurred_at=at,
            actor=actor,
            action=CONFIG_ACTION,
            correlation_id=correlation_id,
            detail=(
                ("name", loaded.name),
                ("sha256", loaded.sha256),
                ("previous", previous),
                ("path", loaded.path),
            ),
        )
        return self._repo.append_audit(event)
