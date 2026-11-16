"""策略发布门禁与衰减监控（T-08，实现 docs/gates/economic-gate.md 第 0–4 节）。

原则：
- 失败即关闭：任何一项检查未通过或缺失证据，门禁结论一律为 `passed=False`；
- 成本与双基准：全部收益按扣费后净值计算，同时对比 SPY 买入持有与 60/40 月度再平衡两个基准；
- 税前/税后双口径：未提供税务参数时显式标注 `UNASSESSED_PENDING_TAX_PROFILE`；
- 只自动降险：实盘/模拟盘衰减监控只触发自动减半仓位、降级资金阶梯或退回 PAPER + HALT，
  绝不自动升级或恢复仓位（升级/恢复须 Dylan 本人批准）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from server.backtest.metrics import (
    ParameterRobustnessResult,
    PerformanceSummary,
    ReplayComparison,
    YearlySubperiodResult,
)

REQUIRED_CRISIS_MONTHS: tuple[tuple[int, int], ...] = (
    (2008, 10),
    (2020, 3),
    (2022, 6),
)


class CapitalTier(StrEnum):
    """实盘资金阶梯（economic-gate.md 第 3 节）。"""

    PAPER = "PAPER"
    L1 = "L1"  # 10% 计划资金，回撤上限 8%
    L2 = "L2"  # 25% 计划资金，回撤上限 10%
    L3 = "L3"  # 50% 计划资金，回撤上限 12%
    L4 = "L4"  # 100% 计划资金，回撤上限 12%


TIER_DRAWDOWN_LIMIT: dict[CapitalTier, Decimal] = {
    CapitalTier.PAPER: Decimal("0.10"),
    CapitalTier.L1: Decimal("0.08"),
    CapitalTier.L2: Decimal("0.10"),
    CapitalTier.L3: Decimal("0.12"),
    CapitalTier.L4: Decimal("0.12"),
}

TIER_STEP_DOWN: dict[CapitalTier, CapitalTier] = {
    CapitalTier.L4: CapitalTier.L3,
    CapitalTier.L3: CapitalTier.L2,
    CapitalTier.L2: CapitalTier.L1,
    CapitalTier.L1: CapitalTier.PAPER,
    CapitalTier.PAPER: CapitalTier.PAPER,
}


class DecayAction(StrEnum):
    """策略衰减与回撤触发的自动降险动作（economic-gate.md 第 3–4 节）。"""

    NONE = "NONE"
    HALVE_POSITION = "HALVE_POSITION"
    STEP_DOWN_TIER = "STEP_DOWN_TIER"
    HALT_TO_PAPER = "HALT_TO_PAPER"


@dataclass(frozen=True)
class GateCheckItem:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class BacktestGateInput:
    """回测发布门禁（economic-gate.md 第 1 节）输入证据。"""

    report_id: str
    strategy_id: str
    strategy_version: str
    evaluated_at: datetime
    full_sample: PerformanceSummary
    oos_sample: PerformanceSummary
    spy_oos: PerformanceSummary
    bench_60_40_oos: PerformanceSummary
    stressed_2x_oos: PerformanceSummary
    full_rebalance_count: int
    oos_rebalance_count: int
    oos_used_once: bool
    parameter_robustness: ParameterRobustnessResult
    yearly_subperiod: YearlySubperiodResult
    independent_verified: bool
    survivorship_bias_free: bool
    for_live_promotion: bool = False


@dataclass(frozen=True)
class PaperForwardGateInput:
    """模拟盘前向检验门禁（economic-gate.md 第 2 节）输入证据。"""

    report_id: str
    strategy_id: str
    strategy_version: str
    calendar_days: int
    trading_days: int
    rebalance_count: int
    smoke_executions: int
    smoke_cancels: int
    replay_comparison: ReplayComparison
    observed_max_drawdown: Decimal
    replay_max_drawdown: Decimal
    p0_incidents: int
    open_p1_incidents: int


@dataclass(frozen=True)
class GateReport:
    report_id: str
    strategy_id: str
    strategy_version: str
    stage: str
    passed: bool
    checks: tuple[GateCheckItem, ...]
    survivorship_note: str
    post_tax_note: str

    def to_markdown(self) -> str:
        verdict = "PASS（通过）" if self.passed else "REJECT（未通过）"
        lines = [
            f"# 策略发布门禁报告：{self.report_id}",
            "",
            f"- **策略版本**：`{self.strategy_id}:{self.strategy_version}`",
            f"- **门禁阶段**：`{self.stage}`",
            f"- **总体结论**：**{verdict}**",
            f"- **数据偏差说明**：{self.survivorship_note}",
            f"- **税务口径说明**：{self.post_tax_note}",
            "",
            "| 检查项 | 结果 | 详情 |",
            "|---|---|---|",
        ]
        for c in self.checks:
            mark = "PASS" if c.passed else "FAIL"
            lines.append(f"| {c.name} | {mark} | {c.detail} |")
        return "\n".join(lines) + "\n"

    def write_markdown(self, gates_dir: Path) -> Path:
        gates_dir.mkdir(parents=True, exist_ok=True)
        out = gates_dir / f"{self.report_id}.md"
        out.write_text(self.to_markdown(), encoding="utf-8")
        return out


def _covers_crisis_months(start: date, end: date) -> bool:
    start_ym = (start.year, start.month)
    end_ym = (end.year, end.month)
    return all(start_ym <= ym <= end_ym for ym in REQUIRED_CRISIS_MONTHS)


def evaluate_backtest_gate(inp: BacktestGateInput) -> GateReport:
    """按 economic-gate.md 第 1 节逐项评估回测发布门禁。"""
    checks: list[GateCheckItem] = []

    # 1. 回测区间：至少 15 年（约 3780 个交易日或日历跨度 >= 15 年）且覆盖 2008、2020-03、2022
    span_days = (inp.full_sample.end_day - inp.full_sample.start_day).days
    covers_crises = _covers_crisis_months(inp.full_sample.start_day, inp.full_sample.end_day)
    span_ok = span_days >= (15 * 365) and covers_crises
    checks.append(
        GateCheckItem(
            "回测区间与危机覆盖",
            span_ok,
            f"评估时刻 {inp.evaluated_at.isoformat()}：{inp.full_sample.start_day} ~ "
            f"{inp.full_sample.end_day}（{span_days} 天），覆盖 2008/2020-03/2022={covers_crises}",
        )
    )

    # 2. 样本外封存区间：至少 3 年，且仅使用一次
    oos_days = (inp.oos_sample.end_day - inp.oos_sample.start_day).days
    oos_ok = oos_days >= (3 * 365) and inp.oos_used_once
    checks.append(
        GateCheckItem(
            "样本外封存（>= 3 年且仅用一次）",
            oos_ok,
            f"样本外 {oos_days} 天，单次使用={inp.oos_used_once}",
        )
    )

    # 3. 交易决策样本量：样本外 >= 30 次，全区间 >= 100 次
    sample_ok = inp.oos_rebalance_count >= 30 and inp.full_rebalance_count >= 100
    checks.append(
        GateCheckItem(
            "再平衡决策样本量",
            sample_ok,
            f"样本外 {inp.oos_rebalance_count} 次（>=30），"
            f"全区间 {inp.full_rebalance_count} 次（>=100）",
        )
    )

    # 4. 风险调整收益：样本外净夏普 >= max(SPY, 60/40) + 0.10，且 Deflated Sharpe PSR >= 0.95
    best_bench_sr = max(inp.spy_oos.sharpe, inp.bench_60_40_oos.sharpe)
    sr_target = best_bench_sr + Decimal("0.10")
    sr_ok = inp.oos_sample.sharpe >= sr_target and inp.oos_sample.deflated_sharpe_psr >= Decimal(
        "0.95"
    )
    checks.append(
        GateCheckItem(
            "风险调整收益与 Deflated Sharpe",
            sr_ok,
            f"OOS 夏普 {inp.oos_sample.sharpe}（目标 >={sr_target}），"
            f"PSR={inp.oos_sample.deflated_sharpe_psr}（需 >=0.95）",
        )
    )

    # 5. 超额收益或回撤优势：年化 >= SPY，或（年化 >= 0.7*SPY 且 最大回撤 <= 0.6*SPY）
    cagr_beat = inp.oos_sample.annualized_return >= inp.spy_oos.annualized_return
    dd_advantage = inp.oos_sample.annualized_return >= inp.spy_oos.annualized_return * Decimal(
        "0.70"
    ) and inp.oos_sample.max_drawdown <= inp.spy_oos.max_drawdown * Decimal("0.60")
    edge_ok = cagr_beat or dd_advantage
    checks.append(
        GateCheckItem(
            "超额收益或回撤优势",
            edge_ok,
            f"OOS 年化 {inp.oos_sample.annualized_return} vs SPY {inp.spy_oos.annualized_return}；"
            f"OOS 回撤 {inp.oos_sample.max_drawdown} vs SPY {inp.spy_oos.max_drawdown}",
        )
    )

    # 6. 最大回撤硬上限：全区间 <= 25%，样本外 <= 20%
    mdd_ok = inp.full_sample.max_drawdown <= Decimal(
        "0.25"
    ) and inp.oos_sample.max_drawdown <= Decimal("0.20")
    checks.append(
        GateCheckItem(
            "最大回撤硬上限",
            mdd_ok,
            f"全区间 {inp.full_sample.max_drawdown}（<=0.25），"
            f"样本外 {inp.oos_sample.max_drawdown}（<=0.20）",
        )
    )

    # 7. 2× 成本压力测试：加倍成本后夏普仍不低于两个基准中较高者
    stress_ok = inp.stressed_2x_oos.sharpe >= best_bench_sr
    checks.append(
        GateCheckItem(
            "2× 成本压力测试",
            stress_ok,
            f"2× 成本夏普 {inp.stressed_2x_oos.sharpe} vs 基准最高夏普 {best_bench_sr}",
        )
    )

    # 8. 参数邻域与分年度稳健性
    rob_ok = inp.parameter_robustness.passed and inp.yearly_subperiod.passed
    checks.append(
        GateCheckItem(
            "参数邻域与分年度稳健性",
            rob_ok,
            f"参数夏普最大降幅 {inp.parameter_robustness.max_sharpe_drop_ratio}（<=0.30），"
            f"分年度胜率 {inp.yearly_subperiod.win_ratio}（>=0.60）",
        )
    )

    # 9. 第二套独立实现复算一致
    checks.append(
        GateCheckItem(
            "第二套独立实现复算",
            inp.independent_verified,
            f"独立复算通过={inp.independent_verified}",
        )
    )

    # 10. 实盘晋级附加要求：申请实盘必须使用无幸存者偏差（含退市标的）数据
    surv_ok = inp.survivorship_bias_free if inp.for_live_promotion else True
    surv_note = (
        "已使用含退市标的数据复核（无幸存者偏差）"
        if inp.survivorship_bias_free
        else "当前使用免费/IBKR 数据，不含退市标的，存在幸存者偏差（不可直接申请实盘）"
    )
    if inp.for_live_promotion:
        checks.append(GateCheckItem("含退市标的数据复核（实盘前置）", surv_ok, surv_note))

    post_tax_note = (
        "税后未评估（待 Dylan 提供预扣税率与资本利得税率参数）"
        if inp.oos_sample.post_tax_status != "ASSESSED"
        else f"税后 OOS 年化收益={inp.oos_sample.post_tax_annualized_return}"
    )

    return GateReport(
        report_id=inp.report_id,
        strategy_id=inp.strategy_id,
        strategy_version=inp.strategy_version,
        stage="BACKTEST_GATE_G5",
        passed=all(c.passed for c in checks),
        checks=tuple(checks),
        survivorship_note=surv_note,
        post_tax_note=post_tax_note,
    )


def evaluate_paper_forward_gate(inp: PaperForwardGateInput) -> GateReport:
    """按 economic-gate.md 第 2 节评估模拟盘前向检验门禁（G6）。"""
    checks: list[GateCheckItem] = []

    obs_ok = inp.calendar_days >= 180 and inp.trading_days >= 120
    checks.append(
        GateCheckItem(
            "最短观察期（>=180 自然日且 >=120 交易日）",
            obs_ok,
            f"自然日 {inp.calendar_days}，交易日 {inp.trading_days}",
        )
    )

    samples_ok = inp.rebalance_count >= 6 and inp.smoke_executions >= 40 and inp.smoke_cancels >= 5
    checks.append(
        GateCheckItem(
            "决策与执行样本量",
            samples_ok,
            f"再平衡 {inp.rebalance_count}（>=6），"
            f"成交 {inp.smoke_executions}（>=40），撤单 {inp.smoke_cancels}（>=5）",
        )
    )

    checks.append(
        GateCheckItem(
            "与同期回测重放一致性",
            inp.replay_comparison.within_gate,
            f"持仓一致率 {inp.replay_comparison.position_agreement_rate}（>=0.95），"
            f"跟踪误差 {inp.replay_comparison.annualized_tracking_error}（<=0.03），"
            f"实现缺口 {inp.replay_comparison.mean_implementation_shortfall}（<=0.0030）",
        )
    )

    dd_cap = min(inp.replay_max_drawdown * Decimal("1.5"), Decimal("0.10"))
    dd_ok = inp.observed_max_drawdown <= dd_cap
    checks.append(
        GateCheckItem(
            "观察期最大回撤上限",
            dd_ok,
            f"实际回撤 {inp.observed_max_drawdown} <= min(1.5×回测回撤, 10%)={dd_cap}",
        )
    )

    incidents_ok = inp.p0_incidents == 0 and inp.open_p1_incidents == 0
    checks.append(
        GateCheckItem(
            "安全事件清零（无 P0 且 P1 全部复盘关闭）",
            incidents_ok,
            f"P0={inp.p0_incidents}，未关闭 P1={inp.open_p1_incidents}",
        )
    )

    return GateReport(
        report_id=inp.report_id,
        strategy_id=inp.strategy_id,
        strategy_version=inp.strategy_version,
        stage="PAPER_FORWARD_GATE_G6",
        passed=all(c.passed for c in checks),
        checks=tuple(checks),
        survivorship_note="前向模拟盘实时检验（无后视偏差）",
        post_tax_note="模拟盘阶段重点检验执行与回测一致性",
    )


# ---------- 第 3–4 节：实盘资金阶梯回退与策略衰减自动降险 ----------


@dataclass(frozen=True)
class DecayEvaluation:
    current_tier: CapitalTier
    recommended_tier: CapitalTier
    action: DecayAction
    reasons: tuple[str, ...]


def evaluate_strategy_decay(  # noqa: PLR0913 - 衰减监控输入全部显式列出
    current_tier: CapitalTier,
    *,
    current_drawdown: Decimal,
    rolling_6m_sharpe: Decimal,
    expected_sharpe: Decimal,
    sharpe_std: Decimal,
    rolling_12m_lag_vs_spy: Decimal,
    rolling_12m_lag_vs_60_40: Decimal,
    tracking_error_breach_days: int,
    has_p0_or_unexplained_recon: bool = False,
    months_since_last_halving: int = 0,
) -> DecayEvaluation:
    """按 economic-gate.md 第 3、4 节计算自动降险动作（系统只自动降险，绝不自动升级）。"""
    reasons: list[str] = []
    dd_limit = TIER_DRAWDOWN_LIMIT[current_tier]

    # 1. 最严重情形：P0 / 无法解释的对账差异 / 回撤超过本级上限 1.5 倍 -> 退回 PAPER 并 HALT
    if has_p0_or_unexplained_recon:
        reasons.append("出现 P0 事件或无法解释的对账差异")
    if current_drawdown > dd_limit * Decimal("1.5"):
        reasons.append(f"回撤 {current_drawdown} 超过本级上限 {dd_limit} 的 1.5 倍")
    if reasons:
        return DecayEvaluation(
            current_tier=current_tier,
            recommended_tier=CapitalTier.PAPER,
            action=DecayAction.HALT_TO_PAPER,
            reasons=tuple(reasons),
        )

    # 2. 收集第 4 节衰减触发项
    decay_triggers: list[str] = []
    if rolling_6m_sharpe < Decimal(0) and (
        expected_sharpe - rolling_6m_sharpe >= Decimal(2) * sharpe_std
    ):
        decay_triggers.append("滚动 6 个月净夏普 < 0 且低于回测预期 2 个标准差以上")
    if rolling_12m_lag_vs_spy > Decimal("0.10") and rolling_12m_lag_vs_60_40 > Decimal("0.10"):
        decay_triggers.append("滚动 12 个月相对两个基准均落后超过 10 个百分点")
    if tracking_error_breach_days >= 20:
        decay_triggers.append(f"跟踪误差连续 {tracking_error_breach_days} 个交易日超过 3%")
    if current_drawdown > dd_limit:
        decay_triggers.append(f"回撤 {current_drawdown} 超过本级上限 {dd_limit}")

    # 两项同时触发，或降仓后 3 个月仍未恢复 -> 暂停并退回 PAPER
    if len(decay_triggers) >= 2 or (decay_triggers and months_since_last_halving >= 3):
        return DecayEvaluation(
            current_tier=current_tier,
            recommended_tier=CapitalTier.PAPER,
            action=DecayAction.HALT_TO_PAPER,
            reasons=tuple(decay_triggers),
        )

    # 回撤超过本级上限或跟踪误差连续 20 日超限 -> 资金阶梯降一级
    if current_drawdown > dd_limit or tracking_error_breach_days >= 20:
        return DecayEvaluation(
            current_tier=current_tier,
            recommended_tier=TIER_STEP_DOWN[current_tier],
            action=DecayAction.STEP_DOWN_TIER,
            reasons=tuple(decay_triggers),
        )

    # 单项业绩衰减触发 -> 自动降仓一半
    if decay_triggers:
        return DecayEvaluation(
            current_tier=current_tier,
            recommended_tier=current_tier,
            action=DecayAction.HALVE_POSITION,
            reasons=tuple(decay_triggers),
        )

    return DecayEvaluation(
        current_tier=current_tier,
        recommended_tier=current_tier,
        action=DecayAction.NONE,
        reasons=(),
    )


__all__ = (
    "BacktestGateInput",
    "CapitalTier",
    "DecayAction",
    "DecayEvaluation",
    "GateCheckItem",
    "GateReport",
    "PaperForwardGateInput",
    "evaluate_backtest_gate",
    "evaluate_paper_forward_gate",
    "evaluate_strategy_decay",
)
