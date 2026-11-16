"""从受保护配置解析 ModeConfig（C-05 → C-04）。

配置文件（由 Dylan 维护）里的 [mode] 表：

    [mode]
    requested_mode = "PAPER"
    account_id = "DU..."
    expected_account_type = "CASH"
    risk_regime = "CASH"
    allow_delayed_data = false

config_version 不写在文件里，由加载器按文件内容哈希生成，保证与审计轨迹一致。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import timedelta

from pydantic import ValidationError

from server.risk.mode_manager import ModeConfig
from server.risk.run_state import Component, Kind
from server.storage.config import ConfigError, LoadedConfig


def parse_mode_config(loaded: LoadedConfig) -> ModeConfig:
    if loaded.name != "mode":
        raise ConfigError(f"ModeConfig 只能从名为 mode 的配置加载（收到 {loaded.name}）")
    table = dict(loaded.only_table("mode"))
    if "config_version" in table:
        raise ConfigError("config_version 由加载器按内容哈希生成，不得写在配置文件里")
    try:
        return ModeConfig.model_validate({**table, "config_version": loaded.version})
    except ValidationError as exc:
        # 只报字段与错误类型，不回显取值（账户号等不进日志）
        fields = sorted({".".join(str(p) for p in e["loc"]) or "<mode>" for e in exc.errors()})
        raise ConfigError(f"{loaded.path} 的 [mode] 不合法：{', '.join(fields)}") from None


_KINDS = {"DATA", "ACCOUNT", "OTHER"}
_COMPONENT_KEYS = {"kind", "critical", "stale_after_seconds"}
# 这些组件必须登记、为关键且类别固定：配置文件不是受保护路径，不能靠改配置或类别关掉门禁
REQUIRED_CRITICAL_KINDS: Mapping[str, Kind] = {
    "broker": Kind.ACCOUNT,
    "account_sync": Kind.ACCOUNT,
    "quotes": Kind.DATA,
}
REQUIRED_CRITICAL = frozenset(REQUIRED_CRITICAL_KINDS)


def require_critical_components(components: Collection[Component]) -> None:
    """校验交易核心的关键组件清单（B-05x）：缺关键组件、降为非关键、类别被篡改或时效非法一律拒绝。"""
    names = [c.name for c in components]
    if len(names) != len(set(names)):
        raise ValueError("组件名重复")
    for c in components:
        if not isinstance(c.kind, Kind) or not isinstance(c.critical, bool):
            raise ValueError(f"组件 {c.name} 类别或 critical 不合法")
        if not isinstance(c.stale_after, timedelta) or c.stale_after <= timedelta(0):
            raise ValueError(f"组件 {c.name} 的 stale_after 必须大于 0")
    by_name = {c.name: c for c in components}
    missing = sorted(
        name for name in REQUIRED_CRITICAL if name not in by_name or not by_name[name].critical
    )
    if missing:
        raise ValueError(f"组件清单必须把这些组件登记为关键：{'、'.join(missing)}")
    wrong_kind = sorted(
        name
        for name, expected in REQUIRED_CRITICAL_KINDS.items()
        if by_name[name].kind is not expected
    )
    if wrong_kind:
        raise ValueError(f"关键组件类别不合法：{'、'.join(wrong_kind)}")


def parse_health_config(loaded: LoadedConfig) -> tuple[Component, ...]:
    """运行状态机的组件登记（R-08 / S-01a，普通运行配置，不是风控参数）。

        [components.broker]
        kind = "ACCOUNT"          # DATA / ACCOUNT / OTHER
        critical = true           # 关键组件故障即 SAFE HALT
        stale_after_seconds = 30  # 超过这么久没有报告即视为故障

    缺键、多余键、类别不认识、时效不是正整数一律拒绝加载（失败即关闭：tradingd 不启动）。
    """
    if loaded.name != "health":
        raise ConfigError(f"组件登记只能从名为 health 的配置加载（收到 {loaded.name}）")
    table = loaded.only_table("components")
    if not table:
        raise ConfigError(f"{loaded.path} 没有登记任何组件")
    components = []
    for name, spec in table.items():
        if not isinstance(spec, Mapping) or set(spec) != _COMPONENT_KEYS:
            raise ConfigError(f"{loaded.path} 的组件 {name} 必须恰好有 {sorted(_COMPONENT_KEYS)}")
        kind, critical, seconds = spec["kind"], spec["critical"], spec["stale_after_seconds"]
        if not isinstance(kind, str) or kind not in _KINDS or not isinstance(critical, bool):
            raise ConfigError(f"{loaded.path} 的组件 {name} 类别或 critical 不合法")
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
            raise ConfigError(f"{loaded.path} 的组件 {name} 的 stale_after_seconds 必须是正整数")
        components.append(Component(str(name), Kind(kind), critical, timedelta(seconds=seconds)))
    try:
        require_critical_components(components)
    except ValueError as exc:
        raise ConfigError(f"{loaded.path} {exc}") from None
    return tuple(components)
