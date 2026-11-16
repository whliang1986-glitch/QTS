"""事件驱动行业 ETF 策略（T-11，V4.1 第 9 章 & docs/tasks/README.md M3 补充）。

原则：
1. 首期只在行业 ETF 层面做、只做多；做空须按 G8 另立项；
2. 仅依据已通过多源交叉核验（`verified=True`）且严格早于 `decision_at` 的影响传导分数生成提议；
3. 正向影响超过 `entry_threshold` 且当前未持仓 -> `OrderClass.OPEN` 买入限价；
   已有持仓但影响转为负向（`<= exit_threshold`） -> `OrderClass.REDUCE` 卖出清仓；
4. 所有提议最终仍须通过 S-14 交易名单、待审批队列（S-10）及风控全套限额逐笔校验。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

from server.domain.enums import OrderClass, Side
from server.strategy.base import Proposal, StrategyContext

# (symbol, impact_score in [-1, 1], verified_by_multi_source)
ImpactFeed = Callable[[datetime], Sequence[tuple[str, Decimal, bool]]]


@dataclass(frozen=True)
class EventDrivenConfig:
    entry_threshold: Decimal = Decimal("0.30")
    exit_threshold: Decimal = Decimal("-0.10")
    position_weight: Decimal = Decimal("0.04")
    max_positions: int = 3


DEFAULT_EVENT_CONFIG = EventDrivenConfig()


class EventDrivenEtfStrategy:
    """T-11 事件驱动行业 ETF 只做多策略。"""

    def __init__(
        self,
        universe: frozenset[str],
        impact_feed: ImpactFeed,
        *,
        config: EventDrivenConfig = DEFAULT_EVENT_CONFIG,
        version: str = "v1",
    ) -> None:
        self._universe = universe
        self._impact_feed = impact_feed
        self._config = config
        self._version = version

    @property
    def strategy_id(self) -> str:
        return "event_driven_etf"

    @property
    def version(self) -> str:
        return self._version

    @property
    def universe(self) -> frozenset[str]:
        return self._universe

    def decide(self, context: StrategyContext) -> Sequence[Proposal]:
        if context.nav is None or context.nav <= Decimal(0):
            return ()
        cash = context.settled_cash if context.settled_cash is not None else Decimal(0)
        impacts = self._impact_feed(context.decision_at)
        score_map: dict[str, tuple[Decimal, bool]] = {}
        for sym, score, verified in impacts:
            if sym in self._universe:
                score_map[sym] = (score, verified)

        proposals: list[Proposal] = []
        # 1. 先处理已有持仓的退出（影响转负或已验证利空）
        for sym, held in sorted(context.positions.items()):
            if held <= Decimal(0) or sym not in self._universe:
                continue
            score, verified = score_map.get(sym, (Decimal(0), False))
            if verified and score <= self._config.exit_threshold:
                px = _ref_price(context, sym)
                if px is not None:
                    proposals.append(
                        Proposal(
                            symbol=sym,
                            side=Side.SELL,
                            quantity=held,
                            limit_price=px,
                            order_class=OrderClass.REDUCE,
                            reason=f"事件影响转负（score={score}），减仓退出",
                        )
                    )

        # 2. 按分数从高到低筛选可开仓行业 ETF（仅限多源核验通过且无持仓）
        active_held = sum(1 for q in context.positions.values() if q > 0)
        slots_left = max(0, self._config.max_positions - active_held)
        if slots_left <= 0 or cash <= Decimal(0):
            return tuple(proposals)

        candidates = sorted(
            (
                (score, sym)
                for sym, (score, verified) in score_map.items()
                if verified
                and score >= self._config.entry_threshold
                and context.positions.get(sym, Decimal(0)) == Decimal(0)
            ),
            reverse=True,
        )
        budget_per_pos = min(context.nav * self._config.position_weight, cash)
        for score, sym in candidates[:slots_left]:
            px = _ref_price(context, sym)
            if px is None or px <= Decimal(0) or not context.bars.get(sym):
                continue
            qty = (budget_per_pos / px).to_integral_value(rounding=ROUND_FLOOR)
            if qty > Decimal(0):
                proposals.append(
                    Proposal(
                        symbol=sym,
                        side=Side.BUY,
                        quantity=qty,
                        limit_price=px,
                        order_class=OrderClass.OPEN,
                        reason=f"多源交叉核验事件正向催化（score={score}）",
                    )
                )
        return tuple(proposals)


def _ref_price(context: StrategyContext, symbol: str) -> Decimal | None:
    q = context.quotes.get(symbol)
    if q is not None and q.last is not None and q.last > Decimal(0):
        return q.last
    seq = context.bars.get(symbol, ())
    if seq:
        return seq[-1].close
    return None


__all__ = ("DEFAULT_EVENT_CONFIG", "EventDrivenConfig", "EventDrivenEtfStrategy", "ImpactFeed")
