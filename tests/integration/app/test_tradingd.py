"""S-01c：tradingd 进程入口。

真实账本、真实进程间通道、注入 FakeBroker；生产入口没有 IBKR 适配器即拒绝启动。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from server.app.supervisor import EXIT_BUSY, EXIT_OK, EXIT_REFUSED
from server.app.tradingd import (
    EXIT_CRASH,
    HEARTBEAT_FILE,
    TradingdDeps,
    TradingdSettings,
    run_tradingd,
)
from server.guard.heartbeat import HeartbeatFile
from server.ipc.protocol import Role
from server.ipc.transport import Endpoints, connect_role
from server.platforms import LocalChannel, PlatformNotImplementedError, local_channel
from server.risk.assembly import CoreInputs
from server.risk.config import parse_health_config
from server.storage.config import load_toml

from tests.integration.risk.test_context import Quotes
from tests.integration.risk.test_core import HEALTH, config
from tests.integration.risk.test_limit_rules_engine import PARAMS
from tests.integration.risk.test_reconcile import Actions
from tests.support.oms import Env


def _channel() -> LocalChannel | None:
    try:
        channel = local_channel()
        channel.current_account()
    except PlatformNotImplementedError:
        return None
    return channel


CHANNEL = _channel()
pytestmark = pytest.mark.skipif(CHANNEL is None, reason="当前平台的通道尚未实现")


class Daemon:
    def __init__(self, env: Env, tmp_path: Path, **overrides: Any) -> None:
        assert CHANNEL is not None
        self.env = env
        self.me = CHANNEL.current_account()
        self.settings = TradingdSettings(
            runtime_dir=tmp_path / "run",
            ledger_path=tmp_path / "ledger.db",
            allowed={Role.WORKERD: frozenset({self.me}), Role.APID: frozenset({self.me})},
            heartbeat_seconds=0.05,
            lock_wait_seconds=0.3,
            instance=f"t-{uuid.uuid4().hex[:10]}",
        )
        self.broker_factory = overrides.pop("broker", lambda: env.broker)
        self.mode_config = overrides.pop("mode_config", config())
        self.stop = asyncio.Event()
        self.task: asyncio.Task[int] | None = None

    def inputs(self) -> CoreInputs:
        components = parse_health_config(load_toml(HEALTH, name="health"))

        async def skew() -> int | None:
            return 0

        return CoreInputs(
            mode_config=self.mode_config, instruments={"SPY": self.env.om._instruments["SPY"]},
            components=components, parameters=lambda: PARAMS, quotes=Quotes(self.env),
            account_synced=lambda: True, clock_skew_ms=skew,
            start_of_day_nav=lambda _a: Decimal(200000),
            strategy_drawdown=lambda: {"s": Decimal(0)}, stop_cooldown=dict,
            corporate_actions=Actions(), broker_window=lambda _now: False,
            degraded_allowed={}, clock=self.env.clock,
        )  # fmt: skip

    @staticmethod
    def healthy(run_state: Any, _broker: Any) -> None:
        for name in ("broker", "account_sync", "quotes", "news", "model"):
            run_state.report(name, ok=True)

    def deps(self) -> TradingdDeps:
        assert CHANNEL is not None
        return TradingdDeps(
            channel=CHANNEL, inputs=self.inputs, broker=self.broker_factory,
            report_health=self.healthy, clock=self.env.clock,
        )  # fmt: skip

    def start(self) -> asyncio.Task[int]:
        self.task = asyncio.create_task(run_tradingd(self.settings, self.deps(), self.stop))
        return self.task

    def endpoints(self) -> Endpoints:
        assert CHANNEL is not None
        return Endpoints(CHANNEL, self.settings.runtime_dir, self.settings.instance)

    async def connect(self, role: Role) -> Any:
        for _ in range(200):
            try:
                return await connect_role(self.endpoints(), role, server_account=self.me)
            except Exception:
                assert self.task is not None and not self.task.done(), "tradingd 提前退出"
                await asyncio.sleep(0.05)
        raise AssertionError("tradingd 端点一直没起来")


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    e = Env(tmp_path)
    yield e


@pytest.mark.asyncio
async def test_tradingd_serves_intents_and_commands_then_stops(env: Env, tmp_path: Path) -> None:
    daemon = Daemon(env, tmp_path)
    task = daemon.start()
    worker = await daemon.connect(Role.WORKERD)
    answer = await worker.submit_intent(env.make_intent("w-1", quantity=Decimal(1)))
    # 首次启动没有对账基准 → SAFE HALT，风控按 HALT 拒绝；回答本身是正常的风控决定
    assert answer["ok"] is True
    assert answer["result"]["verdict"] in {"HALT", "REJECT"}
    apid = await daemon.connect(Role.APID)
    stopped = await apid.command("c-1", "stop_new_orders", {"reason": "演练"}, actor="dylan")
    assert stopped["ok"] is True and stopped["result"]["ok"] is True
    await worker.close()
    await apid.close()
    beat = HeartbeatFile(daemon.settings.runtime_dir / HEARTBEAT_FILE).last()
    assert beat is not None and beat == env.clock()
    daemon.stop.set()
    assert await asyncio.wait_for(task, 10) == EXIT_OK


@pytest.mark.asyncio
async def test_second_instance_waits_then_backs_off(env: Env, tmp_path: Path) -> None:
    """同一账本的第二个 tradingd：等一小会儿仍拿不到锁 → EXIT_BUSY（守护进程退避后再试）。"""
    first = Daemon(env, tmp_path)
    task = first.start()
    await (await first.connect(Role.WORKERD)).close()
    second = Daemon(env, tmp_path)
    assert await asyncio.wait_for(second.start(), 10) == EXIT_BUSY
    first.stop.set()
    assert await asyncio.wait_for(task, 10) == EXIT_OK


@pytest.mark.asyncio
async def test_production_entry_refuses_without_an_ibkr_adapter(env: Env, tmp_path: Path) -> None:
    from server.app.tradingd import production_broker  # noqa: PLC0415

    daemon = Daemon(env, tmp_path, broker=production_broker)
    assert await asyncio.wait_for(daemon.start(), 10) == EXIT_REFUSED


@pytest.mark.asyncio
async def test_unmigrated_ledger_is_refused(env: Env, tmp_path: Path) -> None:
    daemon = Daemon(env, tmp_path)
    object.__setattr__(daemon.settings, "ledger_path", tmp_path / "fresh" / "ledger.db")
    assert await asyncio.wait_for(daemon.start(), 10) == EXIT_REFUSED


@pytest.mark.asyncio
async def test_startup_check_failure_is_refused(env: Env, tmp_path: Path) -> None:
    daemon = Daemon(env, tmp_path, mode_config=config(account_id="DU0000002"))
    assert await asyncio.wait_for(daemon.start(), 10) == EXIT_REFUSED


@pytest.mark.asyncio
async def test_missing_endpoint_policy_is_refused(env: Env, tmp_path: Path) -> None:
    daemon = Daemon(env, tmp_path)
    object.__setattr__(daemon.settings, "allowed", {Role.WORKERD: frozenset({daemon.me})})
    assert await asyncio.wait_for(daemon.start(), 10) == EXIT_REFUSED


@pytest.mark.asyncio
async def test_broker_connect_failure_is_a_crash(env: Env, tmp_path: Path) -> None:
    class Down:
        account = None

        async def connect(self) -> None:
            raise ConnectionError("TWS 没开")

    daemon = Daemon(env, tmp_path, broker=Down)
    assert await asyncio.wait_for(daemon.start(), 10) == EXIT_CRASH


@pytest.mark.asyncio
async def test_broker_event_stream_end_is_a_crash(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def ended() -> AsyncIterator[Any]:
        await asyncio.sleep(0.1)
        nothing: tuple[Any, ...] = ()
        for event in nothing:
            yield event

    monkeypatch.setattr(env.broker, "events", ended)
    daemon = Daemon(env, tmp_path)
    assert await asyncio.wait_for(daemon.start(), 10) == EXIT_CRASH


@pytest.mark.asyncio
async def test_heartbeat_keeps_moving(env: Env, tmp_path: Path) -> None:
    daemon = Daemon(env, tmp_path)
    task = daemon.start()
    await (await daemon.connect(Role.WORKERD)).close()
    path = daemon.settings.runtime_dir / HEARTBEAT_FILE
    for _ in range(100):  # 第一次心跳在端点自检之后
        if HeartbeatFile(path).last() is not None:
            break
        await asyncio.sleep(0.02)
    first = HeartbeatFile(path).last()
    env.clock.advance(5)
    for _ in range(100):
        if HeartbeatFile(path).last() != first:
            break
        await asyncio.sleep(0.02)
    assert HeartbeatFile(path).last() == env.clock()
    assert first is not None and env.clock() - first == timedelta(seconds=5)
    daemon.stop.set()
    assert await asyncio.wait_for(task, 10) == EXIT_OK


# ---------- 评审补充（S-01c 评审 P1-2 / P1-3 / P2） ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["client", "server"])
async def test_broker_disconnect_is_a_crash_and_stops_the_heartbeat(
    env: Env, tmp_path: Path, how: str
) -> None:
    """契约里的断线（DISCONNECTED 事件、connected 变 False，回报流并不结束）。

    tradingd 按崩溃退出，心跳不再更新。
    """
    daemon = Daemon(env, tmp_path)
    task = daemon.start()
    await (await daemon.connect(Role.WORKERD)).close()
    path = daemon.settings.runtime_dir / HEARTBEAT_FILE
    for _ in range(100):
        if HeartbeatFile(path).last() is not None:
            break
        await asyncio.sleep(0.02)
    if how == "client":
        await env.broker.disconnect()
    else:
        env.server.drop_client(env.broker.client_id)  # 网络断、TWS 崩溃
    assert await asyncio.wait_for(task, 10) == EXIT_CRASH
    last = HeartbeatFile(path).last()
    env.clock.advance(60)
    await asyncio.sleep(0.2)
    assert HeartbeatFile(path).last() == last  # 退出后心跳停了，guardd 会介入


@pytest.mark.asyncio
async def test_single_writer_lock_sits_next_to_the_ledger(env: Env, tmp_path: Path) -> None:
    """运行目录不同、账本相同的两个 tradingd：第二个拿不到账本锁。"""
    first = Daemon(env, tmp_path)
    task = first.start()
    await (await first.connect(Role.WORKERD)).close()
    second = Daemon(env, tmp_path)
    object.__setattr__(second.settings, "runtime_dir", tmp_path / "other-run")
    assert await asyncio.wait_for(second.start(), 10) == EXIT_BUSY
    first.stop.set()
    assert await asyncio.wait_for(task, 10) == EXIT_OK


@pytest.mark.asyncio
async def test_unexpected_start_error_is_a_crash(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.risk.core import TradingCore  # noqa: PLC0415

    async def boom(self: Any) -> Any:
        raise RuntimeError("意外")

    monkeypatch.setattr(TradingCore, "start", boom)
    daemon = Daemon(env, tmp_path)
    assert await asyncio.wait_for(daemon.start(), 10) == EXIT_CRASH
    assert env.broker.connected is False  # 启动失败也断开了券商


@pytest.mark.asyncio
async def test_dead_endpoint_is_noticed_and_crashes(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端点不再接受连接（例如 Windows 上 accept 循环停了）：自检发现后按崩溃退出。"""
    from server.app import tradingd  # noqa: PLC0415

    daemon = Daemon(env, tmp_path)
    task = daemon.start()
    await (await daemon.connect(Role.WORKERD)).close()

    async def dead(_channel: Any, address: str) -> None:
        raise ConnectionError(f"端点 {address} 不再接受连接")

    monkeypatch.setattr(tradingd, "_probe", dead)
    assert await asyncio.wait_for(task, 10) == EXIT_CRASH


@pytest.mark.asyncio
async def test_stale_intents_are_refused_by_tradingd(env: Env, tmp_path: Path) -> None:
    """tradingd 自己按 created_at 限龄（不信任 workerd 的判断）。"""
    daemon = Daemon(env, tmp_path)
    task = daemon.start()
    worker = await daemon.connect(Role.WORKERD)
    old = env.make_intent("old-1", quantity=Decimal(1), created_at=env.clock() - timedelta(hours=1))
    answer = await worker.submit_intent(old)
    assert answer["refused"] is True and "过期" in answer["error"]
    assert env.repo.risk_decisions_for("old-1") == []
    await worker.close()
    daemon.stop.set()
    assert await asyncio.wait_for(task, 10) == EXIT_OK
