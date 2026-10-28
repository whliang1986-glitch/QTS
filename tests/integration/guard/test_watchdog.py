"""S-01c：guardd 看门狗（真实账本只读 + FakeBroker 独立 clientId + 真实风控下单）。"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from server.broker.fake.broker import FakeBroker
from server.domain.enums import Side
from server.domain.order_state import OrderStatus
from server.guard.heartbeat import HeartbeatFile
from server.guard.watchdog import FUTURE_TOLERANCE, HEARTBEAT_TIMEOUT, GuardWatchdog
from server.storage.ledger import LedgerRepository, open_ledger

from tests.integration.risk.test_emergency import reduce_intent, stop_intent
from tests.support.oms import Env

GUARD_CLIENT = 99


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> Env:
    e = Env(tmp_path, cross_client_cancel=True)
    await e.broker.connect()
    await e.pump()
    return e


class Beat:
    def __init__(self, value: Any) -> None:
        self.value = value

    def last(self) -> Any:
        return self.value


async def guard_for(env: Env, tmp_path: Path, beat: Any) -> tuple[GuardWatchdog, FakeBroker]:
    broker = FakeBroker(env.server, client_id=GUARD_CLIENT)
    await broker.connect()
    repo = LedgerRepository(open_ledger(tmp_path / "ledger.db", read_only=True))
    return GuardWatchdog(broker, repo, Beat(beat), clock=env.clock), broker


async def place_book(env: Env) -> None:
    """开仓买单 o-1、减仓卖单 r-1、保护性止损 stop-1（经 OMS 落账下单），外加一张外来限价买单。"""
    await env.submit("o-1")
    for intent in (reduce_intent(env, "r-1"), stop_intent(env)):
        data = intent.model_dump(exclude={"intent_id"})
        await env.submit(intent.intent_id, **data)
    env.server.inject_foreign_order("SPY", Side.BUY, Decimal(5), "LMT", Decimal(90))
    await env.pump()


@pytest.mark.asyncio
async def test_alive_tradingd_means_no_action(env: Env, tmp_path: Path) -> None:
    await place_book(env)
    guard, _ = await guard_for(env, tmp_path, env.clock() - timedelta(seconds=30))
    report = await guard.check()
    assert report.alive and report.cancelled == ()
    assert env.om.status("o-1") is OrderStatus.SUBMITTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "beat",
    ["missing", "stale", "future"],
)
async def test_lost_heartbeat_cancels_only_open_orders(env: Env, tmp_path: Path, beat: str) -> None:
    await place_book(env)
    value = {
        "missing": None,
        "stale": env.clock() - HEARTBEAT_TIMEOUT - timedelta(seconds=1),
        "future": env.clock() + FUTURE_TOLERANCE + timedelta(seconds=1),  # 时钟错乱也不可信
    }[beat]
    guard, _ = await guard_for(env, tmp_path, value)
    report = await guard.check()
    assert not report.alive
    assert len(report.cancelled) == 1
    await env.pump()
    assert env.om.status("o-1") is OrderStatus.CANCELLED  # 开仓单撤了
    assert env.om.status("r-1") is OrderStatus.SUBMITTED  # 减仓单不动
    assert env.om.status("stop-1") is OrderStatus.SUBMITTED  # 保护性止损不动
    assert set(report.reasons.values()) >= {"不是开仓单", "不是限价单", "外来订单"}


@pytest.mark.asyncio
async def test_heartbeat_exactly_at_the_timeout_is_alive(env: Env, tmp_path: Path) -> None:
    guard, _ = await guard_for(env, tmp_path, env.clock() - HEARTBEAT_TIMEOUT)
    assert guard.tradingd_alive()


@pytest.mark.asyncio
async def test_mismatched_ledger_binding_is_not_cancelled(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await place_book(env)
    guard, _ = await guard_for(env, tmp_path, None)
    monkeypatch.setattr(guard._repo, "intent_for_broker_order", lambda _oid: "someone-else")
    report = await guard.check()
    assert report.cancelled == ()
    assert "券商订单号与账本不一致" in report.reasons.values()


@pytest.mark.asyncio
async def test_ledger_errors_mean_do_not_cancel(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await place_book(env)
    guard, _ = await guard_for(env, tmp_path, None)

    def broken(_ref: str) -> Any:
        raise RuntimeError("db")

    monkeypatch.setattr(guard._repo, "get_intent", broken)
    report = await guard.check()
    assert report.cancelled == ()
    assert any(r.startswith("读账本出错") for r in report.reasons.values())


@pytest.mark.asyncio
async def test_one_failed_cancel_does_not_stop_the_others(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("o-1", "o-2"):
        await env.submit(name, quantity=Decimal(1))
    await env.pump()
    guard, broker = await guard_for(env, tmp_path, None)
    real = broker.cancel_order
    calls: list[str] = []

    async def flaky(oid: str, **kw: Any) -> None:
        calls.append(oid)
        if len(calls) == 1:
            raise ConnectionError("断线")
        await real(oid, **kw)

    monkeypatch.setattr(broker, "cancel_order", flaky)
    report = await guard.check()
    assert len(report.failed) == 1 and len(report.cancelled) == 1


@pytest.mark.asyncio
async def test_guard_never_passes_the_protective_flag(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await place_book(env)
    guard, broker = await guard_for(env, tmp_path, None)
    seen: list[dict[str, Any]] = []

    async def spy(oid: str, **kw: Any) -> None:
        seen.append(kw)

    monkeypatch.setattr(broker, "cancel_order", spy)
    await guard.check()
    assert seen == [{}]


def test_guard_does_not_write_the_ledger(tmp_path: Path) -> None:
    """guardd 以只读方式打开账本：任何写入都被 SQLite 拒绝。"""
    env = Env(tmp_path)
    repo = LedgerRepository(open_ledger(tmp_path / "ledger.db", read_only=True))
    with pytest.raises(Exception, match=r"readonly|read-only|只读"):
        repo.add_intent(env.make_intent("x"))


def test_timeout_must_be_positive(tmp_path: Path) -> None:
    env = Env(tmp_path)
    with pytest.raises(ValueError, match="心跳超时"):
        GuardWatchdog(env.broker, env.repo, Beat(None), timeout=timedelta(0))


# ---------- 心跳文件 ----------


def test_heartbeat_file_round_trip(tmp_path: Path) -> None:
    env = Env(tmp_path)
    beat = HeartbeatFile(tmp_path / "run" / "tradingd.heartbeat")
    assert beat.last() is None
    beat.beat(env.clock())
    assert beat.last() == env.clock()
    beat.beat(env.clock() + timedelta(seconds=10))
    assert beat.last() == env.clock() + timedelta(seconds=10)
    assert [p.name for p in (tmp_path / "run").iterdir()] == ["tradingd.heartbeat"]  # 不留临时文件


@pytest.mark.parametrize("content", ["", "garbage", "2026-10-28T12:00:00"])
def test_bad_heartbeat_content_counts_as_no_heartbeat(tmp_path: Path, content: str) -> None:
    path = tmp_path / "hb"
    path.write_text(content, encoding="utf-8")
    assert HeartbeatFile(path).last() is None


def test_naive_heartbeat_is_refused(tmp_path: Path) -> None:
    from datetime import datetime  # noqa: PLC0415

    with pytest.raises(ValueError, match="时区"):
        HeartbeatFile(tmp_path / "hb").beat(datetime(2026, 10, 28, 12, 0))  # noqa: DTZ001
