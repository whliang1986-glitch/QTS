"""认证接口（S-02 定义，S-03 实现令牌签发、吊销与 TLS）。

- 每个路由（含 WebSocket）都要认证，没有匿名入口；令牌只从 Authorization: Bearer 头读，
  不从查询参数读（查询参数会进访问日志）。
- 角色：owner（Dylan，可发命令）、viewer（只读，V4.1 10.2）。
- 操作者身份只来自认证结果，不接受请求体自报。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

# 与 domain.values.Identifier 一致：操作者会写进审计事件
_NAME = re.compile(r"^\S{1,128}$")


class Role(StrEnum):
    OWNER = "owner"
    VIEWER = "viewer"


@dataclass(frozen=True)
class Principal:
    """认证后的使用者。name 即命令的操作者（写入审计）。"""

    name: str
    role: Role

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME.fullmatch(self.name):
            raise ValueError("使用者名称必须是 1 到 128 个非空白字符")
        if not isinstance(self.role, Role):
            raise ValueError(f"未知角色：{self.role!r}")


class Authenticator(Protocol):
    """令牌 → 使用者；无效、过期、已吊销返回 None。实现不得抛出令牌内容（S-03）。"""

    def authenticate(self, token: str) -> Principal | None: ...


def bearer_token(header: str | None) -> str | None:
    """从 Authorization 头取 Bearer 令牌；格式不对返回 None。"""
    if header is None:
        return None
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or " " in token:
        return None
    return token


__all__ = ("Authenticator", "Principal", "Role", "bearer_token")
