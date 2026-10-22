"""C-02：订单状态转移表全覆盖。

期望表在这里按 V4.1 6.3（及 docs/tasks/C-02.md 的解读）独立再写一遍，
与 server/domain/order_state.py 的表逐格比对：任何一格不一致都会失败。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from server.domain.order_state import (
    TERMINAL,
    TRANSITIONS,
    EventKind,
    IllegalTransitionError,
    OrderEvent,
    OrderStatus,
    TimeOrderingError,
    apply_event,
    ensure_causal_order,
    ensure_signal_after_bar,
    transition,
)

S = OrderStatus
E = EventKind

# (当前状态, 事件) -> 新状态；不在表里的组合一律非法
EXPECTED: dict[tuple[OrderStatus, EventKind], OrderStatus] = {
    (S.CREATED, E.RISK_APPROVED): S.RISK_APPROVED,
    (S.CREATED, E.RISK_REJECTED): S.REJECTED_BY_RISK,
    (S.RISK_APPROVED, E.SUBMIT_SENT): S.SUBMITTING,
    (S.SUBMITTING, E.BROKER_ACK): S.SUBMITTED,
    (S.SUBMITTING, E.BROKER_REJECTED): S.REJECTED,
    (S.SUBMITTING, E.SUBMIT_TIMEOUT): S.UNKNOWN,
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
    (S.CANCEL_PENDING, E.CANCEL_CONFIRMED): S.CANCELLED,
    (S.CANCEL_PENDING, E.PARTIAL_FILL): S.CANCEL_PENDING,
    (S.CANCEL_PENDING, E.FULL_FILL): S.FILLED,
    (S.CANCEL_PENDING, E.EXPIRED): S.EXPIRED,
    (S.CANCEL_PENDING, E.BROKER_CANCELLED): S.CANCELLED,
}
OBSERVABLE_FROM_UNKNOWN = {
    S.SUBMITTED, S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.REJECTED, S.EXPIRED,
}  # fmt: skip
PLAIN = [k for k in EventKind if k not in {E.BROKER_STATUS_OBSERVED, E.CANCEL_REJECTED}]


@pytest.mark.parametrize("status", list(OrderStatus))
@pytest.mark.parametrize("kind", PLAIN)
def test_every_plain_cell(status: OrderStatus, kind: EventKind) -> None:
    expected = EXPECTED.get((status, kind))
    if expected is None:
        with pytest.raises(IllegalTransitionError):
            transition(status, kind)
    else:
        assert transition(status, kind) is expected


@pytest.mark.parametrize("status", list(OrderStatus))
@pytest.mark.parametrize("observed", list(OrderStatus))
def test_broker_status_observed_cells(status: OrderStatus, observed: OrderStatus) -> None:
    legal = status is S.UNKNOWN and observed in OBSERVABLE_FROM_UNKNOWN
    if legal:
        assert transition(status, E.BROKER_STATUS_OBSERVED, observed=observed) is observed
    else:
        with pytest.raises(IllegalTransitionError):
            transition(status, E.BROKER_STATUS_OBSERVED, observed=observed)


@pytest.mark.parametrize("status", list(OrderStatus))
@pytest.mark.parametrize("has_fills", [True, False])
def test_cancel_rejected_cells(status: OrderStatus, has_fills: bool) -> None:
    if status is S.CANCEL_PENDING:
        expected = S.PARTIALLY_FILLED if has_fills else S.SUBMITTED
        assert transition(status, E.CANCEL_REJECTED, has_fills=has_fills) is expected
    else:
        with pytest.raises(IllegalTransitionError):
            transition(status, E.CANCEL_REJECTED, has_fills=has_fills)


def test_cancel_rejected_target_cannot_be_chosen_by_caller() -> None:
    # 撤单期间已有成交时，调用方不能把状态指定回 SUBMITTED（评审 C-02 问题 2）
    with pytest.raises(IllegalTransitionError):
        transition(S.CANCEL_PENDING, E.CANCEL_REJECTED, observed=S.SUBMITTED)
    with pytest.raises(IllegalTransitionError):
        transition(S.CANCEL_PENDING, E.CANCEL_REJECTED)
    with pytest.raises(IllegalTransitionError):  # 字符串 "False" 不能当布尔真值
        transition(S.CANCEL_PENDING, E.CANCEL_REJECTED, has_fills="False")  # type: ignore[arg-type]


@pytest.mark.parametrize("status", list(OrderStatus))
@pytest.mark.parametrize("kind", PLAIN)
@pytest.mark.parametrize("extra", [{"observed": S.FILLED}, {"has_fills": True}])
def test_plain_events_reject_extra_arguments(
    status: OrderStatus, kind: EventKind, extra: dict[str, object]
) -> None:
    with pytest.raises(IllegalTransitionError):
        transition(status, kind, **extra)  # type: ignore[arg-type]


def test_observation_events_require_their_argument() -> None:
    with pytest.raises(IllegalTransitionError):
        transition(S.UNKNOWN, E.BROKER_STATUS_OBSERVED)
    with pytest.raises(IllegalTransitionError):
        transition(S.UNKNOWN, E.BROKER_STATUS_OBSERVED, observed=S.FILLED, has_fills=True)


def test_raw_strings_are_normalized() -> None:
    # 原始字符串不得走错分支（评审 C-02 问题 4）
    assert transition("UNKNOWN", "BROKER_STATUS_OBSERVED", observed="FILLED") is S.FILLED  # type: ignore[arg-type]
    with pytest.raises(IllegalTransitionError):
        transition("CANCEL_PENDING", "BROKER_STATUS_OBSERVED", observed="SUBMITTED")  # type: ignore[arg-type]
    with pytest.raises(IllegalTransitionError):
        transition("NOT_A_STATUS", E.BROKER_ACK)  # type: ignore[arg-type]
    with pytest.raises(IllegalTransitionError):
        transition(S.SUBMITTED, "NOT_AN_EVENT")  # type: ignore[arg-type]
    assert type(transition("SUBMITTED", "FULL_FILL")) is OrderStatus  # type: ignore[arg-type]


def test_implementation_table_matches_spec_exactly() -> None:
    assert dict(TRANSITIONS) == EXPECTED


def test_terminal_states() -> None:
    assert {S.FILLED, S.CANCELLED, S.REJECTED, S.EXPIRED, S.REJECTED_BY_RISK} == TERMINAL
    for status in TERMINAL:
        for kind in EventKind:
            with pytest.raises(IllegalTransitionError):
                transition(status, kind)


def test_unknown_cannot_be_resent() -> None:
    with pytest.raises(IllegalTransitionError, match="UNKNOWN"):
        transition(S.UNKNOWN, E.SUBMIT_SENT)


def test_no_submission_before_risk() -> None:
    with pytest.raises(IllegalTransitionError):
        transition(S.CREATED, E.SUBMIT_SENT)


def test_every_status_is_reachable_from_created() -> None:
    reached = {S.CREATED}
    frontier = [S.CREATED]
    while frontier:
        status = frontier.pop()
        targets = {t for (s, _), t in TRANSITIONS.items() if s is status}
        if status is S.UNKNOWN:
            targets |= OBSERVABLE_FROM_UNKNOWN
        for t in targets - reached:
            reached.add(t)
            frontier.append(t)
    assert reached == set(OrderStatus)


# ---------- 订单事件 ----------

T = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
S1 = timedelta(seconds=1)


def _event(kind: EventKind, **kw: object) -> OrderEvent:
    return OrderEvent.model_validate(
        {"event_id": "e1", "intent_id": "i1", "kind": kind, "occurred_at": T,
         "correlation_id": "c1", **kw}
    )  # fmt: skip


def test_order_event_applies_and_carries_utc_time() -> None:
    ev = _event(E.BROKER_STATUS_OBSERVED, observed=S.FILLED)
    assert ev.occurred_at.tzinfo is UTC
    assert apply_event(S.UNKNOWN, ev) is S.FILLED
    assert apply_event(S.CANCEL_PENDING, _event(E.CANCEL_REJECTED, has_fills=True)) is (
        S.PARTIALLY_FILLED
    )


@pytest.mark.parametrize(
    ("kind", "extra"),
    [
        (E.BROKER_STATUS_OBSERVED, {}),
        (E.CANCEL_REJECTED, {}),
        (E.BROKER_ACK, {"observed": S.FILLED}),
        (E.BROKER_ACK, {"has_fills": False}),
    ],
)
def test_order_event_arguments_must_match_kind(kind: EventKind, extra: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _event(kind, **extra)


def test_order_event_rejects_naive_time() -> None:
    with pytest.raises(ValidationError):
        _event(E.BROKER_ACK, occurred_at=T.replace(tzinfo=None))


# ---------- 时间先后 ----------


def test_causal_order_accepts_strictly_increasing() -> None:
    ensure_causal_order(signal_at=T, ordered_at=T + S1, filled_at=T + 2 * S1)
    ensure_causal_order(signal_at=T, ordered_at=T + S1, filled_at=None)


@pytest.mark.parametrize(
    ("signal", "ordered", "filled"),
    [(T, T, None), (T + S1, T, None), (T, T + S1, T + S1), (T, T + 2 * S1, T + S1)],
)
def test_causal_order_rejects_violations(
    signal: datetime, ordered: datetime, filled: datetime | None
) -> None:
    with pytest.raises(TimeOrderingError):
        ensure_causal_order(signal_at=signal, ordered_at=ordered, filled_at=filled)


NAIVE = T.replace(tzinfo=None)


@pytest.mark.parametrize("which", ["signal_at", "ordered_at", "filled_at"])
def test_causal_order_rejects_naive_time_in_any_argument(which: str) -> None:
    args: dict[str, datetime] = {
        "signal_at": T, "ordered_at": T + S1, "filled_at": T + 2 * S1,
    }  # fmt: skip
    args[which] = NAIVE
    with pytest.raises(TimeOrderingError):
        ensure_causal_order(**args)


def test_signal_must_come_after_bar_close() -> None:
    ensure_signal_after_bar(bar_end=T, signal_at=T + S1)
    with pytest.raises(TimeOrderingError):
        ensure_signal_after_bar(bar_end=T, signal_at=T)
    with pytest.raises(TimeOrderingError):
        ensure_signal_after_bar(bar_end=T + S1, signal_at=T)
    with pytest.raises(TimeOrderingError):
        ensure_signal_after_bar(bar_end=NAIVE, signal_at=T + S1)
    with pytest.raises(TimeOrderingError):
        ensure_signal_after_bar(bar_end=T, signal_at=NAIVE + S1)
