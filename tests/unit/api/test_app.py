"""apid 路由（S-02）：认证、只读查询、命令校验与结果语义、事件流。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from server.api.app import ApiDeps, create_app
from server.api.auth import Principal, Role, bearer_token
from server.api.commands import (
    CATALOG,
    Answer,
    NotDeliveredError,
    OutcomeUnknownError,
    Tier,
)
from server.api.security import MemoryAudit
from server.domain.enums import Mode, OrderClass, OrderType, Side, TimeInForce
from server.domain.modes import HaltState
from server.domain.order_state import EventKind, OrderEvent, OrderStatus
from server.domain.orders import BrokerOrder, Execution, OrderIntent
from server.storage.errors import StorageError
from server.storage.ledger import LedgerRepository, open_ledger
from server.storage.migrate import upgrade
from starlette.websockets import WebSocketDisconnect

T0 = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
OWNER = "Bearer owner-token"
VIEWER = "Bearer viewer-token"


class Tokens:
    def __init__(self) -> None:
        self.valid = {
            "owner-token": Principal("dylan", Role.OWNER),
            "viewer-token": Principal("guest", Role.VIEWER),
        }
        self.broken = False

    def authenticate(self, token: str) -> Principal | None:
        if self.broken:
            raise RuntimeError("认证库挂了")
        return self.valid.get(token)


@dataclass
class Gateway:
    answer: Answer = field(default_factory=lambda: Answer(True, True, {"x": 1}, False, False, None))
    error: Exception | None = None
    calls: list[tuple[str, str, dict[str, str], str]] = field(default_factory=list)

    async def send(
        self, request_id: str, name: str, params: Mapping[str, str], *, actor: str
    ) -> Answer:
        self.calls.append((request_id, name, dict(params), actor))
        if self.error is not None:
            raise self.error
        return self.answer


def intent(intent_id: str, created: datetime) -> OrderIntent:
    return OrderIntent.model_validate(
        {
            "intent_id": intent_id, "created_at": created, "strategy_id": "s",
            "strategy_version": "1", "account_id": "DU0000001", "symbol": "SPY",
            "side": Side.BUY, "quantity": Decimal(10), "order_type": OrderType.LIMIT,
            "limit_price": Decimal("100.00"), "order_class": OrderClass.OPEN,
            "time_in_force": TimeInForce.DAY,
        }
    )  # fmt: skip


def event(intent_id: str, n: int, kind: EventKind) -> OrderEvent:
    return OrderEvent(
        event_id=f"e-{intent_id}-{n}", intent_id=intent_id, kind=kind,
        occurred_at=T0 + timedelta(seconds=n), correlation_id=f"c-{n}",
    )  # fmt: skip


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.db"
    upgrade(path)
    repo = LedgerRepository(open_ledger(path))
    repo.add_intent(intent("old", T0))
    repo.add_intent(intent("new", T0 + timedelta(minutes=1)))
    repo.add_intent(intent("bare", T0 - timedelta(minutes=1)))
    repo.append_order_event(event("old", 1, EventKind.RISK_APPROVED), OrderStatus.RISK_APPROVED)
    repo.append_order_event(event("new", 2, EventKind.RISK_APPROVED), OrderStatus.RISK_APPROVED)
    repo.append_order_event(event("old", 3, EventKind.SUBMIT_SENT), OrderStatus.SUBMITTING)
    halt = HaltState(halted=True, reason="人工急停", since=T0, set_by="dylan")
    repo.append_mode_change("m-1", Mode.PAPER, halt, T0, "dylan")
    return path


@dataclass
class Api:
    client: TestClient
    tokens: Tokens
    gateway: Gateway
    ledger_ok: list[bool]


def make_api(ledger: Path, **kw: Any) -> Api:
    tokens, gateway, ok = Tokens(), Gateway(), [True]

    def repo() -> LedgerRepository:
        if not ok[0]:
            raise StorageError("磁盘坏了")
        return LedgerRepository(open_ledger(ledger, read_only=True))

    deps = ApiDeps(authenticator=tokens, ledger=repo, gateway=gateway, audit=MemoryAudit(), **kw)
    return Api(TestClient(create_app(deps)), tokens, gateway, ok)


@pytest.fixture
def api(ledger: Path) -> Api:
    return make_api(ledger, stream_interval=0.05, auth_timeout=0.5)


def command(api: Api, body: dict[str, Any], auth: str = OWNER) -> Any:
    return api.client.post("/v1/commands", json=body, headers={"Authorization": auth})


STOP = {"request_id": "req-0001", "name": "stop_new_orders", "params": {"reason": "测试"}}


# ---------- 认证 ----------


@pytest.mark.parametrize(
    "path", ["/v1/whoami", "/v1/status", "/v1/orders", "/v1/orders/old", "/v1/commands/catalog"]
)
@pytest.mark.parametrize("auth", [None, "Bearer nope", "Basic owner-token", "owner-token"])
def test_every_route_needs_a_valid_token(api: Api, path: str, auth: str | None) -> None:
    headers = {} if auth is None else {"Authorization": auth}
    response = api.client.get(path, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_token_is_not_read_from_query_string(api: Api) -> None:
    assert api.client.get("/v1/status?token=owner-token").status_code == 401
    assert api.client.get("/v1/status?access_token=owner-token").status_code == 401


def test_authenticator_failure_is_refusal_not_pass(api: Api) -> None:
    api.tokens.broken = True
    assert api.client.get("/v1/status", headers={"Authorization": OWNER}).status_code == 503
    assert command(api, STOP).status_code == 503
    assert api.gateway.calls == []


def test_no_docs_or_openapi_in_production(api: Api) -> None:
    for path in ("/docs", "/openapi.json", "/redoc"):
        assert api.client.get(path).status_code == 404


def test_whoami(api: Api) -> None:
    body = api.client.get("/v1/whoami", headers={"Authorization": VIEWER}).json()
    assert body == {"name": "guest", "role": "viewer"}


@pytest.mark.parametrize(
    ("header", "token"),
    [("Bearer abc", "abc"), ("bearer abc", "abc"), ("Bearer  abc ", "abc"), ("Bearer", None),
     ("Bearer a b", None), ("Token abc", None), (None, None)],
)  # fmt: skip
def test_bearer_parsing(header: str | None, token: str | None) -> None:
    assert bearer_token(header) == token


@pytest.mark.parametrize("name", ["", " ", "a b", "x" * 129])
def test_principal_names_must_be_identifiers(name: str) -> None:
    with pytest.raises(ValueError, match="使用者名称"):
        Principal(name, Role.OWNER)


# ---------- 查询 ----------


def test_status_shows_mode_and_halt(api: Api) -> None:
    body = api.client.get("/v1/status", headers={"Authorization": VIEWER}).json()
    assert body["mode"] == "PAPER"
    assert body["halt"] == {
        "halted": True, "reason": "人工急停", "since": T0.isoformat(), "set_by": "dylan"
    }  # fmt: skip
    assert body["changed_by"] == "dylan"


def test_status_without_any_mode_is_unknown(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    upgrade(path)
    api = make_api(path)
    body = api.client.get("/v1/status", headers={"Authorization": OWNER}).json()
    assert body == {"mode": None, "halt": None, "changed_at": None, "changed_by": None}


def test_orders_newest_first_with_latest_status(api: Api) -> None:
    body = api.client.get("/v1/orders", headers={"Authorization": VIEWER}).json()
    rows = [(o["intent_id"], o["status"]) for o in body["orders"]]
    assert rows == [("new", "RISK_APPROVED"), ("old", "SUBMITTING"), ("bare", None)]
    first = body["orders"][0]
    assert first["quantity"] == "10" and first["limit_price"] == "100.00"
    assert "account_id" not in first  # 账户号不出现在响应里


def test_orders_limit_is_bounded(api: Api) -> None:
    headers = {"Authorization": VIEWER}
    assert len(api.client.get("/v1/orders?limit=1", headers=headers).json()["orders"]) == 1
    for bad in ("0", "501", "x"):
        assert api.client.get(f"/v1/orders?limit={bad}", headers=headers).status_code == 422


def test_order_detail_has_history_and_effective_executions(ledger: Path, api: Api) -> None:
    repo = LedgerRepository(open_ledger(ledger))
    repo.add_broker_order(
        BrokerOrder(intent_id="old", broker_order_id="1", order_class=OrderClass.OPEN,
                    submitted_at=T0)
    )  # fmt: skip
    repo.add_execution(
        Execution(
            execution_id="x-1", intent_id="old", broker_order_id="1", symbol="SPY",
            side=Side.BUY, quantity=Decimal(4), price=Decimal("99.5"),
            commission=Decimal("1.00"), executed_at=T0 + timedelta(seconds=5),
        )
    )  # fmt: skip
    body = api.client.get("/v1/orders/old", headers={"Authorization": VIEWER}).json()
    assert [h["status"] for h in body["history"]] == ["RISK_APPROVED", "SUBMITTING"]
    assert body["order"]["status"] == "SUBMITTING"
    assert body["executions"][0]["price"] == "99.5"


def test_unknown_order_is_404(api: Api) -> None:
    response = api.client.get("/v1/orders/nope", headers={"Authorization": VIEWER})
    assert response.status_code == 404


def test_unreadable_ledger_is_503_not_empty(api: Api) -> None:
    api.ledger_ok[0] = False
    for path in ("/v1/status", "/v1/orders", "/v1/orders/old"):
        response = api.client.get(path, headers={"Authorization": VIEWER})
        assert response.status_code == 503, path
        assert response.json()["error"] == "账本不可读"


def test_corrupt_record_is_503(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_a: Any) -> Any:
        raise StorageError("记录损坏")

    monkeypatch.setattr(LedgerRepository, "recent_orders", broken)
    assert api.client.get("/v1/orders", headers={"Authorization": VIEWER}).status_code == 503


def test_api_ledger_connection_cannot_write(ledger: Path) -> None:
    repo = LedgerRepository(open_ledger(ledger, read_only=True))
    with pytest.raises(StorageError):
        repo.add_intent(intent("sneaky", T0))


# ---------- 命令 ----------


def test_catalog_lists_tiers_and_params(api: Api) -> None:
    body = api.client.get("/v1/commands/catalog", headers={"Authorization": VIEWER}).json()
    by_name = {c["name"]: c for c in body["commands"]}
    assert set(by_name) == set(CATALOG)
    assert by_name["emergency_flatten"]["tier"] == Tier.HIGH.value
    assert by_name["emergency_flatten"]["params"] == ["confirmation", "preview_id"]


def test_high_risk_commands_all_need_a_confirmation_phrase() -> None:
    for spec in CATALOG.values():
        assert (spec.tier is Tier.HIGH) == ("confirmation" in spec.params), spec.name


def test_no_command_touches_risk_limits() -> None:
    for name in CATALOG:
        assert not any(w in name for w in ("limit", "param", "loosen", "live")), name


def test_owner_command_is_forwarded_with_authenticated_actor(api: Api) -> None:
    response = command(api, STOP)
    assert response.status_code == 200
    assert response.json() == {
        "request_id": "req-0001", "ok": True, "replayed": False, "detail": {"x": 1}
    }  # fmt: skip
    assert api.gateway.calls == [("req-0001", "stop_new_orders", {"reason": "测试"}, "dylan")]


def test_viewer_cannot_send_commands(api: Api) -> None:
    assert command(api, STOP, auth=VIEWER).status_code == 403
    assert command(api, STOP, auth="Bearer nope").status_code == 401
    assert api.gateway.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {**STOP, "actor": "someone"},  # 自报操作者
        {**STOP, "request_id": "short"},
        {**STOP, "request_id": "has space 123"},
        {**STOP, "request_id": "x" * 129},
        {**STOP, "name": "self_destruct"},
        {**STOP, "params": {}},
        {**STOP, "params": {"reason": "x", "extra": "y"}},
        {**STOP, "params": {"reason": "   "}},
        {**STOP, "params": {"reason": 1}},
        {"request_id": "req-0001", "name": "cancel_protective_stops", "params": {}},
        {"request_id": "req-0001", "name": "raise_limits", "params": {}},
        {"name": "stop_new_orders", "params": {"reason": "x"}},
    ],
)
def test_malformed_commands_are_rejected_before_tradingd(api: Api, body: dict[str, Any]) -> None:
    response = command(api, body)
    assert response.status_code == 422, response.text
    assert api.gateway.calls == []


def test_validation_errors_do_not_echo_the_request(api: Api) -> None:
    phrase = "我确认采用券商持仓-机密短语"
    body = {"request_id": "req-0001", "name": "adopt_baseline",
            "params": {"confirmation": phrase, "extra": phrase}}  # fmt: skip
    response = command(api, {**body, "unknown_field": phrase})
    assert response.status_code == 422
    assert phrase not in response.text


def test_unauthenticated_request_with_bad_body_is_401(api: Api) -> None:
    response = command(api, {"garbage": True}, auth="Bearer nope")
    assert response.status_code in {401, 422}
    assert api.gateway.calls == []


def test_decimals_never_use_exponent_notation(ledger: Path, api: Api) -> None:
    repo = LedgerRepository(open_ledger(ledger))
    data = intent("exp", T0 + timedelta(hours=1)).model_dump()
    repo.add_intent(OrderIntent.model_validate({**data, "quantity": Decimal("1E+2")}))
    body = api.client.get("/v1/orders?limit=1", headers={"Authorization": VIEWER}).json()
    assert body["orders"][0]["quantity"] == "100"


def test_not_delivered_is_503_and_safe_to_retry(api: Api) -> None:
    api.gateway.error = NotDeliveredError("down")
    response = command(api, STOP)
    assert response.status_code == 503
    assert response.json()["retry_same_request_id"] is False


def test_unknown_outcome_is_504_and_demands_same_request_id(api: Api) -> None:
    api.gateway.error = OutcomeUnknownError("timeout")
    response = command(api, STOP)
    assert response.status_code == 504
    assert response.json()["retry_same_request_id"] is True


def test_refusal_and_temporary_failure(api: Api) -> None:
    api.gateway.answer = Answer(False, False, {}, False, True, "同一 request_id 的命令内容不同")
    response = command(api, STOP)
    assert response.status_code == 409 and "内容不同" in response.json()["error"]
    api.gateway.answer = Answer(False, False, {}, False, False, "NotReadyError")
    response = command(api, STOP)
    assert response.status_code == 503 and response.json()["retry_same_request_id"] is True


def test_executed_but_refused_by_rules_is_reported_as_not_ok(api: Api) -> None:
    api.gateway.answer = Answer(True, False, {"error": "确认短语不符"}, True, False, None)
    body = command(api, STOP).json()
    assert body["ok"] is False and body["replayed"] is True
    assert body["detail"] == {"error": "确认短语不符"}


# ---------- 事件流 ----------


def stream_auth(api: Api, **kw: Any) -> Any:
    ws = api.client.websocket_connect("/v1/stream")
    return ws, {"type": "auth", "token": "viewer-token", **kw}


def test_stream_pushes_status_then_new_events(ledger: Path, api: Api) -> None:
    ctx, auth = stream_auth(api)
    with ctx as ws:
        ws.send_json(auth)
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["role"] == "viewer"
        assert ws.receive_json()["type"] == "status"
        repo = LedgerRepository(open_ledger(ledger))
        repo.append_order_event(event("new", 9, EventKind.SUBMIT_SENT), OrderStatus.SUBMITTING)
        message = ws.receive_json()
        assert message["type"] == "order_event"
        assert message["event"]["intent_id"] == "new"
        assert message["event"]["seq"] > hello["cursor"]


def test_stream_resumes_after_cursor(api: Api) -> None:
    ctx, auth = stream_auth(api, after=0)
    with ctx as ws:
        ws.send_json(auth)
        assert ws.receive_json()["cursor"] == 0
        ws.receive_json()  # status
        seqs = [ws.receive_json()["event"]["seq"] for _ in range(3)]
        assert seqs == sorted(seqs) and len(set(seqs)) == 3


@pytest.mark.parametrize(
    "first",
    [{"type": "auth", "token": "nope"}, {"type": "hello"}, "not json",
     {"type": "auth", "token": "viewer-token", "extra": 1}],
)  # fmt: skip
def test_stream_refuses_without_valid_auth(api: Api, first: Any) -> None:
    with api.client.websocket_connect("/v1/stream") as ws:
        if isinstance(first, str):
            ws.send_text(first)
        else:
            ws.send_json(first)
        assert ws.receive_json()["type"] == "error"
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code in {4400, 4401}


def test_stream_auth_timeout(api: Api) -> None:
    with api.client.websocket_connect("/v1/stream") as ws:
        assert ws.receive_json() == {"type": "error", "error": "未在限定时间内认证"}
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 4401


def test_revoked_token_closes_the_stream(api: Api) -> None:
    ctx, auth = stream_auth(api)
    with ctx as ws:
        ws.send_json(auth)
        ws.receive_json()
        ws.receive_json()
        del api.tokens.valid["viewer-token"]
        assert ws.receive_json() == {"type": "error", "error": "令牌已失效"}
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 4401


def test_cursor_beyond_the_ledger_is_refused(api: Api) -> None:
    ctx, auth = stream_auth(api, after=10_000)
    with ctx as ws:
        ws.send_json(auth)
        assert ws.receive_json()["type"] == "error"
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 4400


def test_backlog_larger_than_a_batch_is_streamed_without_gaps(
    ledger: Path, api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    import server.api.app as app_module  # noqa: PLC0415

    monkeypatch.setattr(app_module, "STREAM_BATCH", 2)
    ctx, auth = stream_auth(api, after=0)
    with ctx as ws:
        ws.send_json(auth)
        ws.receive_json()  # hello
        ws.receive_json()  # status
        seqs = [ws.receive_json()["event"]["seq"] for _ in range(3)]
    assert seqs == sorted(set(seqs)) and len(seqs) == 3


def test_ledger_unavailable_at_connect_closes_the_stream(api: Api) -> None:
    api.ledger_ok[0] = False
    ctx, auth = stream_auth(api)
    with ctx as ws:
        ws.send_json(auth)
        assert ws.receive_json() == {"type": "error", "error": "账本不可读"}
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 1011


def test_ledger_error_mid_stream_closes_with_error(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, auth = stream_auth(api)
    with ctx as ws:
        ws.send_json(auth)
        ws.receive_json()
        ws.receive_json()

        def broken(*_a: Any) -> Any:
            raise StorageError("记录损坏")

        monkeypatch.setattr(LedgerRepository, "order_events_after", broken)
        assert ws.receive_json() == {"type": "error", "error": "账本不可读"}
