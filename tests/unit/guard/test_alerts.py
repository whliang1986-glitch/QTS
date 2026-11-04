"""告警分发（S-07）：分级、去重、确认与解除、P0 重复提醒、重试退避、限速、Telegram 与外部心跳。"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime, timedelta

import pytest
from server.guard.alerts import Alert, AlertHub, Level, render
from server.guard.heartbeat_ping import HeartbeatPinger
from server.guard.telegram import TOKEN_ENTRY, ChannelError, TelegramChannel
from server.storage.credentials import CredentialUnavailableError, MemoryCredentialStore

T0 = datetime(2026, 11, 4, 1, 0, tzinfo=UTC)
FAKE_TOKEN = "123456:FAKE-bot-value-for-tests"  # noqa: S105 - 测试用假值


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeChannel:
    name = "fake"

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.fail = 0

    def send(self, text: str) -> None:
        if self.fail:
            self.fail -= 1
            raise ConnectionError("渠道暂时不可用")
        self.sent.append(text)


def p0(key: str = "k") -> Alert:
    return Alert(Level.P0, "tradingd_heartbeat_lost", {"seconds": "90"}, key=key)


def p1(key: str = "b") -> Alert:
    return Alert(Level.P1, "broker_disconnected", {"detail": "x"}, key=key)


def hub(channel: FakeChannel | None, clock: Clock, **kw: object) -> AlertHub:
    return AlertHub(channel, clock=clock, **kw)  # type: ignore[arg-type]


# ---------- 生命周期 ----------


def test_raise_does_no_io_pump_sends() -> None:
    clock, channel = Clock(), FakeChannel()
    alerts = hub(channel, clock)
    assert alerts.raise_alert(p0())
    assert channel.sent == []  # 登记不做网络 I/O
    alerts.pump()
    assert len(channel.sent) == 1 and "[P0]" in channel.sent[0] and "90" in channel.sent[0]


def test_p0_repeats_until_acknowledged() -> None:
    clock, channel = Clock(), FakeChannel()
    alerts = hub(channel, clock, repeat_every=timedelta(minutes=5))
    alerts.raise_alert(p0())
    alerts.pump()
    clock.advance(299)
    alerts.pump()
    assert len(channel.sent) == 1
    clock.advance(1)
    alerts.pump()
    assert len(channel.sent) == 2 and "第 2 次提醒" in channel.sent[1]
    assert alerts.acknowledge("k", actor="dylan")
    clock.advance(600)
    alerts.pump()
    assert len(channel.sent) == 2
    assert alerts.open_alerts() == [] and [a.key for a in alerts.unresolved()] == ["k"]
    assert not alerts.acknowledge("k", actor="dylan")  # 重复确认无效


def test_acknowledged_alert_is_not_repushed_while_the_detector_keeps_reporting() -> None:
    """guardd 每轮都会重复上报心跳丢失：确认之后不能每轮重新推送（告警疲劳）。"""
    clock, channel = Clock(), FakeChannel()
    alerts = hub(channel, clock)
    alerts.raise_alert(p0())
    alerts.pump()
    alerts.acknowledge("k", actor="dylan")
    for _ in range(40):  # 10 分钟里每 15 秒上报一次
        clock.advance(15)
        assert not alerts.raise_alert(p0())
        alerts.pump()
    assert len(channel.sent) == 1


def test_resolve_closes_and_a_new_occurrence_alerts_again() -> None:
    clock, channel = Clock(), FakeChannel()
    alerts = hub(channel, clock)
    alerts.raise_alert(p0())
    alerts.pump()
    assert alerts.resolve("k")
    clock.advance(600)
    alerts.pump()
    assert len(channel.sent) == 1  # 解除后不再提醒（未确认也一样）
    assert alerts.unresolved() == [] and not alerts.resolve("k")
    assert alerts.raise_alert(p0())
    alerts.pump()
    assert len(channel.sent) == 2


def test_p1_is_pushed_once_p2_p3_are_not_pushed() -> None:
    clock, channel = Clock(), FakeChannel()
    alerts = hub(channel, clock)
    alerts.raise_alert(p1())
    alerts.raise_alert(Alert(Level.P2, "quotes_stale", {"detail": "x"}, key="q"))
    alerts.raise_alert(Alert(Level.P3, "alert_test", {"detail": "x"}, key="t"))
    for _ in range(10):
        clock.advance(600)
        alerts.pump()
    assert len(channel.sent) == 1 and channel.sent[0].startswith("[P1]")


def test_repeated_reports_do_not_flood_the_log(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    alerts = hub(FakeChannel(), Clock())
    for _ in range(10):
        alerts.raise_alert(p0())
    assert sum(r.getMessage() == "告警" for r in caplog.records) == 1


def test_every_alert_is_logged_even_without_a_channel(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    alerts = hub(None, Clock())
    alerts.raise_alert(p0())
    alerts.pump()
    assert not alerts.stats.channel_ok and alerts.stats.last_error
    assert alerts.stats.deferred_by_rate_limit == 0
    assert any(r.getMessage() == "告警" and r.levelno == logging.CRITICAL for r in caplog.records)


def test_unknown_codes_and_empty_keys_are_refused() -> None:
    with pytest.raises(ValueError, match="未登记"):
        Alert(Level.P0, "nope", {}, key="k")
    with pytest.raises(ValueError, match="key"):
        Alert(Level.P0, "alert_test", {"detail": "x"}, key=" ")


def test_missing_params_still_render() -> None:
    text = render(Alert(Level.P0, "protective_stop_missing", {}, key="s"))
    assert "protective_stop_missing" in text and text.startswith("[P0]")


# ---------- 重试与限速 ----------


def test_failed_send_is_retried_with_backoff() -> None:
    clock, channel = Clock(), FakeChannel()
    channel.fail = 2
    alerts = hub(channel, clock, retry_base=timedelta(seconds=10))
    alerts.raise_alert(p0())
    alerts.pump()
    assert channel.sent == [] and not alerts.stats.channel_ok
    clock.advance(10)
    alerts.pump()  # 第二次失败，下次等 20 秒
    assert channel.sent == []
    clock.advance(19)
    alerts.pump()
    assert channel.sent == []
    clock.advance(1)
    alerts.pump()
    assert len(channel.sent) == 1 and alerts.stats.channel_ok


def test_simulated_fault_reaches_phone_within_a_minute_of_detection() -> None:
    """G4 演练的验收：从检测到故障（登记告警）起算，渠道前几次失败，1 分钟内仍能送达。"""
    clock, channel = Clock(), FakeChannel()
    channel.fail = 2
    alerts = hub(channel, clock)
    alerts.raise_alert(p0())
    for _ in range(60):
        alerts.pump()
        if channel.sent:
            break
        clock.advance(1)
    assert channel.sent and clock.now - T0 <= timedelta(minutes=1)


def test_storm_is_rate_limited_but_not_dropped() -> None:
    clock, channel = Clock(), FakeChannel()
    alerts = hub(channel, clock, max_sends_per_minute=3)
    for i in range(5):
        alerts.raise_alert(Alert(Level.P1, "reconcile_mismatch", {"detail": str(i)}, key=f"r{i}"))
    alerts.pump()
    assert len(channel.sent) == 3 and alerts.stats.deferred_by_rate_limit >= 2
    clock.advance(61)
    alerts.pump()
    assert len(channel.sent) == 5


def test_p0_wins_when_both_wait_for_one_slot() -> None:
    clock, channel = Clock(), FakeChannel()
    alerts = hub(channel, clock, max_sends_per_minute=1)
    alerts.raise_alert(Alert(Level.P1, "alert_test", {"detail": "占名额"}, key="fill"))
    alerts.pump()  # 名额用完
    alerts.raise_alert(p1("late-p1"))  # P1 先登记
    clock.advance(1)
    alerts.raise_alert(p0())  # P0 后登记
    alerts.pump()  # 都被限速
    clock.advance(61)
    alerts.pump()  # 只放出一个名额
    assert channel.sent[1].startswith("[P0]")


def test_pump_sends_outside_the_lock() -> None:
    """发送阻塞时，事件循环那边仍能登记新告警（不被锁住）。"""
    gate, started = threading.Event(), threading.Event()

    class Slow(FakeChannel):
        def send(self, text: str) -> None:
            started.set()
            gate.wait(5)
            super().send(text)

    channel = Slow()
    alerts = hub(channel, Clock())
    alerts.raise_alert(p0())
    worker = threading.Thread(target=alerts.pump)
    worker.start()
    assert started.wait(5)
    assert alerts.raise_alert(p1())  # 不阻塞
    gate.set()
    worker.join(5)
    assert len(channel.sent) == 1


# ---------- Telegram ----------


class Post:
    def __init__(self, status: int = 200, body: bytes = b'{"ok": true}') -> None:
        self.calls: list[tuple[str, bytes]] = []
        self.status = status
        self.body = body
        self.error: Exception | None = None

    def __call__(self, url: str, body: bytes, timeout: float) -> tuple[int, bytes]:
        self.calls.append((url, body))
        if self.error is not None:
            raise self.error
        return self.status, self.body


def telegram(post: Post) -> TelegramChannel:
    store = MemoryCredentialStore({TOKEN_ENTRY: FAKE_TOKEN})
    return TelegramChannel(store, "-1001234", post=post)


def test_telegram_request_format() -> None:
    post = Post()
    telegram(post).send("[P0] 测试")
    url, body = post.calls[0]
    assert url == f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"
    assert json.loads(body) == {"chat_id": "-1001234", "text": "[P0] 测试",
                                "disable_web_page_preview": True}  # fmt: skip


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [(200, b'{"ok": false}', None), (401, b"{}", None), (200, b"not json", None),
     (200, b"", OSError(f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage 超时"))],
)  # fmt: skip
def test_telegram_failures_never_leak_the_token(
    status: int, body: bytes, error: Exception | None
) -> None:
    post = Post(status, body)
    post.error = error
    with pytest.raises(ChannelError) as info:
        telegram(post).send("x")
    assert FAKE_TOKEN not in str(info.value) and FAKE_TOKEN not in repr(info.value)
    # 异常链上不挂任何原异常（原异常的消息与 url 属性都可能含令牌）
    assert info.value.__cause__ is None and info.value.__context__ is None


def test_telegram_without_credentials_fails_closed() -> None:
    channel = TelegramChannel(MemoryCredentialStore(), "42", post=Post())
    with pytest.raises(CredentialUnavailableError):
        channel.send("x")


@pytest.mark.parametrize("chat", ["", "abc", "12a"])
def test_telegram_chat_id_must_be_numeric(chat: str) -> None:
    with pytest.raises(ValueError, match="chat_id"):
        TelegramChannel(MemoryCredentialStore(), chat, post=Post())


def test_hub_logs_never_contain_the_token(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    post = Post(500)
    alerts = AlertHub(telegram(post), clock=Clock())
    alerts.raise_alert(p0())
    alerts.pump()
    assert post.calls and alerts.stats.last_error == "ChannelError"
    assert all(FAKE_TOKEN not in r.getMessage() and FAKE_TOKEN not in str(r.__dict__)
               for r in caplog.records)  # fmt: skip


# ---------- 外部心跳 ----------


def test_heartbeat_pings_on_schedule_and_survives_failures() -> None:
    clock = Clock()
    calls: list[str] = []
    fail = [True]

    def get(url: str, timeout: float) -> int:
        calls.append(url)
        if fail[0]:
            raise OSError("断网")
        return 200

    pinger = HeartbeatPinger("https://hc.example/ping/abc", every=timedelta(minutes=1),
                             get=get, clock=clock)  # fmt: skip
    pinger.tick()
    assert pinger.failures == 1 and pinger.last_ok is None
    pinger.tick()
    assert len(calls) == 1  # 没到点不 ping
    fail[0] = False
    clock.advance(60)
    pinger.tick()
    assert pinger.last_ok == clock.now and pinger.failures == 0


@pytest.mark.parametrize("url", ["http://hc.example/ping", "ftp://x/y", "https://", "hc.example"])
def test_heartbeat_requires_https(url: str) -> None:
    with pytest.raises(ValueError, match="https"):
        HeartbeatPinger(url)


def test_heartbeat_does_not_log_the_private_path(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    def get(url: str, timeout: float) -> int:
        return 503

    HeartbeatPinger("https://hc.example/ping/private-uuid", get=get, clock=Clock()).tick()
    assert caplog.records and all("private-uuid" not in str(r.__dict__) for r in caplog.records)


def test_heartbeat_does_not_follow_redirects() -> None:
    import urllib.request  # noqa: PLC0415

    from server.guard import heartbeat_ping  # noqa: PLC0415

    base = urllib.request.HTTPRedirectHandler.redirect_request
    assert heartbeat_ping._NoRedirect.redirect_request is not base  # 覆盖了跟随重定向的默认行为


# ---------- 多语言（S-12） ----------


def test_alert_languages_have_the_same_codes_and_placeholders() -> None:
    import string  # noqa: PLC0415

    from server.guard.alerts import LANGS, MESSAGES  # noqa: PLC0415

    def holes(text: str) -> set[str]:
        return {name for _, name, _, _ in string.Formatter().parse(text) if name}

    assert set(MESSAGES) == set(LANGS)
    assert set(MESSAGES["en"]) == set(MESSAGES["zh"])
    for code, zh_text in MESSAGES["zh"].items():
        assert holes(MESSAGES["en"][code]) == holes(zh_text), code
        assert MESSAGES["en"][code].strip(), code


def test_english_hub_sends_english_and_unknown_language_falls_back() -> None:
    clock, channel = Clock(), FakeChannel()
    alerts = AlertHub(channel, clock=clock, lang="en")
    alerts.raise_alert(p0())
    alerts.pump()
    clock.advance(300)
    alerts.pump()
    expected = (
        "tradingd heartbeat lost for over 90 s; guardd is cancelling opening orders one by one"
    )
    assert channel.sent[0] == f"[P0] {expected}"
    assert "reminder 2" in channel.sent[1]
    assert render(p0(), "fr").startswith("[P0] tradingd 心跳丢失")
    assert AlertHub(FakeChannel(), lang="fr")._lang == "zh"


def test_unknown_alert_language_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    AlertHub(FakeChannel(), lang="en-US")
    assert any(r.getMessage() == "告警语言不认识，改用中文" for r in caplog.records)
