"""领域枚举（C-01）。枚举值不得使用 arch_scan 的受保护名称（如 cancel_order）。"""

from __future__ import annotations

from enum import StrEnum


class AssetClass(StrEnum):
    """首期只允许股票与 ETF；新资产类别需要单独的 G8 批准。"""

    STK = "STK"
    ETF = "ETF"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderClass(StrEnum):
    """订单类别在创建时确定，之后不可修改；guardd 与急停按类别撤单（V4.1 6.4）。"""

    OPEN = "OPEN"
    REDUCE = "REDUCE"
    PROTECTIVE_STOP = "PROTECTIVE_STOP"


class OrderType(StrEnum):
    """没有市价单：一般订单只用限价单，止损单只用于保护性止损（R-04 专项豁免）。"""

    LIMIT = "LIMIT"
    STOP = "STOP"


class TimeInForce(StrEnum):
    DAY = "DAY"
    GTD = "GTD"
    GTC = "GTC"  # 只允许保护性止损使用


class AccountType(StrEnum):
    """真实账户类型以 IBKR 返回为准（ADR-15）。"""

    CASH = "CASH"
    MARGIN = "MARGIN"


class Mode(StrEnum):
    RESEARCH = "RESEARCH"
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    ASSISTED_LIVE = "ASSISTED_LIVE"
    LIMITED_AUTO_LIVE = "LIMITED_AUTO_LIVE"
    HALT = "HALT"


LIVE_MODES = frozenset({Mode.ASSISTED_LIVE, Mode.LIMITED_AUTO_LIVE})


class RiskVerdict(StrEnum):
    ALLOW = "ALLOW"
    REDUCE = "REDUCE"
    REJECT = "REJECT"
    HALT = "HALT"


class DataQuality(StrEnum):
    STALE = "STALE"
    INCOMPLETE = "INCOMPLETE"
    GAP = "GAP"
    SOURCE_MISMATCH = "SOURCE_MISMATCH"
    ADJUSTMENT_MISMATCH = "ADJUSTMENT_MISMATCH"
