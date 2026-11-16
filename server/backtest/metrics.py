"""回测绩效、基准对比与稳健性报告（T-06，V4.1 8.3 & economic-gate.md 第 0–2 节）。

提供：
- 核心绩效指标：累计收益、年化收益（CAGR）、年化波动率、夏普比率、索提诺比率、卡玛比率、
  最大回撤及回撤持续天数、换手率、总交易费用；
- Deflated Sharpe Ratio（Bailey & López de Prado, 2014）：根据累计试验次数校正多重检验偏差，
  返回显著性概率 PSR；
- 基准曲线计算：SPY 买入持有（股息再投资）与 60/40（SPY 60% + IEF/AGG 40% 月度再平衡）；
- 稳健性评估：参数邻域（±20%）夏普衰减检验、分年度相对 60/40 胜率或回撤优势统计；
- 税前/税后双口径：未提供税务身份参数时显式标明 `UNASSESSED_PENDING_TAX_PROFILE`；
- 模拟盘/实盘与同期回测重放偏差比较：持仓方向一致率、年化跟踪误差、平均每笔执行偏差（实现缺口）。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from server.backtest.engine import EquityPoint
from server.domain.market import MarketBar
from server.domain.orders import Execution

TRADING_DAYS_PER_YEAR = 252.0
EULER_MASCHERONI = 0.5772156649015329
PLACE_6 = Decimal("0.000001")


def _d6(val: float) -> Decimal:
    if not math.isfinite(val):
        return Decimal(0)
    return Decimal(f"{val:.6f}").quantize(PLACE_6)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """标准正态分布分位函数（Acklam 有理逼近，精度 < 1.15e-9）。"""
    if p <= 0.0:
        return -10.0
    if p >= 1.0:
        return 10.0
    a = (
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    )
    b = (
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    )
    c = (
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    )
    d = (
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    )
    p_low = 0.02425
    p_high = 1.0 - p_low
    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (
            (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
            * q
            / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
        )
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
        (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
    )


@dataclass(frozen=True)
class TaxProfile:
    """税务身份参数（economic-gate.md 第 0 节：由 Dylan 确认后提供）。"""

    dividend_withholding_rate: Decimal
    capital_gains_rate: Decimal
    has_tax_treaty: bool = False

    def __post_init__(self) -> None:
        if not (Decimal(0) <= self.dividend_withholding_rate <= Decimal(1)):
            raise ValueError("股息预扣税率必须在 [0, 1] 内")
        if not (Decimal(0) <= self.capital_gains_rate <= Decimal(1)):
            raise ValueError("资本利得税率必须在 [0, 1] 内")


@dataclass(frozen=True)
class PerformanceSummary:
    """单段净值序列的绩效汇总。"""

    start_day: date
    end_day: date
    trading_days: int
    total_return: Decimal
    annualized_return: Decimal
    annualized_volatility: Decimal
    sharpe: Decimal
    sortino: Decimal
    calmar: Decimal
    max_drawdown: Decimal
    max_drawdown_days: int
    deflated_sharpe_psr: Decimal
    total_commission: Decimal
    post_tax_status: str
    post_tax_annualized_return: Decimal | None = None


def daily_returns_from_navs(
    navs: Sequence[Decimal], initial_nav: Decimal | None = None
) -> list[float]:
    if not navs:
        return []
    rets: list[float] = []
    prev = float(initial_nav if initial_nav is not None else navs[0])
    start_idx = 0 if initial_nav is not None else 1
    for nav in navs[start_idx:]:
        cur = float(nav)
        rets.append((cur / prev - 1.0) if prev > 0 else 0.0)
        prev = cur
    return rets


def compute_max_drawdown(
    navs: Sequence[Decimal], initial_nav: Decimal | None = None
) -> tuple[Decimal, int]:
    """返回 (最大回撤比例 [0, 1], 最长未创新高交易日数)。"""
    if not navs:
        return Decimal(0), 0
    peak = float(initial_nav if initial_nav is not None else navs[0])
    max_dd = 0.0
    cur_duration = 0
    max_duration = 0
    for nav in navs:
        val = float(nav)
        if val >= peak:
            peak = val
            cur_duration = 0
        else:
            dd = (peak - val) / peak if peak > 0 else 0.0
            max_dd = max(max_dd, dd)
            cur_duration += 1
            max_duration = max(max_duration, cur_duration)
    return _d6(max_dd), max_duration


def deflated_sharpe_psr(
    daily_returns: Sequence[float],
    *,
    trial_count: int = 1,
    cross_trial_sharpe_std: float = 0.25,
) -> Decimal:
    """计算 Bailey & López de Prado (2014) Deflated Sharpe Ratio 的显著性概率 PSR in [0, 1]。

    当累计试验次数 trial_count > 1 时，按极值理论估计多重试验下的预期最高虚假年化夏普 SR0，
    再根据样本偏度与峰度计算观测夏普显著高于 SR0 的概率。
    """
    n = len(daily_returns)
    if n < 5:
        return Decimal(0)
    mean = sum(daily_returns) / n
    var = sum((r - mean) ** 2 for r in daily_returns) / (n - 1)
    if var <= 1e-18:
        return Decimal(0)
    std = math.sqrt(var)
    sr_daily = mean / std

    trials = max(1, trial_count)
    if trials == 1:
        sr0_annual = 0.0
    else:
        z1 = _norm_ppf(1.0 - 1.0 / trials)
        z2 = _norm_ppf(1.0 - 1.0 / (trials * math.e))
        sr0_annual = cross_trial_sharpe_std * (
            (1.0 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2
        )
    sr0_daily = sr0_annual / math.sqrt(TRADING_DAYS_PER_YEAR)

    m3 = sum(((r - mean) / std) ** 3 for r in daily_returns) / n
    m4 = sum(((r - mean) / std) ** 4 for r in daily_returns) / n
    denom_sq = 1.0 - m3 * sr_daily + ((m4 - 1.0) / 4.0) * (sr_daily**2)
    if denom_sq <= 1e-12:
        return Decimal(0)
    z = (sr_daily - sr0_daily) * math.sqrt(n - 1) / math.sqrt(denom_sq)
    return _d6(_norm_cdf(z))


def summarize_performance(
    curve: Sequence[EquityPoint],
    *,
    initial_nav: Decimal,
    trial_count: int = 1,
    tax_profile: TaxProfile | None = None,
) -> PerformanceSummary:
    """从回测净值曲线计算完整绩效摘要（支持税前与税后双口径）。"""
    if not curve:
        raise ValueError("净值曲线不能为空")
    navs = [p.nav for p in curve]
    rets = daily_returns_from_navs(navs, initial_nav=initial_nav)
    n = len(curve)
    final_nav = navs[-1]
    total_ret = float(final_nav / initial_nav) - 1.0 if initial_nav > 0 else 0.0
    years = max(n / TRADING_DAYS_PER_YEAR, 1.0 / TRADING_DAYS_PER_YEAR)
    cagr = (float(final_nav / initial_nav) ** (1.0 / years) - 1.0) if final_nav > 0 else -1.0

    mean_r = sum(rets) / len(rets) if rets else 0.0
    var_r = sum((r - mean_r) ** 2 for r in rets) / (len(rets) - 1) if len(rets) > 1 else 0.0
    vol_ann = math.sqrt(var_r * TRADING_DAYS_PER_YEAR)
    sharpe = (mean_r * TRADING_DAYS_PER_YEAR) / vol_ann if vol_ann > 1e-12 else 0.0

    downside_sq = [r * r for r in rets if r < 0.0]
    down_vol = (
        math.sqrt((sum(downside_sq) / len(rets)) * TRADING_DAYS_PER_YEAR) if downside_sq else 0.0
    )
    sortino = (mean_r * TRADING_DAYS_PER_YEAR) / down_vol if down_vol > 1e-12 else sharpe

    mdd, mdd_days = compute_max_drawdown(navs, initial_nav=initial_nav)
    calmar = cagr / float(mdd) if mdd > 0 else 0.0
    psr = deflated_sharpe_psr(rets, trial_count=trial_count)

    if tax_profile is None:
        tax_status = "UNASSESSED_PENDING_TAX_PROFILE"
        post_tax_cagr: Decimal | None = None
    else:
        tax_status = "ASSESSED"
        effective_rate = float(tax_profile.capital_gains_rate)
        after_tax_total = total_ret * (1.0 - effective_rate) if total_ret > 0 else total_ret
        post_tax_cagr = _d6((1.0 + after_tax_total) ** (1.0 / years) - 1.0)

    return PerformanceSummary(
        start_day=curve[0].day,
        end_day=curve[-1].day,
        trading_days=n,
        total_return=_d6(total_ret),
        annualized_return=_d6(cagr),
        annualized_volatility=_d6(vol_ann),
        sharpe=_d6(sharpe),
        sortino=_d6(sortino),
        calmar=_d6(calmar),
        max_drawdown=mdd,
        max_drawdown_days=mdd_days,
        deflated_sharpe_psr=psr,
        total_commission=curve[-1].cumulative_commission,
        post_tax_status=tax_status,
        post_tax_annualized_return=post_tax_cagr,
    )


# ---------- 基准组合计算（economic-gate.md 第 0 节） ----------


def compute_spy_buy_hold_navs(
    spy_bars: Sequence[MarketBar], initial_cash: Decimal = Decimal(100_000)
) -> list[tuple[date, Decimal]]:
    """基准 1：买入并持有 SPY（复权价格序列反映股息再投资）。"""
    if not spy_bars:
        return []
    p0 = spy_bars[0].open
    return [(b.end.date(), (initial_cash * b.close / p0).quantize(PLACE_6)) for b in spy_bars]


def compute_60_40_monthly_navs(
    spy_bars: Sequence[MarketBar],
    bond_bars: Sequence[MarketBar],
    initial_cash: Decimal = Decimal(100_000),
) -> list[tuple[date, Decimal]]:
    """基准 2：60% SPY + 40% IEF/AGG，每月首个交易日开盘再平衡。"""
    bond_by_day = {b.end.date(): b for b in bond_bars}
    common = [
        (b.end.date(), b, bond_by_day[b.end.date()])
        for b in spy_bars
        if b.end.date() in bond_by_day
    ]
    if not common:
        return []
    nav = initial_cash
    spy_units = Decimal(0)
    bond_units = Decimal(0)
    out: list[tuple[date, Decimal]] = []
    prev_ym: tuple[int, int] | None = None
    for d, sb, bb in common:
        ym = (d.year, d.month)
        if ym != prev_ym:
            # 月初按开盘价再平衡为 60 / 40
            if prev_ym is not None:
                nav = spy_units * sb.open + bond_units * bb.open
            spy_units = (nav * Decimal("0.60")) / sb.open
            bond_units = (nav * Decimal("0.40")) / bb.open
            prev_ym = ym
        eod_nav = (spy_units * sb.close + bond_units * bb.close).quantize(PLACE_6)
        out.append((d, eod_nav))
    return out


# ---------- 稳健性评估（economic-gate.md 第 1 节） ----------


@dataclass(frozen=True)
class ParameterRobustnessResult:
    center_sharpe: Decimal
    min_neighbor_sharpe: Decimal
    max_sharpe_drop_ratio: Decimal
    passed: bool


def evaluate_parameter_neighborhood(
    center_sharpe: Decimal,
    neighbor_sharpes: Sequence[Decimal],
    *,
    max_allowed_drop_ratio: Decimal = Decimal("0.30"),
) -> ParameterRobustnessResult:
    """检验参数 ±20% 邻域内夏普比率下降是否不超过 30%。"""
    if not neighbor_sharpes or center_sharpe <= 0:
        return ParameterRobustnessResult(
            center_sharpe=center_sharpe,
            min_neighbor_sharpe=Decimal(0),
            max_sharpe_drop_ratio=Decimal(1),
            passed=False,
        )
    min_sr = min(neighbor_sharpes)
    drop = max(Decimal(0), (center_sharpe - min_sr) / center_sharpe).quantize(PLACE_6)
    return ParameterRobustnessResult(
        center_sharpe=center_sharpe,
        min_neighbor_sharpe=min_sr,
        max_sharpe_drop_ratio=drop,
        passed=drop <= max_allowed_drop_ratio,
    )


@dataclass(frozen=True)
class YearlySubperiodResult:
    total_years: int
    winning_years: int
    win_ratio: Decimal
    passed: bool


def evaluate_yearly_vs_60_40(
    strategy_navs: Sequence[tuple[date, Decimal]],
    bench_60_40_navs: Sequence[tuple[date, Decimal]],
    *,
    min_win_ratio: Decimal = Decimal("0.60"),
) -> YearlySubperiodResult:
    """分年度检验：至少 60% 的年份跑赢 60/40 基准或最大回撤更小。"""
    strat_by_yr: dict[int, list[Decimal]] = {}
    bench_by_yr: dict[int, list[Decimal]] = {}
    for d, v in strategy_navs:
        strat_by_yr.setdefault(d.year, []).append(v)
    for d, v in bench_60_40_navs:
        bench_by_yr.setdefault(d.year, []).append(v)
    years = sorted(set(strat_by_yr) & set(bench_by_yr))
    if not years:
        return YearlySubperiodResult(0, 0, Decimal(0), False)
    wins = 0
    for yr in years:
        s_vals = strat_by_yr[yr]
        b_vals = bench_by_yr[yr]
        if len(s_vals) < 2 or len(b_vals) < 2:
            continue
        s_ret = s_vals[-1] / s_vals[0] - Decimal(1)
        b_ret = b_vals[-1] / b_vals[0] - Decimal(1)
        s_mdd, _ = compute_max_drawdown(s_vals)
        b_mdd, _ = compute_max_drawdown(b_vals)
        if s_ret >= b_ret or s_mdd <= b_mdd:
            wins += 1
    ratio = _d6(wins / len(years))
    return YearlySubperiodResult(
        total_years=len(years),
        winning_years=wins,
        win_ratio=ratio,
        passed=ratio >= min_win_ratio,
    )


# ---------- 模拟盘 / 实盘与同期回测重放偏差跟踪（economic-gate.md 第 2 节） ----------


@dataclass(frozen=True)
class ReplayComparison:
    """模拟盘/实盘与同期回测重放的一致性对比结果。"""

    position_agreement_rate: Decimal
    annualized_tracking_error: Decimal
    mean_implementation_shortfall: Decimal
    within_gate: bool


def compare_with_backtest_replay(  # noqa: PLR0913 - 对比参数与门禁阈值显式列出
    actual_curve: Sequence[EquityPoint],
    replay_curve: Sequence[EquityPoint],
    *,
    actual_executions: Sequence[Execution] = (),
    decision_prices: Mapping[str, Decimal] | None = None,
    min_agreement: Decimal = Decimal("0.95"),
    max_tracking_error: Decimal = Decimal("0.03"),
    max_shortfall: Decimal = Decimal("0.0030"),
) -> ReplayComparison:
    """计算同期持仓方向一致率、年化跟踪误差与平均实现缺口。"""
    replay_by_day = {p.day: p for p in replay_curve}
    paired = [(p, replay_by_day[p.day]) for p in actual_curve if p.day in replay_by_day]
    if len(paired) < 2:
        return ReplayComparison(Decimal(0), Decimal(1), Decimal(1), False)

    agree_days = 0
    for act, rep in paired:
        act_syms = {s for s, q in act.positions.items() if q > 0}
        rep_syms = {s for s, q in rep.positions.items() if q > 0}
        if act_syms == rep_syms:
            agree_days += 1
    agreement = _d6(agree_days / len(paired))

    act_rets = daily_returns_from_navs([p[0].nav for p in paired])
    rep_rets = daily_returns_from_navs([p[1].nav for p in paired])
    diffs = [a - b for a, b in zip(act_rets, rep_rets, strict=True)]
    mean_d = sum(diffs) / len(diffs)
    var_d = sum((d - mean_d) ** 2 for d in diffs) / max(len(diffs) - 1, 1)
    te = _d6(math.sqrt(var_d * TRADING_DAYS_PER_YEAR))

    shortfalls: list[float] = []
    if decision_prices:
        for ex in actual_executions:
            ref_px = decision_prices.get(ex.intent_id)
            if ref_px is not None and ref_px > 0:
                sf = abs(float(ex.price - ref_px)) / float(ref_px)
                shortfalls.append(sf)
    mean_sf = _d6(sum(shortfalls) / len(shortfalls)) if shortfalls else Decimal(0)

    ok = agreement >= min_agreement and te <= max_tracking_error and mean_sf <= max_shortfall
    return ReplayComparison(
        position_agreement_rate=agreement,
        annualized_tracking_error=te,
        mean_implementation_shortfall=mean_sf,
        within_gate=ok,
    )


__all__ = (
    "ParameterRobustnessResult",
    "PerformanceSummary",
    "ReplayComparison",
    "TaxProfile",
    "YearlySubperiodResult",
    "compare_with_backtest_replay",
    "compute_60_40_monthly_navs",
    "compute_max_drawdown",
    "compute_spy_buy_hold_navs",
    "daily_returns_from_navs",
    "deflated_sharpe_psr",
    "evaluate_parameter_neighborhood",
    "evaluate_yearly_vs_60_40",
    "summarize_performance",
)
