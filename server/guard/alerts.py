"""告警分发（S-07，V4.1 10.4 / R14）：统一入口、按级别分发、P0 重复提醒直到人工确认。

- 两条通路：所有告警都写结构化日志（面板与日志，第一条）；P0、P1 另经手机渠道推送（第二条）。
- 生命周期：发生（raise_alert）→ 人工确认（acknowledge，只停止重复提醒，不解除风险，V4.1 10.3）
  → 解除（resolve，由检测方在风险确实消失时调用，例如 tradingd 心跳恢复）。
  同一 key 在解除之前只算一条：检测方每轮重复上报只更新时间，不会重新推送，也不刷日志。
- 推送与登记分开：raise_alert 只登记（不做网络 I/O，事件循环里可以直接调用）；
  pump 负责发送，调用方应放在线程里（asyncio.to_thread），慢网络不拖住看门狗。
  状态由锁保护；发送本身在锁外，单次 pump 最多发 max_per_pump 条。
- 发送失败按退避重试；全局限速防止告警风暴打爆渠道（顺延，不丢，P0 优先）。
- 渠道不可用（没有配置、缺凭据）：告警照常写日志，stats.channel_ok=False 供面板与健康检查显示。
- 消息是“代码 + 参数”，按 AlertHub 的语言渲染（S-12：zh / en；不认识的语言用中文）。
- 持久化（S-05a）：给了 store 就把发生、确认、解除写进 alerts.db（网页据此显示），
  启动时恢复未解除的告警（重启不会忘掉没确认的 P0）。store 写不进去或读不出：
  报一条 P1 alert_store_failed，之后只在内存里继续（推送与重复提醒照常，不丢告警）。
- 网页确认（S-05a）：acknowledge 带 alert_id 时只确认那一次发生；同 key 的新发生不受旧请求影响。
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from typing import Protocol, TypeVar

from server.storage.alertsdb import AlertStore
from server.storage.logs import log_event

log = logging.getLogger(__name__)
_T = TypeVar("_T")
STORE_FAILED = "alert_store_failed"  # 告警库失效告警的 key 与代码


class Level(IntEnum):
    P0 = 0  # 资金安全：重复提醒直到确认
    P1 = 1  # 可靠性：推送一次
    P2 = 2  # 只进面板与日志
    P3 = 3


PUSHED = frozenset({Level.P0, Level.P1})
_LOG_LEVEL = {Level.P0: logging.CRITICAL, Level.P1: logging.ERROR,
              Level.P2: logging.WARNING, Level.P3: logging.INFO}  # fmt: skip

# 告警代码 → 各语言模板（S-12）。两种语言的代码与占位符必须一致（单元测试核对）；
# 风险相关的含义必须一致：改一处就要同时改另一处。
LANGS = ("zh", "en")
MESSAGES: dict[str, dict[str, str]] = {
    "zh": {
        "tradingd_heartbeat_lost": (
            "tradingd 心跳丢失超过 {seconds} 秒，guardd 已开始逐单撤销开仓单"
        ),
        "guard_check_failing": "guardd 连续 {count} 轮检查出错（{error}），看门狗可能失效",
        "duplicate_order_risk": "疑似重复下单：{detail}",
        "account_unknown": "账户状态未知：{detail}",
        "live_mode_switch": "检测到实盘模式切换：{detail}",
        "emergency_failed": "急停执行失败：{detail}",
        "protective_stop_missing": "保护性止损缺失：{symbol}",
        "broker_disconnected": "券商连接断开：{detail}",
        "quotes_stale": "行情严重陈旧：{detail}",
        "reconcile_mismatch": "对账不一致：{detail}",
        "alert_test": "告警通路测试：{detail}",
        "alert_store_failed": (
            "告警记录库不可用（{error}），告警改为只在内存里保留，"
            "网页显示“无法获取告警”；重启 guardd 后恢复"
        ),
        "_repeat": "（第 {n} 次提醒，确认前会继续提醒）",
    },
    "en": {
        "tradingd_heartbeat_lost": (
            "tradingd heartbeat lost for over {seconds} s; "
            "guardd is cancelling opening orders one by one"
        ),
        "guard_check_failing": (
            "guardd check failed {count} rounds in a row ({error}); the watchdog may not be working"
        ),
        "duplicate_order_risk": "Possible duplicate order: {detail}",
        "account_unknown": "Account state unknown: {detail}",
        "live_mode_switch": "Live mode switch detected: {detail}",
        "emergency_failed": "Emergency control failed: {detail}",
        "protective_stop_missing": "Protective stop missing: {symbol}",
        "broker_disconnected": "Broker disconnected: {detail}",
        "quotes_stale": "Quotes badly stale: {detail}",
        "reconcile_mismatch": "Reconciliation mismatch: {detail}",
        "alert_test": "Alert channel test: {detail}",
        "alert_store_failed": (
            "Alert store unavailable ({error}); alerts are now kept in memory only and the "
            'web client shows "cannot load alerts"; restart guardd to recover'
        ),
        "_repeat": " (reminder {n}; reminders continue until acknowledged)",
    },
}
CODES = frozenset(k for k in MESSAGES["zh"] if not k.startswith("_"))


class AlertChannel(Protocol):
    """手机渠道：send 失败必须抛出（由 AlertHub 重试）。send 可能阻塞，不得在事件循环里调用。"""

    name: str

    def send(self, text: str) -> None: ...


@dataclass(frozen=True)
class Alert:
    level: Level
    code: str
    params: Mapping[str, str]
    key: str  # 去重键：同一问题用同一 key

    def __post_init__(self) -> None:
        if self.code not in CODES:
            raise ValueError(f"未登记的告警代码：{self.code}")
        if not self.key.strip():
            raise ValueError("告警 key 不能为空")


def render(alert: Alert, lang: str = "zh") -> str:
    table = MESSAGES.get(lang, MESSAGES["zh"])
    try:
        body = table[alert.code].format(**alert.params)
    except (KeyError, IndexError):  # 参数不全也要发出去：宁可难看，不能丢
        body = f"{alert.code} {dict(alert.params)}"
    return f"[{alert.level.name}] {body}"


@dataclass
class _Open:
    alert: Alert
    raised_at: datetime
    next_send: datetime | None  # None：不需要（再）推送
    attempts: int = 0
    sent: int = 0
    in_flight: bool = False
    acknowledged_by: str | None = None
    alert_id: int | None = None  # alerts.db 里这次发生的编号；None：没有落盘


@dataclass
class HubStats:
    channel_ok: bool = True  # 最近一次推送是否成功（无推送时为真）
    last_error: str | None = None
    deferred_by_rate_limit: int = 0
    store_ok: bool = False  # 告警是否在落盘（alerts.db）
    history: deque[str] = field(default_factory=lambda: deque(maxlen=200))


class AlertHub:
    def __init__(  # noqa: PLR0913 - 时间参数全部显式，便于用假时钟测试
        self,
        channel: AlertChannel | None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        repeat_every: timedelta = timedelta(minutes=5),
        retry_base: timedelta = timedelta(seconds=10),
        retry_max: timedelta = timedelta(minutes=2),
        max_sends_per_minute: int = 20,
        max_per_pump: int = 5,
        lang: str = "zh",
        store: AlertStore | None = None,
    ) -> None:
        if repeat_every <= timedelta(0) or retry_base <= timedelta(0):
            raise ValueError("间隔必须为正")
        self._channel = channel
        self._lang = lang if lang in MESSAGES else "zh"
        if lang not in MESSAGES:  # 配置写错不静默：记一条警告再回退中文
            log_event(log, logging.WARNING, "告警语言不认识，改用中文", lang=lang)
        self._clock = clock
        self._repeat = repeat_every
        self._retry_base = retry_base
        self._retry_max = retry_max
        self._max_per_minute = max_sends_per_minute
        self._max_per_pump = max_per_pump
        self._open: dict[str, _Open] = {}
        self._sends: deque[datetime] = deque()
        self._lock = threading.Lock()
        self.stats = HubStats(channel_ok=channel is not None)
        if channel is None:
            self.stats.last_error = "没有配置手机渠道"
        self._store = store
        self.store_id = None if store is None else store.store_id
        self.stats.store_ok = store is not None
        if store is not None:
            with self._lock:
                self._restore(store)

    # ---------- 生命周期 ----------

    def raise_alert(self, alert: Alert) -> bool:
        """登记一条告警（不做网络 I/O）。返回是否是新告警；同 key 未解除时只是重复上报。"""
        with self._lock:
            if alert.key in self._open:
                return False
            now = self._clock()
            alert_id = self._persist(
                lambda s: s.raised(alert.key, alert.level.name, alert.code, alert.params)
            )
            self._open[alert.key] = _Open(
                alert, now, now if alert.level in PUSHED else None, alert_id=alert_id
            )
        log_event(log, _LOG_LEVEL[alert.level], "告警", level=alert.level.name, code=alert.code,
                  key=alert.key, text=render(alert, self._lang))  # fmt: skip
        return True

    def acknowledge(
        self,
        key: str,
        *,
        actor: str,
        alert_id: int | None = None,
        store_id: str | None = None,
        requested_at: datetime | None = None,
    ) -> bool:
        """人工确认：停止重复提醒。不解除风险状态；风险消失后由检测方 resolve。

        给了 alert_id（网页确认）就只确认那一次发生，三样都要对上（S-05a 评审 P1-1）：
        编号相同、库编号相同（库被重建过的旧请求不算）、请求时间不早于这次发生。
        """
        with self._lock:
            item = self._open.get(key)
            if item is None or item.acknowledged_by is not None:
                return False
            if alert_id is not None:
                if item.alert_id != alert_id or store_id is None or store_id != self.store_id:
                    return False
                if requested_at is None or requested_at < item.raised_at:
                    return False
            item.acknowledged_by = actor
            item.next_send = None
            if item.alert_id is not None:
                stored = item.alert_id
                self._persist(lambda s: s.acked(stored, key, actor=actor))
        log_event(log, logging.WARNING, "告警已确认", key=key, actor=actor)
        return True

    def resolve(self, key: str) -> bool:
        """风险已消失（检测方判定）：关闭告警。之后同一问题再发生是新告警。"""
        with self._lock:
            item = self._open.pop(key, None)
            if item is not None and item.alert_id is not None:
                stored = item.alert_id
                self._persist(lambda s: s.resolved(stored, key))
        if item is None:
            return False
        log_event(log, logging.WARNING, "告警已解除", key=key, level=item.alert.level.name)
        return True

    def open_alerts(self) -> list[Alert]:
        """未确认的告警。"""
        with self._lock:
            return [o.alert for o in self._open.values() if o.acknowledged_by is None]

    def unresolved(self) -> list[Alert]:
        """未解除的告警（含已确认的）。"""
        with self._lock:
            return [o.alert for o in self._open.values()]

    def store_unavailable(self, exc: BaseException) -> None:
        """装配时 alerts.db 打不开：报 P1，只在内存里保留告警。"""
        with self._lock:
            self._store_failed(exc)

    # ---------- 落盘（调用方持有 self._lock） ----------

    def _persist(self, write: Callable[[AlertStore], _T]) -> _T | None:
        if self._store is None:
            return None
        try:
            return write(self._store)
        except Exception as exc:
            self._store_failed(exc)
            return None

    def _store_failed(self, exc: BaseException) -> None:
        """告警库失效：停用它，报一条只在内存里的 P1（推送照常），之后不再尝试写。"""
        self._store = None
        self.stats.store_ok = False
        error = type(exc).__name__
        log_event(log, logging.ERROR, "告警库不可用，改为只在内存里保留告警",
                  error=error, recover="重启 guardd 后恢复落盘")  # fmt: skip
        if STORE_FAILED not in self._open:
            alert = Alert(Level.P1, STORE_FAILED, {"error": error}, key=STORE_FAILED)
            now = self._clock()
            self._open[STORE_FAILED] = _Open(alert, now, now)

    def _restore(self, store: AlertStore) -> None:
        """启动时恢复未解除的告警：未确认的 P0 立即再提醒；其余只恢复状态，不重复推送。"""
        try:
            rows = store.unresolved()
        except Exception as exc:
            self._store_failed(exc)
            return
        now = self._clock()
        for row in reversed(rows):  # 从旧到新；同 key 有多条时保留最新那次
            try:
                alert = Alert(Level[row.level], row.code, dict(row.params), key=row.key)
                raised_at = datetime.fromisoformat(row.raised_at)
            except (KeyError, ValueError) as exc:  # 代码已下线等：记为解除，不让它永远挂着
                log_event(log, logging.WARNING, "无法恢复的告警记录，记为已解除",
                          alert_id=row.alert_id, error=type(exc).__name__)  # fmt: skip
                self._persist(_resolver(row.alert_id, row.key))
                continue
            older = self._open.get(alert.key)
            if older is not None and older.alert_id is not None:  # 同 key 的旧发生：被新的取代
                self._persist(_resolver(older.alert_id, alert.key))
            repeat = alert.level is Level.P0 and row.acked_by is None
            self._open[alert.key] = _Open(
                alert, raised_at, now if repeat else None,
                acknowledged_by=row.acked_by, alert_id=row.alert_id,
            )  # fmt: skip

    # ---------- 推送（放在线程里调用） ----------

    def pump(self) -> None:
        if self._channel is None:
            return
        now = self._clock()
        with self._lock:
            due = sorted(
                (o for o in self._open.values()
                 if o.next_send is not None and o.next_send <= now and not o.in_flight),
                key=lambda o: (o.alert.level, o.raised_at),  # P0 优先
            )  # fmt: skip
            batch: list[_Open] = []
            for item in due:
                if len(batch) >= self._max_per_pump or not self._take_slot(now):
                    self.stats.deferred_by_rate_limit += 1
                    item.next_send = now + self._retry_base  # 顺延，不丢
                    continue
                item.in_flight = True
                batch.append(item)
        for item in batch:
            self._send(item)

    def _take_slot(self, now: datetime) -> bool:
        while self._sends and self._sends[0] <= now - timedelta(minutes=1):
            self._sends.popleft()
        if len(self._sends) >= self._max_per_minute:
            return False
        self._sends.append(now)
        return True

    def _send(self, item: _Open) -> None:
        assert self._channel is not None  # noqa: S101 - pump 已检查
        text = render(item.alert, self._lang)
        if item.sent > 0:
            text += MESSAGES[self._lang]["_repeat"].format(n=item.sent + 1)
        error: str | None = None
        try:
            self._channel.send(text)
        except Exception as exc:  # 只记类型名：异常内容可能含令牌
            error = type(exc).__name__
        now = self._clock()
        with self._lock:
            item.in_flight = False
            if error is not None:
                item.attempts += 1
                delay = min(self._retry_base * (2 ** (item.attempts - 1)), self._retry_max)
                item.next_send = now + delay if item.acknowledged_by is None else None
                self.stats.channel_ok = False
                self.stats.last_error = error
            else:
                item.attempts = 0
                item.sent += 1
                self.stats.channel_ok = True
                self.stats.last_error = None
                self.stats.history.append(f"{now.isoformat()} {item.alert.key}")
                repeat = item.alert.level is Level.P0 and item.acknowledged_by is None
                item.next_send = now + self._repeat if repeat else None
        if error is not None:
            log_event(
                log, logging.ERROR, "告警推送失败，稍后重试", channel=self._channel.name,
                key=item.alert.key, error=error, attempt=item.attempts,
            )  # fmt: skip


def _resolver(alert_id: int, key: str) -> Callable[[AlertStore], None]:
    return lambda store: store.resolved(alert_id, key)


__all__ = (
    "CODES",
    "LANGS",
    "MESSAGES",
    "STORE_FAILED",
    "Alert",
    "AlertChannel",
    "AlertHub",
    "HubStats",
    "Level",
    "render",
)
