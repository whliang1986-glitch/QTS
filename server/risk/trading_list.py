"""交易名单风控规则（S-14）：仅放行生效交易名单内的 OPEN 买单，减仓与止损照常。"""

from __future__ import annotations

from collections.abc import Callable

from server.domain.enums import OrderClass, RiskVerdict
from server.risk.engine import RiskContext, RuleResult, Subject
from server.storage.trading_list import (
    CONFIRM_TRADING_LIST_PHRASE,
    TRADING_LIST_NOT_MEMBER,
    EligibilityCriteria,
    EligibilityResult,
    SymbolProfile,
    TradingListError,
    TradingListSnapshot,
    TradingListState,
    TradingListStore,
    check_eligibility,
)


class TradingListRule:
    """风控层交易名单硬拦截（S-14）：仅放行生效名单内的 OPEN 买单；减仓与止损照常。"""

    rule_id = "trading_list"
    version = "1"

    def __init__(self, allowed_symbols: Callable[[], frozenset[str] | None]) -> None:
        self._allowed = allowed_symbols

    def check(self, subject: Subject, _context: RiskContext) -> RuleResult | None:
        if subject.intent.order_class is not OrderClass.OPEN:
            return None
        allowed = self._allowed()
        if allowed is None or subject.intent.symbol not in allowed:
            return RuleResult(verdict=RiskVerdict.REJECT, message=TRADING_LIST_NOT_MEMBER)
        return None


__all__ = (
    "CONFIRM_TRADING_LIST_PHRASE",
    "TRADING_LIST_NOT_MEMBER",
    "EligibilityCriteria",
    "EligibilityResult",
    "SymbolProfile",
    "TradingListError",
    "TradingListRule",
    "TradingListSnapshot",
    "TradingListState",
    "TradingListStore",
    "check_eligibility",
)
