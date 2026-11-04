"""外部心跳（S-07，V4.1 R14：服务器断电、断网时由外部服务报警）。

服务器定时访问一个“死人开关”地址（例如 healthchecks.io 的 ping 地址）。地址由普通配置给出；
如果地址本身带私密路径，按凭据处理（放凭据存储），这里只接收已经取好的值。
外部服务在约定时间内收不到 ping 就向 Dylan 的手机报警：这条通路不经过本机，服务器整机失联也能报警。
ping 失败只记日志，不影响交易。只允许 https。
"""

from __future__ import annotations

import logging
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from server.storage.logs import log_event

log = logging.getLogger(__name__)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_a: object, **_kw: object) -> None:
        return None  # 不跟随重定向：防止被引到 http 或别的主机


def _urllib_get(url: str, timeout: float) -> int:
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(url, timeout=timeout) as response:  # 构造时已限定 https
        return int(response.status)


class HeartbeatPinger:
    def __init__(
        self,
        url: str,
        *,
        every: timedelta = timedelta(minutes=1),
        get: Callable[[str, float], int] = _urllib_get,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        timeout: float = 10.0,
    ) -> None:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("外部心跳地址必须是 https")
        if every <= timedelta(0):
            raise ValueError("心跳间隔必须为正")
        self._url = url
        self._host = parts.hostname
        self._every = every
        self._get = get
        self._clock = clock
        self._timeout = timeout
        self._next: datetime | None = None
        self.last_ok: datetime | None = None
        self.failures = 0

    def tick(self) -> None:
        """到点就 ping 一次。地址（可能含私密路径）不进日志，只记主机名。"""
        now = self._clock()
        if self._next is not None and now < self._next:
            return
        self._next = now + self._every
        try:
            status = self._get(self._url, self._timeout)
        except Exception as exc:
            self.failures += 1
            log_event(log, logging.WARNING, "外部心跳发送失败", host=self._host,
                      error=type(exc).__name__, failures=self.failures)  # fmt: skip
            return
        if 200 <= status < 300:
            self.last_ok = now
            self.failures = 0
            return
        self.failures += 1
        log_event(log, logging.WARNING, "外部心跳被拒绝", host=self._host, status=status)


__all__ = ("HeartbeatPinger",)
