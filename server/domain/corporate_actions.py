"""公司行动记录（D-01a）：数据供应商返回的拆股、合股与现金分红。

- 拆股 / 合股：每 denominator 股变为 numerator 股（2 拆 1 为 2/1，10 合 1 为 1/10），
  整数比例保证精确；
- 现金分红：每股金额用 Decimal；
- 除权日是交易所本地日期（严格 date，不接受 datetime 或字符串）。
复权与版本化在 D-04；对账用的事件（risk.reconcile.CorporateAction）由 D-04 从这里映射。
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Self

from pydantic import PositiveInt, Strict, model_validator

from server.domain.values import Currency, Identifier, Price, ValueModel

StrictDate = Annotated[date, Strict()]


class CorporateActionKind(StrEnum):
    SPLIT = "SPLIT"  # 拆股与合股
    CASH_DIVIDEND = "CASH_DIVIDEND"


class CorporateActionRecord(ValueModel):
    event_id: Identifier
    symbol: Identifier
    kind: CorporateActionKind
    ex_date: StrictDate
    source: Identifier
    numerator: PositiveInt | None = None
    denominator: PositiveInt | None = None
    amount: Price | None = None  # 每股现金
    currency: Currency | None = None

    @model_validator(mode="after")
    def _fields_match_kind(self) -> Self:
        if self.kind is CorporateActionKind.SPLIT:
            if self.numerator is None or self.denominator is None:
                raise ValueError("拆股 / 合股必须给出 numerator 与 denominator")
            if self.numerator == self.denominator:
                raise ValueError("拆股比例不能是 1:1")
            if self.amount is not None or self.currency is not None:
                raise ValueError("拆股 / 合股不能带现金金额")
        else:
            if self.amount is None or self.currency is None:
                raise ValueError("现金分红必须给出每股金额与币种")
            if self.numerator is not None or self.denominator is not None:
                raise ValueError("现金分红不能带拆股比例")
        return self
