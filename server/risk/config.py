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

from pydantic import ValidationError

from server.risk.mode_manager import ModeConfig
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
