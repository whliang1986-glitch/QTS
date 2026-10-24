"""默认禁用与保护性止损专项豁免（R-04，V4.1 7.2、7.6）。

领域模型已在类型层面挡住市价单、非 STK / ETF 标的、卖出开仓与买入减仓；这里是运行时的第二层，
按评估时的券商事实再检查一遍（失败即关闭：所需上下文缺失就拒绝，不当成 0）。

DefaultDenyRule（所有订单，“无专项规则即禁止”）：
- 只放行三种组合：开仓买入限价、减仓卖出限价、保护性止损卖出止损；其余一律拒绝；
- 标的必须有规格、规格与意图一致、可交易（tick 与日历齐全）、STK / ETF、乘数为 1
  （衍生品、期权一律禁止，ADR-16：期权首期不做）；
- 账户一致：持仓与现金必须属于意图的账户；任何一条持仓为负（券商侧异常，R-07）即拒绝；
- 不加杠杆（首期固定 1.0，不是可调参数）：买入名义金额 + 其他未结束买单名义金额 ≤ 可用现金，
  可用现金 = 已结算现金 + min(未结算, 0)（未结算的买入应付款先扣掉，避免同一笔钱用两次）；
- 不做空：减仓卖出 + 其他未结束减仓卖单 ≤ 持仓，没有正持仓一律拒绝卖出。
  保护性止损不占减仓额度（它是保护，不是减仓）；
  止损与减仓同时成交的超卖窗口由 R-06 处理（G3 前置）；
- 保护性止损交给内嵌的专项检查（不依赖 RuleSet 组装时有没有漏掉另一条规则）。

保护性止损（满足全部条件才放行）：
- 只能由 OMS 随持仓创建：strategy_id 必须是 PROTECTIVE_STOP_SOURCE（字符串只是第二层；
  主控是 S-01 的入口拒绝与专用入口，G3 前置）；
- 卖出止损单；持仓为正；每个持仓标的至多一张未结束的保护性止损；**数量必须等于持仓**
  （只保护一部分仓位等于放弃保护，也防止用小止损单占位挤掉真正的止损）；
- 止损价：成本价 ×（1 − 批准的止损距离）≤ 止损价 < 成本价，在最小跳动上，且低于最新价
  （不低于现价会立即触发，等于市价卖出，绕过市价单禁用）。参数或最新价缺失一律拒绝。
"""

from __future__ import annotations

from decimal import Decimal

from server.domain.enums import AssetClass, OrderClass, OrderType, RiskVerdict, Side
from server.domain.errors import InstrumentSpecMissingError
from server.domain.orders import PROTECTIVE_STOP_SOURCE
from server.domain.values import ValueModel
from server.risk.engine import RiskContext, RuleResult, Subject

ALLOWED_ASSETS = frozenset({AssetClass.STK, AssetClass.ETF})
# 唯一放行的（类别, 方向, 类型）组合；不在表里的一律拒绝
ALLOWED_SHAPES = frozenset(
    {
        (OrderClass.OPEN, Side.BUY, OrderType.LIMIT),
        (OrderClass.REDUCE, Side.SELL, OrderType.LIMIT),
        (OrderClass.PROTECTIVE_STOP, Side.SELL, OrderType.STOP),
    }
)

# 拒绝原因（测试按完整文本断言）
NO_SPEC = "缺少标的规格或规格与意图不一致"
NOT_TRADABLE = "标的规格不完整，不能交易"
NOT_SPOT = "衍生品与非现货标的默认禁止（无专项规则）"
BAD_SHAPE = "订单类别、方向与类型的组合不在允许范围（无专项规则即禁止）"
ACCOUNT_MISMATCH = "持仓或现金不属于意图的账户"
NEGATIVE_POSITION = "券商侧存在负持仓，拒绝新增订单（待对账）"
CASH_UNKNOWN = "现金或未结束订单不可用，无法确认不加杠杆"
UNPRICED_BUY = "有未结束买单缺少限价，无法计算资金占用"
LEVERAGE = "买入金额超过可用现金（首期不加杠杆，杠杆固定 1.0）"
HOLDING_UNKNOWN = "持仓或未结束订单不可用，无法确认不做空"
NO_HOLDING = "没有正持仓，卖出会形成空头（做空默认禁止）"
SHORT = "卖出数量加已挂减仓单超过持仓（做空默认禁止）"
STOP_SOURCE = "保护性止损只能由 OMS 随持仓创建"
STOP_PARAMS = "保护性止损参数未批准（受保护配置缺失）"
STOP_CONTEXT = "持仓、未结束订单或最新价不可用，无法核对保护性止损"
STOP_NO_HOLDING = "没有正持仓，不能挂保护性止损"
STOP_DUPLICATE = "该标的已有未结束的保护性止损（每个持仓标的至多一张）"
STOP_QUANTITY = "保护性止损数量必须等于持仓"
STOP_COST = "持仓成本价不可用，无法核对止损价"
STOP_RANGE = "止损价必须在成本价下方、且不低于批准的止损距离"
STOP_TICK = "止损价不在最小跳动上"
STOP_TRIGGERS = "止损价不低于最新价，提交即触发（等于市价卖出）"


def _reject(message: str) -> RuleResult:
    return RuleResult(verdict=RiskVerdict.REJECT, message=message)


class ProtectiveStopParams(ValueModel):
    """附录 A 的保护性止损参数，由 Dylan 在受保护配置里批准（R-02 加载）；开发方不定数值。"""

    stop_distance: Decimal  # 成本价下方的距离（比例，0 < d < 1）

    def model_post_init(self, __context: object) -> None:
        if not Decimal(0) < self.stop_distance < Decimal(1):
            raise ValueError("止损距离必须在 0 与 1 之间（比例）")


class DefaultDenyRule:
    rule_id = "default_deny"
    version = "2"

    def __init__(self, protective: ProtectiveStopParams | None) -> None:
        self._protective = protective

    def check(self, subject: Subject, context: RiskContext) -> RuleResult | None:
        message = _common(subject, context)
        if message is None:
            order_class = subject.intent.order_class
            if order_class is OrderClass.OPEN:
                message = _no_leverage(subject, context)
            elif order_class is OrderClass.REDUCE:
                message = _no_short(subject, context)
            else:
                message = _protective_stop(subject, context, self._protective)
        return None if message is None else _reject(message)


def _common(subject: Subject, context: RiskContext) -> str | None:
    return _spec(subject, context) or _accounts(subject, context)


def _spec(subject: Subject, context: RiskContext) -> str | None:
    intent = subject.intent
    instrument = (context.instruments or {}).get(intent.symbol)
    if instrument is None or instrument.symbol != intent.symbol:
        return NO_SPEC
    try:
        instrument.require_tradable()
    except InstrumentSpecMissingError:
        return NOT_TRADABLE
    if instrument.asset_class not in ALLOWED_ASSETS or instrument.multiplier != 1:
        return NOT_SPOT
    if (intent.order_class, intent.side, intent.order_type) not in ALLOWED_SHAPES:
        return BAD_SHAPE
    return None


def _accounts(subject: Subject, context: RiskContext) -> str | None:
    intent = subject.intent
    accounts = {p.account_id for p in context.positions or ()}
    if context.cash is not None:
        accounts.add(context.cash.account_id)
    if accounts - {intent.account_id}:
        return ACCOUNT_MISMATCH
    if any(p.quantity < 0 for p in context.positions or ()):
        return NEGATIVE_POSITION
    return None


def _no_leverage(subject: Subject, context: RiskContext) -> str | None:
    if context.cash is None or context.open_orders is None:
        return CASH_UNKNOWN
    pending = Decimal(0)
    for order in context.open_orders:
        if order.side is not Side.BUY or order.intent_id == subject.intent.intent_id:
            continue  # 改单：自己那张旧单被新内容替代，不重复占用
        if order.limit_price is None:
            return UNPRICED_BUY
        pending += order.remaining_quantity * order.limit_price
    available = context.cash.settled + min(context.cash.unsettled, Decimal(0))
    if subject.quantity * subject.price + pending > available:
        return LEVERAGE
    return None


def _no_short(subject: Subject, context: RiskContext) -> str | None:
    held = context.position(subject.intent.symbol)
    if held is None or context.open_orders is None:
        return HOLDING_UNKNOWN
    if held <= 0:
        return NO_HOLDING
    pending = sum(
        (
            o.remaining_quantity
            for o in context.open_orders
            if o.symbol == subject.intent.symbol
            and o.order_class is OrderClass.REDUCE
            and o.intent_id != subject.intent.intent_id
        ),
        Decimal(0),
    )
    if subject.quantity + pending > held:
        return SHORT
    return None


def _protective_stop(
    subject: Subject, context: RiskContext, params: ProtectiveStopParams | None
) -> str | None:
    intent = subject.intent
    held = context.position(intent.symbol)
    others = [
        o
        for o in context.open_orders or ()
        if o.symbol == intent.symbol
        and o.order_class is OrderClass.PROTECTIVE_STOP
        and o.intent_id != intent.intent_id
    ]
    last = (context.last_prices or {}).get(intent.symbol)
    checks: list[tuple[bool, str]] = [
        (intent.strategy_id != PROTECTIVE_STOP_SOURCE, STOP_SOURCE),
        (params is None, STOP_PARAMS),
        (held is None or context.open_orders is None or last is None, STOP_CONTEXT),
        (held is not None and held <= 0, STOP_NO_HOLDING),
        (bool(others), STOP_DUPLICATE),
        (held is not None and subject.quantity != held, STOP_QUANTITY),
    ]
    for failed, message in checks:  # 按顺序给出第一条不满足的条件
        if failed:
            return message
    assert params is not None and last is not None  # noqa: S101 - 上面已检查
    return _stop_price(subject, context, params, last)


def _stop_price(
    subject: Subject, context: RiskContext, params: ProtectiveStopParams, last: Decimal
) -> str | None:
    symbol = subject.intent.symbol
    costs = [
        p.average_cost
        for p in context.positions or ()
        if p.symbol == symbol and p.average_cost is not None
    ]
    if len(costs) != 1:
        return STOP_COST
    cost = costs[0]
    if not cost * (Decimal(1) - params.stop_distance) <= subject.price < cost:
        return STOP_RANGE
    if not (context.instruments or {})[symbol].is_on_tick(subject.price):
        return STOP_TICK
    if subject.price >= last:
        return STOP_TRIGGERS
    return None
