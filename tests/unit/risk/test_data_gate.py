"""R-03：数据门禁。阈值是测试夹具（取 F-04 建议值），不是批准的参数；拒绝原因按完整文本断言。"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Any

import pytest
from server.domain.account import Position
from server.domain.enums import DataQuality, OrderClass, RiskVerdict, Side
from server.domain.market import Quote
from server.risk import data_gate as dg
from server.risk.data_gate import DELAYED_FEED_LAG, DataGateRule
from server.risk.engine import OpenOrder, RiskContext, Subject
from server.risk.parameters import SPECS, Kind, RiskParameters
from tests.support.oms import ACCOUNT, T0, market
from tests.unit.risk.test_limits import intent, reduce_order, stop_order

FIXTURE = {"max_quote_age_ms": "5000", "max_spread_bps": "30", "max_clock_skew_ms": "1000"}
PARAMS = RiskParameters(
    version="fixture",
    values=MappingProxyType(
        {
            s.name: Decimal(FIXTURE.get(s.name, "0.5" if s.kind is Kind.RATIO else "10"))
            for s in SPECS
        }
    ),
)


def quote(**kw: Any) -> Quote:
    data: dict[str, Any] = {
        "symbol": "SPY", "ts": T0, "bid": Decimal("99.99"), "ask": Decimal("100.01"),
        "last": Decimal(100), "source": "test", "delayed": False,
    }  # fmt: skip
    data.update(kw)
    return Quote.model_validate(data)


def context(*, q: Any = None, **kw: Any) -> RiskContext:
    data: dict[str, Any] = {"now": T0, **market(T0, {"SPY": Decimal(100)})}
    if q is not None:
        data["quotes"] = {"SPY": q}
    data.update(kw)
    return RiskContext.model_validate(data)


def check(ctx: RiskContext, order: Any = None) -> str | None:
    result = DataGateRule(PARAMS).check(Subject.new_order(order or intent()), ctx)
    if result is None:
        return None
    assert result.verdict is RiskVerdict.REJECT
    return result.message


def test_fresh_data_passes_for_open_and_reduce() -> None:
    assert check(context()) is None
    assert check(context(), reduce_order()) is None


def test_protective_stop_is_exempt_even_with_bad_data() -> None:
    bad = context(account_synced=None, clock_skew_ms=None, quotes=None)
    assert check(bad, stop_order()) is None
    assert check(bad) == dg.ACCOUNT_NOT_SYNCED


@pytest.mark.parametrize("synced", [None, False])
def test_account_must_be_synced(synced: bool | None) -> None:
    assert check(context(account_synced=synced)) == dg.ACCOUNT_NOT_SYNCED


def test_clock_skew() -> None:
    assert check(context(clock_skew_ms=None)) == dg.CLOCK_UNKNOWN
    assert check(context(clock_skew_ms=1000)) is None
    assert check(context(clock_skew_ms=-1000)) is None
    assert check(context(clock_skew_ms=1001)) == dg.CLOCK_SKEW
    assert check(context(clock_skew_ms=-1001)) == dg.CLOCK_SKEW


def test_quote_presence_and_identity() -> None:
    assert check(context(quotes=None)) == dg.NO_QUOTE
    assert check(context(quotes={})) == dg.NO_QUOTE
    assert check(context(q=quote(symbol="QQQ"))) == dg.QUOTE_SYMBOL


@pytest.mark.parametrize("flag", list(DataQuality))
def test_any_quality_flag_rejects(flag: DataQuality) -> None:
    assert check(context(q=quote(quality=frozenset({flag})))) == dg.QUOTE_QUALITY


def test_quote_age_boundary() -> None:
    assert check(context(q=quote(ts=T0 - timedelta(milliseconds=5000)))) is None
    assert check(context(q=quote(ts=T0 - timedelta(milliseconds=5001)))) == dg.QUOTE_STALE


def test_quote_from_the_future_beyond_skew_limit() -> None:
    assert check(context(q=quote(ts=T0 + timedelta(milliseconds=1000)))) is None
    assert check(context(q=quote(ts=T0 + timedelta(milliseconds=1001)))) == dg.QUOTE_FUTURE


def test_delayed_quotes_only_when_allowed() -> None:
    lagged = quote(delayed=True, ts=T0 - DELAYED_FEED_LAG - timedelta(milliseconds=5000))
    assert check(context(q=lagged)) == dg.QUOTE_DELAYED
    assert check(context(q=lagged, delayed_data_allowed=True)) is None
    older = quote(delayed=True, ts=T0 - DELAYED_FEED_LAG - timedelta(milliseconds=5001))
    assert check(context(q=older, delayed_data_allowed=True)) == dg.QUOTE_STALE
    # 允许延迟行情不放宽实时报价
    stale_live = quote(ts=T0 - timedelta(milliseconds=5001))
    assert check(context(q=stale_live, delayed_data_allowed=True)) == dg.QUOTE_STALE


@pytest.mark.parametrize(
    ("bid", "ask"),
    [(None, Decimal("100.01")), (Decimal("99.99"), None), (Decimal(100), Decimal(100)),
     (Decimal("100.02"), Decimal("100.01"))],
)  # fmt: skip
def test_missing_or_crossed_sides(bid: Decimal | None, ask: Decimal | None) -> None:
    assert check(context(q=quote(bid=bid, ask=ask))) == dg.QUOTE_SIDES


def test_spread_boundary() -> None:
    # 中间价 100，价差 0.30 = 30bp 正好在上限；0.32 = 32bp 超出
    assert check(context(q=quote(bid=Decimal("99.85"), ask=Decimal("100.15")))) is None
    assert check(context(q=quote(bid=Decimal("99.84"), ask=Decimal("100.16")))) == dg.QUOTE_SPREAD
    # 以中间价为分母：0.3002 / 100 = 30.02bp 超出（若误用卖价为分母会算成 29.98bp 放行）
    tight = quote(bid=Decimal("99.8499"), ask=Decimal("100.1501"))
    assert check(context(q=tight)) == dg.QUOTE_SPREAD


def test_last_price_must_exist_and_match() -> None:
    assert check(context(q=quote(last=None))) == dg.QUOTE_LAST
    assert check(context(last_prices={"SPY": Decimal("100.01")})) == dg.QUOTE_LAST
    assert check(context(last_prices={})) == dg.QUOTE_LAST


def test_amendments_are_gated_too() -> None:
    subject = Subject(intent=intent(), amendment_id="am", price=Decimal(100), quantity=Decimal(10))
    result = DataGateRule(PARAMS).check(subject, context(account_synced=False))
    assert result is not None and result.message == dg.ACCOUNT_NOT_SYNCED


def test_quote_age_is_corrected_by_known_clock_skew() -> None:
    # 本机快 500 毫秒：券商时间 = 本机 − 500
    ahead = quote(ts=T0 - timedelta(milliseconds=5400))  # 券商时间下 4900 毫秒
    assert check(context(q=ahead, clock_skew_ms=500)) is None
    assert check(context(q=ahead, clock_skew_ms=0)) == dg.QUOTE_STALE
    # 本机慢 500 毫秒：券商时间 = 本机 + 500
    behind = quote(ts=T0 - timedelta(milliseconds=4600))  # 券商时间下 5100 毫秒
    assert check(context(q=behind, clock_skew_ms=-500)) == dg.QUOTE_STALE
    future = quote(ts=T0 + timedelta(milliseconds=1400))
    assert check(context(q=future, clock_skew_ms=-500)) is None  # 券商时间下只超前 900 毫秒
    assert check(context(q=future, clock_skew_ms=500)) == dg.QUOTE_FUTURE


def _holding(symbol: str) -> Position:
    return Position(
        account_id=ACCOUNT.account_id, symbol=symbol, quantity=Decimal(10),
        average_cost=Decimal(100), as_of=T0,
    )  # fmt: skip


def test_open_needs_trustworthy_quotes_for_every_holding_and_open_order() -> None:
    stale_other = quote(symbol="AAPL", ts=T0 - timedelta(milliseconds=5001))
    quotes = {"SPY": quote(), "AAPL": stale_other}
    held = context(quotes=quotes, positions=(_holding("AAPL"),))
    assert check(held) == dg.QUOTE_STALE  # 开仓依赖净值：持仓 AAPL 报价陈旧即拒绝
    assert check(held, reduce_order()) is None  # 减仓只依赖本标的
    pending = OpenOrder(
        intent_id="o-9", symbol="AAPL", side=Side.BUY, order_class=OrderClass.OPEN,
        remaining_quantity=Decimal(1), limit_price=Decimal(100),
    )  # fmt: skip
    assert check(context(quotes=quotes, open_orders=(pending,))) == dg.QUOTE_STALE
    missing = context(positions=(_holding("AAPL"),))  # AAPL 没有报价
    assert check(missing) == dg.NO_QUOTE


def test_open_passes_when_every_holding_quote_is_good() -> None:
    quotes = {"SPY": quote(), "AAPL": quote(symbol="AAPL", last=Decimal(50),
                                           bid=Decimal("49.99"), ask=Decimal("50.01"))}  # fmt: skip
    ctx = context(
        quotes=quotes,
        positions=(_holding("AAPL"),),
        last_prices={"SPY": Decimal(100), "AAPL": Decimal(50)},
    )
    assert check(ctx) is None
