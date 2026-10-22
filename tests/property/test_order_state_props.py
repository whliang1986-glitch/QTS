"""C-02：随机事件序列下的状态机不变量（hypothesis）。

每一步大多从“当前状态的合法事件”里抽取，少量混入任意事件，确保能走到 UNKNOWN、
CANCEL_PENDING 来回切换等深层路径（评审 C-02 问题 6）。
"""

from __future__ import annotations

from typing import Any

from hypothesis import event, given, settings
from hypothesis import strategies as st
from server.domain.order_state import (
    TERMINAL,
    TRANSITIONS,
    EventKind,
    IllegalTransitionError,
    OrderStatus,
    transition,
)

S = OrderStatus
E = EventKind
OBSERVABLE = [S.SUBMITTED, S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.REJECTED, S.EXPIRED]


def _legal_moves(status: OrderStatus) -> list[tuple[EventKind, dict[str, Any]]]:
    moves: list[tuple[EventKind, dict[str, Any]]] = [
        (k, {}) for (s, k) in TRANSITIONS if s is status
    ]
    if status is S.UNKNOWN:
        moves += [(E.BROKER_STATUS_OBSERVED, {"observed": o}) for o in OBSERVABLE]
    if status is S.CANCEL_PENDING:
        moves += [(E.CANCEL_REJECTED, {"has_fills": f}) for f in (True, False)]
    return moves


def _any_move() -> st.SearchStrategy[tuple[EventKind, dict[str, Any]]]:
    return st.tuples(
        st.sampled_from(list(EventKind)),
        st.sampled_from(
            [{}, *({"observed": s} for s in OrderStatus), {"has_fills": True}, {"has_fills": False}]
        ),
    )


@settings(max_examples=1000)
@given(st.data())
def test_random_walks_keep_invariants(data: st.DataObject) -> None:
    status = S.CREATED
    sends = 0
    entered_unknown = False
    for _ in range(30):
        legal = _legal_moves(status)
        if legal and data.draw(st.integers(0, 9)) < 8:
            kind, extra = data.draw(st.sampled_from(legal))
        else:
            kind, extra = data.draw(_any_move())
        before = status
        try:
            status = transition(status, kind, **extra)
        except IllegalTransitionError:
            continue  # 非法转移不改变状态
        event(f"到达 {status}")
        assert before not in TERMINAL, "终态不得再转移"
        if before is S.UNKNOWN:
            assert kind is E.BROKER_STATUS_OBSERVED, "UNKNOWN 只能经查询券商转出"
        if kind is E.SUBMIT_SENT:
            sends += 1
            assert not entered_unknown, "进入 UNKNOWN 后不得再次发送"
        if status is S.UNKNOWN:
            entered_unknown = True
    assert sends <= 1, "一条订单链最多发送一次"
