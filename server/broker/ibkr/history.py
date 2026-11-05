"""IBKR 历史日线的窄接口（D-01b，不连 TWS 的部分）。

ib_async 只能在 server/broker/ibkr 里 import，所以“向 IBKR 要日线、转成 Decimal”放在这里；
按交易日历补开收盘时间、套 DataProvider 契约的适配器在 server/data/ibkr_provider.py。

**未在模拟盘实测**，联调时逐条核对（docs/tasks/D-01b.md）：
- `whatToShow="TRADES"`、`useRTH=True` 的日线口径：IBKR 会追溯调整拆股，不调整分红；
- `endDateTime` 与 `durationStr` 的写法、区间边界；
- 美股日线成交量的单位：股，还是 100 股一手。

失败即关闭：
- 合约必须确认：有 conId、是 STK、美元计价；
- 价格、成交量用 Decimal，NaN、UNSET、价格 ≤ 0、成交量 < 0 一律拒绝；
- K 线日期必须是 date 本身，不接受 datetime（日线不该带时刻）；
- ib_async 在超时、IBKR 报错时返回空列表、不抛异常：这里原样返回，
  由 data 层按“有交易日却没有数据”判为供应商不可用（ibkr_provider.py）。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import ib_async

from server.broker.ibkr.client import IbAsyncClient
from server.broker.ibkr.convert import decimal_of
from server.broker.ibkr.mapping import MappingError

HISTORY_TIMEOUT = 60.0  # 秒：超时由调用方当作供应商不可用
MAX_DAYS_PER_REQUEST = 365  # 超过一年的 durationStr 要写成 “N Y”，这里一律按一年拆分请求


class UnknownContractError(MappingError):
    """IBKR 找不到这个代码，或者找到的不是美元计价的股票或 ETF。"""


@dataclass(frozen=True)
class DailyBar:
    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    raw: str  # IBKR 原始字段（JSON 文本），原样进原始层


def _price(value: Any, what: str) -> Decimal:
    result = decimal_of(value, what)
    if result is None or result <= 0:
        raise MappingError(f"{what} 不是正数：{value!r}")
    return result


def _volume(value: Any, what: str) -> Decimal:
    result = decimal_of(value, what)
    if result is None or result < 0:
        raise MappingError(f"{what} 为负：{value!r}")
    return result


def _raw(bar: Any) -> str:
    def plain(value: Any) -> Any:
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return str(value)
        return value.isoformat() if isinstance(value, date) else value

    fields = {  # 字面量写法，不用反射（arch_scan）
        "date": bar.date, "open": bar.open, "high": bar.high, "low": bar.low,
        "close": bar.close, "volume": bar.volume,
        "average": getattr(bar, "average", None), "barCount": getattr(bar, "barCount", None),
    }  # fmt: skip
    return json.dumps({k: plain(v) for k, v in fields.items()}, sort_keys=True)


def daily_bar(symbol: str, bar: Any) -> DailyBar:
    day = getattr(bar, "date", None)
    if type(day) is not date:  # 日线的 date 应当是 date；datetime 说明请求口径不对
        raise MappingError(f"{symbol} 日线的日期不是 date：{day!r}")
    return DailyBar(
        day=day,
        open=_price(bar.open, f"{symbol} {day} 开盘价"),
        high=_price(bar.high, f"{symbol} {day} 最高价"),
        low=_price(bar.low, f"{symbol} {day} 最低价"),
        close=_price(bar.close, f"{symbol} {day} 收盘价"),
        volume=_volume(bar.volume, f"{symbol} {day} 成交量"),
        raw=_raw(bar),
    )


def duration(start: date, end: date) -> str:
    days = (end - start).days + 1
    if not 1 <= days <= MAX_DAYS_PER_REQUEST:
        raise ValueError(f"单次请求只能是 1 到 {MAX_DAYS_PER_REQUEST} 天：{days}")
    return f"{days} D"


async def qualify_stock(client: IbAsyncClient, symbol: str) -> Any:
    qualified = await client.ib.qualifyContractsAsync(ib_async.Stock(symbol, "SMART", "USD"))
    found: Any = qualified[0] if qualified else None
    if found is None or not getattr(found, "conId", 0):
        raise UnknownContractError(f"IBKR 找不到 {symbol}，或者有歧义")
    if str(getattr(found, "secType", "")).upper() != "STK":
        raise UnknownContractError(f"{symbol} 不是股票或 ETF")
    if str(getattr(found, "currency", "")).upper() != "USD":
        raise UnknownContractError(f"{symbol} 不是美元计价")
    if str(getattr(found, "symbol", "")) != symbol:
        raise UnknownContractError(f"IBKR 确认的合约代码与请求的 {symbol} 不一致")
    return found


async def fetch_daily_bars(
    client: IbAsyncClient, contract: Any, symbol: str, start: date, end: date
) -> list[DailyBar]:
    """[start, end] 的日线（常规时段、TRADES）。只发一次请求，不超过一年；按日期升序返回。"""
    if not client.is_connected():
        raise ConnectionError("没有连接 IBKR")
    bars = await client.ib.reqHistoricalDataAsync(
        contract,
        endDateTime=f"{end:%Y%m%d} 23:59:59 US/Eastern",
        durationStr=duration(start, end),
        barSizeSetting="1 day",
        whatToShow="TRADES",
        useRTH=True,
        formatDate=1,
        timeout=HISTORY_TIMEOUT,
    )
    result = [daily_bar(symbol, b) for b in bars]
    days = [b.day for b in result]
    if days != sorted(days) or len(set(days)) != len(days):
        raise MappingError(f"{symbol} 的日线日期乱序或重复")
    return [b for b in result if start <= b.day <= end]


__all__ = (
    "HISTORY_TIMEOUT",
    "MAX_DAYS_PER_REQUEST",
    "DailyBar",
    "UnknownContractError",
    "daily_bar",
    "duration",
    "fetch_daily_bars",
    "qualify_stock",
)
