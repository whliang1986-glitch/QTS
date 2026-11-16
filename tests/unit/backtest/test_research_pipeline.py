"""T-05 ~ T-08 单元测试：成本模型、绩效稳健性、实验注册表与策略发布门禁。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from server.backtest import (
    BacktestGateInput,
    CapitalTier,
    DecayAction,
    EquityPoint,
    ExperimentRecord,
    ExperimentRegistry,
    PaperForwardGateInput,
    TaxProfile,
    UsEtfCostModel,
    compare_with_backtest_replay,
    compute_60_40_monthly_navs,
    compute_spy_buy_hold_navs,
    deflated_sharpe_psr,
    evaluate_backtest_gate,
    evaluate_paper_forward_gate,
    evaluate_parameter_neighborhood,
    evaluate_strategy_decay,
    evaluate_yearly_vs_60_40,
    summarize_performance,
)
from server.domain.enums import Side
from server.domain.market import MarketBar
from server.domain.orders import Execution
from server.storage.errors import ConflictError

T0 = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)


def _make_equity_curve(
    start: date,
    days: int,
    daily_drift: Decimal = Decimal("0.0006"),
    daily_vol: Decimal = Decimal("0.008"),
) -> list[EquityPoint]:
    pts: list[EquityPoint] = []
    nav = Decimal(100_000)
    for i in range(days):
        d = start + timedelta(days=i)
        if i > 0:
            noise = daily_vol if i % 2 == 0 else -daily_vol
            step = daily_drift + noise
            nav = (nav * (Decimal(1) + step)).quantize(Decimal("0.01"))
        pts.append(
            EquityPoint(
                day=d,
                at=datetime(d.year, d.month, d.day, 21, 0, tzinfo=UTC),
                settled_cash=Decimal(0),
                unsettled_cash=Decimal(0),
                positions_value=nav,
                nav=nav,
                positions={"SPY": Decimal(100)},
                cumulative_commission=Decimal("5.00"),
            )
        )
    return pts


# ---------- T-05：全口径成本与 2× 压力测试 ----------


def test_us_etf_cost_model_and_2x_stress() -> None:
    model = UsEtfCostModel()
    # 买入 100 股 @ $100 = $10,000
    buy_bd = model.breakdown(Decimal(100), Decimal(100), side=Side.BUY)
    assert buy_bd.broker_commission == Decimal("1.00")  # max(100*0.005=0.50, 1.00)
    assert buy_bd.regulatory_fee == Decimal("0.00")  # 买入无 SEC / TAF
    assert buy_bd.slippage_cost == Decimal("3.00")  # 3 bps * 10,000
    assert buy_bd.spread_cost == Decimal("2.00")  # 2 bps * 10,000

    # 卖出 100 股 @ $100：含 SEC 规费与 FINRA TAF
    sell_bd = model.breakdown(Decimal(100), Decimal(100), side=Side.SELL)
    assert sell_bd.regulatory_fee > Decimal("0.00")
    assert sell_bd.explicit_fee > buy_bd.explicit_fee
    assert sell_bd.total_cost == sell_bd.explicit_fee + sell_bd.spread_cost

    # 2× 压力测试
    stressed = model.stressed(Decimal(2))
    assert stressed.effective_half_spread_bps == model.effective_half_spread_bps * Decimal(2)
    assert stressed(Decimal(100), Decimal(100)) >= model(Decimal(100), Decimal(100)) * Decimal(
        "1.9"
    )


# ---------- T-06：绩效、Deflated Sharpe、双基准与稳健性 ----------


def test_performance_summary_and_deflated_sharpe_penalizes_multiple_trials() -> None:
    curve = _make_equity_curve(date(2025, 1, 1), 252, Decimal("0.0015"))
    s_single = summarize_performance(curve, initial_nav=Decimal(100_000), trial_count=1)
    s_many = summarize_performance(curve, initial_nav=Decimal(100_000), trial_count=200)
    assert s_single.sharpe > Decimal("1.0")
    assert s_single.total_return > Decimal(0)
    assert s_single.annualized_volatility > Decimal(0)
    assert s_single.max_drawdown_days >= 0
    assert s_single.total_commission == Decimal("5.00")
    assert s_single.post_tax_status == "UNASSESSED_PENDING_TAX_PROFILE"
    assert s_single.post_tax_annualized_return is None
    # 试验次数越多，Deflated Sharpe 显著性概率 PSR 越低
    assert s_many.deflated_sharpe_psr < s_single.deflated_sharpe_psr
    assert deflated_sharpe_psr([0.001, -0.0005] * 20, trial_count=10) >= Decimal(0)

    # 税后口径评估
    tax = TaxProfile(
        dividend_withholding_rate=Decimal("0.10"),
        capital_gains_rate=Decimal("0.20"),
        has_tax_treaty=True,
    )
    assert tax.has_tax_treaty is True
    s_tax = summarize_performance(
        curve, initial_nav=Decimal(100_000), trial_count=1, tax_profile=tax
    )
    assert s_tax.post_tax_status == "ASSESSED"
    assert s_tax.post_tax_annualized_return is not None
    assert s_tax.post_tax_annualized_return < s_tax.annualized_return


def test_benchmarks_and_robustness_evaluators() -> None:
    d0 = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
    spy_bars: list[MarketBar] = []
    bond_bars: list[MarketBar] = []
    for i in range(40):
        st = d0 + timedelta(days=i)
        ed = st + timedelta(hours=6, minutes=30)
        spy_px = Decimal(100) + Decimal(i) * Decimal("0.5")
        bond_px = Decimal(100) + Decimal(i) * Decimal("0.1")
        spy_bars.append(
            MarketBar(
                symbol="SPY",
                start=st,
                end=ed,
                open=spy_px,
                high=spy_px + 1,
                low=spy_px - 1,
                close=spy_px,
                volume=Decimal(10000),
                source="t",
                adjustment_version="v1",
            )
        )
        bond_bars.append(
            MarketBar(
                symbol="IEF",
                start=st,
                end=ed,
                open=bond_px,
                high=bond_px + 1,
                low=bond_px - 1,
                close=bond_px,
                volume=Decimal(10000),
                source="t",
                adjustment_version="v1",
            )
        )
    spy_navs = compute_spy_buy_hold_navs(spy_bars)
    b60_navs = compute_60_40_monthly_navs(spy_bars, bond_bars)
    assert len(spy_navs) == 40
    assert len(b60_navs) == 40
    assert spy_navs[-1][1] > b60_navs[-1][1] > Decimal(100_000)

    rob_pass = evaluate_parameter_neighborhood(
        Decimal("1.20"), [Decimal("1.00"), Decimal("1.10"), Decimal("0.90")]
    )
    assert rob_pass.passed is True  # 最大降幅 (1.20 - 0.90)/1.20 = 25% <= 30%
    assert rob_pass.min_neighbor_sharpe == Decimal("0.90")
    rob_fail = evaluate_parameter_neighborhood(Decimal("1.20"), [Decimal("1.00"), Decimal("0.70")])
    assert rob_fail.passed is False

    yr_res = evaluate_yearly_vs_60_40(spy_navs, b60_navs)
    assert yr_res.passed is True
    assert yr_res.total_years == 1
    assert yr_res.winning_years == 1


def test_compare_with_backtest_replay() -> None:
    curve = _make_equity_curve(date(2026, 1, 1), 30)
    execs = [
        Execution(
            execution_id="e1",
            intent_id="i1",
            broker_order_id="b1",
            symbol="SPY",
            side=Side.BUY,
            quantity=Decimal(10),
            price=Decimal("100.10"),
            commission=Decimal("1.00"),
            executed_at=T0,
        )
    ]
    comp = compare_with_backtest_replay(
        curve, curve, actual_executions=execs, decision_prices={"i1": Decimal("100.00")}
    )
    assert comp.within_gate is True
    assert comp.position_agreement_rate == Decimal("1.000000")
    assert comp.annualized_tracking_error == Decimal("0.000000")
    assert comp.mean_implementation_shortfall == Decimal("0.001000")


# ---------- T-07：只追加实验注册表与封存样本外单次使用锁 ----------


def test_experiment_registry_append_only_and_oos_single_use_lock(tmp_path: Path) -> None:
    db_path = tmp_path / "meta.db"
    reg = ExperimentRegistry(db_path)
    rec1 = ExperimentRecord(
        experiment_id="exp-1",
        strategy_id="dual_mom",
        strategy_version="v1",
        recorded_at=T0,
        start_day=date(2007, 1, 1),
        end_day=date(2022, 12, 31),
        is_out_of_sample=False,
        data_version="dv1",
        code_commit="abc1234",
        params_hash="p1",
        cost_model="us_etf_full_v1",
        sharpe=Decimal("0.95"),
        annualized_return=Decimal("0.09"),
        max_drawdown=Decimal("0.18"),
        rebalance_count=190,
        survivorship_bias_free=False,
        artifact_dir="backtests/exp-1",
        manifest={"lookback": 20},
    )
    assert reg.register(rec1) is True
    assert reg.register(rec1) is False  # 幂等
    assert reg.trial_count("dual_mom") == 1

    # 首次登记 v1 的封存样本外实验：通过
    oos1 = ExperimentRecord(
        experiment_id="exp-oos-1",
        strategy_id="dual_mom",
        strategy_version="v1",
        recorded_at=T0,
        start_day=date(2023, 1, 1),
        end_day=date(2026, 1, 5),
        is_out_of_sample=True,
        data_version="dv1",
        code_commit="abc1234",
        params_hash="p1",
        cost_model="us_etf_full_v1",
        sharpe=Decimal("1.05"),
        annualized_return=Decimal("0.10"),
        max_drawdown=Decimal("0.12"),
        rebalance_count=36,
        survivorship_bias_free=False,
        artifact_dir="backtests/exp-oos-1",
        manifest={"lookback": 20},
    )
    assert reg.register(oos1) is True
    assert reg.trial_count("dual_mom") == 2

    # 同一版本二次窥探封存样本外：拒绝！
    oos2 = ExperimentRecord(**{**oos1.__dict__, "experiment_id": "exp-oos-2", "params_hash": "p2"})
    with pytest.raises(ConflictError, match="禁止二次窥探"):
        reg.register(oos2)

    # SQLite 触发器阻止直接 DELETE / UPDATE 篡改实验记录
    conn = sqlite3.connect(db_path)
    with pytest.raises(sqlite3.IntegrityError, match="DELETE forbidden"):
        conn.execute("DELETE FROM experiments WHERE experiment_id = 'exp-1'")
    with pytest.raises(sqlite3.IntegrityError, match="UPDATE forbidden"):
        conn.execute("UPDATE experiments SET sharpe = '9.99' WHERE experiment_id = 'exp-1'")
    conn.close()


# ---------- T-08：策略发布门禁（G5 回测、G6 模拟盘、衰减监控） ----------


def test_backtest_paper_and_decay_gates(tmp_path: Path) -> None:
    full_curve = _make_equity_curve(date(2007, 1, 1), 16 * 365, Decimal("0.0012"))
    oos_curve = _make_equity_curve(date(2023, 1, 1), 3 * 365 + 10, Decimal("0.0015"))
    full_sum = summarize_performance(full_curve, initial_nav=Decimal(100_000), trial_count=5)
    oos_sum = summarize_performance(oos_curve, initial_nav=Decimal(100_000), trial_count=5)
    spy_sum = summarize_performance(
        _make_equity_curve(date(2023, 1, 1), 3 * 365 + 10, Decimal("0.0008")),
        initial_nav=Decimal(100_000),
    )
    b60_sum = summarize_performance(
        _make_equity_curve(date(2023, 1, 1), 3 * 365 + 10, Decimal("0.0007")),
        initial_nav=Decimal(100_000),
    )
    stress_sum = summarize_performance(
        _make_equity_curve(date(2023, 1, 1), 3 * 365 + 10, Decimal("0.0012")),
        initial_nav=Decimal(100_000),
    )
    bt_inp = BacktestGateInput(
        report_id="gate-dual-mom-v1",
        strategy_id="dual_mom",
        strategy_version="v1",
        evaluated_at=T0,
        full_sample=full_sum,
        oos_sample=oos_sum,
        spy_oos=spy_sum,
        bench_60_40_oos=b60_sum,
        stressed_2x_oos=stress_sum,
        full_rebalance_count=180,
        oos_rebalance_count=36,
        oos_used_once=True,
        parameter_robustness=evaluate_parameter_neighborhood(
            Decimal("1.2"), [Decimal("1.0"), Decimal("1.1")]
        ),
        yearly_subperiod=evaluate_yearly_vs_60_40(
            [(p.day, p.nav) for p in oos_curve],
            [(p.day, p.nav * Decimal("0.95")) for p in oos_curve],
        ),
        independent_verified=True,
        survivorship_bias_free=False,
        for_live_promotion=False,
    )
    report = evaluate_backtest_gate(bt_inp)
    assert report.passed is True
    assert "存在幸存者偏差" in report.survivorship_note
    assert "税后未评估" in report.post_tax_note
    md_path = report.write_markdown(tmp_path / "gates")
    assert md_path.exists()
    assert "PASS（通过）" in md_path.read_text(encoding="utf-8")

    # 若申请实盘但未使用无幸存者偏差数据，必须被门禁拦截
    live_report = evaluate_backtest_gate(
        BacktestGateInput(**{**bt_inp.__dict__, "for_live_promotion": True})
    )
    assert live_report.passed is False

    # 模拟盘 G6 前向检验门禁
    comp = compare_with_backtest_replay(oos_curve[:130], oos_curve[:130])
    paper_report = evaluate_paper_forward_gate(
        PaperForwardGateInput(
            report_id="paper-gate-v1",
            strategy_id="dual_mom",
            strategy_version="v1",
            calendar_days=185,
            trading_days=130,
            rebalance_count=6,
            smoke_executions=45,
            smoke_cancels=6,
            replay_comparison=comp,
            observed_max_drawdown=Decimal("0.05"),
            replay_max_drawdown=Decimal("0.06"),
            p0_incidents=0,
            open_p1_incidents=0,
        )
    )
    assert paper_report.passed is True

    # 策略衰减自动降险：单触发降半仓、超回撤降阶梯、双触发退回 PAPER
    d_halve = evaluate_strategy_decay(
        CapitalTier.L2,
        current_drawdown=Decimal("0.05"),
        rolling_6m_sharpe=Decimal("-0.5"),
        expected_sharpe=Decimal("1.0"),
        sharpe_std=Decimal("0.5"),
        rolling_12m_lag_vs_spy=Decimal("0.02"),
        rolling_12m_lag_vs_60_40=Decimal("0.02"),
        tracking_error_breach_days=0,
    )
    assert d_halve.action is DecayAction.HALVE_POSITION

    d_step = evaluate_strategy_decay(
        CapitalTier.L2,
        current_drawdown=Decimal("0.11"),  # > L2 上限 10%，但 <= 15%
        rolling_6m_sharpe=Decimal("0.8"),
        expected_sharpe=Decimal("1.0"),
        sharpe_std=Decimal("0.5"),
        rolling_12m_lag_vs_spy=Decimal("0.02"),
        rolling_12m_lag_vs_60_40=Decimal("0.02"),
        tracking_error_breach_days=0,
    )
    assert d_step.action is DecayAction.STEP_DOWN_TIER
    assert d_step.recommended_tier is CapitalTier.L1

    d_halt = evaluate_strategy_decay(
        CapitalTier.L2,
        current_drawdown=Decimal("0.16"),  # > 1.5 * 10%
        rolling_6m_sharpe=Decimal("0.8"),
        expected_sharpe=Decimal("1.0"),
        sharpe_std=Decimal("0.5"),
        rolling_12m_lag_vs_spy=Decimal("0.02"),
        rolling_12m_lag_vs_60_40=Decimal("0.02"),
        tracking_error_breach_days=0,
    )
    assert d_halt.action is DecayAction.HALT_TO_PAPER
    assert d_halt.recommended_tier is CapitalTier.PAPER
