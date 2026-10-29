"""IBKR 专有状态、订单类型与错误码到 BrokerAdapter 统一类型的映射（B-03a）。

**全部未实测**：依据 IBKR TWS API 文档与常见行为整理，B-03b 在模拟盘逐条核对后修正
（B-02 待实测清单第 6、8 条）。失败即关闭：认不出的订单状态抛 MappingError（不猜测），
认不出的订单类型归为 OTHER（撤单需要确认），认不出的错误码归为 OTHER 且不可重试。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from server.broker.base import BrokerOrderState, BrokerOrderType, ErrorCategory

S = BrokerOrderState
E = ErrorCategory


class MappingError(Exception):
    """券商返回了认不出的状态：不得猜测，调用方按状态不可信处理（查单 / SAFE HALT）。"""


# 还没有被券商确认的中间状态：不产生事件，等后续状态（未实测）
PENDING_STATUSES = frozenset({"ApiPending", "PendingSubmit"})

_STATUS = {
    "PreSubmitted": S.ACKNOWLEDGED,  # 已被接受，等待触发（例如止损单、开盘前）
    "Submitted": S.ACKNOWLEDGED,
    "PendingCancel": S.CANCEL_PENDING,
    "ApiCancelled": S.CANCELLED,
    "Cancelled": S.CANCELLED,  # DAY 单到期也可能以 Cancelled 报出（未实测，B-02 清单第 6 条）
    "Filled": S.FILLED,
}
# Inactive 往往不是终态（被搁置、时段外、等待条件，之后还可能转为 Submitted）：
# 不映射为任何状态，抛 MappingError，由调用方先查单（失败即关闭；B-03b 实测，核对前不得下单）
UNRESOLVED_STATUSES = frozenset({"Inactive"})


def order_state(status: str, filled: Decimal, remaining: Decimal) -> BrokerOrderState | None:
    """IBKR orderStatus → 统一状态；中间状态返回 None（不产生事件）。

    Submitted / PreSubmitted 且已有成交、尚有剩余时为部分成交；Filled 必须没有剩余。
    """
    # 非有限值（NaN、Infinity、ib_async 的 UNSET_DOUBLE 哨兵转换而来的大数也应在转换时拒绝）
    if not (filled.is_finite() and remaining.is_finite()):
        raise MappingError(f"成交量或剩余量不是有限值：{filled} / {remaining}")
    if filled < 0 or remaining < 0:
        raise MappingError(f"成交量或剩余量为负：{filled} / {remaining}")
    if status in PENDING_STATUSES:
        return None
    if status in UNRESOLVED_STATUSES:
        raise MappingError(f"IBKR 状态 {status} 含义不确定（可能未结束）：需先查单")
    try:
        state = _STATUS[status]
    except KeyError:
        raise MappingError(f"认不出的 IBKR 订单状态：{status!r}") from None
    if state is S.ACKNOWLEDGED and filled > 0:
        return S.PARTIALLY_FILLED if remaining > 0 else S.FILLED
    if state is S.FILLED and (remaining != 0 or filled <= 0):
        raise MappingError(f"Filled 但成交量 {filled}、剩余 {remaining} 不一致")
    return state


# 止损类：撤单必须显式确认（保护性止损），认不出的也按需要确认处理（OTHER）
STOP_ORDER_TYPES = frozenset({"STP", "STP LMT", "TRAIL", "TRAIL LIMIT", "STP PRT", "TRAILLMT"})


def order_type(raw: str) -> BrokerOrderType:
    """只看订单类型字符串。带条件的 LMT、可调整止损（adjustedOrderType）可能也起保护作用，
    B-03b 映射时须同时检查订单条件，带条件或可调整的一律按 OTHER（撤单需要确认）。"""
    normalized = " ".join(raw.upper().split())
    if normalized == "LMT":
        return BrokerOrderType.LIMIT
    if normalized in STOP_ORDER_TYPES:
        return BrokerOrderType.STOP
    return BrokerOrderType.OTHER  # MKT、LOC、LIT、MIT、REL 等：撤单需要确认


@dataclass(frozen=True)
class ErrorInfo:
    category: ErrorCategory
    retryable: bool  # 只表示查询类请求可重试；从不授权重发订单（base.BrokerError）


# 信息类代码：只是状态通知（行情农场连接、延迟行情提示等），不作为错误上报
# 10167（正在显示延迟行情）只是提示：行情是否延迟以报价上的标记为准（数据层），不靠这个码（ADR-06）
INFO_CODES = frozenset({2104, 2106, 2107, 2108, 2119, 2158, 10167})
# TWS 与 IB 服务器的连接：1100 断开、1101 / 1102 恢复。适配器把它们交给
# ConnectionSupervisor.note_broker_error（断开期间不允许交易，恢复后先对账）
BROKER_LOST = 1100
BROKER_RESTORED = frozenset({1101, 1102})

_ERRORS: dict[int, ErrorInfo] = {
    100: ErrorInfo(E.RATE_LIMITED, retryable=True),  # 超过每秒消息上限
    103: ErrorInfo(E.DUPLICATE_ORDER_ID, retryable=False),
    110: ErrorInfo(E.PRICE_REJECTED, retryable=False),  # 价格不符合最小变动价位
    135: ErrorInfo(E.ORDER_NOT_FOUND, retryable=False),
    161: ErrorInfo(E.CANCEL_REJECTED, retryable=False),  # 订单不在可撤状态
    162: ErrorInfo(E.RATE_LIMITED, retryable=True),  # 历史数据请求过频
    200: ErrorInfo(E.CONTRACT_NOT_FOUND, retryable=False),
    201: ErrorInfo(E.OTHER, retryable=False),  # 订单被拒：原因在消息里，B-03b 细分
    # 202“订单已撤”：可能是撤单成功的回报而非撤单被拒，语义未实测（B-03b 核对）
    202: ErrorInfo(E.CANCEL_REJECTED, retryable=False),
    326: ErrorInfo(E.CONNECTIVITY, retryable=False),  # clientId 已被占用：换 id 前不得重试
    354: ErrorInfo(E.NO_PERMISSION, retryable=False),  # 没有行情订阅
    502: ErrorInfo(E.CONNECTIVITY, retryable=True),  # 连不上 TWS
    504: ErrorInfo(E.CONNECTIVITY, retryable=True),  # 未连接
    1100: ErrorInfo(E.CONNECTIVITY, retryable=True),  # TWS 与 IB 服务器断开
    1300: ErrorInfo(E.CONNECTIVITY, retryable=True),  # 端口改变，需要重连
    10147: ErrorInfo(E.ORDER_NOT_FOUND, retryable=False),  # 要撤的订单号不存在
    10148: ErrorInfo(E.CANCEL_REJECTED, retryable=False),  # 订单状态不可撤
}


def error_info(code: int) -> ErrorInfo | None:
    """IBKR 错误码 → 统一错误类别；信息类代码返回 None（不上报为错误）。"""
    if code in INFO_CODES:
        return None
    if code in BROKER_RESTORED:
        return ErrorInfo(E.CONNECTIVITY, retryable=True)
    return _ERRORS.get(code, ErrorInfo(E.OTHER, retryable=False))
