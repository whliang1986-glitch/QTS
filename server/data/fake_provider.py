"""假数据供应商（D-01a）：给契约测试、入湖、质量门禁与回测骨架用的确定性数据。

不连任何外部服务；它的结果不能冒充真实供应商的联调结论（CLAUDE.md：越界与变更控制）。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from server.data.calendar import Session, TradingCalendar
from server.data.provider import (
    PROVIDER_NAME,
    BarBatch,
    FetchedBar,
    Interval,
    ProviderCapabilities,
    RateLimit,
    UnknownSymbolError,
    UnsupportedCapabilityError,
    check_range,
)
from server.data.quality import UNADJUSTED
from server.domain.corporate_actions import CorporateActionRecord
from server.domain.market import MarketBar

FAKE_LICENSE_URL = "https://example.invalid/fake-data-license"


def raw_record(bar: MarketBar) -> str:
    """确定性的“供应商原始记录”：字段排序、quality 排序，同一根 K 线总得到同一段文本。"""
    data = bar.model_dump(mode="json")
    data["quality"] = sorted(q.value for q in bar.quality)
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def synthetic_daily_bars(  # noqa: PLR0913 - 生成参数全部显式给出
    symbol: str,
    calendar: TradingCalendar,
    start: date,
    end: date,
    *,
    source: str,
    base: Decimal = Decimal("100.00"),
    step: Decimal = Decimal("0.10"),
) -> list[MarketBar]:
    """按交易日历生成确定性的日线：开盘到收盘，第 i 个交易日收盘价 = base + i × step。"""
    bars = []
    for index, session in enumerate(calendar.sessions_between(start, end)):
        close = base + step * index
        bars.append(
            MarketBar(
                symbol=symbol,
                start=session.open,
                end=session.close,
                open=close - step / 2,
                high=close + step,
                low=close - step,
                close=close,
                volume=Decimal(1_000_000 + index),
                source=source,
                adjustment_version=UNADJUSTED,
            )
        )
    return bars


class FakeDataProvider:
    def __init__(  # noqa: PLR0913
        self,
        name: str = "fake",
        *,
        bars: Mapping[str, Sequence[MarketBar]] | None = None,
        actions: Sequence[CorporateActionRecord] = (),
        calendar: TradingCalendar | None = None,
        symbol_listing: bool = True,
        corporate_actions: bool = True,
        includes_delisted: bool = False,
        rate_limit: RateLimit | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not PROVIDER_NAME.fullmatch(name):
            raise ValueError(f"供应商名不合法：{name!r}")
        self._name = name
        self._bars = {symbol: list(series) for symbol, series in (bars or {}).items()}
        self._actions = list(actions)
        self._calendar = calendar
        self._capabilities = ProviderCapabilities(
            intervals=frozenset({Interval.DAY}),
            symbol_listing=symbol_listing,
            corporate_actions=corporate_actions,
            calendar=calendar is not None,
            includes_delisted=includes_delisted,
            license_url=FAKE_LICENSE_URL,
        )
        self._rate_limit = rate_limit or RateLimit(50, timedelta(seconds=1))
        self._clock = clock
        self.calls: list[tuple[str, ...]] = []
        self.fail_with: Exception | None = None  # 设置后每次调用都抛它（模拟供应商不可用）

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    @property
    def rate_limit(self) -> RateLimit:
        return self._rate_limit

    def _call(self, *args: str) -> None:
        self.calls.append(args)
        if self.fail_with is not None:
            raise self.fail_with

    async def list_symbols(self) -> Sequence[str]:
        if not self._capabilities.symbol_listing:
            raise UnsupportedCapabilityError(f"{self._name} 不提供标的列表")
        self._call("list_symbols")
        return sorted(self._bars)

    async def fetch_bars(
        self, symbol: str, start: date, end: date, interval: Interval = Interval.DAY
    ) -> BarBatch:
        check_range(start, end)
        if interval not in self._capabilities.intervals:
            raise UnsupportedCapabilityError(f"{self._name} 不提供 {interval} K 线")
        self._call("fetch_bars", symbol, start.isoformat(), end.isoformat())
        if symbol not in self._bars:
            raise UnknownSymbolError(symbol)
        chosen = sorted(
            (b for b in self._bars[symbol] if start <= b.start.astimezone(UTC).date() <= end),
            key=lambda b: b.start,
        )
        return BarBatch(
            symbol=symbol, interval=interval, start=start, end=end,
            bars=tuple(FetchedBar(b, raw_record(b)) for b in chosen), fetched_at=self._clock(),
        )  # fmt: skip

    async def fetch_corporate_actions(
        self, symbols: Collection[str], start: date, end: date
    ) -> Sequence[CorporateActionRecord]:
        check_range(start, end)
        if not self._capabilities.corporate_actions:
            raise UnsupportedCapabilityError(f"{self._name} 不提供公司行动")
        self._call("fetch_corporate_actions", *sorted(symbols))
        wanted = set(symbols)
        return sorted(
            (a for a in self._actions if a.symbol in wanted and start <= a.ex_date <= end),
            key=lambda a: (a.ex_date, a.symbol, a.event_id),
        )

    async def fetch_calendar(self, start: date, end: date) -> Sequence[Session]:
        check_range(start, end)
        if self._calendar is None:
            raise UnsupportedCapabilityError(f"{self._name} 不提供交易日历")
        self._call("fetch_calendar", start.isoformat(), end.isoformat())
        return list(self._calendar.sessions_between(start, end))
