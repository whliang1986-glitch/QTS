"""命令转发（S-02）：没发出 / 结果未知的区分、回答的严格解析、每条命令一条连接。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from server.api.commands import (
    CommandError,
    IpcCommandGateway,
    NotDeliveredError,
    OutcomeUnknownError,
    check_command,
)


class Client:
    def __init__(self, answer: Any = None, *, delay: float = 0, error: Exception | None = None):
        self.answer = answer
        self.delay = delay
        self.error = error
        self.sent: list[tuple[str, str, dict[str, str], str]] = []
        self.closed = False

    async def command(
        self, request_id: str, name: str, params: Mapping[str, str], *, actor: str
    ) -> Any:
        self.sent.append((request_id, name, dict(params), actor))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.answer

    async def close(self) -> None:
        self.closed = True


def gateway(client: Client, **kw: Any) -> IpcCommandGateway:
    async def connect() -> Client:
        return client

    return IpcCommandGateway(connect, **kw)


OK = {"ok": True, "result": {"ok": True, "detail": {"a": 1}, "replayed": False}}


@pytest.mark.asyncio
async def test_executed_answer_and_connection_closed() -> None:
    client = Client(OK)
    answer = await gateway(client).send("req-00001", "reconcile", {}, actor="dylan")
    assert answer.executed and answer.ok and answer.detail == {"a": 1}
    assert client.sent == [("req-00001", "reconcile", {}, "dylan")]
    assert client.closed


@pytest.mark.asyncio
async def test_refusal_and_temporary_answers() -> None:
    refused = {"ok": False, "error": "不合规", "refused": True}
    answer = await gateway(Client(refused)).send("req-00001", "reconcile", {}, actor="d")
    assert not answer.executed and answer.refused and answer.error == "不合规"
    temporary = {"ok": False, "error": "NotReadyError", "refused": False}
    answer = await gateway(Client(temporary)).send("req-00001", "reconcile", {}, actor="d")
    assert not answer.executed and not answer.refused


@pytest.mark.asyncio
async def test_connect_failure_is_not_delivered() -> None:
    async def connect() -> Client:
        raise ConnectionRefusedError("没有 tradingd")

    with pytest.raises(NotDeliveredError):
        await IpcCommandGateway(connect).send("req-00001", "reconcile", {}, actor="d")


@pytest.mark.asyncio
async def test_connect_timeout_is_not_delivered() -> None:
    async def connect() -> Client:
        await asyncio.sleep(5)
        return Client(OK)

    with pytest.raises(NotDeliveredError):
        await IpcCommandGateway(connect, connect_timeout=0.05).send(
            "req-00001", "reconcile", {}, actor="d"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client",
    [
        Client(OK, delay=5),  # 超时
        Client(error=ConnectionResetError("断开")),
        Client({"ok": "yes"}),
        Client({"ok": True, "result": {"ok": True, "detail": [], "replayed": False}}),
        Client({"ok": True}),
        Client({"ok": False, "error": 3, "refused": True}),
        Client("not a dict"),
    ],
)
async def test_sent_but_no_valid_answer_is_unknown(client: Client) -> None:
    with pytest.raises(OutcomeUnknownError):
        await gateway(client, timeout=0.05).send("req-00001", "reconcile", {}, actor="d")
    assert client.sent and client.closed


def test_timeouts_must_be_positive() -> None:
    async def connect() -> Client:
        return Client(OK)

    with pytest.raises(ValueError, match="超时"):
        IpcCommandGateway(connect, timeout=0)


@pytest.mark.parametrize(
    ("name", "params"),
    [("nope", {}), ("resume", {"x": "1"}), ("emergency_flatten", {"confirmation": "x"}),
     ("adopt_baseline", {"confirmation": ""})],
)  # fmt: skip
def test_check_command_refuses(name: str, params: dict[str, str]) -> None:
    with pytest.raises(CommandError):
        check_command(name, params)
