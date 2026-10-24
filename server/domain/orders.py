"""交易主链路：OrderIntent → RiskDecision → BrokerOrder → Execution（C-01）。"""

from __future__ import annotations

from typing import Any, ClassVar, Self

from pydantic import model_validator

from server.domain.enums import OrderClass, OrderType, RiskVerdict, Side, TimeInForce
from server.domain.values import (
    Identifier,
    Money,
    PositiveQuantity,
    Price,
    ShareQuantity,
    UtcDatetime,
    ValueModel,
)


class OrderIntent(ValueModel):
    """策略或用户提出的交易意图，intent_id 是贯穿重试的幂等键。

    意图创建后完全不可修改：同一个幂等键只能对应一份内容。风控缩量用
    RiskDecision.approved_quantity 表达，不改意图本身。
    """

    evolvable_fields: ClassVar[frozenset[str] | None] = frozenset()

    intent_id: Identifier
    created_at: UtcDatetime
    strategy_id: Identifier
    strategy_version: Identifier
    account_id: Identifier
    symbol: Identifier
    side: Side
    quantity: ShareQuantity
    order_type: OrderType
    limit_price: Price | None = None
    stop_price: Price | None = None
    order_class: OrderClass
    time_in_force: TimeInForce
    expires_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.order_type is OrderType.LIMIT:
            if self.limit_price is None or self.stop_price is not None:
                raise ValueError("限价单必须有限价，且不得带止损价")
        elif self.stop_price is None or self.limit_price is not None:
            raise ValueError("止损单必须有止损价，且不得带限价（首期不用止损限价单）")

        protective = self.order_class is OrderClass.PROTECTIVE_STOP
        if protective != (self.order_type is OrderType.STOP):
            raise ValueError("止损单只能用于保护性止损，保护性止损必须是止损单")
        if protective and self.side is not Side.SELL:
            raise ValueError("保护性止损只能是卖出")
        # 首期仅多头（V4.1 第 1 章）：开仓只能买入，减仓只能卖出；放开做空需另行批准
        if self.order_class is OrderClass.OPEN and self.side is not Side.BUY:
            raise ValueError("首期仅多头：开仓单只能是买入")
        if self.order_class is OrderClass.REDUCE and self.side is not Side.SELL:
            raise ValueError("首期仅多头：减仓单只能是卖出")

        if self.time_in_force is TimeInForce.GTC and not protective:
            raise ValueError(
                "开仓与减仓单必须当日有效或指定到期（V4.1 6.5），只有保护性止损可以 GTC"
            )
        if (self.time_in_force is TimeInForce.GTD) != (self.expires_at is not None):
            raise ValueError("GTD 必须且只有 GTD 才带到期时间")
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("到期时间必须晚于创建时间")
        return self


class RuleHit(ValueModel):
    evolvable_fields: ClassVar[frozenset[str] | None] = frozenset()

    rule_id: Identifier
    rule_version: Identifier
    message: str


class RiskDecision(ValueModel):
    """风控结论是审计事实，创建后不可修改。"""

    evolvable_fields: ClassVar[frozenset[str] | None] = frozenset()

    decision_id: Identifier
    intent_id: Identifier
    verdict: RiskVerdict
    approved_quantity: PositiveQuantity | None = None
    rule_hits: tuple[RuleHit, ...] = ()
    rule_set_version: Identifier
    decided_at: UtcDatetime
    # 评估对象的规范化摘要（R-01，server/oms/binding.py）：OMS 只消费与所签发 / 改单内容一致的决定；
    # 没有摘要的决定（R-01 之前写入的）一律不能用于下单
    subject_digest: Identifier | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.verdict is RiskVerdict.REDUCE) != (self.approved_quantity is not None):
            raise ValueError("只有 REDUCE 带缩减后的数量，且必须带")
        if self.verdict is not RiskVerdict.ALLOW and not self.rule_hits:
            raise ValueError("REDUCE、REJECT 与 HALT 必须说明命中的规则")
        return self


class BrokerOrder(ValueModel):
    """已交给券商的订单。orderRef 即 intent_id；broker_order_id 在提交前分配并持久化。"""

    # 只有 perm_id 可以补录，且只能从空变为某个值（见 evolve）
    evolvable_fields: ClassVar[frozenset[str] | None] = frozenset({"perm_id"})

    intent_id: Identifier
    broker_order_id: Identifier
    perm_id: Identifier | None = None
    order_class: OrderClass
    submitted_at: UtcDatetime

    def evolve(self, **changes: Any) -> Self:
        if "perm_id" in changes and self.perm_id is not None and changes["perm_id"] != self.perm_id:
            raise ValueError("perm_id 只能补录一次，不能改成另一个值")
        return super().evolve(**changes)


class Execution(ValueModel):
    """成交回报是券商事实，按 execution_id 去重入账，创建后不可修改。"""

    evolvable_fields: ClassVar[frozenset[str] | None] = frozenset()

    execution_id: Identifier
    intent_id: Identifier
    broker_order_id: Identifier
    symbol: Identifier
    side: Side
    quantity: ShareQuantity
    price: Price
    commission: Money
    executed_at: UtcDatetime
