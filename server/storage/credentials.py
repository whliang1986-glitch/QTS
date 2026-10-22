"""凭据存储（C-05，V4.1 第 4 章：操作系统凭据存储 keyring）。

- 凭据只从系统凭据存储读取，不写入代码、配置、日志、提示词或提交信息（CLAUDE.md 红线 7）。
- 读到的值包装为 `SensitiveValue`：str、repr、format 一律显示为 ***，不能被序列化；
  只有显式调用 `reveal()` 才拿到明文，调用点应紧挨着真正使用它的地方（如券商登录）。
- 凭据存储不可用或缺少凭据时抛 CredentialUnavailableError，调用方必须拒绝启动（失败即关闭）。
- 本模块不提供写入接口：凭据由 Dylan 在自己的终端用系统工具录入
  （docs/runbooks/protected-config-change.md）。
"""

from __future__ import annotations

import hmac
import re
from typing import Protocol

import keyring

MASK = "***"
DEFAULT_SERVICE = "qts"

# ---------- 密钥类字段名判定（配置拦截 C-05 与日志脱敏 C-06 共用） ----------

# 单独成词即视为密钥
_SENSITIVE_WORDS = frozenset(
    {
        "password", "passwd", "passphrase", "pwd", "pass", "pw", "pin", "secret", "token",
        "credential", "auth", "cookie", "otp", "totp", "mnemonic", "apikey", "privatekey",
    }
)  # fmt: skip
# 出现在全小写连写名里也算（dbpassword、clientsecret）；token 不在其中，避免 tokenizer 误报
_SENSITIVE_FRAGMENTS = ("password", "passwd", "passphrase", "secret", "credential", "apikey",
                        "privatekey", "mnemonic", "authtoken", "accesstoken", "apitoken",
                        "seedphrase")  # fmt: skip
# 中文键名（TOML 引号键、日志字段）
_SENSITIVE_CJK = ("密码", "口令", "密钥", "私钥", "令牌", "凭据", "助记词")
# seed 只在与这些词连用时算密钥（random_seed 是回测参数，不是密钥）
_SEED_QUALIFIERS = frozenset({"wallet", "phrase", "mnemonic", "recovery"})
# 与 key 连用即视为密钥：api_key、signing_key、session-key
_KEY_QUALIFIERS = frozenset(
    {"api", "private", "signing", "encryption", "hmac", "session", "access", "secret", "master"}
)


def _words(name: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
    return [w.lower() for w in re.findall(r"[A-Za-z]+|[0-9]+", spaced)]


def is_credential_key(name: str) -> bool:
    """字段名看起来是密钥：按驼峰、分隔符、字母与数字边界拆词后判定。宁可误报（失败即关闭）。

    凭据条目名请用 `*_entry` / `*_ref` 之类不含上述词的命名（如 tws_login_entry）。
    """
    words = _words(name)
    singular = set(words) | {w[:-1] for w in words if w.endswith("s")}  # 兼顾复数形式
    if singular & _SENSITIVE_WORDS:
        return True
    if "key" in singular and singular & _KEY_QUALIFIERS:
        return True
    if "seed" in singular and singular & _SEED_QUALIFIERS:
        return True
    if any(word in name for word in _SENSITIVE_CJK):
        return True
    joined = "".join(words)
    return any(fragment in joined for fragment in _SENSITIVE_FRAGMENTS)


class CredentialUnavailableError(Exception):
    """凭据存储不可用或缺少所需凭据。"""


class SensitiveValue:
    """敏感值包装：任何文本化都只显示 ***。"""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not value:
            raise ValueError("敏感值不能为空")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __str__(self) -> str:
        return MASK

    def __repr__(self) -> str:
        return f"SensitiveValue({MASK!r})"

    def __format__(self, spec: str) -> str:
        return MASK

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SensitiveValue):
            return NotImplemented
        return hmac.compare_digest(self._value.encode(), other._value.encode())

    def __hash__(self) -> int:
        raise TypeError("SensitiveValue 不可哈希")

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError("SensitiveValue 不可序列化")


class CredentialStore(Protocol):
    def get(self, name: str) -> SensitiveValue:
        """取凭据；缺失或存储不可用抛 CredentialUnavailableError。"""
        ...


class KeyringCredentialStore:
    """系统凭据存储（Windows 凭据管理器、macOS 钥匙串、Linux Secret Service）。"""

    def __init__(self, service: str = DEFAULT_SERVICE) -> None:
        self._service = service

    def get(self, name: str) -> SensitiveValue:
        try:
            value = keyring.get_password(self._service, name)
        except Exception as exc:
            raise CredentialUnavailableError(f"系统凭据存储不可用：{type(exc).__name__}") from None
        if not value:
            raise CredentialUnavailableError(f"系统凭据存储中缺少凭据 {self._service}/{name}")
        return SensitiveValue(value)


class MemoryCredentialStore:
    """测试用的内存凭据存储。"""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = dict(values or {})

    def get(self, name: str) -> SensitiveValue:
        value = self._values.get(name)
        if not value:
            raise CredentialUnavailableError(f"缺少凭据 {name}")
        return SensitiveValue(value)
