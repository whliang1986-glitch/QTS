"""apid 的安全组件（S-03）：令牌认证、认证失败限流、高危命令二次确认、审计接口。

- 令牌认证：查 api.db（只存哈希），无效、过期、吊销都返回 None。
- 限流：同一来源在 window 内失败 max_failures 次，之后 block 时长内一律拒绝（429），
  成功的请求不清零（防止撞库时夹带一个有效令牌刷新计数）。
- 二次确认（V4.1 10.1）：高危命令先申请确认号，确认号绑定“使用者 + 命令名 + 参数”，
  有效期短；第一次使用时绑定 request_id，同一 request_id 的重试可复用，换号不能用。
  只在 apid 内存里（重启即失效：失败即关闭）。tradingd 的确认短语另行逐字核对。
"""

from __future__ import annotations

import json
import secrets
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from server.api.auth import Principal, Role
from server.storage.apidb import ApiDb

MAX_TRACKED_SOURCES = 10_000


class Audit(Protocol):
    """审计写入；写不进去必须抛出（命令据此不转发）。"""

    def audit(self, action: str, actor: str, detail: Mapping[str, str]) -> None: ...


class MemoryAudit:
    """测试与生成文档用：记在内存里。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, str]]] = []

    def audit(self, action: str, actor: str, detail: Mapping[str, str]) -> None:
        self.records.append((action, actor, dict(detail)))


class TokenAdmin(Protocol):
    """在线吊销（apid 是 api.db 的单写者，紧急吊销不必先停 apid）。"""

    def revoke(self, token_id: str, *, actor: str, reason: str) -> None: ...


class TokenAuthenticator:
    """api.db 令牌 → 使用者。"""

    def __init__(self, db: ApiDb) -> None:
        self._db = db

    def authenticate(self, token: str) -> Principal | None:
        identity = self._db.verify(token)
        if identity is None:
            return None
        return Principal(identity.name, Role(identity.role))


class FailureLimiter:
    def __init__(
        self,
        *,
        max_failures: int = 20,
        window: timedelta = timedelta(seconds=60),
        block: timedelta = timedelta(seconds=60),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._max = max_failures
        self._window = window
        self._block = block
        self._clock = clock
        self._failures: dict[str, deque[datetime]] = {}
        self._blocked_until: dict[str, datetime] = {}
        self._lock = threading.Lock()

    def blocked(self, source: str) -> bool:
        with self._lock:
            until = self._blocked_until.get(source)
            if until is None:
                return False
            if until <= self._clock():
                del self._blocked_until[source]
                return False
            return True

    def fail(self, source: str) -> bool:
        """记一次失败；返回这次是否触发了封禁。"""
        now = self._clock()
        with self._lock:
            if len(self._failures) >= MAX_TRACKED_SOURCES:
                self._forget_stale(now)
            times = self._failures.setdefault(source, deque())
            times.append(now)
            while times and times[0] <= now - self._window:
                times.popleft()
            if len(times) >= self._max:
                self._blocked_until[source] = now + self._block
                times.clear()
                return True
            if not times:
                del self._failures[source]
            return False

    def _forget_stale(self, now: datetime) -> None:
        """来源太多时清掉窗口外的记录；仍然太多就整体清空（宁可漏计，不让内存无限增长）。"""
        for key in [k for k, v in self._failures.items() if not v or v[-1] <= now - self._window]:
            del self._failures[key]
        if len(self._failures) >= MAX_TRACKED_SOURCES:
            self._failures.clear()


class ConfirmationError(Exception):
    """二次确认无效：不转发命令。"""


@dataclass
class _Confirmation:
    who: str
    fingerprint: str
    expires_at: datetime
    request_id: str | None = None


def fingerprint(name: str, params: Mapping[str, str]) -> str:
    return json.dumps({"name": name, "params": dict(params)}, sort_keys=True, ensure_ascii=False)


class ConfirmationBook:
    def __init__(
        self,
        *,
        ttl: timedelta = timedelta(seconds=60),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_open: int = 100,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("确认有效期必须为正")
        self._ttl = ttl
        self._clock = clock
        self._max_open = max_open
        self._items: dict[str, _Confirmation] = {}
        self._lock = threading.Lock()

    def issue(self, who: Principal, name: str, params: Mapping[str, str]) -> tuple[str, datetime]:
        now = self._clock()
        with self._lock:
            self._expire(now)
            if len(self._items) >= self._max_open:
                raise ConfirmationError("未使用的确认太多，请稍后再试")
            confirmation_id = secrets.token_urlsafe(16)
            expires = now + self._ttl
            self._items[confirmation_id] = _Confirmation(
                who.name, fingerprint(name, params), expires
            )
        return confirmation_id, expires

    def use(
        self,
        confirmation_id: str,
        who: Principal,
        name: str,
        params: Mapping[str, str],
        request_id: str,
    ) -> None:
        now = self._clock()
        with self._lock:
            item = self._items.get(confirmation_id)
            if item is None or item.expires_at <= now:
                raise ConfirmationError("二次确认不存在或已过期，请重新申请")
            if item.who != who.name or item.fingerprint != fingerprint(name, params):
                raise ConfirmationError("二次确认与这条命令（使用者、命令名或参数）不符")
            if item.request_id is None:
                item.request_id = request_id
            elif item.request_id != request_id:
                raise ConfirmationError("二次确认已被另一个 request_id 使用")

    def _expire(self, now: datetime) -> None:
        for key in [k for k, v in self._items.items() if v.expires_at <= now]:
            del self._items[key]


__all__ = (
    "Audit",
    "ConfirmationBook",
    "ConfirmationError",
    "FailureLimiter",
    "MemoryAudit",
    "TokenAdmin",
    "TokenAuthenticator",
    "fingerprint",
)
