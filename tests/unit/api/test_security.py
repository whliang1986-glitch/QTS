"""二次确认、认证失败限流、高危命令流程（S-03）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from server.api.app import ApiDeps, create_app
from server.api.auth import Principal, Role
from server.api.commands import Answer
from server.api.security import ConfirmationBook, ConfirmationError, FailureLimiter, MemoryAudit

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
DYLAN = Principal("dylan", Role.OWNER)
OTHER = Principal("other", Role.OWNER)
PARAMS = {"confirmation": "短语"}


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


# ---------- ConfirmationBook ----------


def test_confirmation_binds_user_command_params_and_request() -> None:
    clock = Clock()
    book = ConfirmationBook(clock=clock)
    cid, expires = book.issue(DYLAN, "adopt_baseline", PARAMS)
    assert expires == T0 + timedelta(seconds=60)
    with pytest.raises(ConfirmationError, match="不符"):
        book.use(cid, OTHER, "adopt_baseline", PARAMS, "req-1")
    with pytest.raises(ConfirmationError, match="不符"):
        book.use(cid, DYLAN, "emergency_flatten", PARAMS, "req-1")
    with pytest.raises(ConfirmationError, match="不符"):
        book.use(cid, DYLAN, "adopt_baseline", {"confirmation": "别的"}, "req-1")
    book.use(cid, DYLAN, "adopt_baseline", PARAMS, "req-1")
    book.use(cid, DYLAN, "adopt_baseline", PARAMS, "req-1")  # 同号重试
    with pytest.raises(ConfirmationError, match="另一个 request_id"):
        book.use(cid, DYLAN, "adopt_baseline", PARAMS, "req-2")


def test_confirmation_expires() -> None:
    clock = Clock()
    book = ConfirmationBook(clock=clock)
    cid, _ = book.issue(DYLAN, "adopt_baseline", PARAMS)
    clock.now += timedelta(seconds=60)
    with pytest.raises(ConfirmationError, match="过期"):
        book.use(cid, DYLAN, "adopt_baseline", PARAMS, "req-1")
    with pytest.raises(ConfirmationError, match="不存在"):
        book.use("nope", DYLAN, "adopt_baseline", PARAMS, "req-1")


def test_open_confirmations_are_capped() -> None:
    book = ConfirmationBook(clock=Clock(), max_open=2)
    book.issue(DYLAN, "a", {})
    book.issue(DYLAN, "b", {})
    with pytest.raises(ConfirmationError, match="太多"):
        book.issue(DYLAN, "c", {})


# ---------- FailureLimiter ----------


def test_limiter_blocks_after_too_many_failures_then_recovers() -> None:
    clock = Clock()
    limiter = FailureLimiter(max_failures=3, clock=clock)
    assert not limiter.fail("1.2.3.4")
    assert not limiter.fail("1.2.3.4")
    assert limiter.fail("1.2.3.4")
    assert limiter.blocked("1.2.3.4")
    assert not limiter.blocked("5.6.7.8")  # 按来源分开
    clock.now += timedelta(seconds=61)
    assert not limiter.blocked("1.2.3.4")


def test_old_failures_fall_out_of_the_window() -> None:
    clock = Clock()
    limiter = FailureLimiter(max_failures=3, clock=clock)
    limiter.fail("a")
    limiter.fail("a")
    clock.now += timedelta(seconds=61)
    assert not limiter.fail("a")
    assert not limiter.blocked("a")


# ---------- 通过 HTTP 的高危命令流程 ----------


class Tokens:
    def authenticate(self, token: str) -> Principal | None:
        return {"owner": DYLAN, "viewer": Principal("guest", Role.VIEWER)}.get(token)


class Gateway:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def send(self, request_id: str, name: str, params: Any, *, actor: str) -> Answer:
        self.calls.append(request_id)
        return Answer(True, True, {}, False, False, None)


class BrokenAudit:
    def audit(self, action: str, actor: str, detail: Any) -> None:
        raise OSError("磁盘满")


def make(**kw: Any) -> tuple[TestClient, Gateway, MemoryAudit]:
    gateway, audit = Gateway(), kw.pop("audit", MemoryAudit())
    deps = ApiDeps(authenticator=Tokens(), ledger=lambda: None,  # type: ignore[arg-type,return-value]
                   gateway=gateway, audit=audit, **kw)  # fmt: skip
    return TestClient(create_app(deps)), gateway, audit


OWNER = {"Authorization": "Bearer owner"}
HIGH = {"request_id": "req-high-1", "name": "adopt_baseline", "params": PARAMS}


def confirm(client: TestClient, body: dict[str, Any] = HIGH) -> str:
    ask = {"name": body["name"], "params": body["params"]}
    response = client.post("/v1/commands/confirmations", json=ask, headers=OWNER)
    assert response.status_code == 200, response.text
    return str(response.json()["confirmation_id"])


def test_high_risk_command_without_confirmation_is_not_sent() -> None:
    client, gateway, _ = make()
    assert client.post("/v1/commands", json=HIGH, headers=OWNER).status_code == 403
    assert gateway.calls == []


def test_high_risk_command_with_confirmation_and_same_id_retry() -> None:
    client, gateway, audit = make()
    body = {**HIGH, "confirmation_id": confirm(client)}
    assert client.post("/v1/commands", json=body, headers=OWNER).status_code == 200
    assert client.post("/v1/commands", json=body, headers=OWNER).status_code == 200
    other = {**body, "request_id": "req-high-2"}
    assert client.post("/v1/commands", json=other, headers=OWNER).status_code == 403
    assert gateway.calls == ["req-high-1", "req-high-1"]
    actions = [a for a, _, _ in audit.records]
    assert "confirmation_issued" in actions and "confirmation_refused" in actions
    assert all("短语" not in str(d) for a, _, d in audit.records if a.startswith("command"))


def test_confirmation_only_for_high_risk_and_owner() -> None:
    client, _, _ = make()
    ask = {"name": "reconcile", "params": {}}
    assert client.post("/v1/commands/confirmations", json=ask, headers=OWNER).status_code == 422
    viewer = {"Authorization": "Bearer viewer"}
    ask = {"name": "adopt_baseline", "params": PARAMS}
    assert client.post("/v1/commands/confirmations", json=ask, headers=viewer).status_code == 403
    body = {"request_id": "req-ctl-1", "name": "reconcile", "params": {}, "confirmation_id": "x"}
    assert client.post("/v1/commands", json=body, headers=OWNER).status_code == 422


def test_audit_failure_blocks_the_command() -> None:
    client, gateway, _ = make(audit=BrokenAudit())
    body = {"request_id": "req-ctl-1", "name": "reconcile", "params": {}}
    response = client.post("/v1/commands", json=body, headers=OWNER)
    assert response.status_code == 503 and "审计" in response.json()["error"]
    assert gateway.calls == []


def test_repeated_auth_failures_are_throttled() -> None:
    client, _, audit = make(limiter=FailureLimiter(max_failures=3))
    bad = {"Authorization": "Bearer nope"}
    codes = [client.get("/v1/whoami", headers=bad).status_code for _ in range(4)]
    assert codes == [401, 401, 401, 429]
    assert client.get("/v1/whoami", headers=OWNER).status_code == 429  # 封禁期内有效令牌也不放
    assert any(a == "auth_throttled" for a, _, _ in audit.records)


def test_websocket_auth_is_throttled_too() -> None:
    client, _, _ = make(limiter=FailureLimiter(max_failures=1), auth_timeout=1.0)
    with client.websocket_connect("/v1/stream") as ws:
        ws.send_json({"type": "auth", "token": "nope"})
        assert ws.receive_json()["type"] == "error"
    with client.websocket_connect("/v1/stream") as ws:
        ws.send_json({"type": "auth", "token": "owner"})
        assert "过多" in ws.receive_json()["error"]
