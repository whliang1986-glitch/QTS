"""T-04：回测引擎（T-04a 装配隔离、T-04b 时间语义与防前视、T-04c 手工核算、T-04d 独立复算对照）。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest
from server.backtest import (
    BACKTEST_COMPONENTS,
    BacktestConfig,
    DualMomentumStrategy,
    LookaheadError,
    run_backtest,
    vectorized_dual_momentum_reference,
)
from server.broker.sim.kernel import FixedCommission
from server.data.calendar import Session, TradingCalendar
from server.domain.enums import AssetClass, Mode, OrderClass, RiskVerdict, Side
from server.domain.instrument import Instrument
from server.domain.market import MarketBar
from server.risk.assembly import CoreInputs, build_backtest_core, build_trading_core
from server.risk.mode_manager import ModeConfig, TradingHaltedError
from server.storage.ledger import LedgerRepository, open_ledger
from server.storage.migrate import upgrade
from server.strategy.base import Proposal, StrategyContext
from tests.integration.risk.test_context import Quotes
from tests.integration.risk.test_limit_rules_engine import PARAMS
from tests.integration.risk.test_reconcile import Actions
from tests.support.oms import ACCOUNT, SPY, Env

IEF = Instrument(
    symbol="IEF",
    asset_class=AssetClass.ETF,
    exchange="ARCA",
    currency="USD",
    tick_size=Decimal("0.01"),
    trading_calendar="XNYS",
)
INSTRUMENTS = {"SPY": SPY, "IEF": IEF}
CALENDAR = TradingCalendar.nyse(start=date(2026, 1, 1), end=date(2026, 6, 30))


def _make_bar(  # noqa: PLR0917
    session: Session, symbol: str, o: str, h: str, low: str, c: str
) -> MarketBar:
    return MarketBar(
        symbol=symbol,
        start=session.open,
        end=session.close,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal(1_000_000),
        source="test",
        adjustment_version="unadjusted",
    )


def _core_inputs(env: Env, mode: Mode = Mode.BACKTEST) -> CoreInputs:
    async def zero_skew() -> int | None:
        return 0

    return CoreInputs(
        mode_config=ModeConfig(
            requested_mode=mode,
            account_id=ACCOUNT.account_id,
            expected_account_type=ACCOUNT.account_type,
            risk_regime=ACCOUNT.account_type,
            allow_delayed_data=False,
            config_version="bt-test",
        ),
        instruments=INSTRUMENTS,
        components=BACKTEST_COMPONENTS,
        parameters=lambda: PARAMS,
        quotes=Quotes(env),
        account_synced=lambda: True,
        clock_skew_ms=zero_skew,
        start_of_day_nav=lambda _a: Decimal(100_000),
        strategy_drawdown=lambda: {"s": Decimal(0)},
        stop_cooldown=dict,
        corporate_actions=Actions(),
        broker_window=lambda _now: False,
        degraded_allowed={},
        session_day=CALENDAR.session_day,
        clock=env.clock,
    )


# ---------- T-04a：回测上下文隔离与风控同路径 ----------


@pytest.mark.asyncio
async def test_production_core_refuses_backtest_mode_and_backtest_core_enforces_guards(
    tmp_path: Path,
) -> None:
    """生产装配 build_trading_core 不允许 BACKTEST 下单；build_backtest_core 强制核验三条约束。"""
    env = Env(tmp_path, positions={})
    await env.broker.connect()
    await env.pump()
    prod = build_trading_core(env.repo, env.broker, _core_inputs(env, Mode.BACKTEST))
    for comp in BACKTEST_COMPONENTS:
        prod.run_state.report(comp.name, ok=True)
    await prod.core.start()
    with pytest.raises(TradingHaltedError, match="BACKTEST"):
        prod.modes.require_trading_allowed()

    # 1. 拒绝使用 ledger.db
    with pytest.raises(ValueError, match=r"ledger\.db"):
        build_backtest_core(
            env.repo,
            env.broker,
            _core_inputs(env),
            ledger_path=tmp_path / "ledger.db",
            process_role="workerd",
        )

    # 2. 拒绝非 workerd 进程角色
    bt_db = tmp_path / "bt-1.db"
    upgrade(bt_db)
    bt_repo = LedgerRepository(open_ledger(bt_db))
    with pytest.raises(ValueError, match="workerd"):
        build_backtest_core(
            bt_repo,
            env.broker,
            _core_inputs(env),
            ledger_path=bt_db,
            process_role="tradingd",
        )

    # 3. 拒绝非 FakeBroker 适配器
    class NotFakeBroker:
        account = env.broker.account

    with pytest.raises(ValueError, match="模拟券商"):
        build_backtest_core(
            bt_repo,
            NotFakeBroker(),
            _core_inputs(env),
            ledger_path=bt_db,
            process_role="workerd",
        )
    bt_repo._engine.dispose()


def test_production_modules_do_not_reference_backtest_core_or_flag() -> None:
    """架构锁（T-04a 方案 A）：生产装配、API、策略、AI、守卫层不得引用回测专用装配或开关。"""
    server_root = Path(__file__).resolve().parents[3] / "server"
    forbidden_layers = ("app", "api", "strategy", "ai", "guard", "oms", "data", "ipc", "platforms")
    for layer in forbidden_layers:
        for py in (server_root / layer).rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            assert "build_backtest_core" not in text, f"{py} 不得引用 build_backtest_core"
            assert "allow_backtest_mode" not in text, f"{py} 不得引用 allow_backtest_mode"


class _OversizedOrderStrategy:
    """故意提议超过单笔名义上限（5% NAV）的买单，验证风控在回测中同样拦截缩量。"""

    strategy_id = "oversized"
    version = "v1"
    universe = frozenset({"SPY"})

    def decide(self, context: StrategyContext) -> Sequence[Proposal]:
        if len(context.sessions) != 1:
            return ()
        return (
            Proposal(
                symbol="SPY",
                side=Side.BUY,
                quantity=Decimal(100),  # 100 * 100 = 10,000 > 5% * 100,000 (5,000)
                limit_price=Decimal("100.00"),
                order_class=OrderClass.OPEN,
                reason="超限测试",
            ),
        )


@pytest.mark.asyncio
async def test_risk_rules_enforce_limits_in_backtest() -> None:
    all_sess = list(CALENDAR.sessions_between(date(2026, 1, 2), date(2026, 1, 7)))
    bars = {"SPY": [_make_bar(s, "SPY", "100", "101", "99", "100") for s in all_sess]}
    cfg = BacktestConfig(
        experiment_id="exp-risk",
        account=ACCOUNT,
        initial_cash=Decimal(100_000),
        start=all_sess[1].day,
        end=all_sess[-1].day,
        instruments=INSTRUMENTS,
        parameters=PARAMS,
    )
    res = await run_backtest(_OversizedOrderStrategy(), bars, CALENDAR, cfg)
    assert len(res.outcomes) == 1
    assert res.outcomes[0].decision.verdict is RiskVerdict.REDUCE
    # 缩量到 4% * 100,000 / 100 = 40 股
    assert res.equity_curve[0].positions == {"SPY": Decimal(40)}


# ---------- T-04b：时间语义与前视泄漏检测 ----------


class _SpyVisibleBarsStrategy:
    strategy_id = "spy_bars"
    version = "v1"
    universe = frozenset({"SPY"})

    def __init__(self) -> None:
        self.seen_counts: list[int] = []

    def decide(self, context: StrategyContext) -> Sequence[Proposal]:
        visible = context.bars["SPY"]
        self.seen_counts.append(len(visible))
        for b in visible:
            assert b.end <= context.decision_at
        return ()


@pytest.mark.asyncio
async def test_time_semantics_and_lookahead_detection() -> None:
    sessions = list(CALENDAR.sessions_between(date(2026, 1, 2), date(2026, 1, 8)))
    bars = {"SPY": [_make_bar(s, "SPY", "100", "101", "99", "100") for s in sessions]}
    cfg = BacktestConfig(
        experiment_id="exp-time",
        account=ACCOUNT,
        initial_cash=Decimal(100_000),
        start=sessions[0].day,
        end=sessions[-1].day,
        instruments=INSTRUMENTS,
        parameters=PARAMS,
    )
    spy_strat = _SpyVisibleBarsStrategy()
    await run_backtest(spy_strat, bars, CALENDAR, cfg)
    # 第 1 天开盘后决策时只能看到 0 根已收盘 K 线，第 2 天看到 1 根，第 k 天看到 k-1 根
    assert spy_strat.seen_counts == list(range(len(sessions)))

    # 若数据源在决策时刻泄露当日尚未收盘的 K 线，必须被检测出来并抛 LookaheadError
    def leaky_feed(_decision_at: Any) -> Mapping[str, Sequence[MarketBar]]:
        return bars

    with pytest.raises(LookaheadError, match="前视泄漏"):
        await run_backtest(spy_strat, bars, CALENDAR, cfg, bar_feed=leaky_feed)


# ---------- T-04c：手工核算样例与 Parquet 落盘 ----------


class _ScriptedTwoEtfStrategy:
    """手工核算样例策略：
    - 第 1 个交易日（day 0）：按 100.00 买入 40 股 SPY（名义 4,000 ≤ 5% NAV）；
    - 第 2 个交易日（day 1）：按 50.00 买入 60 股 IEF（名义 3,000 ≤ 5% NAV）；
    - 第 3 个交易日（day 2）：按 105.00 卖出 20 股 SPY（减仓，T+1 在 day 3 结算）；
    - 第 4 个交易日（day 3）：不操作，观察卖出款结算与收盘净值。
    """

    strategy_id = "hand_calc"
    version = "v1"
    universe = frozenset({"SPY", "IEF"})

    def decide(self, context: StrategyContext) -> Sequence[Proposal]:
        day_idx = len(context.sessions) - 1
        if day_idx == 0:
            return (
                Proposal(
                    symbol="SPY",
                    side=Side.BUY,
                    quantity=Decimal(40),
                    limit_price=Decimal("100.00"),
                    order_class=OrderClass.OPEN,
                    reason="d0 buy SPY",
                ),
            )
        if day_idx == 1:
            return (
                Proposal(
                    symbol="IEF",
                    side=Side.BUY,
                    quantity=Decimal(60),
                    limit_price=Decimal("50.00"),
                    order_class=OrderClass.OPEN,
                    reason="d1 buy IEF",
                ),
            )
        if day_idx == 2:
            return (
                Proposal(
                    symbol="SPY",
                    side=Side.SELL,
                    quantity=Decimal(20),
                    limit_price=Decimal("105.00"),
                    order_class=OrderClass.REDUCE,
                    reason="d2 sell SPY",
                ),
            )
        return ()


@pytest.mark.asyncio
async def test_hand_calculated_multi_day_sample_matches_exact_decimals(tmp_path: Path) -> None:
    """T-04c：两只 ETF、4 个交易日、带佣金（每股 $0.005，最低 $1.00），逐日手算净值与现金。"""
    warmup = next(iter(CALENDAR.sessions_between(date(2026, 1, 2), date(2026, 1, 2))))
    sessions = list(CALENDAR.sessions_between(date(2026, 1, 5), date(2026, 1, 8)))
    assert len(sessions) == 4
    spy_ohlc = [
        ("100.00", "101.00", "99.50", "101.00"),  # d0: 买 40 @ 100.00，佣金 $1.00，收盘 101.00
        ("101.00", "103.00", "100.50", "102.00"),  # d1: 收盘 102.00
        ("105.00", "106.00", "104.00", "104.00"),  # d2: 卖 20 @ 105.00，佣金 $1.00，收盘 104.00
        ("104.00", "107.00", "103.50", "106.00"),  # d3: 卖款 2,100 结算，收盘 106.00
    ]
    ief_ohlc = [
        ("50.00", "50.50", "49.50", "50.00"),  # d0: 未持仓
        ("50.00", "51.00", "49.80", "51.00"),  # d1: 买 60 @ 50.00，佣金 $1.00，收盘 51.00
        ("51.00", "51.50", "50.50", "50.50"),  # d2: 收盘 50.50
        ("50.50", "52.00", "50.00", "51.50"),  # d3: 收盘 51.50
    ]
    bars = {
        "SPY": [_make_bar(warmup, "SPY", "100.00", "100.50", "99.50", "100.00")]
        + [_make_bar(sessions[i], "SPY", *spy_ohlc[i]) for i in range(4)],
        "IEF": [_make_bar(warmup, "IEF", "50.00", "50.50", "49.50", "50.00")]
        + [_make_bar(sessions[i], "IEF", *ief_ohlc[i]) for i in range(4)],
    }
    cfg = BacktestConfig(
        experiment_id="exp-hand-calc",
        account=ACCOUNT,
        initial_cash=Decimal(100_000),
        start=sessions[0].day,
        end=sessions[-1].day,
        instruments=INSTRUMENTS,
        parameters=PARAMS,
        commission=FixedCommission(),
        cost_model_name="per_share_default",
    )
    res = await run_backtest(_ScriptedTwoEtfStrategy(), bars, CALENDAR, cfg)
    assert len(res.equity_curve) == 4

    # 手工逐日核算：
    # d0: 初始 100,000 - 买 SPY 4,000 - 佣金 1 = 已结算 95,999，未结算 0；
    #     持仓市值 = 40 * 101 = 4,040；NAV = 100,039
    p0 = res.equity_curve[0]
    assert (p0.settled_cash, p0.unsettled_cash, p0.positions_value, p0.nav) == (
        Decimal("95999.00"),
        Decimal(0),
        Decimal("4040.00"),
        Decimal("100039.00"),
    )
    # d1: 95,999 - 买 IEF 3,000 - 佣金 1 = 已结算 92,998，未结算 0；
    #     持仓市值 = 40 * 102 + 60 * 51 = 4,080 + 3,060 = 7,140；NAV = 100,138
    p1 = res.equity_curve[1]
    assert (p1.settled_cash, p1.unsettled_cash, p1.positions_value, p1.nav) == (
        Decimal("92998.00"),
        Decimal(0),
        Decimal("7140.00"),
        Decimal("100138.00"),
    )
    # d2: 卖 20 SPY @ 105 = 2,100 进入未结算（T+1 结算），佣金 1 从已结算扣除 -> 92,997；
    #     持仓市值 = 20 * 104 + 60 * 50.50 = 2,080 + 3,030 = 5,110；NAV = 100,207
    p2 = res.equity_curve[2]
    assert (p2.settled_cash, p2.unsettled_cash, p2.positions_value, p2.nav) == (
        Decimal("92997.00"),
        Decimal("2100.00"),
        Decimal("5110.00"),
        Decimal("100207.00"),
    )
    # d3: 2,100 结算 → 已结算 95,097，未结算 0；
    #     持仓市值 = 20 * 106 + 60 * 51.50 = 2,120 + 3,090 = 5,210；NAV = 100,307，累计佣金 3.00
    p3 = res.equity_curve[3]
    assert (
        p3.settled_cash,
        p3.unsettled_cash,
        p3.positions_value,
        p3.nav,
        p3.cumulative_commission,
    ) == (
        Decimal("95097.00"),
        Decimal(0),
        Decimal("5210.00"),
        Decimal("100307.00"),
        Decimal("3.00"),
    )

    out_dir = res.write_parquet(tmp_path / "backtests")
    assert pq.read_table(out_dir / "equity_curve.parquet").num_rows == 4
    assert pq.read_table(out_dir / "executions.parquet").num_rows == 3
    with pytest.raises(FileExistsError):
        res.write_parquet(tmp_path / "backtests")


# ---------- T-04d：第二套独立实现复算对照 ----------


@pytest.mark.asyncio
async def test_independent_vectorized_reference_matches_daily_positions_and_nav() -> None:
    """T-04d：月度 SPY / IEF 二选一动量（含先卖后买换仓与佣金），逐日持仓一致、净值差 ≤ 1e-4。"""
    sessions = list(CALENDAR.sessions_between(date(2026, 1, 2), date(2026, 4, 30)))
    spy_bars: list[MarketBar] = []
    ief_bars: list[MarketBar] = []
    for idx, s in enumerate(sessions):
        # 前 35 个交易日 SPY 上涨、IEF 平稳；之后 SPY 回落、IEF 上涨，触发跨月换仓
        spy_px = Decimal(100) + (Decimal(idx) if idx < 35 else Decimal(70 - idx)) * Decimal("0.5")
        ief_px = Decimal(100) + (Decimal(0) if idx < 25 else Decimal(idx - 25)) * Decimal("0.4")
        spy_bars.append(
            _make_bar(s, "SPY", str(spy_px), str(spy_px + 1), str(spy_px - 1), str(spy_px))
        )
        ief_bars.append(
            _make_bar(s, "IEF", str(ief_px), str(ief_px + 1), str(ief_px - 1), str(ief_px))
        )

    bars = {"SPY": spy_bars, "IEF": ief_bars}
    comm = FixedCommission()
    strat = DualMomentumStrategy(("SPY", "IEF"), lookback=20, allocation=Decimal("0.04"))
    cfg = BacktestConfig(
        experiment_id="exp-dual-mom",
        account=ACCOUNT,
        initial_cash=Decimal(100_000),
        start=sessions[0].day,
        end=sessions[-1].day,
        instruments=INSTRUMENTS,
        parameters=PARAMS,
        commission=comm,
        cost_model_name="per_share_default",
    )
    engine_res = await run_backtest(strat, bars, CALENDAR, cfg)
    ref_res = vectorized_dual_momentum_reference(
        bars,
        sessions,
        pair=("SPY", "IEF"),
        lookback=20,
        initial_cash=Decimal(100_000),
        allocation=Decimal("0.04"),
        commission=comm,
    )
    assert len(engine_res.equity_curve) == len(ref_res)
    # 确认发生了买卖换仓
    assert len(engine_res.executions) >= 3
    for pt, ref in zip(engine_res.equity_curve, ref_res, strict=True):
        assert pt.day == ref.day
        assert dict(pt.positions) == dict(ref.positions), f"{pt.day} 持仓不一致"
        rel_diff = abs(pt.nav - ref.nav) / ref.nav
        assert rel_diff <= Decimal("0.0001"), f"{pt.day} 净值差异超万分之一：{pt.nav} vs {ref.nav}"
