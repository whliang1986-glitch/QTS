"""S-01b：本机通道与对端身份核验。Unix 域套接字用例只在支持 SO_PEERCRED 的平台（Linux CI）运行。"""

from __future__ import annotations

import asyncio
import os
import socket
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from server.ipc.protocol import IpcServer, ProtocolError, Role
from server.ipc.transport import (
    SOCKET_MODE,
    SUPPORTED,
    PeerCredentials,
    RolePolicy,
    TransportError,
    connect_unix,
    serve_unix,
)
from server.risk.core import IntakeError

unix_only = pytest.mark.skipif(not SUPPORTED, reason="需要 SO_PEERCRED（Linux）")


class Port:
    """记录收到什么的交易核心替身。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def submit_intent(self, intent: Any) -> Any:
        self.calls.append("intent")
        raise IntakeError("替身拒绝")

    async def command(
        self, request_id: str, name: str, params: Mapping[str, str], *, actor: str
    ) -> Any:
        self.calls.append(name)
        raise IntakeError("替身拒绝")


# ---------- 身份策略（各平台都跑） ----------


def test_policy_maps_uids_to_roles() -> None:
    policy = RolePolicy({Role.WORKERD: 1001, Role.APID: 1002})
    peer = policy.identify(PeerCredentials(pid=7, uid=1001, gid=1))
    assert peer is not None and peer.role is Role.WORKERD and "pid=7" in peer.detail
    apid = policy.identify(PeerCredentials(pid=8, uid=1002, gid=1))
    assert apid is not None and apid.role is Role.APID
    assert policy.identify(PeerCredentials(pid=9, uid=0, gid=0)) is None  # root 也不例外


def test_one_account_cannot_hold_two_roles() -> None:
    with pytest.raises(TransportError, match="同一个操作系统账户"):
        RolePolicy({Role.WORKERD: 1001, Role.APID: 1001})


@pytest.mark.parametrize(
    "uids",
    [{}, {Role.WORKERD: -1}, {Role.WORKERD: True}, {Role.WORKERD: "1001"}, {"workerd": 1001}],
)
def test_bad_policies_are_refused(uids: Any) -> None:
    with pytest.raises(TransportError):
        RolePolicy(uids)


@pytest.mark.asyncio
@pytest.mark.skipif(SUPPORTED, reason="只在不支持的平台验证失败即关闭")
async def test_unsupported_platform_refuses_to_open_a_channel(tmp_path: Path) -> None:
    policy = RolePolicy({Role.WORKERD: 1})
    with pytest.raises(TransportError):
        await serve_unix(tmp_path / "t.sock", IpcServer(Port()), policy)
    with pytest.raises(TransportError):
        await connect_unix(tmp_path / "t.sock", server_uid=1)


# ---------- Unix 域套接字（Linux） ----------


def _uid() -> int:
    getuid = os.getuid  # type: ignore[attr-defined,unused-ignore]
    return int(getuid())


async def _serve(path: Path, port: Port, uids: Mapping[Role, int]) -> asyncio.Server:
    return await serve_unix(path, IpcServer(port), RolePolicy(uids))


@unix_only
@pytest.mark.asyncio
async def test_registered_peer_gets_its_role(tmp_path: Path) -> None:
    port, path = Port(), tmp_path / "t.sock"
    listener = await _serve(path, port, {Role.WORKERD: _uid()})
    try:
        assert stat.S_IMODE(path.stat().st_mode) == SOCKET_MODE
        client = await connect_unix(path, server_uid=_uid())
        answer = await client.request({"type": "intent", "intent": "{}"})
        assert answer["refused"] is True  # 到达了 workerd 的意图入口（内容不合法）
        crossed = await client.request(
            {"type": "command", "request_id": "c", "name": "x", "params": {}, "actor": "d"}
        )
        assert crossed["error"] == "workerd 不能发送 'command' 请求"
        await client.close()
    finally:
        listener.close()
        await listener.wait_closed()
    assert port.calls == []  # 越权命令没有到达交易核心


@unix_only
@pytest.mark.asyncio
async def test_apid_role_reaches_commands(tmp_path: Path) -> None:
    port, path = Port(), tmp_path / "t.sock"
    listener = await _serve(path, port, {Role.APID: _uid()})
    try:
        client = await connect_unix(path, server_uid=_uid())
        answer = await client.command("c-1", "reconcile", {}, actor="dylan")
        assert answer == {"ok": False, "error": "替身拒绝", "refused": True}
        await client.close()
    finally:
        listener.close()
        await listener.wait_closed()
    assert port.calls == ["reconcile"]


@unix_only
@pytest.mark.asyncio
async def test_unregistered_peer_is_disconnected(tmp_path: Path) -> None:
    port, path = Port(), tmp_path / "t.sock"
    me = _uid()
    listener = await _serve(path, port, {Role.WORKERD: me + 1, Role.APID: me + 2})
    try:
        client = await connect_unix(path, server_uid=_uid())
        with pytest.raises((ConnectionError, ProtocolError)):
            await client.request({"type": "intent", "intent": "{}"})
        await client.close()
    finally:
        listener.close()
        await listener.wait_closed()
    assert port.calls == []


@unix_only
@pytest.mark.asyncio
async def test_stale_socket_is_replaced_but_other_files_are_not(tmp_path: Path) -> None:
    stale = tmp_path / "s.sock"
    family = socket.AF_UNIX  # type: ignore[attr-defined,unused-ignore]
    old = socket.socket(family, socket.SOCK_STREAM)
    old.bind(str(stale))
    old.close()  # 遗留的套接字文件
    listener = await _serve(stale, Port(), {Role.WORKERD: _uid()})
    listener.close()
    await listener.wait_closed()
    regular = tmp_path / "r.sock"
    regular.write_text("不是套接字")
    with pytest.raises(TransportError, match="不是套接字"):
        await _serve(regular, Port(), {Role.WORKERD: _uid()})
    assert regular.read_text() == "不是套接字"


@unix_only
@pytest.mark.asyncio
async def test_client_refuses_an_impostor_tradingd(tmp_path: Path) -> None:
    """路径被别的账户抢先绑定：客户端核验对端 uid，不把意图交给冒充者。"""
    path = tmp_path / "t.sock"
    listener = await _serve(path, Port(), {Role.WORKERD: _uid()})
    try:
        with pytest.raises(TransportError, match="不是 tradingd"):
            await connect_unix(path, server_uid=_uid() + 1)
    finally:
        listener.close()
        await listener.wait_closed()


@unix_only
@pytest.mark.asyncio
async def test_second_instance_cannot_steal_a_live_socket(tmp_path: Path) -> None:
    path = tmp_path / "t.sock"
    listener = await _serve(path, Port(), {Role.WORKERD: _uid()})
    try:
        with pytest.raises(TransportError, match="已有进程在监听"):
            await _serve(path, Port(), {Role.WORKERD: _uid()})
        client = await connect_unix(path, server_uid=_uid())  # 第一个实例仍可达
        assert (await client.request({"type": "intent", "intent": "{}"}))["refused"] is True
        await client.close()
    finally:
        listener.close()
        await listener.wait_closed()


@unix_only
@pytest.mark.asyncio
async def test_apid_cannot_send_intents_over_the_socket(tmp_path: Path) -> None:
    port, path = Port(), tmp_path / "t.sock"
    listener = await _serve(path, port, {Role.APID: _uid()})
    try:
        client = await connect_unix(path, server_uid=_uid())
        answer = await client.request({"type": "intent", "intent": "{}"})
        assert answer["error"] == "apid 不能发送 'intent' 请求"
        await client.close()
    finally:
        listener.close()
        await listener.wait_closed()
    assert port.calls == []
