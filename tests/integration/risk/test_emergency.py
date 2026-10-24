"""R-05：急停四级（停止新增、撤单保留止损、紧急平仓预览与确认）与 HALT 下的降风险通道。

阈值是测试夹具，不是 Dylan 批准的参数。
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from server.domain.account import Account, CashBalance, Position
from server.domain.enums import (
    AccountType,
    Mode,
    OrderClass,
    OrderType,
    RiskVerdict,
    Side,
    TimeInForce,
)
from server.domain.modes import AuditEvent, HaltState
from server.domain.order_state import OrderStatus
from server.domain.orders import EMERGENCY_FLATTEN_SOURCE, PROTECTIVE_STOP_SOURCE, OrderIntent
from server.oms.manager import CancelRefusedError
from server.risk.emergency import (
    EMERGENCY_ACTION,
    PROTECTIVE_CANCEL_PHRASE,
    EmergencyControls,
    EmergencyError,
)
from server.risk.engine import OpenOrder, RiskContext, RiskEngine, Subject
from server.risk.mode_manager import ModeConfig, ModeManager, ResumeChecks
from server.risk.parameters import RiskParameters
from server.risk.rulesets import build_rule_set
from tests.integration.risk.test_limit_rules_engine import PARAMS
from tests.support.oms import ACCOUNT, SPY, Env

V = RiskVerdict
S = OrderStatus


class Context:
    """可变的账户快照：测试按需改持仓与挂单。"""

    def __init__(self, env: Env) -> None:
        self.env = env
        self.positions: tuple[Position, ...] | None = (self.position(1000),)
        self.open_orders: tuple[OpenOrder, ...] = ()
        self.instruments: dict[str, Any] = {"SPY": SPY}
        self.open_orders_known = True
        self.last = Decimal(100)

    def position(self, qty: int) -> Position:
        return Position(
            account_id=ACCOUNT.account_id, symbol="SPY", quantity=Decimal(qty),
            average_cost=Decimal(100), as_of=self.env.clock(),
        )  # fmt: skip

    async def snapshot(self, subject: Subject) -> RiskContext:
        del subject
        await asyncio.sleep(0)  # 像真实提供者一样让出控制权，并发测试才有交错
        return RiskContext(
            now=self.env.clock(),
            positions=self.positions,
            open_orders=self.open_orders if self.open_orders_known else None,
            cash=CashBalance(
                account_id=ACCOUNT.account_id,
                currency="USD",
                settled=Decimal(100000),
                unsettled=Decimal(0),
                as_of=self.env.clock(),
            ),
            instruments=self.instruments,
            last_prices={"SPY": self.last},
            nav=Decimal(200000),
            start_of_day_nav=Decimal(200000),
            strategy_drawdown={"s": Decimal(0)},
            recent_submissions=(),
            stop_cooldown={},
        )


def config(**kw: Any) -> ModeConfig:
    data: dict[str, Any] = {
        "requested_mode": Mode.PAPER, "account_id": ACCOUNT.account_id,
        "expected_account_type": ACCOUNT.account_type, "risk_regime": ACCOUNT.account_type,
        "allow_delayed_data": False, "config_version": "risk-test",
    }  # fmt: skip
    data.update(kw)
    return ModeConfig.model_validate(data)


class Rig:
    def __init__(self, env: Env, params: RiskParameters | None = PARAMS) -> None:
        self.env = env
        self.ctx = Context(env)
        self.modes = ModeManager(env.repo, clock=env.clock)
        self.modes.start(config(), ACCOUNT)
        self.engine = RiskEngine(
            env.repo,
            env.om,
            self.modes,
            rule_set=lambda: build_rule_set(PARAMS),
            context=self.ctx,
            clock=env.clock,
        )
        ids = iter(f"p{n}" for n in range(100))
        self.controls = EmergencyControls(
            env.repo,
            env.om,
            self.modes,
            self.engine,
            context=self.ctx,
            parameters=lambda: params,
            clock=env.clock,
            new_id=lambda: next(ids),
        )


def stop_intent(env: Env, intent_id: str = "stop-1", **kw: Any) -> OrderIntent:
    data: dict[str, Any] = {
        "side": Side.SELL, "order_class": OrderClass.PROTECTIVE_STOP, "order_type": OrderType.STOP,
        "limit_price": None, "stop_price": Decimal(95), "quantity": Decimal(1000),
        "time_in_force": TimeInForce.GTC, "strategy_id": PROTECTIVE_STOP_SOURCE,
    }  # fmt: skip
    data.update(kw)
    return env.make_intent(intent_id, **data)


def reduce_intent(env: Env, intent_id: str = "r-1", **kw: Any) -> OrderIntent:
    data: dict[str, Any] = {
        "side": Side.SELL, "order_class": OrderClass.REDUCE, "quantity": Decimal(10),
        "limit_price": Decimal(99),
    }  # fmt: skip
    data.update(kw)
    return env.make_intent(intent_id, **data)


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> Env:
    e = Env(tmp_path)
    await e.broker.connect()
    await e.pump()
    return e


def gate(outcome: Any) -> str:
    return str(outcome.decision.rule_hits[0].rule_id)


# ---------- 1. 停止新增订单 ----------


@pytest.mark.asyncio
async def test_stop_new_orders_persists_and_audits(env: Env) -> None:
    rig = Rig(env)
    rig.controls.stop_new_orders("盘中异常", actor="dylan")
    assert gate(await rig.engine.submit(env.make_intent())) == "mode_gate"
    audit = env.repo.audits(EMERGENCY_ACTION)[0]
    assert audit.actor == "dylan" and dict(audit.detail)["operation"] == '"stop_new_orders"'
    restarted = ModeManager(env.repo, clock=env.clock)
    state = restarted.start(config(), ACCOUNT)
    assert state.halt.halted  # 重启不自动恢复


@pytest.mark.asyncio
async def test_stop_new_orders_halts_even_without_actor(env: Env) -> None:
    rig = Rig(env)
    rig.controls.stop_new_orders("x", actor="  ")  # 审计写不了（无操作者），HALT 照样生效
    state = rig.modes.current()
    assert state is not None and state.halt.halted
    with pytest.raises(EmergencyError, match="操作者"):
        await rig.controls.cancel_open_orders(actor="")  # 其他操作必须写明操作者


def test_stop_new_orders_survives_audit_failure(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(env)

    def broken(*args: Any, **kw: Any) -> None:
        raise RuntimeError("db")

    monkeypatch.setattr(rig.controls, "_audit", broken)
    rig.controls.stop_new_orders("x", actor="dylan")
    state = rig.modes.current()
    assert state is not None and state.halt.halted


# ---------- HALT 下的降风险通道 ----------


@pytest.mark.asyncio
async def test_halt_lets_only_risk_reducing_orders_through(env: Env) -> None:
    rig = Rig(env)
    rig.modes.halt("测试", actor="dylan")
    assert gate(await rig.engine.submit(env.make_intent())) == "mode_gate"
    assert gate(await rig.engine.submit(reduce_intent(env))) == "mode_gate"  # 普通减仓
    forged_open = env.make_intent("o-2", strategy_id=EMERGENCY_FLATTEN_SOURCE)
    assert gate(await rig.engine.submit(forged_open)) == "mode_gate"  # 冒用来源的开仓
    forged_stop = stop_intent(env, "stop-x", strategy_id="momentum")
    assert gate(await rig.engine.submit(forged_stop)) == "mode_gate"
    flatten = reduce_intent(env, "r-2", strategy_id=EMERGENCY_FLATTEN_SOURCE)
    assert (await rig.engine.submit(flatten)).decision.verdict is V.ALLOW
    assert (await rig.engine.submit(stop_intent(env))).decision.verdict is V.ALLOW


@pytest.mark.asyncio
async def test_halt_channel_still_applies_every_rule(env: Env) -> None:
    rig = Rig(env)
    rig.modes.halt("测试", actor="dylan")
    oversell = reduce_intent(env, quantity=Decimal(1001), strategy_id=EMERGENCY_FLATTEN_SOURCE)
    outcome = await rig.engine.submit(oversell)
    assert outcome.decision.verdict is V.REJECT and outcome.status is None
    assert [h.rule_id for h in outcome.decision.rule_hits] == ["default_deny"]  # 不做空
    far = reduce_intent(env, "r-2", limit_price=Decimal(90), strategy_id=EMERGENCY_FLATTEN_SOURCE)
    far_outcome = await rig.engine.submit(far)
    assert far_outcome.decision.verdict is V.REJECT
    assert [h.rule_id for h in far_outcome.decision.rule_hits] == ["max_limit_deviation"]


@pytest.mark.asyncio
async def test_halt_channel_refuses_amendments(env: Env) -> None:
    rig = Rig(env)
    flatten = reduce_intent(env, strategy_id=EMERGENCY_FLATTEN_SOURCE)
    assert (await rig.engine.submit(flatten)).decision.verdict is V.ALLOW
    await env.pump()
    rig.modes.halt("测试", actor="dylan")
    outcome = await rig.engine.amend(
        "r-1", amendment_id="am", new_price=Decimal("98.5"), new_quantity=Decimal(10)
    )
    assert gate(outcome) == "mode_gate"


def test_safe_halt_closes_the_channel_across_restarts(env: Env) -> None:
    margin = Account(account_id=ACCOUNT.account_id, account_type=AccountType.MARGIN,
                     base_currency="USD")  # fmt: skip
    modes = ModeManager(env.repo, clock=env.clock)
    modes.start(config(), margin)  # 登记 CASH、券商 MARGIN：SAFE HALT
    assert not modes.risk_reducing_allowed()
    modes.halt("人工再按一次急停", actor="dylan")  # 新的普通 HALT 不能洗掉 SAFE HALT
    assert not modes.risk_reducing_allowed()
    restarted = ModeManager(env.repo, clock=env.clock)
    restarted.start(config(), ACCOUNT)  # 账户类型恢复一致，但仍在 SAFE HALT 之后、未人工恢复
    assert not restarted.risk_reducing_allowed()


def test_channel_reopens_only_after_manual_resume(env: Env) -> None:
    rig = Rig(env)
    assert rig.modes.risk_reducing_allowed()
    other = Account(account_id="DU0000002", account_type=AccountType.CASH, base_currency="USD")
    rig.modes.observe_account(other)  # 重连换了账户：闩锁 + SAFE HALT
    assert not rig.modes.risk_reducing_allowed()
    fresh = ModeManager(env.repo, clock=env.clock)
    assert not fresh.risk_reducing_allowed()  # 未完成启动校验
    fresh.start(config(), ACCOUNT)
    assert not fresh.risk_reducing_allowed()


ALL_OK = ResumeChecks(
    data_ok=True, account_ok=True, orders_ok=True, risk_ok=True, database_ok=True, reconciled=True
)


def test_public_safe_halt_closes_channel_until_manual_resume(env: Env) -> None:
    rig = Rig(env)
    rig.modes.safe_halt("OMS 订单状态查不清", actor="system")
    assert not rig.modes.risk_reducing_allowed()
    restarted = ModeManager(env.repo, clock=env.clock)
    restarted.start(config(), ACCOUNT)  # 账户一致，重启时不会重新写 SAFE HALT
    assert not restarted.risk_reducing_allowed()  # 旧的 SAFE HALT 仍有效
    restarted.halt("再按一次", actor="dylan")
    assert not restarted.risk_reducing_allowed()
    restarted.resume(Mode.PAPER, actor="dylan", checks=ALL_OK)
    assert restarted.risk_reducing_allowed()
    restarted.halt("新的普通急停", actor="dylan")
    assert restarted.risk_reducing_allowed()  # 恢复后的普通 HALT 不再是 SAFE HALT


def test_halt_without_safe_marker_counts_as_safe(env: Env) -> None:
    rig = Rig(env)
    legacy = AuditEvent(
        event_id="legacy", occurred_at=env.clock(), actor="system", action="halt",
        correlation_id="legacy", detail=(("mode", "HALT"), ("reason", "旧版本写入")),
    )  # fmt: skip
    halt = HaltState(halted=True, reason="旧版本写入", since=env.clock(), set_by="system")
    env.repo.commit_mode_and_audits([legacy], ("m-legacy", Mode.HALT, halt, env.clock(), "system"))
    assert not rig.modes.risk_reducing_allowed()  # 缺标记按 SAFE HALT 处理（失败即关闭）


def test_channel_fails_closed_when_ledger_unreadable(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    rig.modes.halt("测试", actor="dylan")

    def broken(*args: Any) -> list[AuditEvent]:
        raise RuntimeError("db")

    monkeypatch.setattr(env.repo, "audits_among", broken)
    assert not rig.modes.risk_reducing_allowed()


def test_plain_halt_keeps_the_channel_open(env: Env) -> None:
    rig = Rig(env)
    rig.modes.halt("测试", actor="dylan")
    assert rig.modes.risk_reducing_allowed()
    restarted = ModeManager(env.repo, clock=env.clock)
    restarted.start(config(), ACCOUNT)
    assert restarted.risk_reducing_allowed()


# ---------- 2. 撤销未成交订单 ----------


async def _open_and_stop(env: Env, rig: Rig) -> None:
    opened = await rig.engine.submit(reduce_intent(env, "o-1"))  # 普通减仓挂单
    assert opened.decision.verdict is V.ALLOW, opened.decision.rule_hits
    assert (await rig.engine.submit(stop_intent(env))).decision.verdict is V.ALLOW
    await env.pump()


@pytest.mark.asyncio
async def test_cancel_open_orders_keeps_protective_stops(env: Env) -> None:
    rig = Rig(env)
    await _open_and_stop(env, rig)
    results = await rig.controls.cancel_open_orders(actor="dylan")
    assert [(r.intent_id, r.status) for r in results] == [("o-1", S.CANCEL_PENDING)]
    assert env.om.status("stop-1") is S.SUBMITTED
    assert rig.controls.unconfirmed_cancels() == ["o-1"]  # 请求发出不算成功
    await env.pump()
    assert env.om.status("o-1") is S.CANCELLED
    assert rig.controls.unconfirmed_cancels() == []


@pytest.mark.asyncio
async def test_cancelling_stops_needs_the_exact_phrase(env: Env) -> None:
    rig = Rig(env)
    await _open_and_stop(env, rig)
    with pytest.raises(EmergencyError, match="确认短语"):
        await rig.controls.cancel_protective_stops(actor="dylan", confirmation="确认")
    assert env.om.status("stop-1") is S.SUBMITTED
    results = await rig.controls.cancel_protective_stops(
        actor="dylan", confirmation=PROTECTIVE_CANCEL_PHRASE
    )
    assert [(r.intent_id, r.status) for r in results] == [("stop-1", S.CANCEL_PENDING)]
    assert env.om.status("o-1") is S.SUBMITTED  # 只撤止损
    await env.pump()
    assert env.om.status("stop-1") is S.CANCELLED
    operations = [dict(a.detail)["operation"] for a in env.repo.audits(EMERGENCY_ACTION)]
    assert operations == ['"cancel_protective_stops"']  # 短语不符的那次没有执行


@pytest.mark.asyncio
async def test_oms_protective_cancel_entry_only_takes_stops(env: Env) -> None:
    rig = Rig(env)
    await _open_and_stop(env, rig)
    with pytest.raises(CancelRefusedError, match="只撤保护性止损"):
        await env.om.request_cancel_protective("o-1")
    with pytest.raises(CancelRefusedError, match="保护性止损"):
        await env.om.request_cancel("stop-1")  # 普通入口仍不撤止损
    with pytest.raises(CancelRefusedError):
        await env.om.request_cancel_protective("missing")


@pytest.mark.asyncio
async def test_unconfirmed_stop_is_not_cancelled(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(env)
    await _open_and_stop(env, rig)
    for unconfirmed in (S.SUBMITTING, S.UNKNOWN):
        monkeypatch.setattr(env.om, "status", lambda _i, u=unconfirmed: u)
        with pytest.raises(CancelRefusedError, match="未确认"):
            await env.om.request_cancel_protective("stop-1")


@pytest.mark.asyncio
async def test_one_failing_cancel_does_not_stop_the_round(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    await _open_and_stop(env, rig)
    await rig.engine.submit(reduce_intent(env, "o-2", limit_price=Decimal("99.5")))
    await env.pump()
    real = env.om.request_cancel

    async def flaky(intent_id: str) -> OrderStatus:
        if intent_id == "o-1":
            raise RuntimeError("broker")
        return await real(intent_id)

    monkeypatch.setattr(env.om, "request_cancel", flaky)
    results = await rig.controls.cancel_open_orders(actor="dylan")
    assert [(r.intent_id, r.status, r.error) for r in results] == [
        ("o-1", None, "RuntimeError"),
        ("o-2", S.CANCEL_PENDING, None),
    ]


# ---------- 4. 紧急平仓 ----------


@pytest.mark.asyncio
async def test_flatten_preview_then_confirm(env: Env) -> None:
    rig = Rig(env)
    rig.ctx.open_orders = (
        OpenOrder(intent_id="r-0", symbol="SPY", side=Side.SELL, order_class=OrderClass.REDUCE,
                  remaining_quantity=Decimal(300), limit_price=Decimal(99)),
    )  # fmt: skip
    rig.modes.halt("急停", actor="dylan")
    preview = await rig.controls.preview_flatten()
    (line,) = preview.lines
    # 已有 300 股减仓挂单不重复卖；保护价 = 100 × (1 − 0.05) = 95.00
    assert (line.symbol, line.quantity, line.limit_price) == ("SPY", Decimal(700), Decimal(95))
    assert line.max_slippage == Decimal(3500)
    assert preview.confirmation == "确认紧急平仓 SPY 700"
    with pytest.raises(EmergencyError, match="确认短语"):
        await rig.controls.emergency_flatten(preview.preview_id, actor="dylan", confirmation="是")
    (result,) = await rig.controls.emergency_flatten(
        preview.preview_id, actor="dylan", confirmation=preview.confirmation
    )
    outcome = result.outcome
    assert outcome is not None and result.error is None
    assert outcome.decision.verdict is V.ALLOW and outcome.status is S.SUBMITTING
    intent = env.repo.get_intent(outcome.decision.intent_id)
    assert intent is not None
    assert (intent.strategy_id, intent.order_class, intent.order_type, intent.side) == (
        EMERGENCY_FLATTEN_SOURCE, OrderClass.REDUCE, OrderType.LIMIT, Side.SELL,
    )  # fmt: skip
    assert (intent.quantity, intent.limit_price) == (Decimal(700), Decimal(95))
    with pytest.raises(EmergencyError, match="重新预览"):  # 一次预览只执行一次
        await rig.controls.emergency_flatten(
            preview.preview_id, actor="dylan", confirmation=preview.confirmation
        )


@pytest.mark.asyncio
async def test_flatten_refuses_when_positions_changed(env: Env) -> None:
    rig = Rig(env)
    preview = await rig.controls.preview_flatten()
    rig.ctx.positions = (rig.ctx.position(400),)
    with pytest.raises(EmergencyError, match="已变化"):
        await rig.controls.emergency_flatten(
            preview.preview_id, actor="dylan", confirmation=preview.confirmation
        )
    assert env.repo.submitted_intents() == []


@pytest.mark.asyncio
async def test_flatten_refused_in_safe_halt(env: Env) -> None:
    rig = Rig(env)
    preview = await rig.controls.preview_flatten()
    rig.modes.observe_account(
        Account(account_id="DU0000002", account_type=AccountType.CASH, base_currency="USD")
    )
    (result,) = await rig.controls.emergency_flatten(
        preview.preview_id, actor="dylan", confirmation=preview.confirmation
    )
    assert result.outcome is not None
    assert gate(result.outcome) == "mode_gate" and result.outcome.status is None
    with pytest.raises(EmergencyError, match="重新预览"):  # 被拒的预览也不能再执行
        await rig.controls.emergency_flatten(
            preview.preview_id, actor="dylan", confirmation=preview.confirmation
        )


@pytest.mark.asyncio
async def test_flatten_fails_closed_without_inputs(env: Env) -> None:
    with pytest.raises(EmergencyError, match="风控参数"):
        await Rig(env, params=None).controls.preview_flatten()
    rig = Rig(env)
    rig.ctx.positions = None
    with pytest.raises(EmergencyError, match="不可用"):
        await rig.controls.preview_flatten()
    rig = Rig(env)
    rig.ctx.instruments = {}
    with pytest.raises(EmergencyError, match="最小跳动"):
        await rig.controls.preview_flatten()
    rig = Rig(env)
    rig.ctx.open_orders_known = False
    with pytest.raises(EmergencyError, match="挂单"):
        await rig.controls.preview_flatten()
    rig = Rig(env)
    rig.ctx.positions = (rig.ctx.position(1000), rig.ctx.position(-1100))
    with pytest.raises(EmergencyError, match="负持仓"):
        await rig.controls.preview_flatten()


@pytest.mark.asyncio
async def test_flatten_aggregates_lots_and_rounds_price_up_to_tick(env: Env) -> None:
    rig = Rig(env)
    lot = Position(
        account_id=ACCOUNT.account_id, symbol="SPY", quantity=Decimal("10.5"),
        average_cost=Decimal(100), as_of=env.clock(),
    )  # fmt: skip
    rig.ctx.positions = (rig.ctx.position(1000), lot)
    rig.ctx.last = Decimal("100.03")  # × 0.95 = 95.0285 → 向上取到 95.03（不超出偏离上限）
    (line,) = (await rig.controls.preview_flatten()).lines
    assert (line.quantity, line.limit_price) == (Decimal(1010), Decimal("95.03"))


@pytest.mark.asyncio
async def test_flatten_rejects_foreign_positions(env: Env) -> None:
    rig = Rig(env)
    rig.ctx.positions = (
        Position(account_id="DU0000002", symbol="SPY", quantity=Decimal(5),
                 average_cost=Decimal(100), as_of=env.clock()),
    )  # fmt: skip
    with pytest.raises(EmergencyError, match="账户"):
        await rig.controls.preview_flatten()


@pytest.mark.asyncio
async def test_flatten_refused_while_protective_stop_is_live(env: Env) -> None:
    rig = Rig(env)
    assert (await rig.engine.submit(stop_intent(env))).decision.verdict is V.ALLOW
    await env.pump()
    with pytest.raises(EmergencyError, match="保护性止损"):
        await rig.controls.preview_flatten()
    await rig.controls.cancel_protective_stops(actor="dylan", confirmation=PROTECTIVE_CANCEL_PHRASE)
    with pytest.raises(EmergencyError, match="保护性止损"):
        await rig.controls.preview_flatten()  # 撤单请求发出但未确认，仍不平仓
    await env.pump()
    (line,) = (await rig.controls.preview_flatten()).lines
    assert line.quantity == Decimal(1000)


@pytest.mark.asyncio
async def test_flatten_preview_expires_and_price_changes_are_caught(env: Env) -> None:
    rig = Rig(env)
    stale = await rig.controls.preview_flatten()
    env.clock.advance(61)
    with pytest.raises(EmergencyError, match="过期"):
        await rig.controls.emergency_flatten(
            stale.preview_id, actor="dylan", confirmation=stale.confirmation
        )
    moved = await rig.controls.preview_flatten()
    rig.ctx.last = Decimal(98)  # 数量没变，保护价低于预览
    with pytest.raises(EmergencyError, match="已变化"):
        await rig.controls.emergency_flatten(
            moved.preview_id, actor="dylan", confirmation=moved.confirmation
        )
    assert env.repo.submitted_intents() == []


@pytest.mark.asyncio
async def test_concurrent_flatten_never_sells_twice(env: Env) -> None:
    rig = Rig(env)
    a = await rig.controls.preview_flatten()
    b = await rig.controls.preview_flatten()
    results = await asyncio.gather(
        rig.controls.emergency_flatten(a.preview_id, actor="dylan", confirmation=a.confirmation),
        rig.controls.emergency_flatten(a.preview_id, actor="dylan", confirmation=a.confirmation),
        rig.controls.emergency_flatten(b.preview_id, actor="dylan", confirmation=b.confirmation),
        return_exceptions=True,
    )
    done = [r for r in results if isinstance(r, list)]
    assert len(done) == 1 and [x.symbol for x in done[0]] == ["SPY"]
    assert sum(isinstance(r, EmergencyError) for r in results) == 2
    assert env.repo.submitted_intents() == [f"flatten-{a.preview_id}-SPY"]
    # 之后再预览：已挂的平仓单计入，不会重复卖
    assert (await rig.controls.preview_flatten()).lines == ()


@pytest.mark.asyncio
async def test_flatten_with_no_positions_is_empty(env: Env) -> None:
    rig = Rig(env)
    rig.ctx.positions = ()
    preview = await rig.controls.preview_flatten()
    assert preview.lines == () and preview.confirmation == "确认紧急平仓 无持仓"
    assert (
        await rig.controls.emergency_flatten(
            preview.preview_id, actor="dylan", confirmation=preview.confirmation
        )
        == []
    )


@pytest.mark.asyncio
async def test_flatten_proceeds_when_protective_price_rises(env: Env) -> None:
    rig = Rig(env)
    preview = await rig.controls.preview_flatten()
    rig.ctx.last = Decimal("100.50")  # 保护价 95.48 高于预览的 95.00：仍满足确认过的下限
    (result,) = await rig.controls.emergency_flatten(
        preview.preview_id, actor="dylan", confirmation=preview.confirmation
    )
    assert result.outcome is not None and result.outcome.decision.verdict is V.ALLOW
    intent = env.repo.get_intent(result.outcome.decision.intent_id)
    assert intent is not None and intent.limit_price == Decimal("95.48")


@pytest.mark.asyncio
async def test_flatten_line_error_is_reported_not_raised(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    preview = await rig.controls.preview_flatten()

    async def broken(intent: OrderIntent) -> Any:
        raise RuntimeError("submit")

    monkeypatch.setattr(rig.engine, "submit", broken)
    (result,) = await rig.controls.emergency_flatten(
        preview.preview_id, actor="dylan", confirmation=preview.confirmation
    )
    assert (result.symbol, result.outcome, result.error) == ("SPY", None, "RuntimeError")


@pytest.mark.asyncio
async def test_empty_actor_does_not_burn_the_preview(env: Env) -> None:
    rig = Rig(env)
    preview = await rig.controls.preview_flatten()
    with pytest.raises(EmergencyError, match="操作者"):
        await rig.controls.emergency_flatten(
            preview.preview_id, actor=" ", confirmation=preview.confirmation
        )
    results = await rig.controls.emergency_flatten(
        preview.preview_id, actor="dylan", confirmation=preview.confirmation
    )
    assert [r.symbol for r in results] == ["SPY"]


def test_stop_new_orders_propagates_halt_failure_and_still_audits(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)

    def broken(*args: Any, **kw: Any) -> None:
        raise RuntimeError("halt write")

    monkeypatch.setattr(rig.modes, "halt", broken)
    with pytest.raises(RuntimeError, match="halt write"):
        rig.controls.stop_new_orders("x", actor="dylan")
    operations = [dict(a.detail)["operation"] for a in env.repo.audits(EMERGENCY_ACTION)]
    assert operations == ['"stop_new_orders"']


@pytest.mark.asyncio
async def test_expired_previews_are_dropped(env: Env) -> None:
    rig = Rig(env)
    old = await rig.controls.preview_flatten()
    env.clock.advance(61)
    await rig.controls.preview_flatten()
    with pytest.raises(EmergencyError, match="没有这个平仓预览"):
        await rig.controls.emergency_flatten(
            old.preview_id, actor="dylan", confirmation=old.confirmation
        )


@pytest.mark.asyncio
async def test_missing_ledger_intent_fails_closed(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    await _open_and_stop(env, rig)
    monkeypatch.setattr(env.repo, "get_intent", lambda _i: None)
    with pytest.raises(EmergencyError, match="对账"):
        await rig.controls.preview_flatten()  # 少算挂单与止损会超卖，宁可拒绝
