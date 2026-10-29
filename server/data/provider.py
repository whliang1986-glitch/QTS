"""数据供应商接口（D-01a，V4.1 5.3）。

每家供应商一个适配器，都实现 `DataProvider`，
并通过同一套契约测试（tests/contract/data_provider_contract.py）。
策略与回测不直接调用供应商 SDK，只读数据湖。

- 日期区间一律是闭区间 [start, end]，K 线按开始时间的 UTC 日期归属（与数据湖分区一致）；
- 不支持的能力抛 UnsupportedCapabilityError，不用空结果冒充“没有数据”；
- 每根 K 线附带供应商原始记录（文本），原样写进原始层，便于追溯与重新标准化；
- 每个入口先调用 check_range（契约测试据其报错断言）；
- K 线形状（时长、是否落在交易时段、OHLC 逻辑）由 D-05 的质量门禁检查，不在接口契约里。
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from server.data.calendar import Session
from server.domain.corporate_actions import CorporateActionRecord
from server.domain.market import MarketBar

# 供应商名会成为数据湖的数据集名前缀（raw/<name>_bars_1d），所以只允许小写标识符
PROVIDER_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")


class DataProviderError(Exception):
    """供应商调用失败的基类。"""


class ProviderUnavailableError(DataProviderError):
    """供应商暂时不可用（网络、限流、维护）：调用方稍后重试，不得把它当成“没有数据”。"""


class UnknownSymbolError(DataProviderError):
    """供应商不认识该标的。"""


class UnsupportedCapabilityError(DataProviderError):
    """该供应商不提供这项数据（例如 IBKR 不提供交易日历）。"""


class Interval(StrEnum):
    DAY = "1d"


@dataclass(frozen=True)
class RateLimit:
    """每 per 时间内最多 requests 次请求（D-02 的 DataJob 执行限速）。"""

    requests: int
    per: timedelta

    def __post_init__(self) -> None:
        if self.requests <= 0 or self.per <= timedelta(0):
            raise ValueError("限速必须是正的请求数与正的时间窗")


@dataclass(frozen=True)
class ProviderCapabilities:
    intervals: frozenset[Interval]
    symbol_listing: bool  # 能否列出全部标的（IBKR 不能：标的池来自配置，不由供应商提供）
    corporate_actions: bool
    calendar: bool
    includes_delisted: bool  # 是否包含退市标的（回测无幸存者偏差的前提，D-08）
    license_url: str  # 授权条款链接，写进 manifest

    def __post_init__(self) -> None:
        if not self.intervals:
            raise ValueError("至少支持一种 K 线周期")
        if not self.license_url.strip():
            raise ValueError("必须给出授权条款链接")


@dataclass(frozen=True)
class FetchedBar:
    bar: MarketBar
    raw: str  # 供应商原始记录


@dataclass(frozen=True)
class BarBatch:
    symbol: str
    interval: Interval
    start: date
    end: date
    bars: tuple[FetchedBar, ...]
    fetched_at: datetime  # 拉取时间（UTC）


def check_range(start: date, end: date) -> None:
    """适配器入口统一调用：日期必须是严格的 date，且 start <= end。"""
    if type(start) is not date or type(end) is not date:
        raise TypeError("日期区间必须是 date（不能是 datetime）")
    if start > end:
        raise ValueError(f"日期区间不合法：{start} > {end}")


class DataProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def capabilities(self) -> ProviderCapabilities: ...

    @property
    def rate_limit(self) -> RateLimit: ...

    async def list_symbols(self) -> Sequence[str]:
        """不支持 symbol_listing 时抛 UnsupportedCapabilityError，不用配置的列表冒充。"""
        ...

    async def fetch_bars(
        self, symbol: str, start: date, end: date, interval: Interval = Interval.DAY
    ) -> BarBatch: ...

    async def fetch_corporate_actions(
        self, symbols: Collection[str], start: date, end: date
    ) -> Sequence[CorporateActionRecord]: ...

    async def fetch_calendar(self, start: date, end: date) -> Sequence[Session]: ...
