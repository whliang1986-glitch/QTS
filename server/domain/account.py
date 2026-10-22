"""账户、现金与持仓（C-01）。现金区分已结算与未结算（ADR-15）。"""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar

from server.domain.enums import AccountType
from server.domain.values import (
    Currency,
    Identifier,
    Money,
    Price,
    SignedQuantity,
    UtcDatetime,
    ValueModel,
)


class Account(ValueModel):
    evolvable_fields: ClassVar[frozenset[str] | None] = frozenset({"account_type", "base_currency"})

    account_id: Identifier
    account_type: AccountType  # 以券商返回为准
    base_currency: Currency


class CashBalance(ValueModel):
    account_id: Identifier
    currency: Currency
    settled: Money
    unsettled: Money
    as_of: UtcDatetime

    @property
    def total(self) -> Decimal:
        return self.settled + self.unsettled


class Position(ValueModel):
    """券商事实：数量可能为负（例如券商侧异常）。

    不在这里静默修正；负持仓必须由对账（R-07）当作差异进入 SAFE HALT，风控不得把它当成可卖数量。
    """

    account_id: Identifier
    symbol: Identifier
    quantity: SignedQuantity
    average_cost: Price | None
    as_of: UtcDatetime
