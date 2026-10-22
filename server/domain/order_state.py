"""订单状态机、订单事件与时间约定（C-02，V4.1 6.3）。

转移表是数据：`TRANSITIONS` 列出所有合法的（状态, 事件）→ 新状态；不在表里的一律非法。
两类“观察型”事件不进表，由 `transition()` 单独处理：
- BROKER_STATUS_OBSERVED：只用于 UNKNOWN，查询券商后转入观察到的状态（禁止直接重发）
- CANCEL_REJECTED：只用于 CANCEL_PENDING，回到撤单前的活动状态；目标由“是否已有成交”推出，
  不由调用方指定

状态机只管状态。成交入账按 execution_id 去重，独立于状态转移：终态之后到达的成交
（IBKR 的 orderStatus 与 execDetails 是两路回调，顺序不保证）仍必须入账，另外生成差异事件，
见 docs/tasks/C-02.md。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Self

from pydantic import model_validator

from server.domain.values import Identifier, UtcDatetime, ValueModel, to_utc


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    RISK_APPROVED = "RISK_APPROVED"
    REJECTED_BY_RISK = "REJECTED_BY_RISK"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    UNKNOWN = "UNKNOWN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class EventKind(StrEnum):
    RISK_APPROVED = "RISK_APPROVED"
    RISK_REJECTED = "RISK_REJECTED"
    SUBMIT_SENT = "SUBMIT_SENT"
    BROKER_ACK = "BROKER_ACK"
    BROKER_REJECTED = "BROKER_REJECTED"
    SUBMIT_TIMEOUT = "SUBMIT_TIMEOUT"
    PARTIAL_FILL = "PARTIAL_FILL"
    FULL_FILL = "FULL_FILL"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCEL_CONFIRMED = "CANCEL_CONFIRMED"
    CANCEL_REJECTED = "CANCEL_REJECTED"
    BROKER_CANCELLED = "BROKER_CANCELLED"
    EXPIRED = "EXPIRED"
    BROKER_STATUS_OBSERVED = "BROKER_STATUS_OBSERVED"


S = OrderStatus
E = EventKind

TERMINAL = frozenset({S.FILLED, S.CANCELLED, S.REJECTED, S.EXPIRED, S.REJECTED_BY_RISK})

TRANSITIONS: Mapping[tuple[OrderStatus, EventKind], OrderStatus] = MappingProxyType(
    {
        (S.CREATED, E.RISK_APPROVED): S.RISK_APPROVED,
        (S.CREATED, E.RISK_REJECTED): S.REJECTED_BY_RISK,
        (S.RISK_APPROVED, E.SUBMIT_SENT): S.SUBMITTING,
        (S.SUBMITTING, E.BROKER_ACK): S.SUBMITTED,
        (S.SUBMITTING, E.BROKER_REJECTED): S.REJECTED,
        (S.SUBMITTING, E.SUBMIT_TIMEOUT): S.UNKNOWN,
        # IBKR 的成交回报可能先于接单确认到达：成交本身证明券商已接单
        (S.SUBMITTING, E.PARTIAL_FILL): S.PARTIALLY_FILLED,
        (S.SUBMITTING, E.FULL_FILL): S.FILLED,
        (S.SUBMITTED, E.PARTIAL_FILL): S.PARTIALLY_FILLED,
        (S.SUBMITTED, E.FULL_FILL): S.FILLED,
        (S.SUBMITTED, E.CANCEL_REQUESTED): S.CANCEL_PENDING,
        (S.SUBMITTED, E.BROKER_REJECTED): S.REJECTED,
        (S.SUBMITTED, E.EXPIRED): S.EXPIRED,
        (S.SUBMITTED, E.BROKER_CANCELLED): S.CANCELLED,
        (S.PARTIALLY_FILLED, E.PARTIAL_FILL): S.PARTIALLY_FILLED,
        (S.PARTIALLY_FILLED, E.FULL_FILL): S.FILLED,
        (S.PARTIALLY_FILLED, E.CANCEL_REQUESTED): S.CANCEL_PENDING,
        (S.PARTIALLY_FILLED, E.EXPIRED): S.EXPIRED,
        (S.PARTIALLY_FILLED, E.BROKER_CANCELLED): S.CANCELLED,
        # 撤单请求发出不等于撤单成功：等待期间的部分成交仍停留在 CANCEL_PENDING
        (S.CANCEL_PENDING, E.CANCEL_CONFIRMED): S.CANCELLED,
        (S.CANCEL_PENDING, E.PARTIAL_FILL): S.CANCEL_PENDING,
        (S.CANCEL_PENDING, E.FULL_FILL): S.FILLED,
        (S.CANCEL_PENDING, E.EXPIRED): S.EXPIRED,
        (S.CANCEL_PENDING, E.BROKER_CANCELLED): S.CANCELLED,
    }
)

_OBSERVABLE_FROM_UNKNOWN = frozenset(
    {S.SUBMITTED, S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.REJECTED, S.EXPIRED}
)


class IllegalTransitionError(Exception):
    """非法状态转移：调用方必须拒绝并告警，不得静默忽略。"""


class TimeOrderingError(Exception):
    """时间先后违反约定（信号 < 下单 < 成交；信号晚于 bar 结束），或时间不带时区。"""


def _as_status(value: object, name: str) -> OrderStatus:
    if not isinstance(value, str):
        raise IllegalTransitionError(f"未知的{name}：{value!r}")
    try:
        return OrderStatus(value)
    except ValueError as exc:
        raise IllegalTransitionError(f"未知的{name}：{value!r}") from exc


def transition(
    status: OrderStatus,
    kind: EventKind,
    *,
    observed: OrderStatus | None = None,
    has_fills: bool | None = None,
) -> OrderStatus:
    """返回新状态；非法转移抛 IllegalTransitionError。

    - BROKER_STATUS_OBSERVED 必须给出 observed；
    - CANCEL_REJECTED 必须给出 has_fills（由累计成交数量得出），目标状态由此推出；
    - 其他事件不得带这两个参数。
    """
    # 边界输入一律规范为枚举，防止原始字符串走错分支
    status = _as_status(status, "状态")
    try:
        kind = EventKind(kind)
    except ValueError as exc:
        raise IllegalTransitionError(f"未知的事件：{kind!r}") from exc
    observed_status = None if observed is None else _as_status(observed, "观察状态")

    if status in TERMINAL:
        raise IllegalTransitionError(f"{status} 是终态，{kind} 不改变状态（成交另行入账并对账）")

    if kind is E.BROKER_STATUS_OBSERVED:
        if has_fills is not None:
            raise IllegalTransitionError(f"{kind} 不接受 has_fills")
        if status is S.UNKNOWN and observed_status in _OBSERVABLE_FROM_UNKNOWN:
            return observed_status
        raise IllegalTransitionError(f"非法转移：{status} 收到 {kind}（观察到 {observed_status}）")

    if kind is E.CANCEL_REJECTED:
        if observed_status is not None or not isinstance(has_fills, bool):
            raise IllegalTransitionError(f"{kind} 只接受 has_fills，目标状态由状态机推出")
        if status is S.CANCEL_PENDING:
            return S.PARTIALLY_FILLED if has_fills else S.SUBMITTED
        raise IllegalTransitionError(f"非法转移：{status} 收到 {kind}")

    if observed_status is not None or has_fills is not None:
        raise IllegalTransitionError(f"{kind} 不接受 observed / has_fills 参数")
    if status is S.UNKNOWN:
        raise IllegalTransitionError(
            f"UNKNOWN 只能先查询券商（{E.BROKER_STATUS_OBSERVED}），禁止 {kind}"
        )
    target = TRANSITIONS.get((status, kind))
    if target is None:
        raise IllegalTransitionError(f"非法转移：{status} 收到 {kind}")
    return target


class OrderEvent(ValueModel):
    """统一的订单事件：带 UTC 时间戳与关联 ID，交给 transition() 计算新状态。"""

    event_id: Identifier
    intent_id: Identifier
    kind: EventKind
    occurred_at: UtcDatetime
    correlation_id: Identifier
    observed: OrderStatus | None = None
    has_fills: bool | None = None

    @model_validator(mode="after")
    def _arguments_match_kind(self) -> Self:
        if (self.kind is E.BROKER_STATUS_OBSERVED) != (self.observed is not None):
            raise ValueError("只有 BROKER_STATUS_OBSERVED 带 observed，且必须带")
        if (self.kind is E.CANCEL_REJECTED) != (self.has_fills is not None):
            raise ValueError("只有 CANCEL_REJECTED 带 has_fills，且必须带")
        return self


def apply_event(status: OrderStatus, event: OrderEvent) -> OrderStatus:
    return transition(status, event.kind, observed=event.observed, has_fills=event.has_fills)


def _utc(name: str, value: datetime) -> datetime:
    try:
        return to_utc(value)
    except ValueError as exc:
        raise TimeOrderingError(f"{name}：{exc}") from exc


def ensure_causal_order(
    *, signal_at: datetime, ordered_at: datetime, filled_at: datetime | None
) -> None:
    """信号可用时间 < 下单时间 < 成交时间（V4.1 8.3）。"""
    signal = _utc("signal_at", signal_at)
    ordered = _utc("ordered_at", ordered_at)
    if not signal < ordered:
        raise TimeOrderingError("下单时间必须晚于信号可用时间")
    if filled_at is not None and not ordered < _utc("filled_at", filled_at):
        raise TimeOrderingError("成交时间必须晚于下单时间")


def ensure_signal_after_bar(*, bar_end: datetime, signal_at: datetime) -> None:
    """信号只能基于已收盘的 bar：禁止同一根 bar 偷看收盘后信息再按收盘价成交。"""
    if not _utc("bar_end", bar_end) < _utc("signal_at", signal_at):
        raise TimeOrderingError("信号时间必须晚于其依据的 bar 结束时间")
