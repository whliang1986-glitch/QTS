"""策略接口与运行器（T-01，V4.1 8.2）。

- 策略是纯计算：输入决策时刻之前已发布的日线与当前持仓，输出交易提议（Proposal）。
  策略不得依赖 broker、oms、storage、risk、ipc、platforms（import-linter 强制），只能提出意图。
- 运行器把提议转成 OrderIntent，逐条校验，有一条不合格整批拒绝（失败即关闭）：
  - 只能交易策略声明的标的池，且该标的必须有数据；
  - 首期只做多：开仓只能买入，减仓只能卖出且不超过持仓；不得提议保护性止损（由 OMS 随持仓创建）；
  - 一律限价、当日有效；同一次运行里同一标的只能有一条提议（不同时买卖同一标的）；
  - 输入数据不得包含决策时刻之后才收盘的日线（前视；发布延迟由调度层另行过滤）。
- 运行器在调用策略前固定它的标识与标的池，交给策略的输入是只读副本，校验一律对照运行器自己的副本：
  有缺陷的策略改写输入或属性，也绕不过校验。
- 幂等：intent_id = 策略、版本、交易日、标的、方向、类别 的确定性组合，created_at 是当日的决策时刻
  （由调度层固定，不是调用时刻）。行情输入与 created_at 固定，同一天重跑得到相同的意图；
  内容变了（例如持仓变化后数量不同）则同键不同内容，由 outbox 拒绝，绝不会多下一单。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Protocol

from server.domain.enums import OrderClass, OrderType, Side, TimeInForce
from server.domain.market import MarketBar
from server.domain.orders import OrderIntent
from server.domain.values import Identifier, Price, ShareQuantity, ValueModel, to_utc

# 策略标识会进入幂等键：小写字母、数字、点、下划线、连字符；不得占用系统保留前缀
# （oms、risk 前缀覆盖了保护性止损与急停平仓的系统来源，交易核心入口也会再拒一次）
_STRATEGY_ID = re.compile(r"[a-z][a-z0-9._-]{0,31}")
RESERVED_STRATEGY_PREFIXES = (
    "flatten", "protective", "emergency", "manual", "oms", "risk", "system",
)  # fmt: skip


class StrategyError(Exception):
    """策略输出或输入不合格：整批拒绝，本次不产生任何意图。"""


class Proposal(ValueModel):
    symbol: Identifier
    side: Side
    quantity: ShareQuantity
    limit_price: Price
    order_class: OrderClass
    reason: str  # 给人看的理由（写进日志与审批界面）


@dataclass(frozen=True)
class StrategyContext:
    decision_at: datetime  # UTC：当日的决策时刻（不是调用时刻）
    session_day: date  # 意图所属的交易时段（当日有效）
    account_id: str
    bars: Mapping[str, Sequence[MarketBar]]  # 每个标的按时间升序，决策时刻之前已发布
    positions: Mapping[str, Decimal] = field(default_factory=dict)


class Strategy(Protocol):
    @property
    def strategy_id(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def universe(self) -> frozenset[str]: ...

    def decide(self, context: StrategyContext) -> Sequence[Proposal]: ...


@dataclass(frozen=True)
class Identity:
    """运行器在调用策略之前固定的标识与标的池。"""

    strategy_id: str
    version: str
    universe: frozenset[str]


def identity_of(strategy: Strategy) -> Identity:
    sid, version, raw = strategy.strategy_id, strategy.version, strategy.universe
    if not isinstance(raw, frozenset | set) or not all(isinstance(x, str) and x for x in raw):
        raise StrategyError(f"{sid!r} 的标的池必须是由标的字符串组成的集合")
    universe = frozenset(raw)
    if (
        not isinstance(sid, str)
        or not _STRATEGY_ID.fullmatch(sid)
        or sid.startswith(RESERVED_STRATEGY_PREFIXES)
    ):
        raise StrategyError(f"策略标识不合法或占用保留标识：{sid!r}")
    if not isinstance(version, str) or not _STRATEGY_ID.fullmatch(version):
        raise StrategyError(f"策略版本不合法：{version!r}")
    if not universe:
        raise StrategyError(f"{sid} 没有声明标的池")
    return Identity(sid, version, universe)


def check_identity(strategy: Strategy) -> None:
    identity_of(strategy)


def intent_id(identity: Identity, session_day: date, proposal: Proposal) -> str:
    return ":".join(
        (
            identity.strategy_id,
            identity.version,
            session_day.isoformat(),
            proposal.symbol,
            proposal.side.value,
            proposal.order_class.value,
        )
    )


def freeze(identity: Identity, context: StrategyContext) -> StrategyContext:
    """校验输入并做只读副本：交给策略的就是它，校验也对照它。"""
    try:
        decision_at = to_utc(context.decision_at)
    except ValueError as exc:
        raise StrategyError(f"决策时刻不合法：{exc}") from exc
    bars: dict[str, tuple[MarketBar, ...]] = {}
    for symbol, series in context.bars.items():
        if symbol not in identity.universe:
            raise StrategyError(f"输入里有标的池以外的 {symbol}")
        frozen = tuple(series)
        for bar in frozen:
            if bar.symbol != symbol:
                raise StrategyError(f"{symbol} 的序列里混入了 {bar.symbol}")
            if bar.end > decision_at:
                day = bar.start.date()
                raise StrategyError(f"{symbol} {day} 的日线在决策时刻之后才收盘（前视）")
        bars[symbol] = frozen
    positions: dict[str, Decimal] = {}
    for symbol, quantity in context.positions.items():
        value: object = quantity  # 来源是外部输入：按 object 检查
        if isinstance(value, bool) or not isinstance(value, Decimal | int):
            raise StrategyError(f"{symbol} 的持仓必须是 Decimal 或 int")
        positions[symbol] = Decimal(value)
    return StrategyContext(
        decision_at=decision_at, session_day=context.session_day,
        account_id=context.account_id, bars=MappingProxyType(bars),
        positions=MappingProxyType(positions),
    )  # fmt: skip


def _check_proposal(identity: Identity, context: StrategyContext, proposal: Proposal) -> None:
    sid = identity.strategy_id
    if proposal.symbol not in identity.universe:
        raise StrategyError(f"{sid} 提议了标的池以外的 {proposal.symbol}")
    if not context.bars.get(proposal.symbol):
        raise StrategyError(f"{sid} 提议的 {proposal.symbol} 没有可用数据")
    if proposal.order_class is OrderClass.OPEN and proposal.side is not Side.BUY:
        raise StrategyError(f"{sid}：首期只做多，开仓只能买入")
    if proposal.order_class is OrderClass.REDUCE:
        held = context.positions.get(proposal.symbol, Decimal(0))
        if proposal.side is not Side.SELL or proposal.quantity > held:
            raise StrategyError(f"{sid}：减仓只能卖出且不超过持仓（{proposal.symbol} 持有 {held}）")
    if proposal.order_class not in (OrderClass.OPEN, OrderClass.REDUCE):
        kind = proposal.order_class.value
        raise StrategyError(f"{sid}：策略不得提议 {kind}（保护性止损由 OMS 创建）")


def build_intents(
    identity: Identity, context: StrategyContext, proposals: Sequence[object]
) -> list[OrderIntent]:
    """逐条校验提议并转成意图；任何一条不合格整批拒绝。context 必须是 freeze 的结果。"""
    seen: set[str] = set()
    intents = []
    for proposal in proposals:
        if not isinstance(proposal, Proposal):
            raise StrategyError(f"{identity.strategy_id} 输出的不是 Proposal")
        _check_proposal(identity, context, proposal)
        if proposal.symbol in seen:
            raise StrategyError(f"{identity.strategy_id} 对 {proposal.symbol} 提议了两次")
        seen.add(proposal.symbol)
        try:
            intent = OrderIntent(
                intent_id=intent_id(identity, context.session_day, proposal),
                created_at=context.decision_at, strategy_id=identity.strategy_id,
                strategy_version=identity.version, account_id=context.account_id,
                symbol=proposal.symbol, side=proposal.side, quantity=proposal.quantity,
                order_type=OrderType.LIMIT, limit_price=proposal.limit_price,
                order_class=proposal.order_class, time_in_force=TimeInForce.DAY,
            )  # fmt: skip
        except ValueError as exc:
            raise StrategyError(f"{identity.strategy_id} 的提议无法构成意图：{exc}") from exc
        intents.append(intent)
    return intents


def evaluate(strategy: Strategy, context: StrategyContext) -> list[OrderIntent]:
    """运行一次策略：先固定标识与输入，再调用策略；策略内部的任何异常都按策略失败处理。"""
    try:
        identity = identity_of(strategy)
    except StrategyError:
        raise
    except Exception as exc:  # 属性本身出错
        raise StrategyError(f"读不到策略标识：{type(exc).__name__}：{exc}") from exc
    frozen = freeze(identity, context)
    try:
        proposals = list(strategy.decide(frozen))
    except StrategyError:
        raise
    except Exception as exc:
        name = type(exc).__name__
        raise StrategyError(f"{identity.strategy_id} 运行出错：{name}：{exc}") from exc
    return build_intents(identity, frozen, proposals)
