"""链路冒烟策略（T-02，只在模拟盘运行）：每天制造成交样本与不会成交的挂单样本。

- 成交样本（fill_symbol）：没持仓就以可成交的限价买入 quantity 股；有持仓就全部卖出。
  一天买、一天卖。
- 挂单样本（passive_symbol）：每天挂一张远离市价的买单（买一价 × (1 − passive_discount)），
  当日有效，通常不成交、收盘过期；万一成交（跳空大跌），下一个交易日把它卖掉。
- 只能在模拟盘：运行模式不是 PAPER（包括未知）就拒绝运行；交易核心入口也会再拒一次
  （SMOKE_STRATEGY_ID 属于 PAPER_ONLY_STRATEGIES，见 server/domain/orders.py）。
- 单笔名义金额不超过 max_order_nav × NAV（附录 A：0.5%），超过就不下这一笔。
- 标的不与基准策略重叠（基准策略用 SPY 等 6 只），这样两个策略的持仓不会互相干扰。
- 改单、撤单样本需要 OMS 层的支持（策略只能提交意图，不能改单撤单），归 B-05 模拟盘联调。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from server.domain.enums import Mode, OrderClass, Side
from server.domain.market import Quote
from server.domain.orders import SMOKE_STRATEGY_ID
from server.strategy.base import Proposal, StrategyContext, StrategyError

CENT = Decimal("0.01")
ZERO = Decimal(0)
ONE = Decimal(1)


@dataclass(frozen=True)
class SmokeParams:
    fill_symbol: str = "IVV"
    passive_symbol: str = "VOO"
    quantity: Decimal = Decimal(1)
    price_concession: Decimal = Decimal("0.002")
    passive_discount: Decimal = Decimal("0.03")
    # 附录 A 的冒烟单上限（受保护风控参数里叫 smoke_order_nav）
    max_order_nav: Decimal = Decimal("0.005")

    def __post_init__(self) -> None:
        if self.fill_symbol == self.passive_symbol:
            raise ValueError("成交样本与挂单样本必须用不同的标的（一次运行每个标的只能提议一次）")
        if self.quantity <= ZERO or not ZERO < self.passive_discount < ONE:
            raise ValueError("股数或挂单折扣不合法")


class SmokeStrategy:
    def __init__(self, params: SmokeParams | None = None) -> None:
        self._p = params or SmokeParams()

    @property
    def strategy_id(self) -> str:
        return SMOKE_STRATEGY_ID

    @property
    def version(self) -> str:
        return "v1"

    @property
    def universe(self) -> frozenset[str]:
        return frozenset({self._p.fill_symbol, self._p.passive_symbol})

    def decide(self, ctx: StrategyContext) -> Sequence[Proposal]:
        if ctx.mode != Mode.PAPER.value:
            raise StrategyError(f"冒烟策略只能在模拟盘运行（当前模式：{ctx.mode or '未知'}）")
        if ctx.nav is None or ctx.nav <= ZERO:
            raise StrategyError("缺少净值，冒烟策略不运行")
        cap = ctx.nav * self._p.max_order_nav
        out = []
        for symbol, passive in ((self._p.fill_symbol, False), (self._p.passive_symbol, True)):
            quote = _quote(ctx, symbol)
            held = ctx.positions.get(symbol, ZERO)
            if held != held.to_integral_value():
                raise StrategyError(f"{symbol} 有碎股持仓 {held}，需人工处理")
            if held > ZERO:
                limit = _cents(quote.bid * (ONE - self._p.price_concession))  # type: ignore[operator]
                out.append(Proposal(symbol=symbol, side=Side.SELL, quantity=held, limit_price=limit,
                                    order_class=OrderClass.REDUCE,
                                    reason="冒烟：卖出前一日的样本"))  # fmt: skip
                continue
            if passive:
                limit = _cents(quote.bid * (ONE - self._p.passive_discount))  # type: ignore[operator]
                why = "冒烟：远离市价的挂单样本（当日过期）"
            else:
                limit = _cents(quote.ask * (ONE + self._p.price_concession))  # type: ignore[operator]
                why = "冒烟：可成交的买入样本"
            if self._p.quantity * limit > cap:
                continue  # 超过冒烟单上限：这一笔不下
            out.append(Proposal(symbol=symbol, side=Side.BUY, quantity=self._p.quantity,
                                limit_price=limit, order_class=OrderClass.OPEN,
                                reason=why))  # fmt: skip
        return out


def _quote(ctx: StrategyContext, symbol: str) -> Quote:
    quote = ctx.quotes.get(symbol)
    if quote is None or quote.bid is None or quote.ask is None or quote.delayed or quote.quality:
        raise StrategyError(f"{symbol} 没有可用的实时买卖报价")
    if not ZERO < quote.bid <= quote.ask:
        raise StrategyError(f"{symbol} 的报价不合理")
    return quote


def _cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


__all__ = ("SmokeParams", "SmokeStrategy")
