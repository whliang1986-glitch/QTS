"""数据门禁（R-03，V4.1 7.5 “行情陈旧或异常：拒绝依赖该数据的新增订单”）。

按评估时的上下文检查本单所依赖的数据，任一项不可信即拒绝（失败即关闭）：
- 账户同步：持仓、现金、订单自上次连接以来已与券商同步（上下文提供者给出）；不同步或未知即拒绝；
- 时钟偏差：|本机 − 券商| ≤ max_clock_skew_ms；未知即拒绝；
- 报价：有该标的的报价，标的一致，没有质量标记（陈旧、缺失、跳空、来源不一致……）；
  按已知偏差换算成券商时间后，报价时间不晚于“现在 + 时钟偏差上限”，报价年龄 ≤ max_quote_age_ms；
  开仓还要求全部持仓与挂单标的的报价同样可信（净值、敞口与单标的占用都依赖这些价格）；
  延迟行情只在受保护配置允许时接受（ADR-06：只限 PAPER），
  年龄上限加上交易所规定的延迟 DELAYED_FEED_LAG；
  买价、卖价都有且不交叉，价差 ≤ max_spread_bps（相对中间价）；最新价有，且与风控用的最新价一致。

适用范围：开仓与减仓（含紧急平仓、改单）；保护性止损豁免——它不新增风险，数据不可信时拒绝它会让持仓失去保护
（它自己的专项规则仍要求最新价可用且止损价低于最新价）。阈值全部来自受保护配置（R-02a），这里不写数值。
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from server.domain.enums import OrderClass, RiskVerdict
from server.risk.engine import RiskContext, RuleResult, Subject
from server.risk.parameters import RiskParameters

V = RiskVerdict
# 交易所对延迟行情规定的延迟时间（单位定义，不是可调阈值；只在 ADR-06 允许延迟行情时使用）
DELAYED_FEED_LAG = timedelta(minutes=15)
BPS = Decimal(10000)

ACCOUNT_NOT_SYNCED = "账户持仓、现金或订单未与券商同步（或同步状态未知），拒绝新增"
CLOCK_UNKNOWN = "时钟偏差未知，拒绝新增"
CLOCK_SKEW = "本机与券商时钟偏差超过上限，拒绝新增"
NO_QUOTE = "没有该标的的报价，拒绝新增"
QUOTE_SYMBOL = "报价标的与订单不一致"
QUOTE_QUALITY = "报价带数据质量标记，拒绝新增"
QUOTE_DELAYED = "延迟行情未获准使用（只在受保护配置允许的模拟盘中可用）"
QUOTE_FUTURE = "报价时间晚于当前时间，时间戳不可信"
QUOTE_STALE = "报价陈旧，拒绝新增"
QUOTE_SIDES = "买价或卖价缺失、为零或交叉，无法核对价差"
QUOTE_SPREAD = "买卖价差超过上限，拒绝新增"
QUOTE_LAST = "报价没有最新价，或与风控使用的最新价不一致"


def _reject(message: str) -> RuleResult:
    return RuleResult(verdict=V.REJECT, message=message)


class DataGateRule:
    rule_id = "data_gate"
    version = "1"

    def __init__(self, params: RiskParameters) -> None:
        self._p = params

    def check(self, subject: Subject, context: RiskContext) -> RuleResult | None:
        if subject.intent.order_class is OrderClass.PROTECTIVE_STOP:
            return None
        if context.account_synced is not True:
            return _reject(ACCOUNT_NOT_SYNCED)
        skew_limit_ms = int(self._p["max_clock_skew_ms"])
        if context.clock_skew_ms is None:
            return _reject(CLOCK_UNKNOWN)
        if abs(context.clock_skew_ms) > skew_limit_ms:
            return _reject(CLOCK_SKEW)
        # 开仓依赖全部持仓与挂单的价格（净值、敞口、单标的占用），这些标的的报价也必须可信
        symbols = [subject.intent.symbol]
        if subject.intent.order_class is OrderClass.OPEN:
            others = {p.symbol for p in context.positions or ()}
            others.update(o.symbol for o in context.open_orders or ())
            symbols += sorted(others - {subject.intent.symbol})
        for symbol in symbols:
            problem = self._check_quote(symbol, context, skew_limit_ms)
            if problem is not None:
                return _reject(problem)
        return None

    def _check_quote(  # noqa: PLR0911 - 每项检查一个拒绝原因，逐条列出便于审计
        self, symbol: str, context: RiskContext, skew_limit_ms: int
    ) -> str | None:
        quote = (context.quotes or {}).get(symbol)
        if quote is None:
            return NO_QUOTE
        if quote.symbol != symbol:
            return QUOTE_SYMBOL
        if quote.quality:
            return QUOTE_QUALITY
        max_age = timedelta(milliseconds=int(self._p["max_quote_age_ms"]))
        if quote.delayed:
            if not context.delayed_data_allowed:
                return QUOTE_DELAYED
            max_age += DELAYED_FEED_LAG
        # 按已知的时钟偏差换算成券商时间再比较（偏差 = 本机 − 券商）
        assert context.clock_skew_ms is not None  # noqa: S101 - 调用方已检查
        broker_now = context.now - timedelta(milliseconds=context.clock_skew_ms)
        if quote.ts > broker_now + timedelta(milliseconds=skew_limit_ms):
            return QUOTE_FUTURE
        if broker_now - quote.ts > max_age:
            return QUOTE_STALE
        bid, ask = quote.bid, quote.ask
        if bid is None or ask is None or bid >= ask:
            return QUOTE_SIDES
        mid = (bid + ask) / 2
        if (ask - bid) / mid * BPS > self._p["max_spread_bps"]:
            return QUOTE_SPREAD
        last = (context.last_prices or {}).get(symbol)
        if quote.last is None or last is None or quote.last != last:
            return QUOTE_LAST
        return None


__all__ = ("DELAYED_FEED_LAG", "DataGateRule")
