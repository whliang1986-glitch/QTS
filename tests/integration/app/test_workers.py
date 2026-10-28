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
from server.app.supervisor import EXIT_OK, EXIT_REFUSED
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
        EXIT_REFUSED
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
                          check_seconds=0.05)  # fmt: skip


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
    assert await run_guardd(guard_settings(tmp_path), second, asyncio.Event()) == EXIT_REFUSED
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
