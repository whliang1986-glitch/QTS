"""R-07：对账核心（真实账本 + FakeBroker）。未解释差异进入 SAFE HALT 并出报告；识别公司行动。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Collection, Sequence
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import server.risk.reconcile as recon_module
from server.broker.base import OrderRecord
from server.domain.enums import Mode, Side
from server.domain.order_state import OrderStatus
from server.risk.mode_manager import ModeConfig, ModeManager, ResumeChecks
from server.risk.reconcile import (
    ADOPT_PHRASE,
    RECON_ACTION,
    Code,
    CorporateAction,
    Reconciler,
    ReconError,
    ReconKind,
    Status,
)
from server.storage.ledger import Amendment
from tests.support.oms import ACCOUNT, T0, Env

K = ReconKind
ALL_OK = ResumeChecks(
    data_ok=True, account_ok=True, orders_ok=True, risk_ok=True, database_ok=True, reconciled=True
)


class Actions:
    def __init__(self) -> None:
        self.events: list[CorporateAction] = []
        self.fail = False

    async def actions(self, symbols: Collection[str]) -> Sequence[CorporateAction]:
        if self.fail:
            raise RuntimeError("feed")
        return [e for e in self.events if e.symbol in symbols]


class Rig:
    def __init__(self, env: Env, *, start: bool = True) -> None:
        self.env = env
        self.modes = ModeManager(env.repo, clock=env.clock)
        if start:
            self.modes.start(
                ModeConfig(
                    requested_mode=Mode.PAPER, account_id=ACCOUNT.account_id,
                    expected_account_type=ACCOUNT.account_type, risk_regime=ACCOUNT.account_type,
                    allow_delayed_data=False, config_version="risk-test",
                ),
                ACCOUNT,
            )  # fmt: skip
        self.actions = Actions()
        self.recon = Reconciler(
            env.repo,
            env.om,
            self.modes,
            account=env.broker,
            broker_orders=env.broker,
            executions=env.broker,
            corporate_actions=self.actions,
            clock=env.clock,
        )

    def halted(self) -> bool:
        state = self.modes.current()
        return state is not None and state.halt.halted

    async def adopt(self) -> list[str]:
        if not self.halted():
            self.modes.halt("对账前人工急停", actor="dylan")
        return await self.recon.adopt_broker_positions(actor="dylan", confirmation=ADOPT_PHRASE)

    async def baseline(self) -> None:
        """采用券商持仓为基准并恢复交易（多数用例的起点）。"""
        await self.adopt()
        assert (await self.recon.reconcile(K.STARTUP)).clean
        self.modes.resume(Mode.PAPER, actor="dylan", checks=ALL_OK)


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> Env:
    e = Env(tmp_path)
    await e.broker.connect()
    await e.pump()
    return e


def event(env: Env, event_id: str, numerator: int, denominator: int) -> CorporateAction:
    """一个刚生效的公司行动（晚于之前的基准，不晚于现在）。"""
    env.clock.advance(1)
    return CorporateAction(
        event_id=event_id, symbol="SPY", numerator=numerator, denominator=denominator,
        effective_at=env.clock(),
    )  # fmt: skip


def codes(report: Any, status: Status = Status.UNEXPLAINED) -> list[str]:
    return sorted(i.code.value for i in report.items if i.status is status)


# ---------- 基准 ----------


@pytest.mark.asyncio
async def test_fresh_ledger_does_not_match_broker_until_baseline_is_adopted(env: Env) -> None:
    rig = Rig(env)
    report = await rig.recon.reconcile(K.STARTUP)
    assert codes(report) == ["position_mismatch"] and not report.clean
    assert rig.halted() and not rig.modes.risk_reducing_allowed()  # SAFE HALT
    written = await rig.adopt()
    assert len(written) == 1
    assert rig.recon.expected_positions(ACCOUNT.account_id) == {"SPY": Decimal(1000)}
    again = await rig.recon.reconcile(K.STARTUP)
    assert again.clean and again.items == ()
    assert rig.recon.last_clean()
    assert rig.halted()  # 对账通过不自动恢复


@pytest.mark.asyncio
async def test_adopt_guards(env: Env) -> None:
    rig = Rig(env)
    with pytest.raises(ReconError, match="HALT"):
        await rig.recon.adopt_broker_positions(actor="dylan", confirmation=ADOPT_PHRASE)
    rig.modes.halt("x", actor="dylan")
    with pytest.raises(ReconError, match="人工"):
        await rig.recon.adopt_broker_positions(actor="tradingd", confirmation=ADOPT_PHRASE)
    with pytest.raises(ReconError, match="确认短语"):
        await rig.recon.adopt_broker_positions(actor="dylan", confirmation="确认")
    assert env.repo.position_adjustments(ACCOUNT.account_id) == []
    unverified = Rig(env, start=False)
    with pytest.raises(ReconError, match="启动校验"):
        await unverified.recon.adopt_broker_positions(actor="dylan", confirmation=ADOPT_PHRASE)


@pytest.mark.asyncio
async def test_own_fills_keep_positions_reconciled(env: Env) -> None:
    rig = Rig(env)
    await rig.baseline()
    await env.submit("o-1")  # 买 10
    await env.pump()
    env.broker.fill(env.oid("o-1"), Decimal(10), Decimal(100))
    await env.pump()
    assert rig.recon.expected_positions(ACCOUNT.account_id) == {"SPY": Decimal(1010)}
    report = await rig.recon.reconcile(K.INTRADAY)
    assert report.clean and not rig.halted()


# ---------- 持仓与公司行动 ----------


@pytest.mark.asyncio
async def test_unexplained_position_change_is_safe_halt(env: Env) -> None:
    rig = Rig(env)
    await rig.baseline()
    env.server.adjust_position("SPY", Decimal(-7))
    report = await rig.recon.reconcile(K.STARTUP)
    assert codes(report) == ["position_mismatch"]
    assert rig.halted() and not rig.modes.risk_reducing_allowed()
    audit = env.repo.audits(RECON_ACTION)[0]
    items = json.loads(json.loads(dict(audit.detail)["items"]))
    assert items[0]["code"] == "position_mismatch" and "预期 1000，券商 993" in items[0]["detail"]


@pytest.mark.asyncio
async def test_split_matching_corporate_action_is_explained_once(env: Env) -> None:
    rig = Rig(env)
    await rig.baseline()
    env.server.apply_split("SPY", Decimal(2), Decimal(1))
    rig.actions.events = [event(env, "ca-1", 2, 1)]
    report = await rig.recon.reconcile(K.END_OF_DAY)
    assert report.clean and not rig.halted()
    assert codes(report, Status.EXPLAINED) == ["corporate_action"]
    assert rig.recon.expected_positions(ACCOUNT.account_id) == {"SPY": Decimal(2000)}
    again = await rig.recon.reconcile(K.INTRADAY)  # 同一事件不再调整
    assert again.clean and codes(again, Status.EXPLAINED) == []
    assert len(env.repo.position_adjustments(ACCOUNT.account_id)) == 2


@pytest.mark.asyncio
async def test_reverse_split_with_cash_in_lieu_is_explained(env: Env) -> None:
    rig = Rig(env)
    env.server.adjust_position("SPY", Decimal(5))  # 1005 股
    await rig.baseline()
    env.server.adjust_position("SPY", Decimal(-905))  # 10 合 1：100.5 → 100，零股以现金补偿
    rig.actions.events = [event(env, "ca-2", 1, 10)]
    assert (await rig.recon.reconcile(K.STARTUP)).settled


@pytest.mark.asyncio
async def test_corporate_action_that_does_not_explain_is_safe_halt(env: Env) -> None:
    rig = Rig(env)
    await rig.baseline()
    env.server.adjust_position("SPY", Decimal(1001))  # 2001 股
    rig.actions.events = [event(env, "ca-3", 2, 1)]
    report = await rig.recon.reconcile(K.STARTUP)
    assert codes(report) == ["position_mismatch"]
    assert len(env.repo.position_adjustments(ACCOUNT.account_id)) == 1  # 没有写调整


@pytest.mark.asyncio
async def test_corporate_action_source_failure_fails_closed(env: Env) -> None:
    rig = Rig(env)
    await rig.baseline()
    env.server.apply_split("SPY", Decimal(2), Decimal(1))
    rig.actions.fail = True
    report = await rig.recon.reconcile(K.INTRADAY)  # 盘中也立即升级：数据源失败单独报出
    assert codes(report) == ["source_unavailable"] and rig.halted()
    assert codes(report, Status.PENDING) == ["position_mismatch"]


@pytest.mark.asyncio
async def test_negative_position_is_unexplained(env: Env) -> None:
    rig = Rig(env)
    await rig.baseline()
    env.server.adjust_position("SPY", Decimal(-1100))
    assert codes(await rig.recon.reconcile(K.STARTUP)) == [
        "negative_position",
        "position_mismatch",
    ]


# ---------- 成交 ----------


@pytest.mark.asyncio
async def test_foreign_execution_needs_acknowledgement(env: Env) -> None:
    rig = Rig(env)
    await rig.baseline()
    env.broker.inject_foreign_execution("SPY", Side.SELL, Decimal(3), Decimal(100))
    await env.pump()  # OMS 把它记为未匹配成交
    report = await rig.recon.reconcile(K.STARTUP)
    assert codes(report) == ["foreign_execution", "position_mismatch"]
    await rig.adopt()  # 人工确认：采用券商持仓，并确认这笔外来成交
    assert (await rig.recon.reconcile(K.STARTUP)).clean


@pytest.mark.asyncio
async def test_own_fill_missing_from_ledger(env: Env) -> None:
    rig = Rig(env)
    await rig.baseline()
    await env.submit("o-1")
    await env.pump()
    env.broker.fill(env.oid("o-1"), Decimal(10), Decimal(100))  # 回报还没处理
    report = await rig.recon.reconcile(K.STARTUP)
    assert "execution_missing" in codes(report)


# ---------- 订单 ----------


@pytest.mark.asyncio
async def test_foreign_open_order_is_unexplained(env: Env) -> None:
    rig = Rig(env)
    await rig.baseline()
    env.broker.inject_foreign_order("SPY", Side.SELL, Decimal(5))
    assert codes(await rig.recon.reconcile(K.INTRADAY)) == ["foreign_open_order"]


@pytest.mark.asyncio
async def test_unresolved_order_blocks(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(env)
    await rig.baseline()
    await env.submit("o-1")
    await env.pump()
    monkeypatch.setattr(env.om, "status", lambda _i: OrderStatus.UNKNOWN)
    assert codes(await rig.recon.reconcile(K.STARTUP)) == ["order_unresolved"]


@pytest.mark.asyncio
async def test_order_terms_mismatch(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(env)
    await rig.baseline()
    await env.submit("o-1")
    await env.pump()
    real = env.broker.open_orders

    async def changed() -> list[OrderRecord]:
        return [
            OrderRecord.model_validate(
                {**r.model_dump(), "remaining_quantity": Decimal(25), "total_quantity": Decimal(25)}
            )
            for r in await real()
        ]

    monkeypatch.setattr(env.broker, "open_orders", changed)
    report = await rig.recon.reconcile(K.STARTUP)
    assert codes(report) == ["order_mismatch"]
    assert "剩余数量不一致：本地 10，券商 25" in report.unexplained[0].detail


@pytest.mark.asyncio
async def test_timing_differences_need_two_intraday_sightings(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    await rig.baseline()
    await env.submit("o-1")
    await env.pump()

    async def nothing() -> list[OrderRecord]:
        return []

    monkeypatch.setattr(env.broker, "open_orders", nothing)
    first = await rig.recon.reconcile(K.INTRADAY)
    assert first.clean and codes(first, Status.PENDING) == ["local_open_missing_at_broker"]
    assert not rig.halted()
    second = await rig.recon.reconcile(K.INTRADAY)
    assert codes(second) == ["local_open_missing_at_broker"] and rig.halted()


@pytest.mark.asyncio
async def test_timing_differences_are_immediate_at_startup(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    await rig.baseline()
    await env.submit("o-1")
    await env.pump()
    monkeypatch.setattr(env.om, "status", lambda _i: OrderStatus.CANCELLED)
    assert codes(await rig.recon.reconcile(K.STARTUP)) == ["broker_open_local_terminal"]


# ---------- 失败即关闭与报告 ----------


@pytest.mark.asyncio
async def test_unverified_account_and_source_failure(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    unverified = Rig(env, start=False)
    assert codes(await unverified.recon.reconcile(K.STARTUP)) == ["account_unverified"]
    rig = Rig(env)

    async def broken(*_a: Any) -> Any:
        raise RuntimeError("down")

    monkeypatch.setattr(env.broker, "positions", broken)
    assert codes(await rig.recon.reconcile(K.STARTUP)) == ["source_unavailable"]
    assert not rig.recon.last_clean()


@pytest.mark.asyncio
async def test_end_of_day_marks_cash_and_commission_as_not_reconciled(env: Env) -> None:
    rig = Rig(env)
    await rig.baseline()
    report = await rig.recon.reconcile(K.END_OF_DAY)
    assert report.clean and codes(report, Status.INFO) == [Code.NOT_RECONCILED.value]


@pytest.mark.asyncio
async def test_executions_outside_lookback_are_not_requested(env: Env) -> None:
    rig = Rig(env)
    asked: list[Any] = []
    real = env.broker.executions

    async def spy(since: Any, *a: Any, **kw: Any) -> Any:
        asked.append(since)
        return await real(since, *a, **kw)

    rig.recon._executions = type("S", (), {"executions": staticmethod(spy)})()
    await rig.recon.reconcile(K.STARTUP)
    assert asked == [env.clock() - timedelta(days=7)]


# ---------- 补充：变异测试暴露的分支 ----------


def _patch_open_orders(env: Env, monkeypatch: pytest.MonkeyPatch, **changes: Any) -> None:
    real = env.broker.open_orders

    async def patched() -> list[OrderRecord]:
        return [OrderRecord.model_validate({**r.model_dump(), **changes}) for r in await real()]

    monkeypatch.setattr(env.broker, "open_orders", patched)


@pytest.mark.asyncio
async def test_duplicate_broker_orders_are_unexplained(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    await rig.baseline()
    await env.submit("o-1")
    await env.pump()
    real = env.broker.open_orders

    async def doubled() -> list[OrderRecord]:
        (mine,) = await real()
        return [mine, mine]

    monkeypatch.setattr(env.broker, "open_orders", doubled)
    assert codes(await rig.recon.reconcile(K.INTRADAY)) == ["duplicate_broker_order"]


@pytest.mark.asyncio
async def test_order_terms_follow_only_accepted_amendments(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    await rig.baseline()
    await env.submit("o-1")  # 10 @ 100
    await env.pump()
    rejected = [Amendment("a1", "o-1", "d", Decimal(105), Decimal(25), "t", "REJECTED")]
    monkeypatch.setattr(env.repo, "amendments_for", lambda _i: rejected)
    assert (await rig.recon.reconcile(K.STARTUP)).clean  # 被拒的改单不算
    accepted = [*rejected, Amendment("a2", "o-1", "d", Decimal(99), Decimal(8), "t", "ACCEPTED")]
    monkeypatch.setattr(env.repo, "amendments_for", lambda _i: accepted)
    _patch_open_orders(
        env, monkeypatch, remaining_quantity=Decimal(8), total_quantity=Decimal(8),
        price=Decimal(99),
    )  # fmt: skip
    assert (await rig.recon.reconcile(K.STARTUP)).clean


@pytest.mark.asyncio
async def test_foreign_fill_seen_only_at_broker(env: Env) -> None:
    rig = Rig(env)
    await rig.baseline()
    env.broker.inject_foreign_execution("SPY", Side.BUY, Decimal(2), Decimal(100))
    # 回报还没处理（账本里还没有未匹配成交）：券商成交本身就要被发现
    assert "foreign_execution" in codes(await rig.recon.reconcile(K.STARTUP))


@pytest.mark.asyncio
async def test_unmatched_execution_outside_lookback_still_needs_acknowledgement(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    await rig.baseline()
    env.repo.add_unmatched_execution("old-foreign", "no_order_ref", "{}", T0)

    async def none(*_a: Any, **_kw: Any) -> list[Any]:
        return []

    monkeypatch.setattr(env.broker, "executions", none)
    assert codes(await rig.recon.reconcile(K.STARTUP)) == ["foreign_execution"]


@pytest.mark.asyncio
async def test_sells_reduce_expected_position(env: Env) -> None:
    rig = Rig(env)
    await rig.baseline()
    await env.submit("r-1", side=Side.SELL, order_class="REDUCE", limit_price=Decimal(99))
    await env.pump()
    env.broker.fill(env.oid("r-1"), Decimal(10), Decimal(99))
    await env.pump()
    assert rig.recon.expected_positions(ACCOUNT.account_id) == {"SPY": Decimal(990)}
    assert (await rig.recon.reconcile(K.INTRADAY)).clean


@pytest.mark.asyncio
async def test_applied_corporate_action_is_not_reused(env: Env) -> None:
    rig = Rig(env)
    await rig.baseline()
    env.server.apply_split("SPY", Decimal(2), Decimal(1))
    rig.actions.events = [event(env, "ca-1", 2, 1)]
    assert (await rig.recon.reconcile(K.INTRADAY)).clean
    env.server.apply_split("SPY", Decimal(2), Decimal(1))  # 又翻倍，但事件已经用过
    assert codes(await rig.recon.reconcile(K.STARTUP)) == ["position_mismatch"]


@pytest.mark.asyncio
async def test_other_account_intents_are_excluded_from_expected_positions(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    await rig.baseline()
    await env.submit("o-1")
    await env.pump()
    env.broker.fill(env.oid("o-1"), Decimal(10), Decimal(100))
    await env.pump()
    real_get = env.repo.get_intent

    def foreign_account(intent_id: str) -> Any:
        intent = real_get(intent_id)
        if intent is None:
            return None
        return intent.model_validate({**intent.model_dump(), "account_id": "DU0000002"})

    monkeypatch.setattr(env.repo, "get_intent", foreign_account)
    assert rig.recon.expected_positions(ACCOUNT.account_id) == {"SPY": Decimal(1000)}


# ---------- 第 1 轮评审补充 ----------


@pytest.mark.asyncio
async def test_any_error_during_reconciliation_is_safe_halt(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    await rig.baseline()

    def broken() -> list[str]:
        raise RuntimeError("ledger")

    monkeypatch.setattr(env.repo, "submitted_intents", broken)
    report = await rig.recon.reconcile(K.INTRADAY)
    assert codes(report) == ["reconcile_failed"] and rig.halted()
    assert not rig.recon.last_clean()


@pytest.mark.asyncio
async def test_report_write_failure_still_halts(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(env)
    await rig.baseline()
    env.server.adjust_position("SPY", Decimal(-7))

    def broken(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("audit")

    monkeypatch.setattr(recon_module, "record_audit", broken)
    report = await rig.recon.reconcile(K.STARTUP)
    assert not report.clean and rig.halted()


@pytest.mark.asyncio
async def test_last_clean_needs_a_settled_report_after_the_latest_halt(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    assert not rig.recon.last_clean()  # 本实例还没对过账（不看账本里以前的报告）
    await rig.baseline()
    assert rig.recon.last_clean()
    env.clock.advance(1)
    rig.modes.halt("人工急停", actor="dylan")
    assert not rig.recon.last_clean()  # HALT 之后要重新对账
    env.clock.advance(1)
    assert (await rig.recon.reconcile(K.STARTUP)).clean
    assert rig.recon.last_clean()
    await env.submit("o-1")
    await env.pump()

    async def nothing() -> list[OrderRecord]:
        return []

    monkeypatch.setattr(env.broker, "open_orders", nothing)
    pending = await rig.recon.reconcile(K.INTRADAY)
    assert pending.clean and not pending.settled
    assert not rig.recon.last_clean()  # 有待观察项不算对账通过


@pytest.mark.asyncio
async def test_each_corporate_action_is_used_once_even_in_combination(env: Env) -> None:
    rig = Rig(env)
    await rig.baseline()
    env.server.apply_split("SPY", Decimal(10), Decimal(1))
    e1, e2 = event(env, "ca-1", 2, 1), event(env, "ca-2", 5, 1)
    rig.actions.events = [e1, e2]
    assert codes(await rig.recon.reconcile(K.STARTUP), Status.EXPLAINED) == ["corporate_action"]
    refs = sorted(a.reference for a in env.repo.position_adjustments(ACCOUNT.account_id)
                  if a.kind == "CORPORATE_ACTION")  # fmt: skip
    assert refs == ["ca-1:SPY", "ca-2:SPY"]  # 逐事件一条
    env.server.apply_split("SPY", Decimal(2), Decimal(1))
    rig.actions.events = [e1]  # 数据源只剩 ca-1：已经用过，不能再解释
    assert codes(await rig.recon.reconcile(K.STARTUP)) == ["position_mismatch"]


@pytest.mark.asyncio
async def test_events_before_baseline_or_in_the_future_are_ignored(env: Env) -> None:
    rig = Rig(env)
    old = CorporateAction(event_id="ca-old", symbol="SPY", numerator=2, denominator=1,
                          effective_at=env.clock())  # fmt: skip
    env.clock.advance(1)
    await rig.baseline()  # 基准晚于这个事件：事件已反映在基准里
    future = CorporateAction(event_id="ca-future", symbol="SPY", numerator=2, denominator=1,
                             effective_at=env.clock() + timedelta(days=1))  # fmt: skip
    env.server.apply_split("SPY", Decimal(2), Decimal(1))
    rig.actions.events = [old, future]
    assert codes(await rig.recon.reconcile(K.STARTUP)) == ["position_mismatch"]


@pytest.mark.asyncio
async def test_one_for_three_reverse_split_is_exact(env: Env) -> None:
    rig = Rig(env)
    env.server.adjust_position("SPY", Decimal(-1))  # 999 股
    await rig.baseline()
    env.server.adjust_position("SPY", Decimal(-666))  # 3 合 1：333 股
    rig.actions.events = [event(env, "ca-3", 1, 3)]
    assert (await rig.recon.reconcile(K.STARTUP)).clean
    assert rig.recon.expected_positions(ACCOUNT.account_id) == {"SPY": Decimal(333)}


@pytest.mark.asyncio
async def test_intraday_tolerates_normal_timing(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(env)
    await rig.baseline()
    await env.submit("o-1")
    await env.pump()
    env.broker.fill(env.oid("o-1"), Decimal(4), Decimal(100))  # 部分成交，回报尚未处理
    first = await rig.recon.reconcile(K.INTRADAY)
    assert first.clean and not rig.halted()
    assert codes(first, Status.PENDING) == [
        "execution_missing", "order_mismatch", "position_mismatch",
    ]  # fmt: skip
    await env.pump()  # 回报处理完
    assert (await rig.recon.reconcile(K.INTRADAY)).settled


@pytest.mark.asyncio
async def test_intraday_unknown_and_foreign_are_immediate(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    await rig.baseline()
    await env.submit("o-1")
    await env.pump()
    monkeypatch.setattr(env.om, "status", lambda _i: OrderStatus.UNKNOWN)
    assert codes(await rig.recon.reconcile(K.INTRADAY)) == ["order_unresolved"]


@pytest.mark.asyncio
async def test_intraday_submitting_is_pending_first(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    await rig.baseline()
    await env.submit("o-1")
    await env.pump()
    monkeypatch.setattr(env.om, "status", lambda _i: OrderStatus.SUBMITTING)
    first = await rig.recon.reconcile(K.INTRADAY)
    assert first.clean and codes(first, Status.PENDING) == ["order_submitting"]
    assert codes(await rig.recon.reconcile(K.INTRADAY)) == ["order_submitting"]


@pytest.mark.asyncio
async def test_flapping_difference_escalates(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(env)
    await rig.baseline()
    await env.submit("o-1")
    await env.pump()
    real = env.broker.open_orders

    async def nothing() -> list[OrderRecord]:
        return []

    monkeypatch.setattr(env.broker, "open_orders", nothing)
    assert (await rig.recon.reconcile(K.INTRADAY)).clean  # 第一次：待观察
    monkeypatch.setattr(env.broker, "open_orders", real)
    assert (await rig.recon.reconcile(K.INTRADAY)).clean  # 消失一次
    monkeypatch.setattr(env.broker, "open_orders", nothing)
    assert codes(await rig.recon.reconcile(K.INTRADAY)) == ["local_open_missing_at_broker"]


@pytest.mark.asyncio
async def test_pending_amendment_is_reported_and_its_terms_accepted(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    await rig.baseline()
    await env.submit("o-1")
    await env.pump()
    pending = [Amendment("a1", "o-1", "d", Decimal(99), Decimal(8), "t", None)]
    monkeypatch.setattr(env.repo, "amendments_for", lambda _i: pending)
    first = await rig.recon.reconcile(K.INTRADAY)  # 券商仍按签发值：条款相符，只报改单未决
    assert first.clean and not first.settled
    assert codes(first, Status.PENDING) == ["amendment_unresolved"]
    assert not rig.recon.last_clean()
    _patch_open_orders(
        env, monkeypatch, remaining_quantity=Decimal(8), total_quantity=Decimal(8),
        price=Decimal(99),
    )  # fmt: skip
    startup = await rig.recon.reconcile(K.STARTUP)  # 券商已按改单值：条款仍相符；启动时未决即升级
    assert codes(startup) == ["amendment_unresolved"]


@pytest.mark.asyncio
async def test_records_of_unknown_accounts_are_unexplained(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    await rig.baseline()
    real = env.broker.positions

    async def mixed() -> list[Any]:
        mine = await real()
        other = mine[0].model_validate({**mine[0].model_dump(), "account_id": "DU0000002"})
        return [*mine, other]

    monkeypatch.setattr(env.broker, "positions", mixed)
    assert codes(await rig.recon.reconcile(K.STARTUP)) == ["account_mismatch"]


@pytest.mark.asyncio
async def test_adopt_refuses_with_unresolved_orders_and_records_deltas(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    await env.submit("o-1")
    await env.pump()
    rig.modes.halt("x", actor="dylan")
    monkeypatch.setattr(env.om, "status", lambda _i: OrderStatus.UNKNOWN)
    with pytest.raises(ReconError, match="未决"):
        await rig.recon.adopt_broker_positions(actor="dylan", confirmation=ADOPT_PHRASE)
    monkeypatch.undo()
    env.broker.fill(env.oid("o-1"), Decimal(10), Decimal(100))  # 成交未入账
    with pytest.raises(ReconError, match="入账"):
        await rig.recon.adopt_broker_positions(actor="dylan", confirmation=ADOPT_PHRASE)
    await env.pump()
    await rig.recon.adopt_broker_positions(actor="dylan", confirmation=ADOPT_PHRASE)
    audit = env.repo.audits("reconciliation_baseline")[0]
    deltas = json.loads(json.loads(dict(audit.detail)["deltas"]))
    assert deltas == {"SPY": "1000"}  # 1010 股中 10 股来自本系统成交，基准只补 1000


# ---------- 第 2 轮评审补充 ----------


@pytest.mark.asyncio
async def test_safe_halt_failure_propagates_but_still_records_and_latches(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    await rig.baseline()
    env.server.adjust_position("SPY", Decimal(-7))
    real = env.repo.commit_mode_and_audits

    def broken(*a: Any, **kw: Any) -> Any:
        if a and any(getattr(e, "action", "") == "halt" for e in a[0]):
            raise RuntimeError("disk full")
        return real(*a, **kw)

    monkeypatch.setattr(env.repo, "commit_mode_and_audits", broken)
    with pytest.raises(RuntimeError, match="disk full"):
        await rig.recon.reconcile(K.STARTUP)
    assert not rig.recon.last_clean()
    assert not rig.modes.risk_reducing_allowed()  # 进程内闩锁：写不进 HALT 也拒绝
    monkeypatch.undo()
    assert env.repo.audits(RECON_ACTION)  # 报告照写


@pytest.mark.asyncio
async def test_adopt_refuses_when_records_belong_to_other_accounts(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    rig.modes.halt("x", actor="dylan")
    real = env.broker.positions

    async def mixed() -> list[Any]:
        mine = await real()
        other = mine[0].model_validate({**mine[0].model_dump(), "account_id": "DU0000002"})
        return [*mine, other]

    monkeypatch.setattr(env.broker, "positions", mixed)
    with pytest.raises(ReconError, match="已校验账户"):
        await rig.recon.adopt_broker_positions(actor="dylan", confirmation=ADOPT_PHRASE)


@pytest.mark.asyncio
async def test_corporate_action_not_matched_while_fills_are_unrecorded(env: Env) -> None:
    rig = Rig(env)
    await rig.baseline()
    await env.submit("o-1", quantity=Decimal(1000), limit_price=Decimal(100))
    await env.pump()
    env.broker.fill(env.oid("o-1"), Decimal(1000), Decimal(100))  # 2000 股，回报未处理
    rig.actions.events = [event(env, "ca-x", 2, 1)]  # 碰巧比例吻合
    report = await rig.recon.reconcile(K.STARTUP)
    assert "position_mismatch" in codes(report)
    assert all(a.kind != "CORPORATE_ACTION"
               for a in env.repo.position_adjustments(ACCOUNT.account_id))  # fmt: skip


@pytest.mark.asyncio
async def test_combined_events_use_integer_steps_and_broker_rounding(env: Env) -> None:
    rig = Rig(env)
    env.server.adjust_position("SPY", Decimal(-899))  # 101 股
    await rig.baseline()
    env.server.adjust_position("SPY", Decimal(125))  # 两次 3/2：101 → 151 → 226（券商逐次取整）
    rig.actions.events = [event(env, "ca-a", 3, 2), event(env, "ca-b", 3, 2)]
    assert (await rig.recon.reconcile(K.STARTUP)).settled
    adjustments = [a for a in env.repo.position_adjustments(ACCOUNT.account_id)
                   if a.kind == "CORPORATE_ACTION"]  # fmt: skip
    assert [a.quantity_delta for a in adjustments] == [Decimal(50), Decimal(75)]  # 中间值为整数
    assert rig.recon.expected_positions(ACCOUNT.account_id) == {"SPY": Decimal(226)}


@pytest.mark.asyncio
async def test_adopt_sees_a_fill_that_lands_while_fetching(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    await env.submit("o-1")
    await env.pump()
    rig.modes.halt("x", actor="dylan")
    real = env.broker.positions

    async def racing() -> list[Any]:
        env.broker.fill(env.oid("o-1"), Decimal(10), Decimal(100))  # 取持仓时恰好成交
        return await real()

    monkeypatch.setattr(env.broker, "positions", racing)
    with pytest.raises(ReconError, match="入账"):  # 先取持仓、再取成交：这笔成交一定被看到
        await rig.recon.adopt_broker_positions(actor="dylan", confirmation=ADOPT_PHRASE)


@pytest.mark.asyncio
async def test_cancelled_reconciliation_voids_the_previous_result(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    await rig.baseline()
    assert rig.recon.last_clean()
    hang = asyncio.Event()

    async def stuck() -> list[Any]:
        await hang.wait()
        return []

    monkeypatch.setattr(env.broker, "positions", stuck)
    task = asyncio.create_task(rig.recon.reconcile(K.INTRADAY))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not rig.recon.last_clean()  # 被取消的对账不能沿用上一次的“通过”


@pytest.mark.asyncio
async def test_fractional_holding_split_matches_exactly(env: Env) -> None:
    rig = Rig(env)
    env.server.adjust_position("SPY", Decimal("1.5"))  # 1001.5 股（券商侧零碎股）
    await rig.baseline()
    env.server.adjust_position("SPY", Decimal("-500.75"))  # 2 合 1 且券商保留零碎股：500.75
    rig.actions.events = [event(env, "ca-f", 1, 2)]
    assert (await rig.recon.reconcile(K.STARTUP)).settled


# ---------- 第 3 轮评审补充 ----------


@pytest.mark.asyncio
async def test_adopt_refuses_when_a_fill_is_booked_while_fetching(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    await env.submit("o-1")
    await env.pump()
    rig.modes.halt("x", actor="dylan")
    real = env.broker.executions

    async def booking(*a: Any, **kw: Any) -> Any:
        env.broker.fill(env.oid("o-1"), Decimal(10), Decimal(100))  # 持仓已取完，此时成交并入账
        await env.pump()
        return await real(*a, **kw)

    monkeypatch.setattr(env.broker, "executions", booking)
    with pytest.raises(ReconError, match="取数期间"):
        await rig.recon.adopt_broker_positions(actor="dylan", confirmation=ADOPT_PHRASE)
    assert env.repo.position_adjustments(ACCOUNT.account_id) == []


@pytest.mark.asyncio
async def test_halt_during_reconciliation_voids_the_result(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    await rig.baseline()
    rig.modes.halt("对账前急停", actor="dylan")
    real = env.broker.positions

    async def halting() -> list[Any]:
        rig.modes.halt("对账进行中又急停", actor="dylan")  # 同一时刻（冻结时钟）也能识别
        return await real()

    monkeypatch.setattr(env.broker, "positions", halting)
    assert (await rig.recon.reconcile(K.STARTUP)).settled
    assert not rig.recon.last_clean()
    monkeypatch.undo()
    assert (await rig.recon.reconcile(K.STARTUP)).settled
    assert rig.recon.last_clean()


@pytest.mark.asyncio
async def test_unresolved_amendment_escalates_on_second_intraday_sighting(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    await rig.baseline()
    await env.submit("o-1")
    await env.pump()
    pending = [Amendment("a1", "o-1", "d", Decimal(99), Decimal(8), "t", "UNKNOWN")]
    monkeypatch.setattr(env.repo, "amendments_for", lambda _i: pending)
    assert (await rig.recon.reconcile(K.INTRADAY)).clean
    assert codes(await rig.recon.reconcile(K.INTRADAY)) == ["amendment_unresolved"]


@pytest.mark.asyncio
async def test_terminal_order_with_old_unresolved_amendment_is_not_reported(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(env)
    await rig.baseline()
    await env.submit("o-1")
    await env.pump()
    await env.om.request_cancel("o-1")
    await env.pump()
    pending = [Amendment("a1", "o-1", "d", Decimal(99), Decimal(8), "t", None)]
    monkeypatch.setattr(env.repo, "amendments_for", lambda _i: pending)
    assert (await rig.recon.reconcile(K.STARTUP)).settled
