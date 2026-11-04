"""Telegram 告警渠道（S-07，Dylan 2026-11-04 选定）。

- 机器人令牌从系统凭据存储读取（C-05，`SensitiveValue`），每次发送时才 reveal，不缓存明文；
  令牌出现在请求 URL 里（Telegram Bot API 的规定），所以：
  - URL 不进日志、不进异常信息：失败只抛 ChannelError(类型名)；
  - 只连 api.telegram.org（HTTPS），不跟随重定向到别处。
- chat_id 不是密钥，写在普通配置里。
- 真正联网发送要等 Dylan 提供机器人（F-05 / G4 演练）；开发期测试用注入的 http 函数。
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable

from server.storage.credentials import CredentialStore

API_HOST = "api.telegram.org"
TOKEN_ENTRY = "telegram_bot_entry"  # noqa: S105 - 凭据条目名，不是密钥
MAX_TEXT = 4000  # Telegram 单条上限 4096，留余量


class ChannelError(Exception):
    """推送失败（不带 URL、令牌或响应原文）。"""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_a: object, **_kw: object) -> None:
        return None


def _urllib_post(url: str, body: bytes, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(  # noqa: S310 - 地址固定为 https://api.telegram.org
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(request, timeout=timeout) as response:
        return int(response.status), response.read(65536)


class TelegramChannel:
    name = "telegram"

    def __init__(
        self,
        credentials: CredentialStore,
        chat_id: str,
        *,
        post: Callable[[str, bytes, float], tuple[int, bytes]] = _urllib_post,
        timeout: float = 10.0,
    ) -> None:
        if not chat_id.strip() or not chat_id.lstrip("-").isdigit():
            raise ValueError("chat_id 必须是数字（群组为负数）")
        self._credentials = credentials
        self._chat_id = chat_id
        self._post = post
        self._timeout = timeout

    def send(self, text: str) -> None:
        body = json.dumps(
            {"chat_id": self._chat_id, "text": text[:MAX_TEXT], "disable_web_page_preview": True},
            ensure_ascii=False,
        ).encode("utf-8")
        status, raw, error = self._request(body)
        if error is not None:  # 在 except 块之外抛出：异常链里不挂原异常（原异常的 URL 含令牌）
            raise ChannelError(f"Telegram 请求失败：{error}")
        if status != 200:
            raise ChannelError(f"Telegram 返回 HTTP {status}")
        try:
            ok = json.loads(raw.decode("utf-8")).get("ok") is True
        except (ValueError, AttributeError):
            ok = False
        if not ok:
            raise ChannelError("Telegram 回答不是 ok")

    def _request(self, body: bytes) -> tuple[int, bytes, str | None]:
        """拼 URL 与请求放在一个短函数里：明文令牌只在这一帧里短暂存在，出错只返回类型名。"""
        token = self._credentials.get(TOKEN_ENTRY)  # 缺凭据抛 CredentialUnavailableError
        try:
            status, raw = self._post(
                f"https://{API_HOST}/bot{token.reveal()}/sendMessage", body, self._timeout
            )
        except Exception as exc:
            return 0, b"", type(exc).__name__
        return status, raw, None


__all__ = ("API_HOST", "TOKEN_ENTRY", "ChannelError", "TelegramChannel")
