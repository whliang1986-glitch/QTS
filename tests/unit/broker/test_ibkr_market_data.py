"""IBKR 行情订阅（B-04，不连 TWS）：用 ib_async 的真实 Ticker、Contract 与 Event，加替身 IB。

这里的结果只说明转换与限速逻辑正确，**不代表模拟盘联调通过**。
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import ib_async
import pytest
from server.broker.ibkr.client import IbAsyncClient
from server.broker.ibkr.mapping import MappingError
from server.broker.ibkr.market_data import (
    DELAYED_FEED_LAG,
    IbkrQuoteSource,
    SubscriptionLimitError,
    quote_of,
)
from server.domain.enums import DataQuality

NOW = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)
SUBSCRIBED_AT = NOW - timedelta(minutes=1)


def feed(t: ib_async.Ticker, *, data_type: int | None = 1, bid: float = 500.25,
         ask: float = 500.35, last: float = 500.3,
         received: datetime | None = NOW) -> ib_async.Ticker:  # fmt: skip
    """模拟 TWS 推来的数据：行情类型回调与报价。"""
    if data_type is not None:
        t.marketDataType = data_type
    t.bid, t.ask, t.last = bid, ask, last
    t.bidSize, t.askSize = 300.0, 200.0
    t.time = received
    return t


def ticker(**kw: Any) -> ib_async.Ticker:
    return feed(ib_async.Ticker(contract=ib_async.Stock("SPY", "SMART", "USD")), **kw)


class FakeIB:
    def __init__(self) -> None:
        self.disconnectedEvent = ib_async.IB().disconnectedEvent
        self.connected = True
        self.types: list[int] = []
        self.requested: list[str] = []
        self.cancelled: list[str] = []
        self.tickers: dict[str, ib_async.Ticker] = {}  # 与 ib_async 一样按合约复用 ticker
        self.qualify: dict[str, Any] = {}
        self.drop_during_qualify = False
        self.cancel_result = True

    def isConnected(self) -> bool:  # noqa: N802
        return self.connected

    def reqMarketDataType(self, kind: int) -> None:  # noqa: N802
        self.types.append(kind)

    async def qualifyContractsAsync(self, contract: Any) -> list[Any]:  # noqa: N802
        await asyncio.sleep(0)
        if self.drop_during_qualify:
            self.drop()
        if contract.symbol in self.qualify:
            return [self.qualify[contract.symbol]]
        found = ib_async.Stock(contract.symbol, "SMART", "USD")
        found.conId, found.secType = 1000 + len(self.requested), "STK"
        return [found]

    def reqMktData(self, contract: Any, *args: Any) -> ib_async.Ticker:  # noqa: N802
        if not self.connected:
            raise ConnectionError("Not connected")
        self.requested.append(contract.symbol)
        return self.tickers.setdefault(contract.symbol, ib_async.Ticker(contract=contract))

    def cancelMktData(self, contract: Any) -> bool:  # noqa: N802
        self.cancelled.append(contract.symbol)
        return self.cancel_result

    def drop(self) -> None:
        self.connected = False
        self.tickers.clear()  # ib_async 断线时 reset：重连后是新 ticker
        self.disconnectedEvent.emit()


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def source(ib: FakeIB, **kw: Any) -> tuple[IbkrQuoteSource, Clock]:
    clock = Clock()
    return IbkrQuoteSource(IbAsyncClient(ib), sleep=clock.sleep, monotonic=clock.monotonic,
                           clock=lambda: SUBSCRIBED_AT, **kw), clock  # fmt: skip


# ---------- 转换 ----------


def test_live_quote_converts_exactly() -> None:
    quote = quote_of("SPY", ticker())
    assert quote is not None
    assert (quote.bid, quote.ask, quote.last) == (Decimal("500.25"), Decimal("500.35"),
                                                  Decimal("500.3"))  # fmt: skip
    assert quote.bid_size == Decimal(300) and quote.ts == NOW
    assert quote.delayed is False and quote.quality == frozenset() and quote.source == "ibkr"


@pytest.mark.parametrize(
    ("data_type", "delayed", "stale"),
    [
        (1, False, False),
        (3, True, False),
        (2, False, True),
        (4, True, True),
        (0, False, True),
        (7, False, True),
    ],
)
def test_delay_and_frozen_marks_come_from_the_ticker(
    data_type: int, delayed: bool, stale: bool
) -> None:
    quote = quote_of("SPY", ticker(data_type=data_type))
    assert quote is not None
    assert quote.delayed is delayed
    assert (DataQuality.STALE in quote.quality) is stale


def test_delayed_quote_time_is_moved_back_by_the_feed_lag() -> None:
    quote = quote_of("SPY", ticker(data_type=3))
    assert quote is not None and quote.ts == NOW - DELAYED_FEED_LAG


def test_halted_symbol_is_stale() -> None:
    t = ticker()
    t.halted = 1.0
    quote = quote_of("SPY", t)
    assert quote is not None and DataQuality.STALE in quote.quality


def test_missing_prices_are_none_not_zero() -> None:
    quote = quote_of("SPY", ticker(bid=math.nan, ask=-1.0, last=1.7976931348623157e308))
    assert quote is not None and (quote.bid, quote.ask, quote.last) == (None, None, None)


def test_no_data_yet_means_no_quote() -> None:
    assert quote_of("SPY", ticker(received=None)) is None
    assert quote_of("SPY", ticker(received=datetime.min)) is None  # noqa: DTZ901 - ib_async 初始值
    assert quote_of("SPY", ticker(), since=NOW) is None  # 不晚于订阅时刻：是旧数据


# ---------- 订阅 ----------


@pytest.mark.asyncio
async def test_type_not_reported_by_tws_is_never_treated_as_live() -> None:
    """ib_async 的 Ticker 默认 marketDataType=1：TWS 没报类型时也不能当作实时。"""
    ib = FakeIB()
    quotes, _ = source(ib)
    await quotes.subscribe(["SPY"])
    feed(ib.tickers["SPY"], data_type=None)  # 只有报价，没有类型回调
    quote = (await quotes.quotes(["SPY"]))["SPY"]
    assert DataQuality.STALE in quote.quality
    feed(ib.tickers["SPY"], data_type=1)  # 回调到了
    assert (await quotes.quotes(["SPY"]))["SPY"].quality == frozenset()


@pytest.mark.asyncio
async def test_only_live_data_is_requested_unless_delayed_is_allowed() -> None:
    ib = FakeIB()
    quotes, _ = source(ib)
    await quotes.subscribe(["SPY"])
    ib2 = FakeIB()
    delayed, _ = source(ib2, allow_delayed=True)
    await delayed.subscribe(["SPY"])
    assert ib.types == [1] and ib2.types == [3]


@pytest.mark.asyncio
async def test_quotes_only_for_subscribed_symbols_with_data() -> None:
    ib = FakeIB()
    quotes, _ = source(ib)
    await quotes.subscribe(["SPY", "QQQ", "SPY"])
    assert ib.requested == ["QQQ", "SPY"]  # 去重，按代码排序
    feed(ib.tickers["SPY"])
    result = await quotes.quotes(["SPY", "QQQ", "DIA"])
    assert set(result) == {"SPY"}  # QQQ 还没有数据，DIA 没订阅：都不返回


@pytest.mark.asyncio
async def test_resubscribing_in_the_same_session_ignores_the_old_ticker_values() -> None:
    ib = FakeIB()
    quotes, _ = source(ib)
    await quotes.subscribe(["SPY"])
    feed(ib.tickers["SPY"])
    await quotes.unsubscribe(["SPY"])
    await quotes.subscribe(["SPY"])  # ib_async 复用同一个 ticker，旧值还在
    assert await quotes.quotes(["SPY"]) == {}
    assert ib.tickers["SPY"].marketDataType == 0


@pytest.mark.asyncio
async def test_untrusted_ticker_gives_no_quote_without_breaking_others() -> None:
    ib = FakeIB()
    quotes, _ = source(ib)
    await quotes.subscribe(["SPY", "QQQ"])
    feed(ib.tickers["SPY"])
    feed(ib.tickers["QQQ"], received=datetime(2026, 11, 5, 15, 0))  # noqa: DTZ001 - 故意不带时区
    assert set(await quotes.quotes(["SPY", "QQQ"])) == {"SPY"}


@pytest.mark.asyncio
async def test_disconnect_drops_old_values_and_resubscribe_restores() -> None:
    ib = FakeIB()
    quotes, _ = source(ib)
    await quotes.subscribe(["SPY"])
    feed(ib.tickers["SPY"])
    ib.drop()
    assert await quotes.quotes(["SPY"]) == {}
    assert quotes.subscribed == frozenset()
    ib.connected = True
    assert await quotes.quotes(["SPY"]) == {}  # 重连后旧 ticker 不复用
    assert await quotes.resubscribe() == {}
    assert ib.requested == ["SPY", "SPY"] and ib.types == [1, 1]
    feed(ib.tickers["SPY"])
    assert set(await quotes.quotes(["SPY"])) == {"SPY"}


@pytest.mark.asyncio
async def test_failed_symbols_are_not_kept_and_resubscribe_isolates_failures() -> None:
    ib = FakeIB()
    quotes, _ = source(ib)
    ib.qualify["BAD"] = None
    with pytest.raises(MappingError):
        await quotes.subscribe(["AAA", "BAD", "ZZZ"])
    assert quotes.subscribed == frozenset({"AAA"})  # 前面订上的保留，失败的不加，后面的没订
    await quotes.subscribe(["ZZZ"])
    ib.drop()
    ib.connected = True
    ib.qualify["AAA"] = None  # 重连后这只确认失败：不影响其他
    assert await quotes.resubscribe() == {"AAA": "MappingError"}
    assert quotes.subscribed == frozenset({"ZZZ"})


@pytest.mark.asyncio
async def test_disconnect_while_confirming_leaves_no_dead_line() -> None:
    ib = FakeIB()
    ib.drop_during_qualify = True
    quotes, _ = source(ib)
    with pytest.raises(ConnectionError):
        await quotes.subscribe(["SPY"])
    assert quotes.subscribed == frozenset() and ib.requested == []


@pytest.mark.asyncio
async def test_subscribing_while_disconnected_is_refused() -> None:
    ib = FakeIB()
    ib.connected = False
    quotes, _ = source(ib)
    with pytest.raises(ConnectionError):
        await quotes.subscribe(["SPY"])


@pytest.mark.asyncio
async def test_line_limit_refuses_the_whole_batch() -> None:
    ib = FakeIB()
    quotes, _ = source(ib, max_lines=2)
    await quotes.subscribe(["SPY"])
    with pytest.raises(SubscriptionLimitError):
        await quotes.subscribe(["QQQ", "DIA"])
    assert quotes.subscribed == frozenset({"SPY"}) and ib.requested == ["SPY"]
    await quotes.unsubscribe(["SPY"])
    assert ib.cancelled == ["SPY"]
    await quotes.subscribe(["QQQ", "DIA"])
    assert quotes.subscribed == frozenset({"QQQ", "DIA"})


@pytest.mark.asyncio
async def test_symbols_waiting_for_resubscribe_count_toward_the_limit() -> None:
    ib = FakeIB()
    quotes, _ = source(ib, max_lines=2)
    await quotes.subscribe(["AAA", "BBB"])
    ib.drop()
    ib.connected = True
    with pytest.raises(SubscriptionLimitError):
        await quotes.subscribe(["CCC"])  # 断线前的两只还要恢复：没有余量
    assert await quotes.resubscribe() == {}
    assert quotes.subscribed == frozenset({"AAA", "BBB"})


@pytest.mark.asyncio
async def test_concurrent_subscriptions_cannot_exceed_the_limit() -> None:
    ib = FakeIB()
    quotes, _ = source(ib, max_lines=3)
    results = await asyncio.gather(quotes.subscribe(["AAA", "BBB"]),
                                   quotes.subscribe(["CCC", "DDD"]),
                                   return_exceptions=True)  # fmt: skip
    assert sum(isinstance(r, SubscriptionLimitError) for r in results) == 1
    assert len(quotes.subscribed) == 2


@pytest.mark.asyncio
async def test_failed_unsubscribe_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    ib = FakeIB()
    ib.cancel_result = False
    quotes, _ = source(ib)
    await quotes.subscribe(["SPY"])
    await quotes.unsubscribe(["SPY"])
    assert "退订失败" in caplog.text


@pytest.mark.asyncio
async def test_close_unsubscribes_everything() -> None:
    ib = FakeIB()
    quotes, _ = source(ib)
    await quotes.subscribe(["SPY", "QQQ"])
    await quotes.close()
    assert sorted(ib.cancelled) == ["QQQ", "SPY"] and quotes.subscribed == frozenset()


@pytest.mark.asyncio
async def test_requests_are_paced() -> None:
    ib = FakeIB()
    quotes, clock = source(ib, min_request_interval=timedelta(milliseconds=100))
    await quotes.subscribe(["SPY", "QQQ"])
    # 每只两次请求（确认合约、订阅）：第一次不等，其余三次各等满 0.1 秒
    assert clock.slept == [pytest.approx(0.1)] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["missing", "option", "currency"])
async def test_unconfirmed_or_wrong_contracts_are_refused(bad: str) -> None:
    ib = FakeIB()
    if bad == "missing":
        ib.qualify["SPY"] = None
    elif bad == "option":
        ib.qualify["SPY"] = ib_async.Option("SPY", conId=5)
    else:
        found = ib_async.Stock("SPY", "SMART", "CAD")
        found.conId, found.secType = 7, "STK"
        ib.qualify["SPY"] = found
    quotes, _ = source(ib)
    with pytest.raises(MappingError):
        await quotes.subscribe(["SPY"])
    assert quotes.subscribed == frozenset() and ib.requested == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "spy", "SPY:OPT:1", "BRK B", "A" * 11])
async def test_odd_symbols_are_refused(bad: str) -> None:
    quotes, _ = source(FakeIB())
    with pytest.raises(ValueError, match="不支持的代码"):
        await quotes.subscribe([bad])
