"""S-01c：workerd（outbox 投递循环）与 guardd（看门狗）进程入口。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from server.app.guardd import GuarddDeps, GuarddSettings, production_guard_broker, run_guardd
from server.app.supervisor import EXIT_BUSY, EXIT_OK, EXIT_REFUSED
from server.app.tradingd import EXIT_CRASH
from server.app.workerd import WorkerdSettings, run_workerd
from server.broker.fake.broker import FakeBroker
from server.domain.order_state import OrderStatus
from server.ipc.outbox import Outbox

from tests.integration.app.test_tradingd import CHANNEL, Daemon
from tests.support.oms import Env

pytestmark = pytest.mark.skipif(CHANNEL is None, reason="当前平台的通道尚未实现")


async def until(check: Callable[[], bool], seconds: float = 10) -> None:
    for _ in range(int(seconds / 0.05)):
        if check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("条件一直没有满足")


def worker_settings(daemon: Daemon, tmp_path: Path) -> WorkerdSettings:
    return WorkerdSettings(
        runtime_dir=daemon.settings.runtime_dir, meta_path=tmp_path / "meta.db",
        tradingd_account=daemon.me, deliver_seconds=0.05, instance=daemon.settings.instance,
        max_age=timedelta(days=3650),  # 测试时钟固定在过去：不让过期规则干扰投递本身
        lock_wait_seconds=0.3,
    )  # fmt: skip


# ---------- workerd ----------


@pytest.mark.asyncio
async def test_workerd_delivers_queued_intents_to_tradingd(tmp_path: Path) -> None:
    env = Env(tmp_path)
    daemon = Daemon(env, tmp_path)
    tradingd = daemon.start()
    settings = worker_settings(daemon, tmp_path)
    box = Outbox(settings.meta_path)
    box.enqueue(env.make_intent("q-1", quantity=Decimal(1)))
    stop = asyncio.Event()
    assert CHANNEL is not None
    workerd = asyncio.create_task(run_workerd(settings, CHANNEL, stop))
    await until(lambda: box.receipt("q-1") is not None)
    receipt = box.receipt("q-1")
    assert receipt is not None and not receipt.refused  # tradingd 作出了风控决定
    assert env.repo.risk_decisions_for("q-1")
    stop.set()
    daemon.stop.set()
    assert await asyncio.wait_for(workerd, 10) == EXIT_OK
    assert await asyncio.wait_for(tradingd, 10) == EXIT_OK
    box.close()


@pytest.mark.asyncio
async def test_workerd_keeps_intents_while_tradingd_is_down(tmp_path: Path) -> None:
    env = Env(tmp_path)
    daemon = Daemon(env, tmp_path)  # 只用它的路径与账户，不启动 tradingd
    settings = worker_settings(daemon, tmp_path)
    box = Outbox(settings.meta_path)
    box.enqueue(env.make_intent("q-1", quantity=Decimal(1)))
    stop = asyncio.Event()
    assert CHANNEL is not None
    workerd = asyncio.create_task(run_workerd(settings, CHANNEL, stop))
    await asyncio.sleep(0.3)
    assert not workerd.done() and box.receipt("q-1") is None  # 留在队列里，进程不退出
    tradingd = daemon.start()  # tradingd 起来之后自动投递
    await until(lambda: box.receipt("q-1") is not None)
    stop.set()
    daemon.stop.set()
    assert await asyncio.wait_for(workerd, 10) == EXIT_OK
    assert await asyncio.wait_for(tradingd, 10) == EXIT_OK
    assert len(env.repo.risk_decisions_for("q-1")) == 1
    box.close()


@pytest.mark.asyncio
async def test_second_workerd_is_refused(tmp_path: Path) -> None:
    env = Env(tmp_path)
    daemon = Daemon(env, tmp_path)
    settings = worker_settings(daemon, tmp_path)
    stop = asyncio.Event()
    assert CHANNEL is not None
    first = asyncio.create_task(run_workerd(settings, CHANNEL, stop))
    await asyncio.sleep(0.2)
    assert await asyncio.wait_for(run_workerd(settings, CHANNEL, asyncio.Event()), 10) == (
        EXIT_BUSY
    )
    stop.set()
    assert await asyncio.wait_for(first, 10) == EXIT_OK


@pytest.mark.asyncio
async def test_workerd_refuses_an_impostor_tradingd(tmp_path: Path) -> None:
    env = Env(tmp_path)
    daemon = Daemon(env, tmp_path)
    tradingd = daemon.start()
    settings = worker_settings(daemon, tmp_path)
    object.__setattr__(settings, "tradingd_account", "someone-else")
    box = Outbox(settings.meta_path)
    box.enqueue(env.make_intent("q-1", quantity=Decimal(1)))
    stop = asyncio.Event()
    assert CHANNEL is not None
    workerd = asyncio.create_task(run_workerd(settings, CHANNEL, stop))
    await asyncio.sleep(0.5)
    assert box.receipt("q-1") is None and env.repo.risk_decisions_for("q-1") == []
    stop.set()
    daemon.stop.set()
    assert await asyncio.wait_for(workerd, 10) == EXIT_OK
    assert await asyncio.wait_for(tradingd, 10) == EXIT_OK
    box.close()


# ---------- guardd ----------


def guard_settings(tmp_path: Path) -> GuarddSettings:
    return GuarddSettings(runtime_dir=tmp_path / "run", ledger_path=tmp_path / "ledger.db",
                          check_seconds=0.05, lock_wait_seconds=0.3)  # fmt: skip


@pytest.mark.asyncio
async def test_guardd_cancels_open_orders_when_heartbeat_is_missing(tmp_path: Path) -> None:
    env = Env(tmp_path, cross_client_cancel=True)
    await env.broker.connect()
    await env.submit("o-1")
    await env.pump()
    guard_broker = FakeBroker(env.server, client_id=99)
    stop = asyncio.Event()
    deps = GuarddDeps(broker=lambda: guard_broker, clock=env.clock)
    guardd = asyncio.create_task(run_guardd(guard_settings(tmp_path), deps, stop))

    async def cancelled() -> bool:
        await env.pump()
        return env.om.status("o-1") is OrderStatus.CANCELLED

    for _ in range(200):
        if await cancelled():
            break
        await asyncio.sleep(0.05)
    assert env.om.status("o-1") is OrderStatus.CANCELLED
    stop.set()
    assert await asyncio.wait_for(guardd, 10) == EXIT_OK


@pytest.mark.asyncio
async def test_guardd_refuses_without_an_ibkr_adapter(tmp_path: Path) -> None:
    Env(tmp_path)
    deps = GuarddDeps(broker=production_guard_broker)
    assert await run_guardd(guard_settings(tmp_path), deps, asyncio.Event()) == EXIT_REFUSED


@pytest.mark.asyncio
async def test_second_guardd_is_refused(tmp_path: Path) -> None:
    env = Env(tmp_path)
    stop = asyncio.Event()
    first = asyncio.create_task(
        run_guardd(guard_settings(tmp_path), GuarddDeps(broker=lambda: env.broker), stop)
    )
    await asyncio.sleep(0.2)
    second = GuarddDeps(broker=lambda: FakeBroker(env.server, client_id=98))
    assert await run_guardd(guard_settings(tmp_path), second, asyncio.Event()) == EXIT_BUSY
    stop.set()
    assert await asyncio.wait_for(first, 10) == EXIT_OK


@pytest.mark.asyncio
async def test_guardd_broker_failure_is_a_crash(tmp_path: Path) -> None:
    Env(tmp_path)

    class Down:
        async def connect(self) -> None:
            raise ConnectionError("TWS 没开")

        async def disconnect(self) -> None:
            return None

    deps = GuarddDeps(broker=Down)
    assert await run_guardd(guard_settings(tmp_path), deps, asyncio.Event()) == EXIT_CRASH


@pytest.mark.asyncio
async def test_guardd_lost_connection_mid_check_is_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)

    async def dropped(*_a: Any) -> Any:
        raise ConnectionError("断线")

    monkeypatch.setattr(env.broker, "open_orders", dropped)
    deps = GuarddDeps(broker=lambda: env.broker, clock=env.clock)
    assert (
        await asyncio.wait_for(run_guardd(guard_settings(tmp_path), deps, asyncio.Event()), 10)
        == EXIT_CRASH
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["client", "server"])
async def test_guardd_contract_disconnect_is_a_crash(tmp_path: Path, how: str) -> None:
    """契约里的断线（connected 变 False / 服务端断开）：guardd 按崩溃退出，由守护进程重连。"""
    env = Env(tmp_path)
    broker = FakeBroker(env.server, client_id=97)
    stop = asyncio.Event()
    deps = GuarddDeps(broker=lambda: broker, clock=env.clock)
    guardd = asyncio.create_task(run_guardd(guard_settings(tmp_path), deps, stop))
    await until(lambda: broker.connected)
    if how == "client":
        await broker.disconnect()
    else:
        env.server.drop_client(97)
    assert await asyncio.wait_for(guardd, 10) == EXIT_CRASH


class Phone:
    name = "fake"

    def __init__(self, delay: float = 0.0) -> None:
        self.sent: list[str] = []
        self.delay = delay

    def send(self, text: str) -> None:
        import time  # noqa: PLC0415

        time.sleep(self.delay)
        self.sent.append(text)


async def wait_for(condition: Any, rounds: int = 200) -> bool:
    for _ in range(rounds):
        if condition():
            return True
        await asyncio.sleep(0.05)
    return bool(condition())


@pytest.mark.asyncio
async def test_guardd_alert_lifecycle_lost_ack_restored_lost(tmp_path: Path) -> None:
    """S-07：心跳丢失 → P0 推送；确认后每轮重复上报也不再推送；心跳恢复即解除；再丢失重新推送。"""
    from server.guard.alerts import AlertHub  # noqa: PLC0415
    from server.guard.heartbeat import HeartbeatFile  # noqa: PLC0415

    env = Env(tmp_path, cross_client_cancel=True)
    await env.broker.connect()
    phone = Phone()
    alerts = AlertHub(phone, clock=env.clock)
    beat = HeartbeatFile(tmp_path / "run" / "tradingd.heartbeat")
    stop = asyncio.Event()
    deps = GuarddDeps(broker=lambda: FakeBroker(env.server, client_id=99), clock=env.clock,
                      alerts=alerts)  # fmt: skip
    guardd = asyncio.create_task(run_guardd(guard_settings(tmp_path), deps, stop))
    assert await wait_for(lambda: len(phone.sent) == 1)
    assert phone.sent[0].startswith("[P0]") and "心跳丢失" in phone.sent[0]
    alerts.acknowledge("tradingd_heartbeat_lost", actor="dylan")
    await asyncio.sleep(0.6)  # guardd 又跑了很多轮，仍在上报心跳丢失
    assert len(phone.sent) == 1
    beat.beat(env.clock())  # tradingd 恢复
    assert await wait_for(lambda: alerts.unresolved() == [])
    env.clock.advance(120)  # 心跳又过期
    assert await wait_for(lambda: len(phone.sent) == 2)
    stop.set()
    assert await asyncio.wait_for(guardd, 10) == EXIT_OK


@pytest.mark.asyncio
async def test_slow_phone_channel_does_not_block_the_watchdog(tmp_path: Path) -> None:
    """S-07：推送在线程里：渠道每次发送卡 3 秒，guardd 照样按时检查、及时退出。"""
    from server.guard.alerts import AlertHub  # noqa: PLC0415

    env = Env(tmp_path, cross_client_cancel=True)
    await env.broker.connect()
    await env.submit("o-slow")
    await env.pump()
    phone = Phone(delay=3.0)
    deps = GuarddDeps(broker=lambda: FakeBroker(env.server, client_id=99), clock=env.clock,
                      alerts=AlertHub(phone, clock=env.clock))  # fmt: skip
    stop = asyncio.Event()
    guardd = asyncio.create_task(run_guardd(guard_settings(tmp_path), deps, stop))

    async def cancelled() -> bool:
        await env.pump()
        return env.om.status("o-slow") is OrderStatus.CANCELLED

    for _ in range(40):  # 2 秒内就撤掉，不等 3 秒的推送
        if await cancelled():
            break
        await asyncio.sleep(0.05)
    assert env.om.status("o-slow") is OrderStatus.CANCELLED
    stop.set()
    assert await asyncio.wait_for(guardd, 2) == EXIT_OK  # 退出不被推送拖住


# ---------- S-05a：告警落盘、网页确认、降级 ----------


def stored_guard_settings(tmp_path: Path, **kw: Any) -> GuarddSettings:
    base = {"alerts_db": tmp_path / "data" / "alerts.db", "api_db": tmp_path / "data" / "api.db"}
    return GuarddSettings(runtime_dir=tmp_path / "run", ledger_path=tmp_path / "ledger.db",
                          check_seconds=0.05, lock_wait_seconds=0.3, **{**base, **kw})  # fmt: skip


class Down:
    async def connect(self) -> None:
        raise ConnectionError("TWS 没开")

    async def disconnect(self) -> None:
        return None


@pytest.mark.asyncio
async def test_broker_alert_is_resolved_after_reconnect_and_raised_again(tmp_path: Path) -> None:
    """评审 P1-2：断线告警落盘后，重连成功要解除；之后再断线是新告警（会再推送）。"""
    from server.storage.alertsdb import read_alerts  # noqa: PLC0415

    env = Env(tmp_path)
    settings = stored_guard_settings(tmp_path)
    assert settings.alerts_db is not None
    assert await run_guardd(settings, GuarddDeps(broker=Down), asyncio.Event()) == EXIT_CRASH
    [first] = read_alerts(settings.alerts_db)
    assert first.key == "guardd_broker_disconnected" and first.resolved_at is None
    stop = asyncio.Event()
    deps = GuarddDeps(broker=lambda: env.broker, clock=env.clock)
    guardd = asyncio.create_task(run_guardd(settings, deps, stop))
    assert await wait_for(lambda: read_alerts(settings.alerts_db)[-1].resolved_at is not None)
    stop.set()
    assert await asyncio.wait_for(guardd, 10) == EXIT_OK
    assert await run_guardd(settings, GuarddDeps(broker=Down), asyncio.Event()) == EXIT_CRASH
    rows = [a for a in read_alerts(settings.alerts_db) if a.key == "guardd_broker_disconnected"]
    assert len(rows) == 2 and rows[0].resolved_at is None  # 新的一次发生


@pytest.mark.asyncio
async def test_web_ack_reaches_guardd_and_status_marker_is_written(tmp_path: Path) -> None:
    from server.storage.alertsdb import (  # noqa: PLC0415
        StoredAlert,
        check_status,
        read_alert,
        read_alerts,
    )
    from server.storage.apidb import ApiDb  # noqa: PLC0415

    env = Env(tmp_path, cross_client_cancel=True)
    await env.broker.connect()
    settings = stored_guard_settings(tmp_path)
    assert settings.alerts_db is not None and settings.api_db is not None
    api = ApiDb(settings.api_db)
    stop = asyncio.Event()
    deps = GuarddDeps(broker=lambda: FakeBroker(env.server, client_id=99), clock=env.clock)
    guardd = asyncio.create_task(run_guardd(settings, deps, stop))
    db = settings.alerts_db

    def lost() -> list[StoredAlert]:
        return [a for a in read_alerts(db) if a.key == "tradingd_heartbeat_lost"]

    assert await wait_for(lambda: settings.alerts_db.exists() and bool(lost()))
    alert = lost()[0]
    store_id, _ = read_alert(settings.alerts_db, alert.alert_id)
    env.clock.advance(1)  # 请求晚于发生
    api.request_ack(alert.alert_id, alert.key, store_id, actor="dylan")
    assert await wait_for(lambda: lost()[0].acked_by == "dylan")
    check_status(settings.alerts_db, env.clock())  # 健康标记存在且新鲜
    stop.set()
    assert await asyncio.wait_for(guardd, 10) == EXIT_OK
    api.close()


@pytest.mark.asyncio
async def test_guardd_still_guards_when_alerts_dir_is_unusable(tmp_path: Path) -> None:
    """评审 P1-3：告警库目录建不出来，guardd 照常撤单，告警改为只在内存里。"""
    env = Env(tmp_path, cross_client_cancel=True)
    await env.broker.connect()
    await env.submit("o-nodb")
    await env.pump()
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    settings = stored_guard_settings(tmp_path, alerts_db=blocker / "alerts.db", api_db=None)
    stop = asyncio.Event()
    deps = GuarddDeps(broker=lambda: FakeBroker(env.server, client_id=99), clock=env.clock)
    guardd = asyncio.create_task(run_guardd(settings, deps, stop))

    async def cancelled() -> bool:
        await env.pump()
        return env.om.status("o-nodb") is OrderStatus.CANCELLED

    for _ in range(200):
        if await cancelled():
            break
        await asyncio.sleep(0.05)
    assert env.om.status("o-nodb") is OrderStatus.CANCELLED
    stop.set()
    assert await asyncio.wait_for(guardd, 10) == EXIT_OK
