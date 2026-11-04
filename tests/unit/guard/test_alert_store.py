"""告警落盘与网页确认（S-05a）：AlertHub 写 alerts.db、重启恢复、库失效降级、按发生编号确认。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from server.app.guardd import AckPoller, GuarddDeps, GuarddSettings, StatusWriter, _hub
from server.guard.alerts import STORE_FAILED, Alert, AlertHub, Level
from server.storage.alertsdb import AlertStore, read_alerts, status_path
from server.storage.apidb import ApiDb

T0 = datetime(2026, 11, 4, 1, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class FakeChannel:
    name = "fake"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


def p0(key: str = "k") -> Alert:
    return Alert(Level.P0, "tradingd_heartbeat_lost", {"seconds": "90"}, key=key)


def web_ack(hub: AlertHub, alert_id: int, *, at: datetime, store_id: str | None = None) -> bool:
    return hub.acknowledge(
        "k", actor="dylan", alert_id=alert_id,
        store_id=hub.store_id if store_id is None else store_id, requested_at=at,
    )  # fmt: skip


def test_lifecycle_is_persisted(tmp_path: Path) -> None:
    path = tmp_path / "alerts.db"
    clock = Clock()
    hub = AlertHub(None, clock=clock, store=AlertStore(path, clock=clock))
    assert hub.stats.store_ok
    hub.raise_alert(p0())
    hub.raise_alert(p0())  # 重复上报不重复落盘
    [stored] = read_alerts(path)
    assert (stored.key, stored.level, stored.code) == ("k", "P0", "tradingd_heartbeat_lost")
    assert web_ack(hub, stored.alert_id, at=T0 + timedelta(seconds=1))
    assert hub.resolve("k")
    [after] = read_alerts(path)
    assert after.acked_by == "dylan"
    assert after.resolved_at is not None


def test_ack_with_stale_id_does_nothing(tmp_path: Path) -> None:
    path = tmp_path / "alerts.db"
    clock = Clock()
    hub = AlertHub(None, clock=clock, store=AlertStore(path, clock=clock))
    hub.raise_alert(p0())
    old = read_alerts(path)[0].alert_id
    hub.resolve("k")
    hub.raise_alert(p0())  # 同一问题再次发生：新编号
    later = T0 + timedelta(seconds=1)
    assert not web_ack(hub, old, at=later)  # 旧页面上的确认不作用于新发生
    assert [a.key for a in hub.open_alerts()] == ["k"]
    new = read_alerts(path)[0].alert_id
    assert web_ack(hub, new, at=later)
    assert not web_ack(hub, new, at=later)  # 幂等


def test_ack_from_a_rebuilt_store_or_before_the_occurrence_is_ignored(tmp_path: Path) -> None:
    """评审 P1-1：alerts.db 重建后编号从 1 重新开始，旧请求的库编号对不上；早于发生的请求也不算。"""
    path = tmp_path / "alerts.db"
    clock = Clock()
    old = AlertStore(path, clock=clock)
    first = AlertHub(None, clock=clock, store=old)
    first.raise_alert(p0())
    old_store = first.store_id
    old.close()
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    clock.now = T0 + timedelta(hours=1)
    second = AlertHub(None, clock=clock, store=AlertStore(path, clock=clock))
    second.raise_alert(p0())
    [fresh] = read_alerts(path)
    assert fresh.alert_id == 1  # 同一个编号
    assert old_store is not None
    assert not web_ack(second, 1, at=clock.now, store_id=old_store)  # 旧库的请求
    assert not web_ack(second, 1, at=T0)  # 请求早于这次发生
    assert not second.acknowledge("k", actor="dylan", alert_id=1)  # 缺库编号与时间
    assert [a.key for a in second.open_alerts()] == ["k"]
    assert web_ack(second, 1, at=clock.now)


def test_restart_restores_unresolved_and_keeps_reminding(tmp_path: Path) -> None:
    path = tmp_path / "alerts.db"
    clock = Clock()
    first = AlertHub(None, clock=clock, store=AlertStore(path, clock=clock))
    first.raise_alert(p0("lost"))
    first.raise_alert(Alert(Level.P1, "broker_disconnected", {"detail": "x"}, key="b"))
    first.raise_alert(p0("done"))
    first.acknowledge("b", actor="dylan")
    first.resolve("done")
    channel = FakeChannel()
    clock.now += timedelta(minutes=1)
    second = AlertHub(channel, clock=clock, store=AlertStore(path, clock=clock))
    assert sorted(a.key for a in second.unresolved()) == ["b", "lost"]
    assert [a.key for a in second.open_alerts()] == ["lost"]  # 确认状态也恢复
    second.pump()
    assert len(channel.sent) == 1  # 只有未确认的 P0 立刻再提醒
    assert "心跳" in channel.sent[0]


def test_restore_resolves_records_it_cannot_load(tmp_path: Path) -> None:
    """评审 P2-6：代码已下线的旧记录记为解除，不会在网页上永远挂着。"""
    path = tmp_path / "alerts.db"
    store = AlertStore(path)
    gone = store.raised("old", "P1", "code_removed_long_ago", {})
    store.close()
    AlertHub(None, store=AlertStore(path))
    [row] = [a for a in read_alerts(path) if a.alert_id == gone]
    assert row.resolved_at is not None


def test_store_write_failure_degrades_to_memory(tmp_path: Path) -> None:
    path = tmp_path / "alerts.db"
    channel = FakeChannel()
    hub = AlertHub(channel, store=AlertStore(path))
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TRIGGER breaks BEFORE INSERT ON alert_events BEGIN SELECT RAISE(ABORT, 'x'); END"
    )
    conn.commit()
    conn.close()
    assert hub.raise_alert(p0())  # 写不进去也要登记并推送
    keys = sorted(a.key for a in hub.unresolved())
    assert keys == sorted(["k", STORE_FAILED])
    assert not hub.stats.store_ok
    hub.pump()
    assert len(channel.sent) == 2  # P0 与 P1 告警库失效都推出去了
    assert hub.acknowledge("k", actor="dylan")  # 内存里的告警仍可在服务器上确认


def test_corrupt_store_at_startup_raises_p1(tmp_path: Path) -> None:
    bad = tmp_path / "alerts.db"
    bad.write_bytes(b"garbage" * 200)
    settings = GuarddSettings(runtime_dir=tmp_path, ledger_path=tmp_path / "l.db", alerts_db=bad)
    hub = _hub(settings, GuarddDeps())
    assert [a.key for a in hub.unresolved()] == [STORE_FAILED]
    assert hub.unresolved()[0].level is Level.P1
    assert hub.raise_alert(p0())  # 照常工作


def test_unusable_directory_or_missing_config_degrades_to_p1(tmp_path: Path) -> None:
    """评审 P1-3、P1-4：目录建不出来、没有配置都不能让 guardd 起不来，但要报 P1。"""
    blocker = tmp_path / "file"
    blocker.write_text("x")
    for settings in (
        GuarddSettings(runtime_dir=tmp_path, ledger_path=tmp_path / "l.db",
                       alerts_db=blocker / "alerts.db"),
        GuarddSettings(runtime_dir=tmp_path, ledger_path=tmp_path / "l.db", store_expected=True),
    ):  # fmt: skip
        hub = _hub(settings, GuarddDeps())
        assert [a.key for a in hub.unresolved()] == [STORE_FAILED]
        assert not hub.stats.store_ok


def test_ack_poller_applies_web_acks_once(tmp_path: Path) -> None:
    store_path = tmp_path / "alerts.db"
    api_path = tmp_path / "api.db"
    hub = AlertHub(None, store=AlertStore(store_path))
    poller = AckPoller(hub, api_path)
    poller()  # api.db 还不存在：不出错，只记一次
    hub.raise_alert(p0())
    alert_id = read_alerts(store_path)[0].alert_id
    assert hub.store_id is not None
    api = ApiDb(api_path)
    api.request_ack(alert_id, "k", hub.store_id, actor="dylan")
    api.request_ack(alert_id, "k", hub.store_id, actor="dylan")
    api.request_ack(alert_id + 100, "k", hub.store_id, actor="mallory")  # 编号不符：不动作
    api.request_ack(alert_id, "k", "another-store", actor="mallory")  # 库编号不符：不动作
    poller()
    assert hub.open_alerts() == []
    [stored] = read_alerts(store_path)
    assert stored.acked_by == "dylan"
    poller()  # 续读：没有新请求
    restarted = AckPoller(hub, api_path)
    restarted()  # 重启后从头读也无害
    assert read_alerts(store_path)[0].acked_by == "dylan"


def test_status_writer_reports_store_health(tmp_path: Path) -> None:
    path = tmp_path / "alerts.db"
    clock = Clock()
    hub = AlertHub(None, clock=clock, store=AlertStore(path, clock=clock))
    writer = StatusWriter(hub, path, clock, every=5.0)
    writer()
    assert json.loads(status_path(path).read_text(encoding="utf-8"))["store_ok"] is True
    hub.store_unavailable(OSError("disk"))
    writer()  # 不到 5 秒：不重写
    assert json.loads(status_path(path).read_text(encoding="utf-8"))["store_ok"] is True
    clock.now += timedelta(seconds=6)
    writer()
    marker = json.loads(status_path(path).read_text(encoding="utf-8"))
    assert marker["store_ok"] is False
    assert datetime.fromisoformat(marker["at"]) == clock.now
