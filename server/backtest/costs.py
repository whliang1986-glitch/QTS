"""成本与撮合费用模型（T-05，V4.1 8.3 & economic-gate.md 第 0 节）。

涵盖全部交易摩擦成本：
1. 券商佣金（IBKR Fixed 口径：每股 $0.005、最低 $1.00、上限成交额 1%）；
2. 买卖半价差（`half_spread_bps`）与冲击滑点（`slippage_bps`）；
3. 美股监管费（SEC 规费 + FINRA 交易活动费 TAF，按分向上取整）；
4. 汇兑费用（`fx_fee_bps`，USD 本币账户默认 0）；
5. 压力测试乘数（`stress_multiplier`）：`model.stressed(Decimal(2))` 一键生成 2× 成本压力模型。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from server.domain.enums import Side

CENT = Decimal("0.01")
BPS = Decimal("0.0001")


@dataclass(frozen=True)
class CostBreakdown:
    """单笔交易的成本拆解（美元，精确到美分）。"""

    broker_commission: Decimal
    regulatory_fee: Decimal
    slippage_cost: Decimal
    fx_cost: Decimal
    spread_cost: Decimal

    @property
    def explicit_fee(self) -> Decimal:
        """记入券商成交单 commission 字段的费用（佣金 + 监管费 + 滑点折算 + 汇兑费）。"""
        return self.broker_commission + self.regulatory_fee + self.slippage_cost + self.fx_cost

    @property
    def total_cost(self) -> Decimal:
        """含半价差在内的全口径交易成本。"""
        return self.explicit_fee + self.spread_cost


@dataclass(frozen=True)
class UsEtfCostModel:
    """美股与 ETF 全口径成本模型（实现 CommissionModel 协议，可直接注入 SimBroker）。"""

    name: str = "us_etf_full_v1"
    per_share: Decimal = Decimal("0.005")
    min_commission: Decimal = Decimal("1.00")
    max_commission_rate: Decimal = Decimal("0.01")
    sec_fee_rate: Decimal = Decimal("0.0000278")  # SEC 卖出规费率（约 $27.80 / 百万美元）
    finra_taf_per_share: Decimal = Decimal("0.000166")  # FINRA TAF 每股费率
    finra_taf_cap: Decimal = Decimal("8.30")  # 单笔 TAF 上限
    half_spread_bps: Decimal = Decimal("2")
    slippage_bps: Decimal = Decimal("3")
    fx_fee_bps: Decimal = Decimal("0")
    stress_multiplier: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if self.per_share < 0 or self.min_commission < 0 or self.max_commission_rate <= 0:
            raise ValueError("佣金参数不合法")
        if self.sec_fee_rate < 0 or self.finra_taf_per_share < 0 or self.finra_taf_cap < 0:
            raise ValueError("监管费参数不能为负")
        if self.half_spread_bps < 0 or self.slippage_bps < 0 or self.fx_fee_bps < 0:
            raise ValueError("点差、滑点与汇兑费率不能为负")
        if self.stress_multiplier <= 0:
            raise ValueError("压力乘数必须为正")

    @property
    def effective_half_spread_bps(self) -> Decimal:
        """考虑压力乘数后的有效半价差（bps）。"""
        return self.half_spread_bps * self.stress_multiplier

    def stressed(self, multiplier: Decimal = Decimal(2)) -> UsEtfCostModel:
        """返回成本放大 multiplier 倍的压力测试模型（economic-gate.md 第 0 节：成本加倍）。"""
        if multiplier <= 0:
            raise ValueError("压力乘数必须为正")
        new_mult = self.stress_multiplier * multiplier
        return replace(
            self,
            name=f"{self.name}_stress_{new_mult}x",
            stress_multiplier=new_mult,
        )

    def breakdown(
        self, quantity: Decimal, price: Decimal, *, side: Side | None = None
    ) -> CostBreakdown:
        """计算单笔成交的各项成本明细。若未区分方向，按保守口径计入监管费。"""
        if quantity <= 0 or price <= 0:
            raise ValueError("成交数量与价格必须为正")
        notional = quantity * price
        mult = self.stress_multiplier

        raw_comm = max(quantity * self.per_share, self.min_commission)
        raw_comm = min(raw_comm, notional * self.max_commission_rate)
        broker_comm = (raw_comm * mult).quantize(CENT, rounding=ROUND_HALF_UP)

        # SEC 费与 FINRA TAF 在卖出时收取；无方向上下文时按保守全额计入，避免低估成本
        charge_reg = side is None or side is Side.SELL
        if charge_reg:
            sec = (notional * self.sec_fee_rate * mult).quantize(CENT, rounding=ROUND_CEILING)
            taf_raw = min(quantity * self.finra_taf_per_share, self.finra_taf_cap)
            taf = (taf_raw * mult).quantize(CENT, rounding=ROUND_CEILING)
            reg_fee = sec + taf
        else:
            reg_fee = Decimal("0.00")

        slip = (notional * self.slippage_bps * BPS * mult).quantize(CENT, rounding=ROUND_HALF_UP)
        fx = (notional * self.fx_fee_bps * BPS * mult).quantize(CENT, rounding=ROUND_HALF_UP)
        spread = (notional * self.half_spread_bps * BPS * mult).quantize(
            CENT, rounding=ROUND_HALF_UP
        )
        return CostBreakdown(
            broker_commission=broker_comm,
            regulatory_fee=reg_fee,
            slippage_cost=slip,
            fx_cost=fx,
            spread_cost=spread,
        )

    def __call__(self, quantity: Decimal, price: Decimal) -> Decimal:
        """CommissionModel 协议入口：返回记入账本的单笔显式+滑点成本。"""
        return self.breakdown(quantity, price).explicit_fee


__all__ = ("CostBreakdown", "UsEtfCostModel")
