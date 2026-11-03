"""apid 端到端（S-02）：HTTP → apid → 本机通道 → tradingd 交易核心（FakeBroker）→ 账本 → 只读查询。

FakeBroker 结果不等于真实 IBKR 联调（CLAUDE.md）；这里验证的是命令幂等与结果语义。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from server.api.app import ApiDeps, create_app
from server.api.auth import Principal, Role
from server.api.commands import CommandClient, IpcCommandGateway
from server.api.security import MemoryAudit
from server.ipc.protocol import IpcServer
from server.ipc.protocol import Role as IpcRole
from server.ipc.transport import Endpoints, connect_role, serve_role
from server.platforms import Listener, PlatformNotImplementedError, local_channel
from server.storage.ledger import LedgerRepository, open_ledger

from tests.integration.risk.test_core import Rig
from tests.support.oms import Env


def _channel() -> Any:
    try:
        channel = local_channel()
        channel.current_account()
    except PlatformNotImplementedError:
        return None
    return channel


CHANNEL = _channel()
pytestmark = pytest.mark.skipif(CHANNEL is None, reason="当前平台的通道尚未实现")
TEST_TOKEN = "owner-token"  # noqa: S105 - 测试用假令牌
OWNER = {"Authorization": f"Bearer {TEST_TOKEN}"}


class Tokens:
    def authenticate(self, token: str) -> Principal | None:
        return Principal("dylan", Role.OWNER) if token == TEST_TOKEN else None


class LosesAnswer:
    """命令真的发到了 tradingd 并执行，但回答在路上丢了（模拟断线 / 超时）。"""

    def __init__(self, inner: CommandClient) -> None:
        self._inner = inner

    async def command(
        self, request_id: str, name: str, params: Mapping[str, str], *, actor: str
    ) -> dict[str, Any]:
        await self._inner.command(request_id, name, params, actor=actor)
        raise ConnectionError("回答丢失")

    async def close(self) -> None:
        await self._inner.close()


@dataclass
class Stack:
    env: Env
    rig: Rig
    client: httpx.AsyncClient
    endpoints: Endpoints
    me: str
    lose_answers: list[bool]


@pytest_asyncio.fixture
async def stack(tmp_path: Path) -> AsyncIterator[Stack]:
    env = Env(tmp_path)
    await env.broker.connect()
    await env.pump()
    rig = Rig(env)
    await rig.bring_up()
    me = CHANNEL.current_account()
    endpoints = Endpoints(CHANNEL, tmp_path, instance=f"api-{uuid.uuid4().hex[:10]}")
    listener: Listener = await serve_role(endpoints, IpcRole.APID, {me}, IpcServer(rig.core))
    lose = [False]

    async def connect() -> CommandClient:
        client = await connect_role(endpoints, IpcRole.APID, server_account=me)
        return LosesAnswer(client) if lose[0] else client

    ledger = tmp_path / "ledger.db"
    deps = ApiDeps(
        authenticator=Tokens(),
        audit=MemoryAudit(),
        ledger=lambda: LedgerRepository(open_ledger(ledger, read_only=True)),
        gateway=IpcCommandGateway(connect, timeout=10),
    )
    transport = httpx.ASGITransport(app=create_app(deps))
    async with httpx.AsyncClient(transport=transport, base_url="http://apid") as client:
        yield Stack(env, rig, client, endpoints, me, lose)
    listener.close()
    await listener.wait_closed()


def stop(request_id: str, reason: str = "演练") -> dict[str, Any]:
    return {"request_id": request_id, "name": "stop_new_orders", "params": {"reason": reason}}


@pytest.mark.asyncio
async def test_command_is_executed_once_and_retries_replay(stack: Stack) -> None:
    halts_before = len(stack.env.repo.audits("halt"))
    first = await stack.client.post("/v1/commands", json=stop("api-req-001"), headers=OWNER)
    assert first.status_code == 200, first.text
    assert first.json()["ok"] is True and first.json()["replayed"] is False
    again = await stack.client.post("/v1/commands", json=stop("api-req-001"), headers=OWNER)
    assert again.json()["replayed"] is True
    assert len(stack.env.repo.audits("halt")) == halts_before + 1  # 只执行了一次
    status = (await stack.client.get("/v1/status", headers=OWNER)).json()
    assert status["halt"]["halted"] is True and status["halt"]["set_by"] == "dylan"


@pytest.mark.asyncio
async def test_same_request_id_with_other_params_is_refused(stack: Stack) -> None:
    await stack.client.post("/v1/commands", json=stop("api-req-002"), headers=OWNER)
    other = await stack.client.post(
        "/v1/commands", json=stop("api-req-002", reason="另一个原因"), headers=OWNER
    )
    assert other.status_code == 409
    assert "命令内容或操作者不同" in other.json()["error"]


@pytest.mark.asyncio
async def test_lost_answer_is_504_and_same_id_retry_returns_original(stack: Stack) -> None:
    halts_before = len(stack.env.repo.audits("halt"))
    stack.lose_answers[0] = True
    lost = await stack.client.post("/v1/commands", json=stop("api-req-003"), headers=OWNER)
    assert lost.status_code == 504 and lost.json()["retry_same_request_id"] is True
    stack.lose_answers[0] = False
    retry = await stack.client.post("/v1/commands", json=stop("api-req-003"), headers=OWNER)
    assert retry.status_code == 200 and retry.json()["replayed"] is True
    assert len(stack.env.repo.audits("halt")) == halts_before + 1  # 命令只执行了一次


@pytest.mark.asyncio
async def test_rule_refusal_is_recorded_and_replayed(stack: Stack) -> None:
    body: dict[str, Any] = {"request_id": "api-req-004", "name": "adopt_baseline",
                            "params": {"confirmation": "错误的短语"}}  # fmt: skip
    ask = {"name": body["name"], "params": body["params"]}
    confirm = await stack.client.post("/v1/commands/confirmations", json=ask, headers=OWNER)
    body["confirmation_id"] = confirm.json()["confirmation_id"]
    first = (await stack.client.post("/v1/commands", json=body, headers=OWNER)).json()
    assert first["ok"] is False and "error" in first["detail"]
    again = (await stack.client.post("/v1/commands", json=body, headers=OWNER)).json()
    assert again["replayed"] is True and again["detail"] == first["detail"]


@pytest.mark.asyncio
async def test_tradingd_down_is_503_not_sent(stack: Stack, tmp_path: Path) -> None:
    missing = Endpoints(CHANNEL, tmp_path, instance=f"gone-{uuid.uuid4().hex[:10]}")

    async def connect() -> CommandClient:
        return await connect_role(missing, IpcRole.APID, server_account=stack.me)

    deps = ApiDeps(
        authenticator=Tokens(),
        audit=MemoryAudit(),
        ledger=lambda: LedgerRepository(open_ledger(tmp_path / "ledger.db", read_only=True)),
        gateway=IpcCommandGateway(connect, connect_timeout=2),
    )
    transport = httpx.ASGITransport(app=create_app(deps))
    async with httpx.AsyncClient(transport=transport, base_url="http://apid") as client:
        response = await client.post("/v1/commands", json=stop("api-req-005"), headers=OWNER)
    assert response.status_code == 503
    assert response.json()["retry_same_request_id"] is False
    assert stack.env.repo.command_result("api-req-005") is None


@pytest.mark.asyncio
async def test_orders_submitted_through_the_core_are_visible(stack: Stack) -> None:
    from tests.integration.risk.test_core import reduce_intent  # noqa: PLC0415

    outcome = await stack.rig.core.submit_intent(reduce_intent(stack.env, "api-r-1"))
    assert outcome.status is not None
    body = (await stack.client.get("/v1/orders", headers=OWNER)).json()
    row = next(o for o in body["orders"] if o["intent_id"] == "api-r-1")
    assert row["status"] == outcome.status.value
    detail = (await stack.client.get("/v1/orders/api-r-1", headers=OWNER)).json()
    assert detail["history"][-1]["status"] == outcome.status.value
