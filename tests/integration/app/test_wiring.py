"""B-05w：数据来源接线的端到端验证。

真实 tradingd 入口、真实账本、真实进程间通道、注入 FakeBroker（不连 TWS，不代表 IBKR 联调）。
意图从 workerd 端点进来，经风控、OMS 到券商，成交回到账本；任何数据来源“不知道”都不会下单。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import pytest_asyncio
from server.app.__main__ import TimeWiring, production_time
from server.app.common import AdapterNotAvailableError
from server.app.scheduler import ScheduleSettings
from server.app.supervisor import EXIT_OK, EXIT_REFUSED
from server.app.tradingd import TradingdDeps, production_broker
from server.app.wiring import Feeds, UnavailableCorporateActions, health_reporter
from server.broker.ibkr.client import IbAsyncClient
from server.broker.ibkr.market_data import IbkrQuoteSource
from server.domain.enums import OrderClass, Side
from server.domain.order_state import OrderStatus
from server.ipc.protocol import Role
from server.risk.assembly import CoreInputs
from server.risk.config import parse_health_config
from server.risk.context import QuoteSource
from server.risk.reconcile import ADOPT_PHRASE, RECON_ACTION
from server.storage.config import load_toml

from tests.integration.app.test_tradingd import CALENDAR, CHANNEL, Daemon
from tests.integration.risk.test_context import Quotes
from tests.integration.risk.test_core import HEALTH
from tests.integration.risk.test_limit_rules_engine import PARAMS
from tests.integration.risk.test_reconcile import Actions, event
from tests.support.oms import Env

pytestmark = pytest.mark.skipif(CHANNEL is None, reason="当前平台的通道尚未实现")
CHECKED = ("account_sync", "quotes", "news", "model")
COMPONENTS = parse_health_config(load_toml(HEALTH, name="health"))


async def _in_sync() -> int | None:
    return 0


class Wired(Daemon):
    """用 Feeds 与真实的 TimeWiring 接线：默认全部可用，feeds 换掉某项，omit 让某项留成默认。"""

    def __init__(
        self,
        env: Env,
        tmp_path: Path,
        *,
        omit: str = "",
        checked: tuple[str, ...] = CHECKED,
        **feeds: Any,
    ) -> None:
        super().__init__(env, tmp_path)
        self.time = TimeWiring(CALENDAR, ScheduleSettings())
        self.quotes = Quotes(env)
        self.feeds = feeds
        self.omit = omit
        self.checked = checked

    def build_feeds(self) -> Feeds:
        available: dict[str, Any] = {
            "account_synced": lambda: True,
            "clock_skew_ms": _in_sync,
            "start_of_day_nav": lambda _a: Decimal(200000),
            "strategy_drawdown": lambda: {"s": Decimal(0)},
            "stop_cooldown": dict,
            "corporate_actions": Actions(),
        }
        return Feeds(
            mode_config=self.mode_config,
            instruments={"SPY": self.env.om._instruments["SPY"]},
            components=COMPONENTS,
            parameters=lambda: PARAMS,
            quotes=self.quotes,
            **{k: v for k, v in {**available, **self.feeds}.items() if k != self.omit},
        )

    def inputs(self) -> CoreInputs:
        return self.time.wire(self.build_feeds(), self.env.clock)

    def deps(self) -> TradingdDeps:
        checks = dict.fromkeys(self.checked, lambda: True)
        return replace(super().deps(), report_health=health_reporter(checks, COMPONENTS))

    async def recover(self, apid: Any) -> bool:
        """首次启动的人工流程：采用券商持仓作基准、对账、恢复。返回是否恢复成功。"""
        adopted = await apid.command(
            "c-adopt", "adopt_baseline", {"confirmation": ADOPT_PHRASE}, actor="dylan"
        )
        assert adopted["ok"] and adopted["result"]["ok"], adopted
        await apid.command("c-recon", "reconcile", {}, actor="dylan")
        resumed = await apid.command("c-resume", "resume", {}, actor="dylan")
        assert resumed["ok"], resumed
        self.resumed: dict[str, Any] = resumed["result"]
        return bool(self.resumed["ok"])


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    # 净值正好等于日初净值；小仓位：既需要人工采用基准，买入一股也不超单票上限
    yield Env(
        tmp_path, positions={"SPY": (Decimal(10), Decimal(100))}, settled_cash=Decimal(199000)
    )


async def _finish(daemon: Wired, *clients: Any) -> None:
    for client in clients:
        await client.close()
    daemon.stop.set()
    assert daemon.task is not None
    assert await asyncio.wait_for(daemon.task, 10) == EXIT_OK


async def _wait_for_status(env: Env, intent_id: str, status: OrderStatus) -> None:
    for _ in range(200):
        history = env.repo.order_history(intent_id)
        if history and history[-1][1] is status:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{intent_id} 没有到达 {status}：{env.repo.order_history(intent_id)}")


@pytest.mark.asyncio
async def test_intent_reaches_fake_broker_and_fill_returns_to_ledger(
    env: Env, tmp_path: Path
) -> None:
    daemon = Wired(env, tmp_path)
    daemon.start()
    worker = await daemon.connect(Role.WORKERD)
    apid = await daemon.connect(Role.APID)
    # 恢复之前：SAFE HALT，一张单都到不了券商
    early = await worker.submit_intent(env.make_intent("w-early", quantity=Decimal(1)))
    assert early["result"]["verdict"] in {"HALT", "REJECT"}
    assert env.server._open_orders() == []

    assert await daemon.recover(apid)
    answer = await worker.submit_intent(env.make_intent("w-1", quantity=Decimal(1)))
    assert answer["ok"] and answer["result"]["verdict"] == "ALLOW", [
        d.rule_hits for d in env.repo.risk_decisions_for("w-1")
    ]
    await _wait_for_status(env, "w-1", OrderStatus.SUBMITTED)
    orders = env.server._open_orders()
    assert len(orders) == 1

    # 同一 intent_id 重投：回答原决定，券商那边不多一张单
    again = await worker.submit_intent(env.make_intent("w-1", quantity=Decimal(1)))
    assert again["result"]["verdict"] == "ALLOW"
    assert len(env.server._open_orders()) == 1

    env.server.fill(orders[0].broker_order_id, Decimal(1), Decimal(100))
    await _wait_for_status(env, "w-1", OrderStatus.FILLED)
    fills = env.repo.effective_executions_for("w-1")
    assert [(f.quantity, f.price) for f in fills] == [(Decimal(1), Decimal(100))]
    await _finish(daemon, worker, apid)


def _rules(env: Env, intent_id: str) -> set[str]:
    return {h.rule_id for d in env.repo.risk_decisions_for(intent_id) for h in d.rule_hits}


def _recon_items(env: Env) -> list[dict[str, str]]:
    """最近一次对账报告的条目。"""
    audit = env.repo.audits(RECON_ACTION)[0]  # 从新到旧排列
    items: list[dict[str, str]] = json.loads(json.loads(dict(audit.detail)["items"]))
    return items


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("unknown", "rule"),
    [
        ("account_synced", "data_gate"),
        ("clock_skew_ms", "data_gate"),
        ("start_of_day_nav", "max_daily_loss"),
        ("strategy_drawdown", "max_strategy_drawdown"),
        ("stop_cooldown", "stop_cooldown"),
    ],
)
async def test_each_unknown_feed_blocks_new_risk(
    env: Env, tmp_path: Path, unknown: str, rule: str
) -> None:
    """只把这一项留成默认，其他都可用：恢复成功，意图被这一项对应的规则拦下，券商没有订单。"""
    daemon = Wired(env, tmp_path, omit=unknown)
    daemon.start()
    worker = await daemon.connect(Role.WORKERD)
    apid = await daemon.connect(Role.APID)
    assert await daemon.recover(apid)
    answer = await worker.submit_intent(env.make_intent("w-1", quantity=Decimal(1)))
    assert answer["ok"] and answer["result"]["verdict"] != "ALLOW", answer
    assert rule in _rules(env, "w-1")
    assert env.server._open_orders() == []
    await _finish(daemon, worker, apid)


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", ["account_synced", "clock_skew_ms"])
async def test_unknown_account_or_clock_also_blocks_reducing(
    env: Env, tmp_path: Path, unknown: str
) -> None:
    """账户同步或时钟未知（未 SAFE HALT）时，减仓单同样被拒。

    只有保护性止损能过 data_gate，由风控层的测试覆盖；SAFE HALT 下保护性止损也被拒。
    """
    daemon = Wired(env, tmp_path, omit=unknown)
    daemon.start()
    worker = await daemon.connect(Role.WORKERD)
    apid = await daemon.connect(Role.APID)
    assert await daemon.recover(apid)
    intent = env.make_intent(
        "w-r", side=Side.SELL, order_class=OrderClass.REDUCE, quantity=Decimal(1)
    )
    answer = await worker.submit_intent(intent)
    assert answer["result"]["verdict"] != "ALLOW"
    assert "data_gate" in _rules(env, "w-r")
    assert env.server._open_orders() == []
    await _finish(daemon, worker, apid)


@pytest.mark.asyncio
async def test_default_corporate_actions_cannot_explain_a_split(env: Env, tmp_path: Path) -> None:
    """默认没有公司行动来源：持仓一致时不取数，可以恢复；拆股对不上时取数报错，停下且恢复不了。"""
    daemon = Wired(env, tmp_path, omit="corporate_actions")
    daemon.start()
    worker = await daemon.connect(Role.WORKERD)
    apid = await daemon.connect(Role.APID)
    assert await daemon.recover(apid)

    env.server.apply_split("SPY", Decimal(2), Decimal(1))
    checked = await apid.command("c-recon-2", "reconcile", {}, actor="dylan")
    assert checked["result"]["detail"]["settled"] is False
    found = {(i["code"], i["key"]) for i in _recon_items(env)}
    assert ("source_unavailable", "corporate_actions") in found
    assert ("position_mismatch", "SPY") in found  # 读到的确实是拆股之后的这份报告
    answer = await worker.submit_intent(env.make_intent("w-1", quantity=Decimal(1)))
    assert answer["result"]["verdict"] != "ALLOW"
    assert env.server._open_orders() == []
    resumed = await apid.command("c-resume-2", "resume", {}, actor="dylan")
    assert resumed["result"]["ok"] is False
    await _finish(daemon, worker, apid)


@pytest.mark.asyncio
async def test_real_corporate_actions_explain_the_same_split(env: Env, tmp_path: Path) -> None:
    """对照：同样的拆股，来源正常时能解释。上一个用例的失败确实来自来源缺失。"""
    actions = Actions()
    daemon = Wired(env, tmp_path, corporate_actions=actions)
    daemon.start()
    apid = await daemon.connect(Role.APID)
    assert await daemon.recover(apid)

    env.server.apply_split("SPY", Decimal(2), Decimal(1))
    actions.events = [event(env, "ca-1", 2, 1)]
    checked = await apid.command("c-recon-2", "reconcile", {}, actor="dylan")
    assert checked["result"]["detail"]["settled"] is True
    await _finish(daemon, apid)


@pytest.mark.asyncio
async def test_unavailable_corporate_actions_raises() -> None:
    with pytest.raises(RuntimeError, match="公司行动"):
        await UnavailableCorporateActions().actions(["SPY"])


@pytest.mark.asyncio
async def test_quote_failure_blocks_new_risk(env: Env, tmp_path: Path) -> None:
    daemon = Wired(env, tmp_path)
    daemon.quotes.fail = True
    daemon.start()
    worker = await daemon.connect(Role.WORKERD)
    apid = await daemon.connect(Role.APID)
    assert await daemon.recover(apid)
    answer = await worker.submit_intent(env.make_intent("w-1", quantity=Decimal(1)))
    assert answer["result"]["verdict"] != "ALLOW"
    assert "data_gate" in _rules(env, "w-1")
    assert env.server._open_orders() == []
    await _finish(daemon, worker, apid)


@pytest.mark.asyncio
async def test_critical_component_without_a_check_cannot_resume(env: Env, tmp_path: Path) -> None:
    """行情组件没有健康检查：按“从未报告”算故障，恢复失败，意图被拒。"""
    daemon = Wired(env, tmp_path, checked=("account_sync", "news", "model"))
    daemon.start()
    worker = await daemon.connect(Role.WORKERD)
    apid = await daemon.connect(Role.APID)
    assert not await daemon.recover(apid)
    assert "data_ok" in daemon.resumed["detail"]["error"]
    answer = await worker.submit_intent(env.make_intent("w-1", quantity=Decimal(1)))
    assert answer["result"]["verdict"] != "ALLOW"
    assert env.server._open_orders() == []
    await _finish(daemon, worker, apid)


@pytest.mark.asyncio
async def test_news_without_a_check_cannot_resume(env: Env, tmp_path: Path) -> None:
    """news 非关键，但属于 DATA 类：没有检查时恢复同样失败（M1 接入时不能用常量 True 充数）。"""
    daemon = Wired(env, tmp_path, checked=("account_sync", "quotes", "model"))
    daemon.start()
    worker = await daemon.connect(Role.WORKERD)
    apid = await daemon.connect(Role.APID)
    assert not await daemon.recover(apid)
    assert "data_ok" in daemon.resumed["detail"]["error"]
    answer = await worker.submit_intent(env.make_intent("w-1", quantity=Decimal(1)))
    assert answer["result"]["verdict"] != "ALLOW"
    assert env.server._open_orders() == []
    await _finish(daemon, worker, apid)


@pytest.mark.asyncio
async def test_model_without_a_check_stays_degraded(env: Env, tmp_path: Path) -> None:
    """model 非关键、也不是 DATA 类：可以恢复，但一直 DEGRADED，未获准的策略新单被拒。"""
    daemon = Wired(env, tmp_path, checked=("account_sync", "quotes", "news"))
    daemon.start()
    worker = await daemon.connect(Role.WORKERD)
    apid = await daemon.connect(Role.APID)
    assert await daemon.recover(apid)
    answer = await worker.submit_intent(env.make_intent("w-1", quantity=Decimal(1)))
    assert answer["result"]["verdict"] != "ALLOW"
    assert _rules(env, "w-1") == {"run_state"}
    assert env.server._open_orders() == []
    await _finish(daemon, worker, apid)


@pytest.mark.asyncio
async def test_missing_critical_components_refuse_to_start(env: Env, tmp_path: Path) -> None:
    daemon = Wired(env, tmp_path)
    feeds = replace(daemon.build_feeds(), components=())
    daemon.build_feeds = lambda: feeds  # type: ignore[method-assign]
    assert await asyncio.wait_for(daemon.start(), 10) == EXIT_REFUSED


@pytest.mark.parametrize("name", ["broker", "account_sync", "quotes"])
def test_a_demoted_or_missing_critical_component_is_refused(
    env: Env, tmp_path: Path, name: str
) -> None:
    daemon = Wired(env, tmp_path)
    feeds = daemon.build_feeds()
    demoted = tuple(replace(c, critical=False) if c.name == name else c for c in COMPONENTS)
    missing = tuple(c for c in COMPONENTS if c.name != name)
    for components in (demoted, missing):
        with pytest.raises(ValueError, match=name):
            daemon.time.wire(replace(feeds, components=components), env.clock)


def _run_state() -> Mock:
    return Mock(spec_set=["report"])


def _reports(run_state: Mock) -> dict[str, bool]:
    return {c.args[0]: c.kwargs["ok"] for c in run_state.report.call_args_list}


def test_health_reporter_reports_only_what_it_checked() -> None:
    def broken() -> bool:
        raise RuntimeError("feed")

    report = health_reporter(
        {"quotes": lambda: True, "model": broken, "news": lambda: None},  # type: ignore[dict-item,return-value]
        COMPONENTS,
    )
    run_state = _run_state()
    report(run_state, Mock(connected=False))
    assert _reports(run_state) == {"broker": False, "quotes": True, "model": False, "news": False}


def test_health_reporter_broker_follows_the_connection() -> None:
    run_state = _run_state()
    health_reporter({}, COMPONENTS)(run_state, Mock(connected=True))
    assert _reports(run_state) == {"broker": True}


def test_health_reporter_treats_a_failing_connection_read_as_down() -> None:
    class Broken:
        @property
        def connected(self) -> bool:
            raise RuntimeError("断了")

    run_state = _run_state()
    health_reporter({}, COMPONENTS)(run_state, Broken())
    assert _reports(run_state) == {"broker": False}


def test_health_reporter_refuses_a_custom_broker_check() -> None:
    with pytest.raises(ValueError, match="broker"):
        health_reporter({"broker": lambda: True}, COMPONENTS)


def test_health_reporter_refuses_unregistered_components() -> None:
    with pytest.raises(ValueError, match="typo"):
        health_reporter({"typo": lambda: True}, COMPONENTS)
    without_broker = tuple(c for c in COMPONENTS if c.name != "broker")
    with pytest.raises(ValueError, match="broker"):  # 券商组件总要上报，也必须已登记
        health_reporter({}, without_broker)


def test_wired_time_comes_from_the_same_calendar_and_window(env: Env, tmp_path: Path) -> None:
    daemon = Wired(env, tmp_path)
    inputs = daemon.inputs()
    assert inputs.broker_window == daemon.time.broker_window
    assert inputs.session_day == daemon.time.calendar.session_day
    assert inputs.clock is env.clock
    scheduler = daemon.time.scheduler(Mock())
    assert scheduler._calendar is daemon.time.calendar
    assert scheduler._settings is daemon.time.schedule


def test_ibkr_quote_source_fits_the_feed_slot() -> None:
    """只验证接口形状（mypy 检查这个赋值），不连接 TWS。"""
    quotes: QuoteSource = IbkrQuoteSource(IbAsyncClient())
    assert callable(quotes.quotes)


@pytest.mark.asyncio
async def test_production_entry_still_refuses(env: Env, tmp_path: Path) -> None:
    """接线就绪不等于接入生产：券商和数据来源两个入口都继续拒绝启动。"""
    daemon = Daemon(env, tmp_path, broker=production_broker)
    assert await asyncio.wait_for(daemon.start(), 10) == EXIT_REFUSED
    time = production_time(ScheduleSettings(), date(2026, 10, 28), tmp_path / "none.toml")
    with pytest.raises(AdapterNotAvailableError):
        time.inputs()
