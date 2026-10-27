"""R-02b 端到端：build_rule_set 的阈值规则经 RiskEngine 汇总。

多条缩量取最小、日内亏损 HALT 持久化、改单缩量改判拒绝。

阈值是测试夹具，不是 Dylan 批准的参数。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
import pytest_asyncio
from server.domain.account import CashBalance
from server.domain.enums import Mode, RiskVerdict
from server.risk.engine import RiskContext, RiskEngine, Subject
from server.risk.mode_manager import ModeConfig, ModeManager
from server.risk.parameters import SPECS, Kind, RiskParameters
from server.risk.rulesets import build_rule_set
from tests.support.oms import ACCOUNT, SPY, Env, market

V = RiskVerdict
FIXTURE = {"max_order_notional_nav": "0.05", "max_position_etf_nav": "0.04",
           "max_daily_loss_nav": "0.02", "max_open_orders": "10", "max_orders_per_minute": "10",
           "min_cash_buffer_nav": "0.01", "max_total_exposure_nav": "1",
           "max_limit_deviation": "0.05", "protective_stop_distance": "0.1"}  # fmt: skip
PARAMS = RiskParameters(
    version="fixture",
    values=MappingProxyType(
        {
            s.name: Decimal(FIXTURE.get(s.name, "0.5" if s.kind is Kind.RATIO else "10"))
            for s in SPECS
        }
    ),
)


class Context:
    def __init__(self, env: Env, **overrides: Any) -> None:
        self.env, self.overrides = env, overrides

    async def snapshot(self, subject: Subject) -> RiskContext:
        data: dict[str, Any] = {
            "now": self.env.clock(),
            "positions": (),
            "open_orders": (),
            "cash": CashBalance(
                account_id=ACCOUNT.account_id,
                currency="USD",
                settled=Decimal(100000),
                unsettled=Decimal(0),
                as_of=self.env.clock(),
            ),
            "instruments": {"SPY": SPY},
            **market(self.env.clock(), {"SPY": Decimal(100)}),
            "nav": Decimal(100000),
            "start_of_day_nav": Decimal(100000),
            "strategy_drawdown": {"s": Decimal(0)},
            "recent_submissions": (),
            "stop_cooldown": {},
        }
        data.update(self.overrides)
        return RiskContext.model_validate(data)


def manager(env: Env) -> ModeManager:
    modes = ModeManager(env.repo, clock=env.clock)
    modes.start(
        ModeConfig(
            requested_mode=Mode.PAPER, account_id=ACCOUNT.account_id,
            expected_account_type=ACCOUNT.account_type, risk_regime=ACCOUNT.account_type,
            allow_delayed_data=False, config_version="risk-test",
        ),
        ACCOUNT,
    )  # fmt: skip
    return modes


def engine(env: Env, modes: ModeManager, **context: Any) -> RiskEngine:
    return RiskEngine(
        env.repo,
        env.om,
        modes,
        rule_set=lambda: build_rule_set(PARAMS),
        context=Context(env, **context),
        clock=env.clock,
    )


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> Env:
    e = Env(tmp_path)
    await e.broker.connect()
    await e.pump()
    return e


@pytest.mark.asyncio
async def test_several_reduces_take_the_smallest(env: Env) -> None:
    # 单笔上限 5000 → 50 股；单标的上限 4000 → 40 股：取 40
    outcome = await engine(env, manager(env)).submit(env.make_intent(quantity=Decimal(60)))
    assert outcome.decision.verdict is V.REDUCE
    assert outcome.decision.approved_quantity == Decimal(40)
    assert {h.rule_id for h in outcome.decision.rule_hits} == {"max_order_notional", "max_position"}
    issued = env.repo.issuance_for("i-1")
    assert issued is not None and issued.quantity == Decimal(40)


@pytest.mark.asyncio
async def test_daily_loss_halts_and_persists(env: Env) -> None:
    modes = manager(env)
    outcome = await engine(env, modes, nav=Decimal(97000)).submit(env.make_intent())
    assert outcome.decision.verdict is V.HALT and outcome.status is None
    state = modes.current()
    assert state is not None and state.halt.halted
    after = await engine(env, modes).submit(env.make_intent("i-2"))
    assert after.decision.rule_hits[0].rule_id == "mode_gate"


@pytest.mark.asyncio
async def test_amendment_that_would_need_reducing_is_rejected(env: Env) -> None:
    modes = manager(env)
    await engine(env, modes).submit(env.make_intent(quantity=Decimal(30)))
    await env.pump()
    env.clock.advance(1)
    # 30 股改价到 104 仍在上限内；39 股改价到 104 = 4056 超出单标的上限 4000
    outcome = await engine(env, modes).amend(
        "i-1", amendment_id="am-1", new_price=Decimal(104), new_quantity=Decimal(30)
    )
    assert outcome.decision.verdict in {V.ALLOW, V.REDUCE}
    big = await engine(env, modes).submit(env.make_intent("i-2", quantity=Decimal(39)))
    await env.pump()
    env.clock.advance(1)
    raised = await engine(env, modes).amend(
        "i-2", amendment_id="am-2", new_price=Decimal(104), new_quantity=Decimal(39)
    )
    assert big.decision.verdict is V.ALLOW
    # 改单遇到缩量一律拒绝；拒绝必须来自单标的上限（简化上下文：挂单未计入，只验证改判逻辑）
    assert raised.decision.verdict is V.REJECT
    assert "max_position" in {h.rule_id for h in raised.decision.rule_hits}
    assert env.repo.amendments_for("i-2") == []
