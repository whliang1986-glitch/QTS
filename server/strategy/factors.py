"""因子定义、受限 as_of 计算、声明式积木与十步自动验证流水线（T-10a/b/c/d/e/f，V4.1 8.1）。

分层契约：本模块位于 `server/strategy`，只依赖 `server.domain`、标准库与 `pyarrow`，
绝不导入 `broker`、`oms`、`risk`、`storage`、`api`、`ipc`、`platforms`。
回测（T-04）、模拟盘与实盘共用同一个 `evaluate_factor` 入口。
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from server.domain.market import MarketBar

PLACE_6 = Decimal("0.000001")
FORBIDDEN_FACTOR_IMPORTS = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "socket",
        "http",
        "urllib",
        "pathlib",
        "shutil",
        "sqlite3",
        "sqlalchemy",
        "pickle",
        "ctypes",
        "importlib",
        "builtins",
        "server.broker",
        "server.oms",
        "server.risk",
        "server.storage",
        "server.api",
        "server.ipc",
    }
)
FORBIDDEN_FACTOR_CALLS = frozenset(
    {"open", "eval", "exec", "compile", "__import__", "getattr", "setattr", "delattr", "globals"}
)


def _d6(val: float) -> Decimal:
    if not math.isfinite(val):
        return Decimal(0)
    return Decimal(f"{val:.6f}").quantize(PLACE_6)


class FactorError(ValueError):
    """因子定义、权限、静态检查或计算违约。"""


class FactorLookaheadError(FactorError):
    """因子前视违约：试图访问决策时刻 as_of 之后才收盘的数据，或全量/截断重算不一致。"""


class FactorStage(StrEnum):
    """因子生命周期（T-10 第 3 节）。"""

    CANDIDATE = "CANDIDATE"
    VALIDATED = "VALIDATED"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"
    REJECTED = "REJECTED"


class FactorAuthorKind(StrEnum):
    OWNER = "OWNER"
    AI = "AI"


class BlockKind(StrEnum):
    """声明式因子积木（T-10 第 2 节）。"""

    RETURN = "RETURN"  # 过去 window 日收益率
    VOLATILITY = "VOLATILITY"  # 过去 window 日日收益波动率（取负或按权重组合）
    SMA_DEVIATION = "SMA_DEVIATION"  # 收盘价相对 window 日均线偏离率
    VOLUME_RATIO = "VOLUME_RATIO"  # 近 5 日均量相对 window 日均量之比
    MOMENTUM_RANK = "MOMENTUM_RANK"  # 截面過去 window 日收益率排名分位 [0, 1]


@dataclass(frozen=True)
class DeclarativeBlock:
    kind: BlockKind
    window: int
    weight: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if self.window < 2 or self.window > 504:
            raise FactorError("积木窗口必须在 [2, 504] 交易日之间")
        if not self.weight.is_finite() or self.weight == Decimal(0):
            raise FactorError("积木权重必须是非零有限 Decimal")


@dataclass(frozen=True)
class DeclarativeSpec:
    blocks: tuple[DeclarativeBlock, ...]
    warmup_days: int
    fields: tuple[str, ...] = ("close", "volume")

    def __post_init__(self) -> None:
        if not self.blocks:
            raise FactorError("声明式因子至少包含一个积木")
        min_warmup = max(b.window for b in self.blocks) + 1
        if self.warmup_days < min_warmup:
            raise FactorError(f"预热天数 {self.warmup_days} 小于积木所需最小天数 {min_warmup}")


class AsOfBarView:
    """受决策时刻 as_of 严格限制的只读行情视图（T-10b）：绝不暴露 bar.end > as_of 的数据。"""

    def __init__(self, raw_bars: Mapping[str, Sequence[MarketBar]], as_of: datetime) -> None:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise FactorError("as_of 必须带时区")
        self._as_of = as_of
        self._visible: dict[str, tuple[MarketBar, ...]] = {
            sym: tuple(b for b in seq if b.end <= as_of) for sym, seq in raw_bars.items()
        }

    @property
    def as_of(self) -> datetime:
        return self._as_of

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._visible))

    def bars_for(self, symbol: str) -> tuple[MarketBar, ...]:
        return self._visible.get(symbol, ())


CodeFactorFn = Callable[[AsOfBarView, Mapping[str, Decimal]], Mapping[str, Decimal]]


def validate_factor_code_source(source: str) -> str:  # noqa: PLR0912
    """对手动提交的代码式因子做 AST 静态安全检查，返回源码 SHA-256 哈希（T-10c）。"""
    if not source.strip():
        raise FactorError("代码式因子源码不能为空")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise FactorError(f"因子代码语法错误：{exc}") from exc

    has_compute = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "compute":
            if len(node.args.args) != 2:
                raise FactorError("compute 函数签名必须为 compute(data_as_of, params)")
            has_compute = True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0]
                if alias.name in FORBIDDEN_FACTOR_IMPORTS or top in FORBIDDEN_FACTOR_IMPORTS:
                    raise FactorError(f"因子代码禁止导入模块：{alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            top = mod.split(".", 1)[0]
            if mod in FORBIDDEN_FACTOR_IMPORTS or top in FORBIDDEN_FACTOR_IMPORTS:
                raise FactorError(f"因子代码禁止从模块导入：{mod}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_FACTOR_CALLS:
                raise FactorError(f"因子代码禁止调用内置函数：{node.func.id}")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise FactorError(f"因子代码禁止访问私有或双下划线属性：{node.attr}")

    if not has_compute:
        raise FactorError("代码式因子必须定义 compute(data_as_of, params) 函数")
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class FactorDefinition:
    """不可变的因子版本定义（T-10a/b/f）。"""

    factor_id: str
    version: str
    hypothesis: str
    author_kind: FactorAuthorKind
    author_name: str
    created_at: datetime
    declarative: DeclarativeSpec | None = None
    code_source: str | None = None
    code_fn: CodeFactorFn | None = None
    params: Mapping[str, Decimal] = field(default_factory=dict)
    max_ai_blocks: int = 4

    def __post_init__(self) -> None:
        if not self.factor_id.strip() or not self.version.strip():
            raise FactorError("factor_id 与 version 不能为空")
        if not self.hypothesis.strip():
            raise FactorError("因子假设说明（hypothesis）必填")
        if not self.author_name.strip():
            raise FactorError("作者名不能为空")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise FactorError("created_at 必须带时区")
        if self.declarative is None and self.code_fn is None:
            raise FactorError("因子必须提供声明式定义或代码函数")
        if self.declarative is not None and (
            self.code_fn is not None or self.code_source is not None
        ):
            raise FactorError("因子不能同时是声明式和代码式")
        if self.author_kind is FactorAuthorKind.AI:
            if self.declarative is None or self.code_fn is not None or self.code_source is not None:
                raise FactorError("AI 候选通道只允许提交声明式因子，禁止提交代码式因子（T-10f）")
            if len(self.declarative.blocks) > self.max_ai_blocks:
                cnt = len(self.declarative.blocks)
                raise FactorError(f"AI 因子积木数量（{cnt}）超过复杂度上限 {self.max_ai_blocks}")
        if self.code_source is not None:
            validate_factor_code_source(self.code_source)

    @property
    def warmup_days(self) -> int:
        if self.declarative is not None:
            return self.declarative.warmup_days
        return int(self.params.get("warmup_days", Decimal(20)))

    @property
    def definition_hash(self) -> str:
        payload = {
            "factor_id": self.factor_id,
            "version": self.version,
            "declarative": (
                [
                    {"kind": b.kind.value, "window": b.window, "weight": str(b.weight)}
                    for b in self.declarative.blocks
                ]
                if self.declarative is not None
                else None
            ),
            "code_source": self.code_source,
            "params": {k: str(v) for k, v in sorted(self.params.items())},
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:16]


def _compute_single_block(block: DeclarativeBlock, view: AsOfBarView) -> dict[str, float]:
    w = block.window
    raw: dict[str, float] = {}
    for sym in view.symbols:
        seq = view.bars_for(sym)
        if len(seq) < w + 1:
            continue
        closes = [float(b.close) for b in seq[-(w + 1) :]]
        vols = [float(b.volume) for b in seq[-(w + 1) :]]
        if block.kind in (BlockKind.RETURN, BlockKind.MOMENTUM_RANK):
            if closes[0] > 0:
                raw[sym] = closes[-1] / closes[0] - 1.0
        elif block.kind is BlockKind.VOLATILITY:
            rets = [
                closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1] > 0
            ]
            if len(rets) >= 2:
                m = sum(rets) / len(rets)
                var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
                raw[sym] = math.sqrt(var)
        elif block.kind is BlockKind.SMA_DEVIATION:
            sma = sum(closes[1:]) / w
            if sma > 0:
                raw[sym] = closes[-1] / sma - 1.0
        elif block.kind is BlockKind.VOLUME_RATIO:
            short_n = min(5, w)
            avg_all = sum(vols[1:]) / w
            avg_short = sum(vols[-short_n:]) / short_n
            if avg_all > 0:
                raw[sym] = avg_short / avg_all - 1.0

    if block.kind is BlockKind.MOMENTUM_RANK and raw:
        sorted_syms = sorted(raw, key=lambda s: (raw[s], s))
        denom = max(len(sorted_syms) - 1, 1)
        return {s: idx / denom for idx, s in enumerate(sorted_syms)}
    return raw


def evaluate_factor(
    defn: FactorDefinition,
    bars: Mapping[str, Sequence[MarketBar]],
    as_of: datetime,
) -> dict[str, Decimal]:
    """统一因子计算入口（回测、模拟盘、实盘共用同一实现，严格按 as_of 截断可见 K 线）。"""
    view = AsOfBarView(bars, as_of)
    if defn.declarative is not None:
        combined: dict[str, float] = {}
        valid_counts: dict[str, int] = {}
        total_blocks = len(defn.declarative.blocks)
        for block in defn.declarative.blocks:
            b_vals = _compute_single_block(block, view)
            w_float = float(block.weight)
            for sym, val in b_vals.items():
                combined[sym] = combined.get(sym, 0.0) + val * w_float
                valid_counts[sym] = valid_counts.get(sym, 0) + 1
        return {
            sym: _d6(val)
            for sym, val in sorted(combined.items())
            if valid_counts.get(sym, 0) == total_blocks and math.isfinite(val)
        }
    if defn.code_fn is not None:
        out = defn.code_fn(view, defn.params)
        result: dict[str, Decimal] = {}
        for sym, dval in sorted(out.items()):
            if not isinstance(dval, Decimal) or not dval.is_finite():
                raise FactorError(f"因子 {defn.factor_id} 对 {sym} 输出了非有限 Decimal：{dval!r}")
            result[sym] = dval.quantize(PLACE_6)
        return result
    raise FactorError(f"因子 {defn.factor_id} 缺少可执行定义")


@dataclass(frozen=True)
class FeatureSnapshotRow:
    as_of: datetime
    symbol: str
    value: Decimal


def write_feature_snapshot(
    features_root: Path,
    *,
    factor_id: str,
    version: str,
    data_version: str,
    rows: Sequence[FeatureSnapshotRow],
) -> Path:
    """将因子值写入 lake/features/<factor_id>/<version>/<data_version>.parquet（不可覆盖）。"""
    out_dir = features_root / factor_id / version
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{data_version}.parquet"
    if out_file.exists():
        raise FileExistsError(f"特征快照已存在，拒绝覆盖：{out_file}")
    table = pa.table(
        {
            "as_of": [r.as_of.isoformat() for r in rows],
            "symbol": [r.symbol for r in rows],
            "value": [str(r.value) for r in rows],
            "factor_id": [factor_id for _ in rows],
            "version": [version for _ in rows],
            "data_version": [data_version for _ in rows],
        }
    )
    pq.write_table(table, out_file)
    return out_file


# ---------- T-10d / T-10e：十步自动验证流水线 ----------


@dataclass(frozen=True)
class FactorValidationConfig:
    """因子验证阈值（T-10 第 5 节：起始偏严，放普通运行配置，调松须二次确认并留记录）。"""

    min_sample_days: int = 30
    min_coverage_ratio: Decimal = Decimal("0.80")
    min_abs_rank_ic_1d: Decimal = Decimal("0.02")
    min_abs_rank_ic_5d: Decimal = Decimal("0.02")
    min_abs_rank_ic_20d: Decimal = Decimal("0.02")
    min_oos_rank_ic: Decimal = Decimal("0.02")
    max_redundancy_corr: Decimal = Decimal("0.70")
    max_daily_turnover: Decimal = Decimal("0.80")
    oos_split_ratio: Decimal = Decimal("0.70")


DEFAULT_VALIDATION_CONFIG = FactorValidationConfig()


@dataclass(frozen=True)
class FactorValidationReport:
    report_id: str
    factor_id: str
    version: str
    data_version: str
    definition_hash: str
    verdict: LiteralVerdict
    passed: bool
    sample_days: int
    lookahead_clean: bool
    deterministic_clean: bool
    coverage_ratio: Decimal
    rank_ic_1d: Decimal
    rank_ic_5d: Decimal
    rank_ic_20d: Decimal
    oos_rank_ic: Decimal
    stability_passed: bool
    decay_reasonable: bool
    max_redundancy_corr: Decimal
    daily_turnover: Decimal
    stressed_cost_positive: bool
    trial_count: int
    multiple_testing_passed: bool
    reasons: tuple[str, ...]


LiteralVerdict = str  # "VALIDATED" | "REJECTED" | "INCONCLUSIVE"


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    n = len(xs)
    if n < 2 or len(ys) != n:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    dx = sum((x - mx) ** 2 for x in xs)
    dy = sum((y - my) ** 2 for y in ys)
    denom = math.sqrt(dx * dy)
    return num / denom if denom > 1e-12 else 0.0


def _ranks(vals: Sequence[float]) -> list[float]:
    indexed = sorted(enumerate(vals), key=lambda pair: pair[1])
    out = [0.0] * len(vals)
    for rank_idx, (orig_idx, _) in enumerate(indexed):
        out[orig_idx] = float(rank_idx)
    return out


def _rank_ic(xs: Sequence[float], ys: Sequence[float]) -> float:
    if len(xs) < 2 or len(ys) != len(xs):
        return 0.0
    return _pearson(_ranks(xs), _ranks(ys))


def run_factor_validation(  # noqa: PLR0912, PLR0913, PLR0915
    defn: FactorDefinition,
    bars: Mapping[str, Sequence[MarketBar]],
    *,
    report_id: str,
    data_version: str,
    config: FactorValidationConfig = DEFAULT_VALIDATION_CONFIG,
    trial_count: int = 1,
    active_factor_series: Mapping[str, Sequence[float]] | None = None,
    untruncated_probe: Callable[
        [Mapping[str, Sequence[MarketBar]], datetime], Mapping[str, Decimal]
    ]
    | None = None,
) -> FactorValidationReport:
    """执行 T-10 第 5 节的十步自动验证流水线，任何一项不达标即判失败。"""
    all_dates: sorted_dates_type = sorted({b.end for seq in bars.values() for b in seq})
    warmup = defn.warmup_days
    eval_points = all_dates[warmup:]
    if len(eval_points) < config.min_sample_days:
        return FactorValidationReport(
            report_id=report_id,
            factor_id=defn.factor_id,
            version=defn.version,
            data_version=data_version,
            definition_hash=defn.definition_hash,
            verdict="INCONCLUSIVE",
            passed=False,
            sample_days=len(eval_points),
            lookahead_clean=True,
            deterministic_clean=True,
            coverage_ratio=Decimal(0),
            rank_ic_1d=Decimal(0),
            rank_ic_5d=Decimal(0),
            rank_ic_20d=Decimal(0),
            oos_rank_ic=Decimal(0),
            stability_passed=False,
            decay_reasonable=False,
            max_redundancy_corr=Decimal(0),
            daily_turnover=Decimal(0),
            stressed_cost_positive=False,
            trial_count=max(1, trial_count),
            multiple_testing_passed=False,
            reasons=(f"样本量不足：{len(eval_points)} < {config.min_sample_days}（不可判定）",),
        )

    universe_size = max(len(bars), 1)
    lookahead_clean = True
    deterministic_clean = True
    coverage_ratios: list[float] = []
    daily_snapshots: list[dict[str, float]] = []

    # 第 1、2、3 步：前视检查、确定性检查、覆盖率检查
    for ts in eval_points:
        truncated_bars = {sym: [b for b in seq if b.end <= ts] for sym, seq in bars.items()}
        try:
            run1 = evaluate_factor(defn, truncated_bars, ts)
            run2 = evaluate_factor(defn, truncated_bars, ts)
        except Exception:
            deterministic_clean = False
            break
        if run1 != run2:
            deterministic_clean = False
        # 用全量数据调用 probe（或默认 evaluate_factor）：若偷看未来 K 线则与截断数据结果不一致
        probe_fn = untruncated_probe or (lambda b_map, at: evaluate_factor(defn, b_map, at))
        full_out = probe_fn(bars, ts)
        if dict(full_out) != run1:
            lookahead_clean = False
        coverage_ratios.append(len(run1) / universe_size)
        daily_snapshots.append({s: float(v) for s, v in run1.items()})

    avg_cov = _d6(sum(coverage_ratios) / len(coverage_ratios)) if coverage_ratios else Decimal(0)

    # 构造未来 1、5、20 日收益率以计算 RankIC / 时序预测力
    close_by_ts: dict[datetime, dict[str, float]] = {}
    for sym, seq in bars.items():
        for b in seq:
            close_by_ts.setdefault(b.end, {})[sym] = float(b.close)

    def _horizon_ics(horizon: int, indices: range) -> list[float]:
        ics: list[float] = []
        for idx in indices:
            if idx + horizon >= len(eval_points):
                break
            t_now = eval_points[idx]
            t_fut = eval_points[idx + horizon]
            f_map = daily_snapshots[idx]
            p_now = close_by_ts.get(t_now, {})
            p_fut = close_by_ts.get(t_fut, {})
            common = sorted(s for s in f_map if s in p_now and s in p_fut and p_now[s] > 0)
            if len(common) >= 2:
                xs = [f_map[s] for s in common]
                ys = [p_fut[s] / p_now[s] - 1.0 for s in common]
                ics.append(_rank_ic(xs, ys))
        return ics

    split_idx = max(1, int(len(eval_points) * float(config.oos_split_ratio)))
    in_sample_range = range(0, split_idx)
    oos_range = range(split_idx, len(eval_points))

    ics_1d = _horizon_ics(1, in_sample_range)
    ics_5d = _horizon_ics(5, in_sample_range)
    ics_20d = _horizon_ics(20, in_sample_range)
    oos_ics = _horizon_ics(1, oos_range) or _horizon_ics(5, oos_range)

    mean_ic_1d = sum(ics_1d) / len(ics_1d) if ics_1d else 0.0
    mean_ic_5d = sum(ics_5d) / len(ics_5d) if ics_5d else mean_ic_1d
    mean_ic_20d = sum(ics_20d) / len(ics_20d) if ics_20d else mean_ic_5d
    mean_oos_ic = sum(oos_ics) / len(oos_ics) if oos_ics else 0.0

    # 第 5 步：稳定性（前半段与后半段 IC 同号且方向与样本外一致）
    half = max(1, len(ics_1d) // 2)
    first_half = sum(ics_1d[:half]) / max(len(ics_1d[:half]), 1)
    second_half = sum(ics_1d[half:]) / max(len(ics_1d[half:]), 1)
    stability_passed = (first_half * second_half >= 0.0) and (mean_ic_1d * mean_oos_ic >= 0.0)

    # 第 6 步：衰减合理性（1d 与 5d 同向且未发生反向崩塌）
    decay_reasonable = (mean_ic_1d * mean_ic_5d >= 0.0) and abs(mean_ic_1d) >= float(
        config.min_abs_rank_ic_1d
    )

    # 第 7 步：与已有正式因子的冗余相关性检查
    flat_self = [sum(snap.values()) / max(len(snap), 1) for snap in daily_snapshots]
    max_corr = 0.0
    if active_factor_series:
        for _other_id, other_vals in active_factor_series.items():
            m_len = min(len(flat_self), len(other_vals))
            if m_len >= 5:
                c = abs(_pearson(flat_self[:m_len], list(other_vals[:m_len])))
                max_corr = max(max_corr, c)
    max_red_dec = _d6(max_corr)

    # 第 8 步：换手率与 2× 成本压力后预测力
    top_picks = [max(snap, key=lambda k: snap[k]) if snap else "" for snap in daily_snapshots]
    switches = sum(1 for i in range(1, len(top_picks)) if top_picks[i] != top_picks[i - 1])
    turnover = _d6(switches / max(len(top_picks) - 1, 1))
    # 2× 交易成本折算：每次换手扣除 2 * 10 bps = 20 bps 后净收益或净 IC 仍大于 0
    stressed_net_edge = abs(mean_oos_ic) - float(turnover) * 0.0020
    stressed_cost_positive = stressed_net_edge > 0.0 and turnover <= config.max_daily_turnover

    # 第 9 步：多重检验校正（随累计试验次数提高所需的 t 统计量门槛）
    trials = max(1, trial_count)
    ic_std = (
        math.sqrt(sum((x - mean_ic_1d) ** 2 for x in ics_1d) / max(len(ics_1d) - 1, 1))
        if len(ics_1d) > 1
        else 1.0
    )
    t_stat = abs(mean_ic_1d) * math.sqrt(len(ics_1d)) / max(ic_std, 1e-6) if ics_1d else 0.0
    required_t = 1.96 + 0.35 * math.log(trials)
    multiple_testing_passed = t_stat >= required_t

    reasons: list[str] = []
    if not lookahead_clean:
        reasons.append("前视检查未通过：全量与截断 as_of 重算结果不一致")
    if not deterministic_clean:
        reasons.append("确定性检查未通过")
    if avg_cov < config.min_coverage_ratio:
        reasons.append(f"覆盖率不足：{avg_cov} < {config.min_coverage_ratio}")
    if abs(_d6(mean_ic_1d)) < config.min_abs_rank_ic_1d:
        reasons.append(f"1日 RankIC 不足：{_d6(mean_ic_1d)}")
    if abs(_d6(mean_ic_5d)) < config.min_abs_rank_ic_5d:
        reasons.append(f"5日 RankIC 不足：{_d6(mean_ic_5d)}")
    if abs(_d6(mean_ic_20d)) < config.min_abs_rank_ic_20d:
        reasons.append(f"20日 RankIC 不足：{_d6(mean_ic_20d)}")
    if abs(_d6(mean_oos_ic)) < config.min_oos_rank_ic:
        reasons.append(f"样本外 RankIC 不足：{_d6(mean_oos_ic)}")
    if not stability_passed:
        reasons.append("子区间稳定性检查未通过（IC 方向反转）")
    if not decay_reasonable:
        reasons.append("持有期衰减曲线异常")
    if max_red_dec > config.max_redundancy_corr:
        reasons.append(f"与已有正式因子相关性过高：{max_red_dec} > {config.max_redundancy_corr}")
    if not stressed_cost_positive:
        reasons.append("换手过高或 2× 成本压力下净预测力不足")
    if not multiple_testing_passed:
        reasons.append(
            f"多重检验校正未通过：t={t_stat:.2f} < 门槛 {required_t:.2f}（trials={trials}）"
        )

    passed = len(reasons) == 0
    return FactorValidationReport(
        report_id=report_id,
        factor_id=defn.factor_id,
        version=defn.version,
        data_version=data_version,
        definition_hash=defn.definition_hash,
        verdict="VALIDATED" if passed else "REJECTED",
        passed=passed,
        sample_days=len(eval_points),
        lookahead_clean=lookahead_clean,
        deterministic_clean=deterministic_clean,
        coverage_ratio=avg_cov,
        rank_ic_1d=_d6(mean_ic_1d),
        rank_ic_5d=_d6(mean_ic_5d),
        rank_ic_20d=_d6(mean_ic_20d),
        oos_rank_ic=_d6(mean_oos_ic),
        stability_passed=stability_passed,
        decay_reasonable=decay_reasonable,
        max_redundancy_corr=max_red_dec,
        daily_turnover=turnover,
        stressed_cost_positive=stressed_cost_positive,
        trial_count=trials,
        multiple_testing_passed=multiple_testing_passed,
        reasons=tuple(reasons),
    )


sorted_dates_type = list[datetime]

__all__ = (
    "AsOfBarView",
    "BlockKind",
    "CodeFactorFn",
    "DeclarativeBlock",
    "DeclarativeSpec",
    "FactorAuthorKind",
    "FactorDefinition",
    "FactorError",
    "FactorLookaheadError",
    "FactorStage",
    "FactorValidationConfig",
    "FactorValidationReport",
    "FeatureSnapshotRow",
    "evaluate_factor",
    "run_factor_validation",
    "validate_factor_code_source",
    "write_feature_snapshot",
)
