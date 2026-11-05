"""IBKR 行情订阅与实时/延迟标记（B-04，不连 TWS 的部分）。

实现风控的 `QuoteSource` 协议（`quotes(symbols) -> {symbol: Quote}`），
供数据门禁（R-03）与策略使用。
**未在模拟盘实测**：字段口径与错误码在 F-05 之后联调核对。
核对之前，延迟行情本来就会被数据门禁拒绝。

失败即关闭：
- **如实标记实时/延迟**：
  - ib_async 的 `Ticker.marketDataType` 默认就是 1（实时），延迟 tick 也写进同一组买卖价字段，
    所以不能看默认值。订阅后立刻把它改成哨兵 0，只有 TWS 的 marketDataType 回调能改回 1 或 3；
  - 1 = 实时；3 = 延迟（`delayed=True`）；
  - 2、4 = 冻结，标 STALE；哨兵（还没报）或认不出，也标 STALE；
  - 停牌（`halted` 非 0）标 STALE。
- **只用本次订阅之后收到的数据**：
  - 同一会话里退订再订阅，ib_async 会复用旧 ticker；
  - 订阅时清掉旧的价格与时间，并要求数据时间晚于订阅时刻。
- 请求哪种行情类型：只有 `allow_delayed=True` 时才请求“没有实时就给延迟”（类型 3），
  否则只请求实时（类型 1）。
  延迟行情能不能用于下单，仍由受保护配置决定（ADR-06）。这里只决定向 IBKR 请求什么。
- **没有收到过数据的标的不返回**，数据门禁按“没有报价”拒绝。不编造报价。
- 价格：
  - NaN、UNSET、≤0（IBKR 用 −1 表示“没有”）都当作缺失；
  - 买价或卖价缺失时，数据门禁会拒绝（价差无法核对）。
- 断线：
  - 断线时清空全部 ticker，`quotes()` 返回空；
  - 重连后由装配层调用 `resubscribe()`，逐只恢复，失败的列出来，不复用断线前的旧值。
- **订阅限速**：
  - 同时订阅的行数有上限（IBKR 默认 100 行，这里默认留余量）；
    一批订阅超出上限时一只都不加，并抛出 `SubscriptionLimitError`；
  - 相邻两次请求至少间隔 `min_request_interval`（IBKR 限制每秒 50 条消息）。
- 合约必须先经 `qualifyContractsAsync` 确认（拿到 conId），只订阅美元计价的股票或 ETF。

每个 IBKR 连接只用一个 `IbkrQuoteSource` 实例：
- ib_async 按合约共用 ticker，退订也只按合约找订阅；
- 两个实例或别的模块订阅同一合约，会互相清值、互相退订。装配层照此执行。

时间口径：
- `Quote.ts` 是“数据本身的时间”的保守估计：
  - 实时行情取 ib_async 收到数据的时刻（本机时钟，UTC）；
  - 延迟行情再往前推 `DELAYED_FEED_LAG`（15 分钟）。
- 这与数据门禁给延迟行情的 15 分钟宽限口径一致，避免宽限被算两次。
- 本机时钟与券商时钟的偏差由数据门禁检查。偏差为正时，报价年龄最多被低估一个偏差上限。联调时核对。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import ib_async

from server.broker.ibkr.client import IbAsyncClient
from server.broker.ibkr.convert import decimal_of, utc
from server.broker.ibkr.mapping import MappingError
from server.domain.enums import DataQuality
from server.domain.market import Quote
from server.storage.logs import log_event

log = logging.getLogger(__name__)

SOURCE = "ibkr"
UNREPORTED = 0  # 哨兵：TWS 还没报行情类型
LIVE, DELAYED, DELAYED_FROZEN = 1, 3, 4  # 2 是冻结（实时的最后值）：和认不出的类型一样按陈旧处理
# 交易所对延迟行情规定的延迟（与 server/risk/data_gate.py 的 DELAYED_FEED_LAG 同一个单位定义）
DELAYED_FEED_LAG = timedelta(minutes=15)
DEFAULT_MAX_LINES = 80  # IBKR 默认 100 行：留出余量给 TWS 自己与其他客户端
DEFAULT_REQUEST_INTERVAL = timedelta(milliseconds=50)  # 每秒最多 20 次请求
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")  # 首期美股、ETF 代码；带空格的写法联调时核对


class SubscriptionLimitError(RuntimeError):
    """同时订阅的行数超过上限：拒绝新增订阅（不挤掉已有的）。"""


@dataclass
class _Line:
    contract: Any
    ticker: Any
    since: datetime  # 订阅时刻：只用这之后收到的数据


@dataclass
class _Pacer:
    """相邻两次请求至少间隔 interval（单调时钟）。"""

    interval: timedelta
    sleep: Callable[[float], Awaitable[None]]
    monotonic: Callable[[], float]
    _last: float | None = field(default=None)

    async def wait(self) -> None:
        if self._last is not None:
            delay = self.interval.total_seconds() - (self.monotonic() - self._last)
            if delay > 0:
                await self.sleep(delay)
        self._last = self.monotonic()


def _price(value: Any, what: str) -> Decimal | None:
    result = decimal_of(value, what, allow_unset=True)
    return result if result is not None and result > 0 else None


def _size(value: Any, what: str) -> Decimal | None:
    result = decimal_of(value, what, allow_unset=True)
    return result if result is not None and result >= 0 else None


def _halted(ticker: Any) -> bool:
    value = getattr(ticker, "halted", None)
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return False
    return bool(value != 0)


def quote_of(symbol: str, ticker: Any, *, since: datetime | None = None) -> Quote | None:
    """ticker → Quote。还没收到数据，或数据早于 since（本次订阅之前）时返回 None。"""
    received = getattr(ticker, "time", None)
    if not isinstance(received, datetime) or received.tzinfo is None:
        return None  # ib_async 初始值是不带时区的 datetime.min 或 None：还没有数据
    received = utc(received, f"{symbol} 行情时间")
    if since is not None and received <= since:
        return None
    data_type = getattr(ticker, "marketDataType", None)
    delayed = data_type in (DELAYED, DELAYED_FROZEN)
    quality: frozenset[DataQuality] = frozenset()
    if data_type not in (LIVE, DELAYED) or _halted(ticker):  # 冻结、没报、认不出、停牌
        quality = frozenset({DataQuality.STALE})
    return Quote(
        symbol=symbol,
        ts=received - DELAYED_FEED_LAG if delayed else received,
        bid=_price(getattr(ticker, "bid", None), f"{symbol} 买价"),
        ask=_price(getattr(ticker, "ask", None), f"{symbol} 卖价"),
        last=_price(getattr(ticker, "last", None), f"{symbol} 最新价"),
        bid_size=_size(getattr(ticker, "bidSize", None), f"{symbol} 买量"),
        ask_size=_size(getattr(ticker, "askSize", None), f"{symbol} 卖量"),
        source=SOURCE,
        delayed=delayed,
        quality=quality,
    )


def _clear(ticker: Any) -> None:
    """订阅时清掉 ticker 里的旧值（复用的 ticker 会带着上一次订阅的数据与行情类型）。"""
    ticker.marketDataType = UNREPORTED
    ticker.time = None
    ticker.bid = ticker.ask = ticker.last = math.nan  # 字面量写法，不用反射
    ticker.bidSize = ticker.askSize = math.nan


class IbkrQuoteSource:
    """IBKR 行情订阅。与只读适配器共用一个 IbAsyncClient（同一个连接）。"""

    def __init__(  # noqa: PLR0913 - 限速与时钟显式注入，便于测试
        self,
        client: IbAsyncClient,
        *,
        allow_delayed: bool = False,
        max_lines: int = DEFAULT_MAX_LINES,
        min_request_interval: timedelta = DEFAULT_REQUEST_INTERVAL,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if max_lines <= 0:
            raise ValueError("max_lines 必须为正")
        self._client = client
        self._allow_delayed = allow_delayed
        self._max_lines = max_lines
        loop_time = monotonic or (lambda: asyncio.get_running_loop().time())
        self._pacer = _Pacer(min_request_interval, sleep, loop_time)
        self._clock = clock
        self._lines: dict[str, _Line] = {}
        self._wanted: set[str] = set()  # 订阅成功过、没有退订的代码：重连后据此恢复
        self._type_requested = False
        self._lock = asyncio.Lock()
        self._client.ib.disconnectedEvent += self._on_disconnected

    @property
    def subscribed(self) -> frozenset[str]:
        return frozenset(self._lines)

    def _on_disconnected(self) -> None:
        self._lines.clear()  # 旧 ticker 不再更新：不能继续当作行情（订阅清单保留，供重连后恢复）
        self._type_requested = False

    async def subscribe(self, symbols: Collection[str]) -> None:
        """订阅（已订阅的跳过）。

        超出上限时一只都不加。合约确认失败或中途断线时抛出：前面已经订上的保留，失败的那只不加。
        """
        wanted = {self._check_symbol(s) for s in symbols}
        async with self._lock:
            self._require_connection()
            new = sorted(wanted - set(self._lines))
            # 断线后待恢复的代码也算在内：否则 resubscribe 恢复时会超过上限
            held = set(self._lines) | self._wanted
            if len(held | set(new)) > self._max_lines:
                raise SubscriptionLimitError(
                    f"同时订阅上限 {self._max_lines} 行：已有 {len(held)}，又要 {len(new)}"
                )
            await self._request_type()
            for symbol in new:
                await self._open(symbol)
                self._wanted.add(symbol)

    async def resubscribe(self) -> dict[str, str]:
        """重连后恢复订阅清单：逐只恢复，一只失败不影响其他。返回失败的 {代码: 原因}。"""
        failed: dict[str, str] = {}
        async with self._lock:
            self._require_connection()
            await self._request_type()
            for symbol in sorted(self._wanted - set(self._lines)):
                try:
                    await self._open(symbol)
                except ConnectionError:
                    raise  # 又断了：剩下的等下一次重连
                except Exception as exc:
                    failed[symbol] = type(exc).__name__
        if failed:
            log_event(log, logging.ERROR, "IBKR 行情重新订阅有失败，这些标的没有报价",
                      failed=sorted(failed))  # fmt: skip
        return failed

    async def unsubscribe(self, symbols: Collection[str]) -> None:
        async with self._lock:
            for symbol in symbols:
                self._wanted.discard(symbol)
                line = self._lines.pop(symbol, None)
                if line is None or not self._client.is_connected():
                    continue
                try:
                    ok = self._client.ib.cancelMktData(line.contract)
                except Exception as exc:
                    ok, reason = False, type(exc).__name__
                else:
                    reason = "IBKR 找不到这条订阅"
                if ok is False:  # 本地已不计数，TWS 可能还占着这一行：留痕，联调时核对
                    log_event(log, logging.WARNING, "IBKR 行情退订失败", symbol=symbol,
                              reason=reason)  # fmt: skip

    async def quotes(self, symbols: Collection[str]) -> Mapping[str, Quote]:
        """只返回已订阅、有本次订阅后的数据、转换成功的标的；其余不返回，数据门禁按没有报价拒绝。"""
        if not self._client.is_connected():
            return {}
        result: dict[str, Quote] = {}
        for symbol in symbols:
            line = self._lines.get(symbol)
            if line is None:
                continue
            try:
                quote = quote_of(symbol, line.ticker, since=line.since)
            except (MappingError, ValueError):
                continue  # 数据不可信：不给报价（不抛出，免得一只标的拖垮整批）
            if quote is not None:
                result[symbol] = quote
        return result

    async def close(self) -> None:
        """退订全部并解绑断线事件（装配层停止时调用，避免残留占用行数）。"""
        await self.unsubscribe(sorted(set(self._lines) | self._wanted))
        with contextlib.suppress(Exception):
            self._client.ib.disconnectedEvent -= self._on_disconnected

    # ---------- 内部 ----------

    @staticmethod
    def _check_symbol(symbol: str) -> str:
        if not isinstance(symbol, str) or not _SYMBOL.match(symbol):
            raise ValueError(f"不支持的代码：{symbol!r}")
        return symbol

    def _require_connection(self) -> None:
        if not self._client.is_connected():
            raise ConnectionError("没有连接 IBKR，不能订阅行情")

    async def _request_type(self) -> None:
        if not self._type_requested:
            self._client.ib.reqMarketDataType(DELAYED if self._allow_delayed else LIVE)
            self._type_requested = True

    async def _open(self, symbol: str) -> None:
        contract = ib_async.Stock(symbol, "SMART", "USD")
        await self._pacer.wait()
        qualified = await self._client.ib.qualifyContractsAsync(contract)
        if not qualified or qualified[0] is None:
            raise MappingError(f"{symbol} 的合约没有确认（IBKR 找不到或有歧义）")
        found: Any = qualified[0]
        sec_type = str(getattr(found, "secType", "") or "").upper()
        if not getattr(found, "conId", 0) or sec_type not in {"STK", "ETF"}:
            raise MappingError(f"{symbol} 不是可订阅的股票或 ETF 合约")
        if str(getattr(found, "currency", "") or "").upper() != "USD":
            raise MappingError(f"{symbol} 不是美元计价")
        await self._pacer.wait()
        self._require_connection()  # 等待期间可能断线：不留下死线路
        since = self._clock()
        ticker = self._client.ib.reqMktData(found, "", False, False)
        # reqMktData 是同步发送，TWS 的回调要等交还事件循环后才处理：此时清空不会有竞态
        _clear(ticker)
        self._lines[symbol] = _Line(found, ticker, since)


__all__ = (
    "DEFAULT_MAX_LINES",
    "DEFAULT_REQUEST_INTERVAL",
    "DELAYED_FEED_LAG",
    "IbkrQuoteSource",
    "SubscriptionLimitError",
    "quote_of",
)
