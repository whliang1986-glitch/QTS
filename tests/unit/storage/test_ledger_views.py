"""账本只读视图（S-02）：最近订单与最新状态、按游标续读订单事件。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from server.domain.order_state import EventKind, OrderStatus
from server.storage.errors import StorageError
from server.storage.ledger import MAX_VIEW_ROWS, LedgerRepository, open_ledger
from server.storage.migrate import upgrade
from tests.unit.api.test_app import event, intent

T0 = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> LedgerRepository:
    path = tmp_path / "ledger.db"
    upgrade(path)
    return LedgerRepository(open_ledger(path))


def test_empty_ledger(repo: LedgerRepository) -> None:
    assert repo.recent_orders(10) == []
    assert repo.order_events_after(0, 10) == []
    assert repo.last_order_event_seq() == 0


def test_latest_status_per_order(repo: LedgerRepository) -> None:
    repo.add_intent(intent("a", T0))
    repo.add_intent(intent("b", T0 + timedelta(seconds=1)))
    repo.append_order_event(event("a", 1, EventKind.RISK_APPROVED), OrderStatus.RISK_APPROVED)
    repo.append_order_event(event("b", 2, EventKind.RISK_APPROVED), OrderStatus.RISK_APPROVED)
    repo.append_order_event(event("a", 3, EventKind.SUBMIT_SENT), OrderStatus.SUBMITTING)
    rows = repo.recent_orders(10)
    assert [(r.intent.intent_id, r.status) for r in rows] == [
        ("b", OrderStatus.RISK_APPROVED), ("a", OrderStatus.SUBMITTING)
    ]  # fmt: skip
    assert rows[1].updated_at == (T0 + timedelta(seconds=3)).isoformat(timespec="microseconds")


def test_events_after_cursor_in_write_order(repo: LedgerRepository) -> None:
    repo.add_intent(intent("a", T0))
    for n, (kind, status) in enumerate(
        [(EventKind.RISK_APPROVED, OrderStatus.RISK_APPROVED),
         (EventKind.SUBMIT_SENT, OrderStatus.SUBMITTING)], start=1
    ):  # fmt: skip
        repo.append_order_event(event("a", n, kind), status)
    first = repo.order_events_after(0, 1)
    assert [r.status for r in first] == [OrderStatus.RISK_APPROVED]
    rest = repo.order_events_after(first[-1].seq, 10)
    assert [r.status for r in rest] == [OrderStatus.SUBMITTING]
    assert repo.last_order_event_seq() == rest[-1].seq


@pytest.mark.parametrize("limit", [0, -1, MAX_VIEW_ROWS + 1])
def test_limits_are_bounded(repo: LedgerRepository, limit: int) -> None:
    with pytest.raises(StorageError, match="limit"):
        repo.recent_orders(limit)
    with pytest.raises(StorageError, match="limit"):
        repo.order_events_after(0, limit)
