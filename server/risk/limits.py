"""阈值规则（R-02b，V4.1 7.2 订单级 / 标的级 / 账户级 / 策略级；阈值见附录 A，由 Dylan 批准）。

每条规则只读 `RiskParameters`（当前生效值，面板调严立即生效）与 `RiskContext`；
所需上下文缺失一律拒绝。
数值一个都不在这里：比例与笔数全部来自受保护配置（R-02a）。

适用范围（R-04 移交：阈值规则豁免保护性止损，否则缩量会被改判拒绝、持仓失去保护）：
- 保护性止损：不受任何阈值规则约束（它的专项规则在 default_deny）；
- 减仓：降低风险，只受下单频率、未成交订单数、限价偏离约束；
- 开仓：受全部规则约束。可以缩量的给 REDUCE（单笔名义、单标的、总敞口、现金缓冲），其余给 REJECT；
  日内亏损超限给 HALT（持久化停止新增，V4.1 附录 A）。
止损后冷却期：冷却期内不得买回（不少于 N 个交易日，且须已过一个再平衡日）。
暂不在这里：报价陈旧、价差、时钟偏差（R-03 数据门禁）；冒烟策略单笔上限（T-02 定策略登记后接入）。
"""

from __future__ import annotations

from datetime import timedelta
from decimal import ROUND_FLOOR, Decimal

from server.domain.enums import AssetClass, OrderClass, OrderType, RiskVerdict, Side
from server.risk.engine import RiskContext, Rule, RuleResult, Subject
from server.risk.parameters import RiskParameters

V = RiskVerdict
RATE_WINDOW = timedelta(minutes=1)  # “笔/分钟”的计数窗口（单位定义，不是可调阈值）


def _reject(message: str) -> RuleResult:
    return RuleResult(verdict=V.REJECT, message=message)


def _reduce_to(subject: Subject, max_quantity: Decimal, message: str) -> RuleResult:
    """把数量缩到 max_quantity 以内（向下取整股）；缩到 0 即拒绝。"""
    allowed = max_quantity.to_integral_value(rounding=ROUND_FLOOR)
    if allowed <= 0:
        return _reject(message)
    if allowed >= subject.quantity:  # pragma: no cover - 调用方只在超限时调用
        return _reject(message)
    return RuleResult(verdict=V.REDUCE, message=message, max_quantity=allowed)


def _is_open(subject: Subject) -> bool:
    return subject.intent.order_class is OrderClass.OPEN


def _is_stop(subject: Subject) -> bool:
    return subject.intent.order_class is OrderClass.PROTECTIVE_STOP


def _pending_buy_notional(
    subject: Subject, context: RiskContext, symbol: str | None
) -> Decimal | None:
    """其他未结束买单的名义金额（symbol 为 None 表示全部标的）；有买单缺限价返回 None。"""
    total = Decimal(0)
    for o in context.open_orders or ():
        if o.side is not Side.BUY or o.intent_id == subject.intent.intent_id:
            continue
        if symbol is not None and o.symbol != symbol:
            continue
        if o.limit_price is None:
            return None
        total += o.remaining_quantity * o.limit_price
    return total


class _Rule:
    rule_id = ""
    version = "1"

    def __init__(self, params: RiskParameters) -> None:
        self._p = params

    def check(self, subject: Subject, context: RiskContext) -> RuleResult | None:
        raise NotImplementedError


class OrderNotionalRule(_Rule):
    """单笔订单最大名义金额 ≤ max_order_notional_nav × NAV（开仓）。"""

    rule_id = "max_order_notional"

    def check(self, subject: Subject, context: RiskContext) -> RuleResult | None:
        if not _is_open(subject):
            return None
        if context.nav is None or context.nav <= 0:
            return _reject("账户净值不可用，无法核对单笔上限")
        cap = self._p["max_order_notional_nav"] * context.nav
        if subject.quantity * subject.price <= cap:
            return None
        return _reduce_to(subject, cap / subject.price, f"单笔名义金额超过上限（上限 {cap}）")


class PositionLimitRule(_Rule):
    """单标的市值（持仓 + 其他未结束买单 + 本单）≤ 上限 × NAV（ETF 与个股分开，开仓）。"""

    rule_id = "max_position"

    def check(self, subject: Subject, context: RiskContext) -> RuleResult | None:
        if not _is_open(subject):
            return None
        symbol = subject.intent.symbol
        instrument = (context.instruments or {}).get(symbol)
        held = context.position(symbol)
        last = (context.last_prices or {}).get(symbol)
        pending = _pending_buy_notional(subject, context, symbol)
        if None in (instrument, held, last, pending, context.nav) or context.open_orders is None:
            return _reject("持仓、最新价、未结束订单、标的规格或净值不可用，无法核对单标的上限")
        assert instrument is not None and held is not None and last is not None  # noqa: S101
        assert pending is not None and context.nav is not None  # noqa: S101
        caps = {AssetClass.ETF: "max_position_etf_nav", AssetClass.STK: "max_position_stk_nav"}
        name = caps.get(instrument.asset_class)
        # 负持仓会让剩余额度变大；没有上限的资产类别无专项规则即禁止（不依赖 DefaultDeny 的顺序）
        if held < 0 or name is None:
            return _reject("存在负持仓或资产类别没有单标的上限")
        cap = self._p[name] * context.nav
        room = cap - held * last - pending
        if subject.quantity * subject.price <= room:
            return None
        return _reduce_to(
            subject, room / subject.price, f"单标的市值超过上限（上限 {cap}，已占用 {cap - room}）"
        )


class ExposureAndCashRule(_Rule):
    """总敞口 ≤ 上限 × NAV，且买入后现金 ≥ 最低现金缓冲 × NAV（开仓，取更严者）。"""

    rule_id = "exposure_and_cash"

    def check(self, subject: Subject, context: RiskContext) -> RuleResult | None:
        if not _is_open(subject):
            return None
        pending = _pending_buy_notional(subject, context, None)
        if (
            context.nav is None
            or context.cash is None
            or pending is None
            or context.open_orders is None
            or context.positions is None
        ):
            return _reject("净值、现金、持仓或未结束订单不可用，无法核对总敞口与现金缓冲")
        if any(p.quantity < 0 for p in context.positions):
            return _reject("存在负持仓，无法核对总敞口")
        exposure = Decimal(0)
        for position in context.positions:
            last = (context.last_prices or {}).get(position.symbol)
            if last is None:
                return _reject(f"{position.symbol} 没有最新价，无法计算总敞口")
            exposure += position.quantity * last
        exposure_room = self._p["max_total_exposure_nav"] * context.nav - exposure - pending
        available = context.cash.settled + min(context.cash.unsettled, Decimal(0)) - pending
        cash_room = available - self._p["min_cash_buffer_nav"] * context.nav
        room = min(exposure_room, cash_room)
        if subject.quantity * subject.price <= room:
            return None
        return _reduce_to(
            subject,
            room / subject.price,
            f"超过总敞口上限或低于最低现金缓冲（敞口余量 {exposure_room}，现金余量 {cash_room}）",
        )


class DailyLossRule(_Rule):
    """当日亏损（相对开盘净值）达到上限：HALT，持久化停止新增（开仓）。"""

    rule_id = "max_daily_loss"

    def check(self, subject: Subject, context: RiskContext) -> RuleResult | None:
        if not _is_open(subject):
            return None
        if context.nav is None or context.start_of_day_nav is None or context.start_of_day_nav <= 0:
            return _reject("净值或当日开盘净值不可用，无法核对日内亏损")
        loss = (context.start_of_day_nav - context.nav) / context.start_of_day_nav
        if loss >= self._p["max_daily_loss_nav"]:
            return RuleResult(verdict=V.HALT, message="日内亏损达到上限，停止新增")
        return None


class StrategyDrawdownRule(_Rule):
    """策略回撤达到上限：拒绝该策略的开仓（暂停策略；现场快照由策略层负责）。"""

    rule_id = "max_strategy_drawdown"

    def check(self, subject: Subject, context: RiskContext) -> RuleResult | None:
        if not _is_open(subject):
            return None
        drawdown = (context.strategy_drawdown or {}).get(subject.intent.strategy_id)
        if drawdown is None:
            return _reject("策略回撤不可用，无法核对策略回撤上限")
        if not Decimal(0) <= drawdown <= Decimal(1):  # 口径：回撤为 [0, 1] 的正比例，不是收益率
            return _reject(f"策略回撤口径异常（{drawdown}），应为 0 到 1 之间的比例")
        if drawdown >= self._p["max_strategy_drawdown"]:
            return _reject("策略回撤达到上限，暂停开仓")
        return None


class OpenOrdersRule(_Rule):
    """未成交订单数（不含保护性止损）达到上限：拒绝（开仓与减仓）。"""

    rule_id = "max_open_orders"

    def check(self, subject: Subject, context: RiskContext) -> RuleResult | None:
        if _is_stop(subject):
            return None
        if context.open_orders is None:
            return _reject("未结束订单不可用，无法核对未成交订单数")
        count = sum(
            1
            for o in context.open_orders
            if o.order_class is not OrderClass.PROTECTIVE_STOP
            and o.intent_id != subject.intent.intent_id
        )
        if count >= self._p["max_open_orders"]:
            return _reject("未成交订单数达到上限")
        return None


class OrderRateRule(_Rule):
    """一分钟内的签发笔数达到上限：拒绝（开仓与减仓；改单不计新签发）。"""

    rule_id = "max_orders_per_minute"

    def check(self, subject: Subject, context: RiskContext) -> RuleResult | None:
        if _is_stop(subject) or subject.amendment_id is not None:
            return None
        if context.recent_submissions is None:
            return _reject("近期签发记录不可用，无法核对下单频率")
        since = context.now - RATE_WINDOW
        recent = sum(1 for t in context.recent_submissions if t > since)
        if recent >= self._p["max_orders_per_minute"]:
            return _reject("下单频率达到上限")
        return None


class LimitDeviationRule(_Rule):
    """限价相对最新价的偏离 ≤ max_limit_deviation（开仓与减仓的限价单，含改单）。"""

    rule_id = "max_limit_deviation"

    def check(self, subject: Subject, context: RiskContext) -> RuleResult | None:
        if subject.intent.order_type is not OrderType.LIMIT:
            return None
        last = (context.last_prices or {}).get(subject.intent.symbol)
        if last is None or last <= 0:
            return _reject("最新价不可用，无法核对限价偏离")
        if abs(subject.price - last) / last > self._p["max_limit_deviation"]:
            return _reject("限价偏离最新价超过上限")
        return None


class StopCooldownRule(_Rule):
    """保护性止损成交后的冷却期内不得买回：不少于 N 个交易日，且须已过一个再平衡日（开仓）。"""

    rule_id = "stop_cooldown"

    def check(self, subject: Subject, context: RiskContext) -> RuleResult | None:
        if not _is_open(subject):
            return None
        if context.stop_cooldown is None:
            return _reject("止损冷却信息不可用，无法核对冷却期")
        cooldown = context.stop_cooldown.get(subject.intent.symbol)
        if cooldown is None:
            return None  # 该标的没有止损记录
        if (
            cooldown.trading_days_since_stop < self._p["stop_cooldown_trading_days"]
            or not cooldown.rebalanced_since_stop
        ):
            return _reject("该标的在止损后冷却期内，不得买回")
        return None


def limit_rules(params: RiskParameters) -> tuple[Rule, ...]:
    return (
        OrderNotionalRule(params),
        PositionLimitRule(params),
        ExposureAndCashRule(params),
        DailyLossRule(params),
        StrategyDrawdownRule(params),
        OpenOrdersRule(params),
        OrderRateRule(params),
        LimitDeviationRule(params),
        StopCooldownRule(params),
    )
