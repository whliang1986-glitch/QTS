"""S-01b：进程间协议（分帧、按对端身份分发）与意图待发队列（outbox）。真实交易核心 + FakeBroker。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import sqlite3
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from server.ipc.outbox import (
    EXPIRED,
    Outbox,
    OutboxConflictError,
    OutboxError,
    Receipt,
    deliver_pending,
)
from server.ipc.protocol import (
    MAX_FRAME,
    IpcClient,
    IpcServer,
    Peer,
    ProtocolError,
    Role,
    read_frame,
    write_frame,
)
from server.risk.core import IntakeError

from tests.integration.risk.test_core import Rig, reduce_intent
from tests.support.oms import Env

WORKERD = Peer(Role.WORKERD, "test-workerd")
APID = Peer(Role.APID, "test-apid")
AGE = timedelta(minutes=5)


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> Env:
    e = Env(tmp_path)
    await e.broker.connect()
    await e.pump()
    return e


_WRITERS: list[asyncio.StreamWriter] = []


@pytest_asyncio.fixture(autouse=True)
async def close_streams() -> AsyncIterator[None]:
    yield
    while _WRITERS:
        writer = _WRITERS.pop()
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


async def stream_pair() -> tuple[
    tuple[asyncio.StreamReader, asyncio.StreamWriter],
    tuple[asyncio.StreamReader, asyncio.StreamWriter],
]:
    left, right = socket.socketpair()
    a = await asyncio.open_connection(sock=left)
    b = await asyncio.open_connection(sock=right)
    _WRITERS.extend((a[1], b[1]))
    return a, b


class Link:
    """一条接到 IpcServer 的连接（对端身份由测试指定，相当于传输层已核验）。"""

    def __init__(self, server: IpcServer, peer: Peer) -> None:
        self.server = server
        self.peer = peer

    async def __aenter__(self) -> IpcClient:
        (cr, cw), (sr, sw) = await stream_pair()
        self.task = asyncio.create_task(self.server.serve(sr, sw, self.peer))
        self.client = IpcClient(cr, cw)
        return self.client

    async def __aexit__(self, *exc: object) -> None:
        await self.client.close()
        await asyncio.wait_for(self.task, 5)


@pytest_asyncio.fixture
async def rig(env: Env) -> AsyncIterator[Rig]:
    r = Rig(env)
    await r.bring_up()
    yield r


def outbox(env: Env, tmp_path: Path, name: str = "meta.db") -> Outbox:
    return Outbox(tmp_path / name, clock=env.clock)


# ---------- 分帧 ----------


async def raw(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def frame(body: bytes) -> bytes:
    return len(body).to_bytes(4, "big") + body


@pytest.mark.asyncio
async def test_frames_round_trip() -> None:
    (_, cw), (sr, _) = await stream_pair()
    await write_frame(cw, {"type": "intent", "x": "中文"})
    assert await read_frame(sr) == {"type": "intent", "x": "中文"}
    cw.close()
    assert await read_frame(sr) is None  # 对端正常关闭


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"\x00\x00", "帧头被截断"),
        ((0).to_bytes(4, "big"), "帧长度不合法"),
        ((MAX_FRAME + 1).to_bytes(4, "big"), "帧长度不合法"),
        ((10).to_bytes(4, "big") + b"{}", "帧体被截断"),
        (frame(b"\xff\xfe"), "UTF-8 JSON"),
        (frame(b"{not json"), "UTF-8 JSON"),
        (frame(b"[1, 2]"), "JSON 对象"),
    ],
)
async def test_bad_frames_are_protocol_errors(data: bytes, reason: str) -> None:
    with pytest.raises(ProtocolError, match=reason):
        await read_frame(await raw(data))


@pytest.mark.asyncio
async def test_largest_frame_is_accepted_and_oversize_is_not_sent() -> None:
    body = json.dumps({"p": "x" * (MAX_FRAME - 9)}).encode()
    assert len(body) == MAX_FRAME
    assert await read_frame(await raw(frame(body))) == {"p": "x" * (MAX_FRAME - 9)}
    (_, cw), _ = await stream_pair()
    with pytest.raises(ProtocolError, match="超过帧上限"):
        await write_frame(cw, {"p": "x" * MAX_FRAME})


@pytest.mark.asyncio
async def test_server_disconnects_on_a_bad_frame(rig: Rig) -> None:
    (cr, cw), (sr, sw) = await stream_pair()
    task = asyncio.create_task(IpcServer(rig.core).serve(sr, sw, WORKERD))
    cw.write(frame(b"[]"))
    await cw.drain()
    await asyncio.wait_for(task, 5)
    assert await cr.read() == b""  # 没有回答，直接断开


# ---------- 按对端身份分发 ----------


@pytest.mark.asyncio
async def test_workerd_intent_reaches_the_core(env: Env, rig: Rig) -> None:
    async with Link(IpcServer(rig.core), WORKERD) as client:
        answer = await client.submit_intent(reduce_intent(env))
    assert answer["ok"] is True, answer
    assert answer["result"]["intent_id"] == "r-1"
    assert answer["result"]["verdict"] == "ALLOW"
    assert answer["result"]["status"] is not None
    assert len(rig.env.repo.risk_decisions_for("r-1")) == 1


@pytest.mark.asyncio
async def test_roles_cannot_cross(env: Env, rig: Rig) -> None:
    server = IpcServer(rig.core)
    async with Link(server, APID) as client:
        answer = await client.submit_intent(reduce_intent(env))
    assert answer == {"ok": False, "error": "apid 不能发送 'intent' 请求", "refused": True}
    async with Link(server, WORKERD) as client:
        answer = await client.command("c-1", "stop_new_orders", {"reason": "x"}, actor="dylan")
    assert answer["refused"] is True and "不能发送 'command'" in answer["error"]
    assert rig.env.repo.risk_decisions_for("r-1") == []
    state = rig.modes.current()
    assert state is not None and not state.halt.halted  # 越权命令没有执行


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [{}, {"type": "hello"}, {"type": None}])
async def test_unknown_request_types_are_refused(rig: Rig, message: dict[str, Any]) -> None:
    for peer in (WORKERD, APID):
        answer = await IpcServer(rig.core).dispatch(message, peer)
        assert answer["ok"] is False and answer["refused"] is True


@pytest.mark.asyncio
async def test_apid_command_reaches_the_core(rig: Rig) -> None:
    async with Link(IpcServer(rig.core), APID) as client:
        answer = await client.command("c-1", "stop_new_orders", {"reason": "演练"}, actor="dylan")
        again = await client.command("c-1", "stop_new_orders", {"reason": "演练"}, actor="dylan")
    assert answer["ok"] is True and answer["result"]["ok"] is True
    assert answer["result"]["replayed"] is False
    assert again["result"]["replayed"] is True
    state = rig.modes.current()
    assert state is not None and state.halt.halted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        {"type": "intent"},
        {"type": "intent", "intent": 3},
        {"type": "intent", "intent": "{}"},
        {"type": "intent", "intent": "not json"},
    ],
)
async def test_malformed_intents_are_refused(rig: Rig, message: dict[str, Any]) -> None:
    answer = await IpcServer(rig.core).dispatch(message, WORKERD)
    assert answer["ok"] is False and answer["refused"] is True


@pytest.mark.asyncio
async def test_malformed_commands_are_refused_by_the_core(rig: Rig) -> None:
    server = IpcServer(rig.core)
    for message in (
        {"type": "command"},
        {"type": "command", "request_id": "c", "name": "reconcile", "params": [], "actor": "d"},
        {"type": "command", "request_id": 1, "name": "reconcile", "params": {}, "actor": "d"},
        {"type": "command", "request_id": "c", "name": "nope", "params": {}, "actor": "d"},
    ):
        answer = await server.dispatch(message, APID)
        assert answer["ok"] is False and answer["refused"] is True, message


@pytest.mark.asyncio
async def test_refusals_carry_the_reason(env: Env, rig: Rig) -> None:
    foreign = reduce_intent(env, account_id="DU0000009")
    answer = await IpcServer(rig.core).dispatch(
        {"type": "intent", "intent": foreign.model_dump_json()}, WORKERD
    )
    assert answer == {"ok": False, "error": "意图账户不是本进程已校验的账户", "refused": True}


@pytest.mark.asyncio
async def test_not_ready_is_transient_not_a_refusal(env: Env) -> None:
    rig = Rig(env)  # 还没有 start()
    answer = await IpcServer(rig.core).dispatch(
        {"type": "intent", "intent": reduce_intent(env).model_dump_json()}, WORKERD
    )
    assert answer == {"ok": False, "error": "NotReadyError", "refused": False}


class Broken:
    async def submit_intent(self, intent: Any) -> Any:
        raise RuntimeError("内部细节不外泄")

    async def command(self, *_a: Any, **_kw: Any) -> Any:
        raise IntakeError("命令不合规")


@pytest.mark.asyncio
async def test_internal_errors_are_reported_by_type_only(env: Env) -> None:
    server = IpcServer(Broken())
    answer = await server.dispatch(
        {"type": "intent", "intent": reduce_intent(env).model_dump_json()}, WORKERD
    )
    assert answer == {"ok": False, "error": "RuntimeError", "refused": False}
    answer = await server.dispatch(
        {"type": "command", "request_id": "c", "name": "x", "params": {}, "actor": "d"}, APID
    )
    assert answer == {"ok": False, "error": "命令不合规", "refused": True}


@pytest.mark.asyncio
async def test_one_connection_serves_many_requests_in_order(env: Env, rig: Rig) -> None:
    async with Link(IpcServer(rig.core), WORKERD) as client:
        answers = await asyncio.gather(
            *(client.submit_intent(reduce_intent(env, f"r-{i}", quantity=1)) for i in range(4))
        )
    assert [a["result"]["intent_id"] for a in answers] == [f"r-{i}" for i in range(4)]


@pytest.mark.asyncio
async def test_client_raises_when_tradingd_hangs_up() -> None:
    (cr, cw), (_, sw) = await stream_pair()
    sw.close()
    with pytest.raises((ConnectionError, ProtocolError)):
        await IpcClient(cr, cw).request({"type": "intent"})


# ---------- outbox ----------


def test_enqueue_is_idempotent_and_ordered(env: Env, tmp_path: Path) -> None:
    box = outbox(env, tmp_path)
    a, b = reduce_intent(env, "a"), reduce_intent(env, "b")
    assert box.enqueue(b) and box.enqueue(a)
    assert not box.enqueue(b)
    assert [i.intent_id for i in box.pending()] == ["b", "a"]
    assert box.pending()[0] == b


def test_same_intent_id_with_other_content_conflicts(env: Env, tmp_path: Path) -> None:
    box = outbox(env, tmp_path)
    box.enqueue(reduce_intent(env, "a"))
    with pytest.raises(OutboxConflictError):
        box.enqueue(reduce_intent(env, "a", quantity=2))
    assert len(box.pending()) == 1


def test_outbox_survives_restart(env: Env, tmp_path: Path) -> None:
    box = outbox(env, tmp_path)
    box.enqueue(reduce_intent(env, "a"))
    box.enqueue(reduce_intent(env, "b"))
    box.record_receipt(Receipt("a", "ALLOW", "SUBMITTED", False, None))
    box.close()
    again = outbox(env, tmp_path)
    assert [i.intent_id for i in again.pending()] == ["b"]
    assert again.receipt("a") == Receipt("a", "ALLOW", "SUBMITTED", False, None)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE outbox_intents SET payload = '{}'",
        "DELETE FROM outbox_intents",
        "UPDATE outbox_receipts SET verdict = 'ALLOW'",
        "DELETE FROM outbox_receipts",
    ],
)
def test_outbox_is_append_only(env: Env, tmp_path: Path, sql: str) -> None:
    box = outbox(env, tmp_path)
    box.enqueue(reduce_intent(env, "a"))
    box.record_receipt(Receipt("a", "REJECT", None, False, None))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        box._conn.execute(sql)


def test_receipts_are_written_once(env: Env, tmp_path: Path) -> None:
    box = outbox(env, tmp_path)
    box.enqueue(reduce_intent(env, "a"))
    first = Receipt("a", "ALLOW", "SUBMITTED", False, None)
    box.record_receipt(first)
    box.record_receipt(first)  # 同样的回执：幂等
    with pytest.raises(OutboxConflictError):
        box.record_receipt(Receipt("a", "REJECT", None, False, None))
    with pytest.raises(OutboxError):
        box.record_receipt(Receipt("ghost", "ALLOW", None, False, None))  # 没入队的意图


def test_unknown_schema_or_missing_triggers_are_refused(env: Env, tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 7")
    conn.close()
    with pytest.raises(OutboxError, match="结构版本 7"):
        Outbox(path, clock=env.clock)
    box = outbox(env, tmp_path, "tampered.db")
    box._conn.execute("DROP TRIGGER outbox_receipts_no_delete")
    box.close()
    with pytest.raises(OutboxError, match="触发器缺失"):
        outbox(env, tmp_path, "tampered.db")


def test_unopenable_outbox_fails_closed(env: Env, tmp_path: Path) -> None:
    with pytest.raises(OutboxError):
        Outbox(tmp_path / "no-such-dir" / "meta.db", clock=env.clock)


# ---------- 投递 ----------


class Channel:
    """可编排回答的通道：answers 用完后转给真实客户端（若有）。"""

    def __init__(self, answers: list[Any], client: IpcClient | None = None) -> None:
        self.answers = answers
        self.client = client
        self.seen: list[str] = []

    async def submit_intent(self, intent: Any) -> dict[str, Any]:
        self.seen.append(intent.intent_id)
        if self.answers:
            answer = self.answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return dict(answer)
        assert self.client is not None
        return await self.client.submit_intent(intent)


@pytest.mark.asyncio
async def test_delivery_end_to_end_records_receipts(env: Env, rig: Rig, tmp_path: Path) -> None:
    box = outbox(env, tmp_path)
    box.enqueue(reduce_intent(env, "a", quantity=1))
    box.enqueue(reduce_intent(env, "b", quantity=1))
    async with Link(IpcServer(rig.core), WORKERD) as client:
        report = await deliver_pending(box, client, max_age=AGE)
    assert (report.delivered, report.refused, report.expired, report.remaining) == (2, 0, 0, 0)
    assert box.pending() == []
    receipt = box.receipt("a")
    assert receipt is not None and receipt.verdict == "ALLOW" and not receipt.refused
    assert receipt.status is not None


@pytest.mark.asyncio
async def test_lost_answer_redelivery_does_not_place_twice(
    env: Env, rig: Rig, tmp_path: Path
) -> None:
    """tradingd 处理了意图但回答丢了：下次重投同一 intent_id，风控返回原决定，不重复下单。"""
    box = outbox(env, tmp_path)
    intent = reduce_intent(env, "a", quantity=1)
    box.enqueue(intent)
    await rig.core.submit_intent(intent)  # 已到达 tradingd，回执没写下
    before = len(await env.broker.open_orders())
    async with Link(IpcServer(rig.core), WORKERD) as client:
        report = await deliver_pending(box, client, max_age=AGE)
    assert report.delivered == 1
    assert len(await env.broker.open_orders()) == before
    assert len(env.repo.risk_decisions_for("a")) == 1


@pytest.mark.asyncio
async def test_transient_failures_stop_delivery_in_order(env: Env, tmp_path: Path) -> None:
    box = outbox(env, tmp_path)
    for name in ("a", "b", "c"):
        box.enqueue(reduce_intent(env, name))
    ok = {"ok": True, "result": {"intent_id": "a", "verdict": "ALLOW", "status": "SUBMITTED"}}
    for failure in (
        ConnectionError("断开"),
        ProtocolError("坏帧"),
        {"ok": False, "error": "NotReadyError", "refused": False},
        {"ok": False, "error": "RuntimeError", "refused": False},
        {"ok": False},
    ):
        channel = Channel([ok, failure])
        report = await deliver_pending(box, channel, max_age=AGE)
        assert report.remaining == 2, failure
        assert channel.seen[-1] == "b"
        assert [i.intent_id for i in box.pending()] == ["b", "c"]
        ok = {}  # a 已有回执，之后不再投 a
        channel.answers.clear()


@pytest.mark.asyncio
async def test_refusals_are_receipted_and_not_retried(env: Env, tmp_path: Path) -> None:
    box = outbox(env, tmp_path)
    box.enqueue(reduce_intent(env, "a"))
    box.enqueue(reduce_intent(env, "b"))
    reason = "意图账户不是本进程已校验的账户"
    refusal = {"ok": False, "error": reason, "refused": True}
    ok = {"ok": True, "result": {"intent_id": "b", "verdict": "REJECT", "status": None}}
    report = await deliver_pending(box, Channel([refusal, ok]), max_age=AGE)
    assert (report.delivered, report.refused, report.remaining) == (1, 1, 0)
    assert box.receipt("a") == Receipt("a", None, None, True, reason)
    assert box.receipt("b") == Receipt("b", "REJECT", None, False, None)
    assert (await deliver_pending(box, Channel([]), max_age=AGE)).delivered == 0


@pytest.mark.asyncio
async def test_stale_intents_expire_instead_of_being_sent(env: Env, tmp_path: Path) -> None:
    box = outbox(env, tmp_path)
    box.enqueue(reduce_intent(env, "old"))
    box.enqueue(reduce_intent(env, "future", created_at=env.clock() + AGE * 2))
    env.clock.advance(AGE.total_seconds() + 1)
    box.enqueue(reduce_intent(env, "fresh", created_at=env.clock()))
    box.enqueue(reduce_intent(env, "edge", created_at=env.clock() - AGE))
    ok = {"ok": True, "result": {"intent_id": "x", "verdict": "ALLOW", "status": None}}
    channel = Channel([ok, ok, ok])
    report = await deliver_pending(box, channel, max_age=AGE)
    assert channel.seen == ["future", "fresh", "edge"]  # future 只超前 AGE - 1 秒，不算离谱
    assert (report.delivered, report.expired) == (3, 1)
    receipt = box.receipt("old")
    assert receipt == Receipt("old", None, None, True, EXPIRED)


@pytest.mark.asyncio
async def test_far_future_intents_expire(env: Env, tmp_path: Path) -> None:
    box = outbox(env, tmp_path)
    box.enqueue(reduce_intent(env, "f", created_at=env.clock() + AGE + timedelta(seconds=1)))
    channel = Channel([])
    report = await deliver_pending(box, channel, max_age=AGE)
    assert channel.seen == [] and report.expired == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("age", [timedelta(0), timedelta(seconds=-1)])
async def test_max_age_must_be_positive(env: Env, tmp_path: Path, age: timedelta) -> None:
    with pytest.raises(ValueError, match="max_age"):
        await deliver_pending(outbox(env, tmp_path), Channel([]), max_age=age)


@pytest.mark.asyncio
async def test_tradingd_restart_keeps_intents_pending(env: Env, tmp_path: Path) -> None:
    """tradingd 还没启动完：意图留在队列；启动完成后再投即成功。"""
    box = outbox(env, tmp_path)
    box.enqueue(reduce_intent(env, "a", quantity=1))
    rig = Rig(env)
    server = IpcServer(rig.core)
    async with Link(server, WORKERD) as client:
        report = await deliver_pending(box, client, max_age=AGE)
        assert report.remaining == 1 and box.receipt("a") is None
        await rig.bring_up()
        report = await deliver_pending(box, client, max_age=AGE)
    assert report.delivered == 1
    receipt = box.receipt("a")
    assert receipt is not None and receipt.verdict == "ALLOW"


def test_peer_roles_are_the_only_two() -> None:
    assert {r.value for r in Role} == {"workerd", "apid"}
