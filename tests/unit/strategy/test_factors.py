"""T-10（T-10a ~ T-10f）因子库测试：声明式/代码式定义、as_of 防前视、十步自动验证与 AI 通道。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from server.domain.market import MarketBar
from server.storage.factor_registry import (
    DELETE_FACTOR_PHRASE,
    PROMOTE_FACTOR_PHRASE,
    FactorRegistry,
)
from server.strategy.factors import (
    AsOfBarView,
    BlockKind,
    DeclarativeBlock,
    DeclarativeSpec,
    FactorAuthorKind,
    FactorDefinition,
    FactorError,
    FactorStage,
    FactorValidationConfig,
    FeatureSnapshotRow,
    evaluate_factor,
    run_factor_validation,
    validate_factor_code_source,
    write_feature_snapshot,
)

T0 = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)


def _build_trending_bars(days: int = 80) -> dict[str, list[MarketBar]]:
    """构造具备稳定动量差异的三只 ETF 日线序列（SPY 强趋势、QQQ 中趋势、IEF 弱趋势）。"""
    rates = {"SPY": Decimal("0.0030"), "QQQ": Decimal("0.0015"), "IEF": Decimal("0.0002")}
    prices = {"SPY": Decimal("100.00"), "QQQ": Decimal("100.00"), "IEF": Decimal("100.00")}
    out: dict[str, list[MarketBar]] = {s: [] for s in rates}
    for i in range(days):
        st = T0 + timedelta(days=i)
        ed = st + timedelta(hours=6, minutes=30)
        for sym, r in rates.items():
            # 加入微幅波动使标准差非零，同时保持截面排名稳定正相关
            wiggle = Decimal("0.0004") if (i + len(sym)) % 2 == 0 else Decimal("-0.0002")
            prices[sym] = (prices[sym] * (Decimal(1) + r + wiggle)).quantize(Decimal("0.0001"))
            px = prices[sym]
            out[sym].append(
                MarketBar(
                    symbol=sym,
                    start=st,
                    end=ed,
                    open=px,
                    high=px + Decimal("0.50"),
                    low=px - Decimal("0.50"),
                    close=px,
                    volume=Decimal(100_000 + i * 500),
                    source="test",
                    adjustment_version="v1",
                )
            )
    return out


def test_declarative_and_code_factors_full_lifecycle_to_active(tmp_path: Path) -> None:
    """验收项 1、5、7：声明式与代码式因子走完 候选->验证->批准->正式，三入口计算一致。"""
    bars = _build_trending_bars(80)
    reg = FactorRegistry(tmp_path / "meta.db", daily_ai_budget=3)

    # 1. 声明式因子（动量排名 + 收益率 + 均线偏离）
    decl_spec = DeclarativeSpec(
        blocks=(
            DeclarativeBlock(BlockKind.RETURN, window=5, weight=Decimal("0.5")),
            DeclarativeBlock(BlockKind.MOMENTUM_RANK, window=5, weight=Decimal("0.5")),
        ),
        warmup_days=10,
    )
    decl_def = FactorDefinition(
        factor_id="mom_combo",
        version="v1",
        hypothesis="短期动量与截面排名正相关延续",
        author_kind=FactorAuthorKind.OWNER,
        author_name="dylan",
        created_at=T0,
        declarative=decl_spec,
    )
    rec_decl = reg.add_candidate(decl_def)
    assert rec_decl.stage is FactorStage.CANDIDATE
    assert rec_decl.author_name == "dylan"
    assert rec_decl.hypothesis.startswith("短期动量")
    assert rec_decl.warmup_days == 10
    assert rec_decl.created_at == T0
    assert rec_decl.updated_at == T0

    rep_decl = run_factor_validation(
        decl_def,
        bars,
        report_id="val-mom-v1",
        data_version="dv1",
        config=FactorValidationConfig(min_sample_days=30),
        trial_count=1,
    )
    assert rep_decl.passed is True
    assert rep_decl.coverage_ratio == Decimal("1.000000")
    assert rep_decl.daily_turnover <= Decimal("0.800000")
    assert rep_decl.rank_ic_5d > Decimal(0)
    assert rep_decl.rank_ic_20d > Decimal(0)

    rec_decl = reg.record_validation(rep_decl, recorded_at=T0)
    assert rec_decl.stage is FactorStage.VALIDATED

    # 转正必须输入二次确认短语且必须为 OWNER
    with pytest.raises(FactorError, match="二次确认短语"):
        reg.promote_to_active(
            "mom_combo",
            "v1",
            actor_kind=FactorAuthorKind.OWNER,
            actor_name="dylan",
            at=T0,
            confirmation_phrase="WRONG",
        )
    rec_decl = reg.promote_to_active(
        "mom_combo",
        "v1",
        actor_kind=FactorAuthorKind.OWNER,
        actor_name="dylan",
        at=T0,
        confirmation_phrase=PROMOTE_FACTOR_PHRASE,
    )
    assert rec_decl.stage is FactorStage.ACTIVE

    # 2. 代码式因子（手动添加 + AST 静态安全检查）
    code_src = """
def compute(data_as_of, params):
    out = {}
    for sym in data_as_of.symbols:
        seq = data_as_of.bars_for(sym)
        if len(seq) >= 6:
            out[sym] = seq[-1].close / seq[-6].close - 1
    return out
"""
    assert len(validate_factor_code_source(code_src)) == 16

    def _code_fn(view: AsOfBarView, _params: Mapping[str, Decimal]) -> Mapping[str, Decimal]:
        res: dict[str, Decimal] = {}
        for sym in view.symbols:
            seq = view.bars_for(sym)
            if len(seq) >= 6:
                res[sym] = seq[-1].close / seq[-6].close - Decimal(1)
        return res

    code_def = FactorDefinition(
        factor_id="code_mom5",
        version="v1",
        hypothesis="5日动量纯代码计算",
        author_kind=FactorAuthorKind.OWNER,
        author_name="dylan",
        created_at=T0,
        code_source=code_src,
        code_fn=_code_fn,
        params={"warmup_days": Decimal(10)},
    )
    reg.add_candidate(code_def)
    rep_code = run_factor_validation(
        code_def,
        bars,
        report_id="val-code-v1",
        data_version="dv1",
        trial_count=reg.total_trials() + 1,
    )
    assert rep_code.passed is True
    reg.record_validation(rep_code, recorded_at=T0)
    rec_code = reg.promote_to_active(
        "code_mom5",
        "v1",
        actor_kind=FactorAuthorKind.OWNER,
        actor_name="dylan",
        at=T0,
        confirmation_phrase=PROMOTE_FACTOR_PHRASE,
    )
    assert rec_code.stage is FactorStage.ACTIVE

    # 3. 回测、模拟盘、实盘三处调用同一入口结果逐位一致，并可固化写入 features/ Parquet
    decision_ts = bars["SPY"][40].end
    bt_vals = evaluate_factor(decl_def, bars, decision_ts)
    paper_vals = evaluate_factor(decl_def, bars, decision_ts)
    live_vals = evaluate_factor(decl_def, bars, decision_ts)
    assert bt_vals == paper_vals == live_vals

    snap_path = write_feature_snapshot(
        tmp_path / "features",
        factor_id="mom_combo",
        version="v1",
        data_version="dv1",
        rows=[FeatureSnapshotRow(decision_ts, s, v) for s, v in bt_vals.items()],
    )
    assert pq.read_table(snap_path).num_rows == 3
    with pytest.raises(FileExistsError):
        write_feature_snapshot(
            tmp_path / "features",
            factor_id="mom_combo",
            version="v1",
            data_version="dv1",
            rows=[],
        )
    assert len(reg.audit_trail("mom_combo")) == 3


def test_lookahead_detection_and_forbidden_code_imports() -> None:
    """验收项 2：故意偷看未来数据的因子必须判失败；禁用模块与危险调用在静态检查阶段即拒绝。"""
    with pytest.raises(FactorError, match="禁止导入模块"):
        validate_factor_code_source("import os\ndef compute(data_as_of, params): return {}\n")
    with pytest.raises(FactorError, match="禁止调用内置函数"):
        validate_factor_code_source("def compute(data_as_of, params): return eval('{}')\n")

    bars = _build_trending_bars(60)
    spec = DeclarativeSpec(
        blocks=(
            DeclarativeBlock(BlockKind.SMA_DEVIATION, window=5),
            DeclarativeBlock(BlockKind.VOLATILITY, window=5),
            DeclarativeBlock(BlockKind.VOLUME_RATIO, window=6),
        ),
        warmup_days=10,
    )
    defn = FactorDefinition(
        factor_id="leaky",
        version="v1",
        hypothesis="测试偷看未来",
        author_kind=FactorAuthorKind.OWNER,
        author_name="dylan",
        created_at=T0,
        declarative=spec,
    )

    # 模拟一个在全量数据下偷看最后一根 K 线的违规探针
    def leaky_probe(
        raw_bars: Mapping[str, Sequence[MarketBar]], _at: datetime
    ) -> Mapping[str, Decimal]:
        return {sym: seq[-1].close for sym, seq in raw_bars.items()}

    rep = run_factor_validation(
        defn,
        bars,
        report_id="val-leaky",
        data_version="dv1",
        untruncated_probe=leaky_probe,
    )
    assert rep.lookahead_clean is False
    assert rep.passed is False
    assert rep.verdict == "REJECTED"


def test_deletion_rules_preserve_trial_count_and_protect_referenced_factors(tmp_path: Path) -> None:
    """验收项 3、4：删除候选因子后试验次数不减少；被引用过的因子拒绝删除，只能停用。"""
    bars = _build_trending_bars(70)
    reg = FactorRegistry(tmp_path / "meta.db")
    spec = DeclarativeSpec(
        blocks=(DeclarativeBlock(BlockKind.RETURN, window=5),),
        warmup_days=8,
    )
    cand1 = FactorDefinition(
        factor_id="f_cand",
        version="v1",
        hypothesis="待删候选",
        author_kind=FactorAuthorKind.OWNER,
        author_name="dylan",
        created_at=T0,
        declarative=spec,
    )
    reg.add_candidate(cand1)
    rep1 = run_factor_validation(cand1, bars, report_id="trial-1", data_version="dv1")
    reg.record_validation(rep1, recorded_at=T0)
    assert reg.total_trials() == 1

    # 未被引用的验证通过/候选因子可以删除，但试验次数绝不减少
    reg.delete_factor(
        "f_cand",
        "v1",
        actor_kind=FactorAuthorKind.OWNER,
        actor_name="dylan",
        at=T0,
        reason="放弃该候选",
    )
    assert reg.get("f_cand", "v1") is None
    assert reg.total_trials() == 1

    # 正式且被策略引用过的因子：不能直接删除，停用后因有引用也永不删除
    f_ref = FactorDefinition(
        factor_id="f_active",
        version="v1",
        hypothesis="被策略引用的正式因子",
        author_kind=FactorAuthorKind.OWNER,
        author_name="dylan",
        created_at=T0,
        declarative=spec,
    )
    reg.add_candidate(f_ref)
    rep2 = run_factor_validation(f_ref, bars, report_id="trial-2", data_version="dv1")
    reg.record_validation(rep2, recorded_at=T0)
    reg.promote_to_active(
        "f_active",
        "v1",
        actor_kind=FactorAuthorKind.OWNER,
        actor_name="dylan",
        at=T0,
        confirmation_phrase=PROMOTE_FACTOR_PHRASE,
    )
    refs = reg.register_reference(
        "f_active", "v1", consumer_kind="strategy", consumer_id="dual_mom:v1", at=T0
    )
    assert refs == ["strategy:dual_mom:v1"]

    with pytest.raises(FactorError, match="正式（ACTIVE）因子不能直接删除"):
        reg.delete_factor(
            "f_active",
            "v1",
            actor_kind=FactorAuthorKind.OWNER,
            actor_name="dylan",
            at=T0,
            reason="试图直接删",
        )

    retired_rec, affected = reg.retire_factor(
        "f_active",
        "v1",
        actor_kind=FactorAuthorKind.OWNER,
        actor_name="dylan",
        at=T0,
        reason="因子衰减停用",
    )
    assert retired_rec.stage is FactorStage.RETIRED
    assert affected == ["strategy:dual_mom:v1"]

    with pytest.raises(FactorError, match="已被 strategy:dual_mom:v1 引用过，禁止删除"):
        reg.delete_factor(
            "f_active",
            "v1",
            actor_kind=FactorAuthorKind.OWNER,
            actor_name="dylan",
            at=T0,
            reason="试图删已引用因子",
            confirmation_phrase=DELETE_FACTOR_PHRASE,
        )
    assert len(reg.list_factors()) == 1


def test_ai_candidate_channel_enforces_declarative_complexity_budget_and_no_promotion(
    tmp_path: Path,
) -> None:
    """验收项 6：AI 只能提交声明式因子，受复杂度上限与每日预算约束，且无法自行转正。"""
    reg = FactorRegistry(tmp_path / "meta.db", daily_ai_budget=2)
    spec_ok = DeclarativeSpec(
        blocks=(DeclarativeBlock(BlockKind.RETURN, window=5),),
        warmup_days=8,
    )

    # 1. AI 提交代码式因子：立即拒绝
    with pytest.raises(FactorError, match="只允许提交声明式因子"):
        FactorDefinition(
            factor_id="ai_code",
            version="v1",
            hypothesis="AI 试图传代码",
            author_kind=FactorAuthorKind.AI,
            author_name="alpha_bot",
            created_at=T0,
            code_fn=lambda _v, _p: {},
        )

    # 2. AI 提交超过积木复杂度上限（> 4 个积木）：立即拒绝
    spec_too_complex = DeclarativeSpec(
        blocks=tuple(DeclarativeBlock(BlockKind.RETURN, window=w) for w in (3, 5, 10, 15, 20)),
        warmup_days=25,
    )
    with pytest.raises(FactorError, match="超过复杂度上限"):
        FactorDefinition(
            factor_id="ai_complex",
            version="v1",
            hypothesis="过多积木拼接",
            author_kind=FactorAuthorKind.AI,
            author_name="alpha_bot",
            created_at=T0,
            declarative=spec_too_complex,
        )

    # 3. 每日预算为 2：第 1、2 个通过，第 3 个被预算拦截
    for idx in (1, 2):
        reg.submit_ai_candidate(
            FactorDefinition(
                factor_id=f"ai_f{idx}",
                version="v1",
                hypothesis=f"AI 候选 {idx}",
                author_kind=FactorAuthorKind.AI,
                author_name="alpha_bot",
                created_at=T0,
                declarative=spec_ok,
            )
        )
    with pytest.raises(FactorError, match="已达每日预算上限"):
        reg.submit_ai_candidate(
            FactorDefinition(
                factor_id="ai_f3",
                version="v1",
                hypothesis="超预算候选",
                author_kind=FactorAuthorKind.AI,
                author_name="alpha_bot",
                created_at=T0,
                declarative=spec_ok,
            )
        )

    # 4. AI 试图自行转正：立即拒绝
    with pytest.raises(FactorError, match="AI 不能批准因子转正"):
        reg.promote_to_active(
            "ai_f1",
            "v1",
            actor_kind=FactorAuthorKind.AI,
            actor_name="alpha_bot",
            at=T0,
            confirmation_phrase=PROMOTE_FACTOR_PHRASE,
        )
