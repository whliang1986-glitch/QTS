"""R-03：风控上下文提供者（真实账本 + FakeBroker）与数据门禁端到端。阈值是测试夹具。"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from server.broker.base import OrderRecord
from server.domain.enums import Mode, RiskVerdict, Side
from server.domain.market import Quote
from server.domain.order_state import OrderStatus
from server.risk.context import (
    BROKER_ORDER_MISMATCH,
    UNKNOWN_BROKER_ORDER,
    LedgerContextProvider,
)
from server.risk.emergency import EmergencyControls
from server.risk.engine import RiskEngine, Subject
from server.risk.mode_manager import ModeConfig, ModeManager
from server.risk.rulesets import build_rule_set
from server.storage.ledger import Amendment
from tests.integration.risk.test_limit_rules_engine import PARAMS
from tests.support.oms import ACCOUNT, SPY, Env

V = RiskVerdict


class Quotes:
    def __init__(self, env: Env) -> None:
        self.env = env
        self.age = timedelta(0)
        self.fail = False
        self.asked: list[list[str]] = []

    async def quotes(self, symbols: Collection[str]) -> Mapping[str, Quote]:
        self.asked.append(list(symbols))
        if self.fail:
            raise RuntimeError("feed")
        return {
            s: Quote(
                symbol=s,
                ts=self.env.clock() - self.age,
                bid=Decimal("99.99"),
                ask=Decimal("100.01"),
                last=Decimal(100),
                source="test",
                delayed=False,
            )
            for s in symbols
        }


class Rig:
    def __init__(self, env: Env, **overrides: Any) -> None:
        self.env = env
        self.quotes = Quotes(env)
        self.untrusted: list[str] = []
        self.synced: bool | None = True
        self.skew: int | None = 0
        kw: dict[str, Any] = {
            "account": env.broker,
            "broker_orders": env.broker,
            "quotes": self.quotes,
            "instruments": {"SPY": SPY},
            "account_synced": lambda: self.synced,
            "clock_skew_ms": self._skew,
            "start_of_day_nav": lambda _a: Decimal(200000),
            "strategy_drawdown": lambda: {"s": Decimal(0)},
            "stop_cooldown": dict,
            "delayed_data_allowed": lambda: False,
            "on_untrusted": self.untrusted.append,
            "clock": env.clock,
        }
        kw.update(overrides)
        self.provider = LedgerContextProvider(env.repo, env.om, **kw)

    async def _skew(self) -> int | None:
        return self.skew

    def engine(self) -> RiskEngine:
        modes = ModeManager(self.env.repo, clock=self.env.clock)
        modes.start(
            ModeConfig(
                requested_mode=Mode.PAPER, account_id=ACCOUNT.account_id,
                expected_account_type=ACCOUNT.account_type, risk_regime=ACCOUNT.account_type,
                allow_delayed_data=False, config_version="risk-test",
            ),
            ACCOUNT,
        )  # fmt: skip
        self.modes = modes
        return RiskEngine(
            self.env.repo,
            self.env.om,
            modes,
            rule_set=lambda: build_rule_set(PARAMS),
            context=self.provider,
            clock=self.env.clock,
        )


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> Env:
    e = Env(tmp_path)
    await e.broker.connect()
    await e.pump()
    return e


def subject(env: Env, **kw: Any) -> Subject:
    return Subject.new_order(env.make_intent("probe", **kw))


@pytest.mark.asyncio
async def test_snapshot_from_broker_and_ledger(env: Env) -> None:
    await env.submit("o-1")  # 本系统一张买单 10 @ 100
    await env.pump()
    rig = Rig(env)
    ctx = await rig.provider.snapshot(subject(env))
    assert ctx.positions is not None and [p.quantity for p in ctx.positions] == [Decimal(1000)]
    assert ctx.cash is not None
    assert ctx.open_orders is not None
    ((order),) = ctx.open_orders
    assert (order.intent_id, order.side, order.remaining_quantity, order.limit_price) == (
        "o-1", Side.BUY, Decimal(10), Decimal(100),
    )  # fmt: skip
    assert ctx.last_prices == {"SPY": Decimal(100)}
    assert ctx.nav == ctx.cash.total + Decimal(1000) * Decimal(100)
    assert ctx.start_of_day_nav == Decimal(200000)
    assert ctx.recent_submissions is not None and len(ctx.recent_submissions) == 1
    assert (ctx.account_synced, ctx.clock_skew_ms, ctx.delayed_data_allowed) == (True, 0, False)
    assert rig.quotes.asked == [["SPY"]]


@pytest.mark.asyncio
async def test_recent_submissions_only_cover_the_rate_window(env: Env) -> None:
    await env.submit("o-1")
    env.clock.advance(61)
    ctx = await Rig(env).provider.snapshot(subject(env))
    assert ctx.recent_submissions == ()


@pytest.mark.asyncio
async def test_terminal_and_foreign_account_orders_are_excluded(env: Env) -> None:
    await env.submit("o-1")
    await env.pump()
    await env.om.request_cancel("o-1")
    await env.pump()
    ctx = await Rig(env).provider.snapshot(subject(env))
    assert ctx.open_orders == ()


@pytest.mark.asyncio
async def test_open_order_view_is_conservative(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    await env.submit("o-1")
    await env.pump()
    amendments = [
        Amendment("a1", "o-1", "d", Decimal(101), Decimal(12), "t", "UNKNOWN"),
        Amendment("a2", "o-1", "d", Decimal(103), Decimal(15), "t", "REJECTED"),
        Amendment("a3", "o-1", "d", Decimal(99), Decimal(8), "t", "ACCEPTED"),
    ]
    monkeypatch.setattr(env.repo, "amendments_for", lambda _i: amendments)
    ctx = await Rig(env).provider.snapshot(subject(env))
    assert ctx.open_orders is not None
    (order,) = ctx.open_orders
    # 被拒的改单不算；数量取最大 12，买单价格取最高 101
    assert (order.remaining_quantity, order.limit_price) == (Decimal(12), Decimal(101))


@pytest.mark.asyncio
async def test_foreign_broker_order_makes_orders_untrusted(env: Env) -> None:
    env.broker.inject_foreign_order("SPY", Side.SELL, Decimal(5))
    rig = Rig(env)
    ctx = await rig.provider.snapshot(subject(env))
    assert ctx.open_orders is None
    await rig.provider.snapshot(subject(env))
    assert len(rig.untrusted) == 1  # 同一张只报告一次
    assert re.fullmatch(f"{UNKNOWN_BROKER_ORDER}：foreign-1/.+", rig.untrusted[0])


@pytest.mark.asyncio
async def test_locally_terminal_but_open_at_broker_rejects_without_safe_halt(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.submit("o-1")
    await env.pump()
    monkeypatch.setattr(env.om, "status", lambda _i: OrderStatus.CANCELLED)
    rig = Rig(env)
    ctx = await rig.provider.snapshot(subject(env))
    assert ctx.open_orders is None and rig.untrusted == []


@pytest.mark.asyncio
async def test_every_failing_source_becomes_none(env: Env) -> None:
    def boom(*_a: Any) -> Any:
        raise RuntimeError("x")

    async def aboom(*_a: Any) -> Any:
        raise RuntimeError("x")

    class Broken:
        positions = aboom
        cash = aboom
        open_orders = aboom

    rig = Rig(
        env, account=Broken(), broker_orders=Broken(), clock_skew_ms=aboom,
        account_synced=boom, start_of_day_nav=boom, strategy_drawdown=boom, stop_cooldown=boom,
        delayed_data_allowed=boom,
    )  # fmt: skip
    rig.quotes.fail = True
    ctx = await rig.provider.snapshot(subject(env))
    for field in (
        "positions", "open_orders", "cash", "last_prices", "nav", "start_of_day_nav",
        "strategy_drawdown", "stop_cooldown", "quotes", "clock_skew_ms", "account_synced",
    ):  # fmt: skip
        assert getattr(ctx, field) is None, field
    assert ctx.delayed_data_allowed is False


@pytest.mark.asyncio
async def test_nav_needs_every_price(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(env)

    async def no_last(symbols: Collection[str]) -> Mapping[str, Quote]:
        return {}

    monkeypatch.setattr(rig.quotes, "quotes", no_last)
    assert (await rig.provider.snapshot(subject(env))).nav is None


# ---------- 端到端：数据门禁经引擎拒绝 ----------


@pytest.mark.asyncio
async def test_fresh_data_allows_and_stale_quote_rejects(env: Env) -> None:
    rig = Rig(env)
    engine = rig.engine()
    reduce = {"side": Side.SELL, "order_class": "REDUCE", "limit_price": Decimal(100)}
    allowed = await engine.submit(env.make_intent("r-1", **reduce))
    assert allowed.decision.verdict is V.ALLOW, allowed.decision.rule_hits
    rig.quotes.age = timedelta(milliseconds=11)  # 夹具上限 10 毫秒
    stale = await engine.submit(env.make_intent("r-2", **reduce))
    assert stale.decision.verdict is V.REJECT
    assert [h.rule_id for h in stale.decision.rule_hits] == ["data_gate"]
    assert stale.status is None


@pytest.mark.asyncio
async def test_unsynced_account_rejects_end_to_end(env: Env) -> None:
    rig = Rig(env)
    rig.synced = None
    outcome = await rig.engine().submit(
        env.make_intent("r-1", side=Side.SELL, order_class="REDUCE", limit_price=Decimal(100))
    )
    assert [h.rule_id for h in outcome.decision.rule_hits] == ["data_gate"]


@pytest.mark.asyncio
async def test_symbols_without_spec_are_not_quoted(env: Env) -> None:
    rig = Rig(env)
    ctx = await rig.provider.snapshot(subject(env, symbol="SNAPSHOT"))
    assert rig.quotes.asked == [["SPY"]]  # 只取持仓标的；占位对象不取
    assert ctx.quotes is not None and set(ctx.quotes) == {"SPY"}


def test_delayed_data_flag_comes_from_verified_config(env: Env) -> None:
    modes = ModeManager(env.repo, clock=env.clock)
    assert modes.delayed_data_allowed() is False
    modes.start(
        ModeConfig(
            requested_mode=Mode.PAPER, account_id=ACCOUNT.account_id,
            expected_account_type=ACCOUNT.account_type, risk_regime=ACCOUNT.account_type,
            allow_delayed_data=True, config_version="risk-test",
        ),
        ACCOUNT,
    )  # fmt: skip
    assert modes.delayed_data_allowed() is True


@pytest.mark.asyncio
async def test_emergency_flatten_through_the_real_provider(env: Env) -> None:
    rig = Rig(env)
    engine = rig.engine()
    controls = EmergencyControls(
        env.repo, env.om, rig.modes, engine, context=rig.provider,
        parameters=lambda: PARAMS, clock=env.clock,
    )  # fmt: skip
    rig.modes.halt("急停", actor="dylan")
    preview = await controls.preview_flatten()
    (line,) = preview.lines
    assert (line.symbol, line.quantity, line.limit_price) == ("SPY", Decimal(1000), Decimal(95))
    (result,) = await controls.emergency_flatten(
        preview.preview_id, actor="dylan", confirmation=preview.confirmation
    )
    assert result.outcome is not None
    assert result.outcome.decision.verdict is V.ALLOW, result.outcome.decision.rule_hits


@pytest.mark.asyncio
async def test_open_orders_are_scoped_to_the_subject_account(env: Env) -> None:
    await env.submit("o-1")
    await env.pump()
    ctx = await Rig(env).provider.snapshot(subject(env, account_id="DU0000002"))
    assert ctx.open_orders == ()  # 别的账户的订单不计入（OpenOrder 没有账户字段，只能在这里过滤）


def _patch_broker_orders(env: Env, monkeypatch: pytest.MonkeyPatch, **changes: Any) -> None:
    real = env.broker.open_orders

    async def patched() -> list[OrderRecord]:
        return [OrderRecord.model_validate({**r.model_dump(), **changes}) for r in await real()]

    monkeypatch.setattr(env.broker, "open_orders", patched)


@pytest.mark.asyncio
async def test_order_issued_between_reads_is_not_foreign(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = env.broker.open_orders

    async def racing() -> list[OrderRecord]:
        await env.submit("o-2")  # 读取间隙里另一个协程签发并下单
        return await real()

    monkeypatch.setattr(env.broker, "open_orders", racing)
    rig = Rig(env)
    ctx = await rig.provider.snapshot(subject(env))
    assert rig.untrusted == []
    assert ctx.open_orders is not None and [o.intent_id for o in ctx.open_orders] == ["o-2"]


@pytest.mark.asyncio
async def test_failed_report_is_retried_and_new_foreign_orders_are_reported(env: Env) -> None:
    calls: list[str] = []

    def flaky(reason: str) -> None:
        calls.append(reason)
        if len(calls) == 1:
            raise RuntimeError("safe_halt write")

    rig = Rig(env, on_untrusted=flaky)
    env.broker.inject_foreign_order("SPY", Side.SELL, Decimal(5))
    await rig.provider.snapshot(subject(env))
    await rig.provider.snapshot(subject(env))  # 第一次报告失败：重试
    await rig.provider.snapshot(subject(env))  # 已报告成功：不再重复
    env.broker.inject_foreign_order("SPY", Side.SELL, Decimal(3))  # 又来一张：再报
    await rig.provider.snapshot(subject(env))
    pattern = [
        f"{UNKNOWN_BROKER_ORDER}：foreign-1/[^、]+",
        f"{UNKNOWN_BROKER_ORDER}：foreign-1/[^、]+",
        f"{UNKNOWN_BROKER_ORDER}：foreign-1/[^、]+、foreign-2/[^、]+",
    ]
    assert len(calls) == len(pattern)
    assert all(re.fullmatch(p, c) for p, c in zip(pattern, calls, strict=True)), calls


@pytest.mark.asyncio
async def test_other_account_broker_orders_are_ignored(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.broker.inject_foreign_order("SPY", Side.SELL, Decimal(5))
    _patch_broker_orders(env, monkeypatch, account_id="DU0000002")
    rig = Rig(env)
    ctx = await rig.provider.snapshot(subject(env))
    assert ctx.open_orders == () and rig.untrusted == []


@pytest.mark.asyncio
async def test_broker_side_symbol_mismatch_is_untrusted(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.submit("o-1")
    await env.pump()
    _patch_broker_orders(env, monkeypatch, side=Side.SELL)
    rig = Rig(env)
    assert (await rig.provider.snapshot(subject(env))).open_orders is None
    assert len(rig.untrusted) == 1 and rig.untrusted[0].startswith(BROKER_ORDER_MISMATCH)


@pytest.mark.asyncio
async def test_broker_facts_win_when_more_conservative(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.submit("o-1")  # 本地 10 @ 100
    await env.pump()
    _patch_broker_orders(  # 有人在 TWS 把单子改成 25 @ 102
        env, monkeypatch, remaining_quantity=Decimal(25), total_quantity=Decimal(25),
        price=Decimal(102),
    )  # fmt: skip
    ctx = await Rig(env).provider.snapshot(subject(env))
    assert ctx.open_orders is not None
    (order,) = ctx.open_orders
    assert (order.remaining_quantity, order.limit_price) == (Decimal(25), Decimal(102))


@pytest.mark.asyncio
async def test_sell_order_uses_lowest_price_and_partial_fill(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.submit("r-1", side=Side.SELL, order_class="REDUCE", quantity=Decimal(10))
    await env.pump()
    env.broker.fill(env.oid("r-1"), Decimal(4), Decimal(100))
    await env.pump()
    amendments = [Amendment("a1", "r-1", "d", Decimal(98), Decimal(10), "t", "UNKNOWN")]
    monkeypatch.setattr(env.repo, "amendments_for", lambda _i: amendments)
    ctx = await Rig(env).provider.snapshot(subject(env))
    assert ctx.open_orders is not None
    (order,) = ctx.open_orders
    assert (order.remaining_quantity, order.limit_price) == (Decimal(6), Decimal(98))


@pytest.mark.asyncio
async def test_ledger_errors_make_open_orders_unknown(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.submit("o-1")
    await env.pump()
    monkeypatch.setattr(env.repo, "get_intent", lambda _i: None)
    ctx = await Rig(env).provider.snapshot(subject(env))
    assert ctx.open_orders is None


@pytest.mark.asyncio
async def test_positions_of_other_accounts_are_dropped(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = env.broker.positions

    async def mixed() -> list[Any]:
        mine = await real()
        other = mine[0].model_validate({**mine[0].model_dump(), "account_id": "DU0000002"})
        return [*mine, other]

    monkeypatch.setattr(env.broker, "positions", mixed)
    ctx = await Rig(env).provider.snapshot(subject(env))
    assert ctx.positions is not None and [p.account_id for p in ctx.positions] == [
        ACCOUNT.account_id
    ]


@pytest.mark.asyncio
async def test_amendment_is_rejected_by_the_data_gate_end_to_end(env: Env) -> None:
    rig = Rig(env)
    engine = rig.engine()
    reduce = {"side": Side.SELL, "order_class": "REDUCE", "limit_price": Decimal(100)}
    assert (await engine.submit(env.make_intent("r-1", **reduce))).decision.verdict is V.ALLOW
    await env.pump()
    rig.synced = False
    outcome = await engine.amend(
        "r-1", amendment_id="am", new_price=Decimal("99.99"), new_quantity=Decimal(10)
    )
    assert [h.rule_id for h in outcome.decision.rule_hits] == ["data_gate"]


@pytest.mark.asyncio
async def test_two_broker_orders_for_one_intent_are_untrusted(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.submit("o-1")
    await env.pump()
    real = env.broker.open_orders

    async def doubled() -> list[OrderRecord]:
        (mine,) = await real()
        return [mine, mine]  # 同一意图在券商侧出现两张未结束订单（例如重复下单）

    monkeypatch.setattr(env.broker, "open_orders", doubled)
    rig = Rig(env)
    assert (await rig.provider.snapshot(subject(env))).open_orders is None
    assert len(rig.untrusted) == 1 and rig.untrusted[0].startswith(BROKER_ORDER_MISMATCH)


@pytest.mark.asyncio
async def test_foreign_order_borrowing_our_order_ref_is_foreign(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.submit("o-1")
    await env.pump()
    real = env.broker.open_orders

    async def impostor() -> list[OrderRecord]:
        (mine,) = await real()
        fake = OrderRecord.model_validate(
            {**mine.model_dump(), "broker_order_id": "999", "perm_id": "x-perm"}
        )
        return [mine, fake]  # 冒用本系统 orderRef 的外来单：订单号没登记在该意图名下

    monkeypatch.setattr(env.broker, "open_orders", impostor)
    rig = Rig(env)
    assert (await rig.provider.snapshot(subject(env))).open_orders is None
    assert len(rig.untrusted) == 1 and rig.untrusted[0].startswith(UNKNOWN_BROKER_ORDER)


@pytest.mark.asyncio
async def test_perm_id_mismatch_is_foreign(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    await env.submit("o-1")
    await env.pump()
    _patch_broker_orders(env, monkeypatch, perm_id="someone-else")
    rig = Rig(env)
    assert (await rig.provider.snapshot(subject(env))).open_orders is None
    assert rig.untrusted and rig.untrusted[0].startswith(UNKNOWN_BROKER_ORDER)


@pytest.mark.asyncio
async def test_order_of_another_account_intent_is_a_mismatch(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.submit("o-1")  # 意图属于 DU0000001
    await env.pump()
    _patch_broker_orders(env, monkeypatch, account_id="DU0000002")  # 券商说在 DU0000002 名下
    rig = Rig(env)
    ctx = await rig.provider.snapshot(subject(env, account_id="DU0000002"))
    assert ctx.open_orders is None
    assert rig.untrusted and rig.untrusted[0].startswith(BROKER_ORDER_MISMATCH)


@pytest.mark.asyncio
async def test_cash_of_another_account_is_not_used(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = env.broker.cash

    async def other() -> Any:
        mine = await real()
        return mine.model_validate({**mine.model_dump(), "account_id": "DU0000002"})

    monkeypatch.setattr(env.broker, "cash", other)
    ctx = await Rig(env).provider.snapshot(subject(env))
    assert ctx.cash is None and ctx.nav is None


@pytest.mark.asyncio
async def test_missing_broker_price_on_buy_becomes_unknown(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.submit("o-1")
    await env.pump()
    _patch_broker_orders(env, monkeypatch, price=None)
    ctx = await Rig(env).provider.snapshot(subject(env))
    assert ctx.open_orders is not None
    (order,) = ctx.open_orders
    assert order.limit_price is None  # 占用现金无法估计，交给规则拒绝
