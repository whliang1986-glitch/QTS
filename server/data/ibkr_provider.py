"""IBKR 历史日线数据源（D-01b，不连 TWS 的部分）：实现 DataProvider，通过同一套契约测试。

- 能力：只提供日线；不能列出全部标的（标的池来自配置）；
  不提供公司行动与交易日历（分别来自 D-04 的数据源与 D-03 的日历），
  这些一律抛 UnsupportedCapabilityError；
  不包含退市标的（回测用 D-08 的供应商）；
- K 线的开收盘时间取自交易日历（D-03），IBKR 只给日期：
  - 区间内没有交易日：返回空批次，不发请求；
  - 区间内有交易日，IBKR 却一根都没给：按“供应商不可用”上抛，不当作“没有数据”；
  - IBKR 给出日历上不是交易日的日期：整批拒绝（口径对不上）；
- 复权口径：IBKR 的日线会追溯调整拆股、不调整分红，所以标成 `ibkr_split_adjusted`，
  不冒充未复权序列（D-04 只对未复权序列做复权，遇到别的口径会拒绝）；
- **一次 fetch_bars 只发一次历史数据请求**：区间内首尾交易日相距超过一年时直接拒绝（ValueError），
  由调用方切分（D-02 按“标的 × 年”分片）。这样 D-02 的限速按调用计数就等于按请求计数；
  限速按 IBKR 历史数据规则（10 分钟 60 次）留余量；
- ib_async 在超时、IBKR 报错（限速违规 162、无数据权限 354 / 10167 等）时**不抛异常，返回空列表**
  （RaiseRequestErrors 默认关闭，这里不改共用连接的设置）。所以：区间内有交易日、返回却为空，
  一律按 ProviderUnavailableError 上抛（调用方稍后重试），不当作“没有数据”。
  代价：区间跨过上市日、上市前那段本来就没有数据时也会报不可用，由调用方收窄区间；
- 连接层抛出的异常、确认合约超时，同样按 ProviderUnavailableError 上抛；
- 确认合约得到空结果时按“未知标的”处理（IBKR 报错时 ib_async 也给空结果：联调核对错误码 200
  与其他错误的区别，D-02 对未知标的记失败、不重试，也不会把标的移出标的池）；
- 交易日历不覆盖请求区间时，按日历的 CalendarRangeError 上抛（D-02 已按失败处理）。

**未在模拟盘实测**：契约测试这里只跑在替身 IB 上，不代表 IBKR 联调通过（docs/tasks/D-01b.md）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Sequence
from datetime import UTC, date, datetime, timedelta

from server.broker.ibkr.client import IbAsyncClient
from server.broker.ibkr.history import (
    HISTORY_TIMEOUT,
    MAX_DAYS_PER_REQUEST,
    UnknownContractError,
    fetch_daily_bars,
    qualify_stock,
)
from server.broker.ibkr.mapping import MappingError
from server.data.calendar import Session, TradingCalendar
from server.data.provider import (
    BarBatch,
    FetchedBar,
    Interval,
    ProviderCapabilities,
    ProviderUnavailableError,
    RateLimit,
    UnknownSymbolError,
    UnsupportedCapabilityError,
    check_range,
)
from server.domain.corporate_actions import CorporateActionRecord
from server.domain.market import MarketBar
from server.domain.values import to_utc

NAME = "ibkr"
SPLIT_ADJUSTED = "ibkr_split_adjusted"
# 先填 IBKR 官网首页：市场数据与 API 条款的准确页面在 F-05 开通账户时核对后更新（manifest 会记录）
LICENSE_URL = "https://www.interactivebrokers.com/"
RATE_LIMIT = RateLimit(50, timedelta(minutes=10))  # IBKR：10 分钟内不超过 60 次历史数据请求


class IbkrHistoryProvider:
    def __init__(
        self,
        client: IbAsyncClient,
        calendar: TradingCalendar,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        timeout: float = HISTORY_TIMEOUT,
    ) -> None:
        self._client = client
        self._timeout = timeout
        self._calendar = calendar
        self._clock = clock
        self._capabilities = ProviderCapabilities(
            intervals=frozenset({Interval.DAY}),
            symbol_listing=False,
            corporate_actions=False,
            calendar=False,
            includes_delisted=False,
            license_url=LICENSE_URL,
        )

    @property
    def name(self) -> str:
        return NAME

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    @property
    def rate_limit(self) -> RateLimit:
        return RATE_LIMIT

    async def list_symbols(self) -> Sequence[str]:
        raise UnsupportedCapabilityError("IBKR 不能列出全部标的：标的池来自配置")

    async def fetch_corporate_actions(
        self, symbols: Collection[str], start: date, end: date
    ) -> Sequence[CorporateActionRecord]:
        check_range(start, end)
        raise UnsupportedCapabilityError("IBKR 历史数据源不提供公司行动（D-04 另有来源）")

    async def fetch_calendar(self, start: date, end: date) -> Sequence[Session]:
        check_range(start, end)
        raise UnsupportedCapabilityError("IBKR 历史数据源不提供交易日历（用 D-03 的日历）")

    async def fetch_bars(
        self, symbol: str, start: date, end: date, interval: Interval = Interval.DAY
    ) -> BarBatch:
        check_range(start, end)
        if interval not in self._capabilities.intervals:
            raise UnsupportedCapabilityError(f"IBKR 历史数据源不提供 {interval} K 线")
        sessions = {s.day: s for s in self._calendar.sessions_between(start, end)}
        # 拉取时间取在请求之前：下游按它判断当天 K 线是否已收盘，取早不取晚（更保守）
        fetched_at = to_utc(self._clock())
        bars: list[FetchedBar] = []
        if sessions:
            first, last = min(sessions), max(sessions)
            if (last - first).days + 1 > MAX_DAYS_PER_REQUEST:
                raise ValueError(f"一次最多取 {MAX_DAYS_PER_REQUEST} 天：请按年切分（D-02 分片）")
            try:
                contract = await asyncio.wait_for(qualify_stock(self._client, symbol),
                                                  self._timeout)  # fmt: skip
            except UnknownContractError as exc:
                raise UnknownSymbolError(symbol) from exc
            except Exception as exc:
                raise ProviderUnavailableError(f"IBKR 确认合约失败：{type(exc).__name__}") from exc
            bars = await self._fetch(contract, symbol, first, last, sessions)
            if not bars:  # ib_async 超时、报错都返回空列表：不能当作“没有数据”
                count = len(sessions)
                raise ProviderUnavailableError(f"区间内有 {count} 个交易日，IBKR 却没有返回日线")
        return BarBatch(symbol=symbol, interval=interval, start=start, end=end, bars=tuple(bars),
                        fetched_at=fetched_at)  # fmt: skip

    async def _fetch(
        self, contract: object, symbol: str, first: date, last: date,
        sessions: dict[date, Session],
    ) -> list[FetchedBar]:  # fmt: skip
        try:
            daily = await fetch_daily_bars(self._client, contract, symbol, first, last)
        except MappingError:
            raise  # 数据口径对不上：不是暂时不可用，原样上抛（整批拒绝）
        except Exception as exc:
            raise ProviderUnavailableError(f"IBKR 历史数据请求失败：{type(exc).__name__}") from exc
        result = []
        for item in daily:
            session = sessions.get(item.day)
            if session is None:
                raise MappingError(f"IBKR 给出了日历上不是交易日的 {symbol} 日线：{item.day}")
            bar = MarketBar(symbol=symbol, start=session.open, end=session.close, open=item.open,
                            high=item.high, low=item.low, close=item.close, volume=item.volume,
                            source=NAME, adjustment_version=SPLIT_ADJUSTED)  # fmt: skip
            result.append(FetchedBar(bar=bar, raw=item.raw))
        return result


__all__ = ("LICENSE_URL", "NAME", "RATE_LIMIT", "SPLIT_ADJUSTED", "IbkrHistoryProvider")
