"""策略调度与端到端链路（T-01）：数据湖 → 策略 → outbox → 交易核心（风控、OMS）→ FakeBroker。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from server.app.strategies import (
    ScheduleError,
    StrategySchedule,
    decision_time,
    run_strategies,
)
from server.data.calendar import TradingCalendar
from server.data.fake_provider import FakeDataProvider, synthetic_daily_bars
from server.data.ingest import ingest_bars
from server.data.lake import DataLake
from server.domain.enums import OrderClass, RiskVerdict, Side
from server.domain.orders import OrderIntent
from server.ipc.outbox import Outbox, deliver_pending
from server.strategy.base import Proposal, StrategyContext

CALENDAR = TradingCalendar.nyse(start=date(2026, 9, 1), end=date(2026, 12, 31))
SCHEDULE = StrategySchedule(
    decision_offset=timedelta(minutes=28), run_window=timedelta(minutes=4), lookback_days=30,
    publish_lag=timedelta(hours=1),
)  # fmt: skip
DECIDED = datetime(2026, 10, 23, 13, 58, tzinfo=UTC)
NOW = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)  # 周五 10:00 美东，开盘 30 分钟
ACCOUNT_ID = "DU0000001"


@dataclass
class SellSome:
    """卖出部分持仓的演示策略：持仓里有 SPY 就以前收价卖出 qty 股。"""

    qty: int = 10
    price_bump: Decimal = Decimal(0)
    strategy_id: str = "demo"
    version: str = "v1"
    universe: frozenset[str] = field(default_factory=lambda: frozenset({"SPY"}))
    seen: list[StrategyContext] = field(default_factory=list)
    crash: bool = False

    def decide(self, context: StrategyContext) -> Sequence[Proposal]:
        self.seen.append(context)
        if self.crash:
            raise RuntimeError("策略缺陷")
        if context.positions.get("SPY", Decimal(0)) < self.qty:
            return []
        last = context.bars["SPY"][-1].close
        return [
            Proposal(
                symbol="SPY",
                side=Side.SELL,
                quantity=Decimal(self.qty),
                limit_price=(last + self.price_bump).quantize(Decimal("0.01")),
                order_class=OrderClass.REDUCE,
                reason="演示",
            )
        ]


async def _lake(tmp_path: Path) -> DataLake:
    lake = DataLake(tmp_path / "lake")
    bars = synthetic_daily_bars(
        "SPY", CALENDAR, date(2026, 9, 1), date(2026, 12, 31), source="fake"
    )
    provider = FakeDataProvider(bars={"SPY": bars}, clock=lambda: NOW)
    await ingest_bars(provider, lake, "SPY", date(2026, 9, 1), date(2026, 10, 23),
                      calendar=CALENDAR, publish_lag=timedelta(0), clock=lambda: NOW)  # fmt: skip
    return lake


def _run(lake: DataLake, outbox: Outbox, strategies: Sequence[Any], **kw: Any) -> Any:
    args: dict[str, Any] = {
        "lake": lake, "calendar": CALENDAR, "outbox": outbox, "schedule": SCHEDULE, "now": NOW,
        "account_id": ACCOUNT_ID, "positions": lambda: {"SPY": Decimal(1000)}, "blocked": (),
    }  # fmt: skip
    args.update(kw)
    return run_strategies(strategies, **args)


@pytest.mark.asyncio
async def test_strategy_sees_only_published_history_and_enqueues_once(tmp_path: Path) -> None:
    lake = await _lake(tmp_path)
    outbox = Outbox(tmp_path / "meta.db", clock=lambda: NOW)
    strategy = SellSome()
    report = _run(lake, outbox, [strategy])
    assert report.enqueued == ("demo:v1:2026-10-23:SPY:SELL:REDUCE",)
    context = strategy.seen[0]
    assert context.session_day == date(2026, 10, 23)
    assert context.bars["SPY"][-1].start.date() == date(2026, 10, 22)  # 当天的不可见
    assert context.positions == {"SPY": Decimal(1000)}
    assert context.decision_at == DECIDED  # 固定为决策时刻，不是调用时刻
    again = _run(lake, outbox, [SellSome()], now=NOW + timedelta(minutes=1))  # 晚一分钟重跑
    assert again.failed == {}
    assert again.enqueued == ()
    assert again.duplicates == ("demo:v1:2026-10-23:SPY:SELL:REDUCE",)  # 重跑不多下单
    assert [i.intent_id for i in outbox.pending()] == ["demo:v1:2026-10-23:SPY:SELL:REDUCE"]
    outbox.close()


@pytest.mark.asyncio
async def test_changed_content_for_the_same_key_is_refused(tmp_path: Path) -> None:
    lake = await _lake(tmp_path)
    outbox = Outbox(tmp_path / "meta.db", clock=lambda: NOW)
    _run(lake, outbox, [SellSome()])
    changed = _run(lake, outbox, [SellSome(price_bump=Decimal("0.05"))])
    assert "入队被拒" in changed.failed["demo"]
    assert len(outbox.pending()) == 1
    outbox.close()


@pytest.mark.asyncio
async def test_schedule_gates(tmp_path: Path) -> None:
    lake = await _lake(tmp_path)
    outbox = Outbox(tmp_path / "meta.db", clock=lambda: NOW)
    early = _run(lake, outbox, [SellSome()], now=datetime(2026, 10, 23, 13, 40, tzinfo=UTC))
    assert early.skipped == {"demo": "还没到决策时刻"}
    late = _run(lake, outbox, [SellSome()], now=datetime(2026, 10, 23, 14, 3, tzinfo=UTC))
    assert late.skipped == {"demo": "已过运行窗口"}
    closed = _run(lake, outbox, [SellSome()], now=datetime(2026, 10, 24, 15, 0, tzinfo=UTC))
    assert closed.skipped == {"demo": "不在交易时段内"}
    blocked = _run(lake, outbox, [SellSome()], blocked={"SPY"})
    assert "数据质量门禁阻断" in blocked.skipped["demo"]
    missing = _run(lake, outbox, [SellSome(universe=frozenset({"SPY", "QQQ"}))])
    assert "缺数据" in missing.skipped["demo"]
    # 数据湖只到 10-20：最新日线不是上一交易日 10-22，数据不新鲜
    stale_lake = DataLake(tmp_path / "stale")
    bars = synthetic_daily_bars(
        "SPY", CALENDAR, date(2026, 9, 1), date(2026, 10, 20), source="fake"
    )
    await ingest_bars(FakeDataProvider(bars={"SPY": bars}, clock=lambda: NOW), stale_lake, "SPY",
                      date(2026, 9, 1), date(2026, 10, 23), calendar=CALENDAR,
                      publish_lag=timedelta(0), clock=lambda: NOW)  # fmt: skip
    stale = _run(stale_lake, outbox, [SellSome()])
    assert "数据不新鲜" in stale.skipped["demo"]
    assert outbox.pending() == []
    outbox.close()


@pytest.mark.asyncio
async def test_a_failing_strategy_does_not_affect_others(tmp_path: Path) -> None:
    lake = await _lake(tmp_path)
    outbox = Outbox(tmp_path / "meta.db", clock=lambda: NOW)
    report = _run(lake, outbox, [SellSome(crash=True, strategy_id="broken"), SellSome()])
    assert "运行出错：RuntimeError" in report.failed["broken"]
    assert report.enqueued == ("demo:v1:2026-10-23:SPY:SELL:REDUCE",)
    outbox.close()


def test_decision_time_follows_the_calendar() -> None:
    assert decision_time(CALENDAR, date(2026, 10, 23), SCHEDULE) == DECIDED
    assert decision_time(CALENDAR, date(2026, 10, 24), SCHEDULE) is None  # 周六
    late = StrategySchedule(
        timedelta(hours=3, minutes=28), timedelta(minutes=4), 30, timedelta(hours=1)
    )
    with pytest.raises(ScheduleError, match="不早于收盘"):
        decision_time(CALENDAR, date(2026, 11, 27), late)  # 提前收市日 13:00 美东
    with pytest.raises(ScheduleError, match="不覆盖"):
        decision_time(CALENDAR, date(2027, 6, 1), SCHEDULE)
    window = timedelta(minutes=4)
    with pytest.raises(ScheduleError, match="开盘之后"):
        StrategySchedule(timedelta(0), window, 30, timedelta(hours=1))
    with pytest.raises(ScheduleError, match="回看天数"):
        StrategySchedule(timedelta(minutes=1), window, 0, timedelta(hours=1))
    with pytest.raises(ScheduleError, match="运行窗口"):
        StrategySchedule(timedelta(minutes=1), timedelta(minutes=5), 30, timedelta(hours=1))


# ---------- 端到端：策略意图经 outbox 投递到交易核心，风控放行后由 OMS 发到 FakeBroker ----------


class _CoreChannel:
    """模拟 tradingd 的 submit_intent 端点（与 server/ipc/protocol.py 的回答格式一致）。"""

    def __init__(self, core: Any) -> None:
        self.core = core

    async def submit_intent(self, intent: OrderIntent) -> dict[str, Any]:
        outcome = await self.core.submit_intent(intent)
        status = None if outcome.status is None else str(outcome.status.value)
        return {"ok": True, "result": {"intent_id": outcome.decision.intent_id,
                                       "verdict": str(outcome.decision.verdict.value),
                                       "status": status}}  # fmt: skip


@pytest.mark.asyncio
async def test_end_to_end_strategy_to_fake_broker(tmp_path: Path) -> None:
    from tests.integration.risk.test_core import Rig  # noqa: PLC0415
    from tests.support.oms import Env  # noqa: PLC0415

    env = Env(tmp_path / "env")
    await env.broker.connect()
    await env.pump()
    rig = Rig(env)
    await rig.bring_up()
    now = env.clock()
    lake = await _lake(tmp_path)
    outbox = Outbox(tmp_path / "meta.db", clock=env.clock)
    report = _run(lake, outbox, [SellSome()], now=now)
    assert report.enqueued == ("demo:v1:2026-10-23:SPY:SELL:REDUCE",)
    delivery = await deliver_pending(outbox, _CoreChannel(rig.core), max_age=timedelta(minutes=5))
    assert delivery.delivered == 1
    receipt = outbox.receipt("demo:v1:2026-10-23:SPY:SELL:REDUCE")
    assert receipt is not None
    assert receipt.verdict == RiskVerdict.ALLOW.value
    await env.pump()
    orders = await env.broker.open_orders()
    assert [(o.symbol, o.side, o.total_quantity) for o in orders] == [
        ("SPY", Side.SELL, Decimal(10))
    ]
    assert outbox.pending() == []
    outbox.close()


@pytest.mark.asyncio
async def test_a_strategy_with_broken_properties_does_not_stop_the_round(tmp_path: Path) -> None:
    class _Broken:
        strategy_id = "broken"
        version = "v1"

        @property
        def universe(self) -> frozenset[str]:
            raise RuntimeError("属性缺陷")

        def decide(self, context: StrategyContext) -> Sequence[Proposal]:
            return []

    lake = await _lake(tmp_path)
    outbox = Outbox(tmp_path / "meta.db", clock=lambda: NOW)
    report = _run(lake, outbox, [_Broken(), SellSome()])
    assert "意外错误 RuntimeError" in report.failed["broken"]
    assert report.enqueued == ("demo:v1:2026-10-23:SPY:SELL:REDUCE",)
    outbox.close()


def test_previous_session_skips_holidays_and_duplicate_ids_are_refused() -> None:
    from server.app.strategies import _previous_session  # noqa: PLC0415

    assert _previous_session(CALENDAR, date(2026, 11, 27)) == date(2026, 11, 25)  # 跨感恩节
    assert _previous_session(CALENDAR, date(2026, 10, 26)) == date(2026, 10, 23)  # 跨周末
    with pytest.raises(ScheduleError, match="重复的策略标识"):
        run_strategies(
            [SellSome(), SellSome()], lake=None, calendar=CALENDAR, outbox=None,  # type: ignore[arg-type]
            schedule=SCHEDULE, now=NOW, account_id=ACCOUNT_ID, positions=dict, blocked=(),
        )  # fmt: skip


@pytest.mark.asyncio
async def test_outbox_infrastructure_errors_propagate(tmp_path: Path) -> None:
    from server.ipc.outbox import OutboxError  # noqa: PLC0415

    class _BrokenOutbox:
        def enqueue(self, intent: OrderIntent) -> bool:
            raise OutboxError("数据库损坏")

    lake = await _lake(tmp_path)
    with pytest.raises(OutboxError, match="数据库损坏"):
        _run(lake, _BrokenOutbox(), [SellSome()])  # type: ignore[arg-type]
