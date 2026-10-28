"""S-01b：本机通道与双向账户核验。

用当前操作系统的真实平台实现跑：Windows（本机与 CI windows-latest）走命名管道，
Linux（CI ubuntu-latest）走 Unix 域套接字；macOS 尚未实现，验证它明确报错。
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from server.ipc.protocol import MAX_FRAME, IpcServer, ProtocolError, Role
from server.ipc.transport import ENDPOINTS, Endpoints, TransportError, connect_role, serve_role
from server.platforms import (
    Listener,
    LocalChannel,
    PlatformError,
    PlatformNotImplementedError,
    local_channel,
)
from server.platforms.base import UnavailableChannel, check_endpoint_name
from server.platforms.macos import MacChannel
from server.risk.core import IntakeError


def _channel() -> LocalChannel | None:
    try:
        channel = local_channel()
        channel.current_account()
    except PlatformNotImplementedError:
        return None
    return channel


CHANNEL = _channel()
real = pytest.mark.skipif(CHANNEL is None, reason="当前平台的通道尚未实现")


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


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        assert CHANNEL is not None
        self.channel = CHANNEL
        self.me = CHANNEL.current_account()
        self.endpoints = self.fresh()
        self.listeners: list[Listener] = []
        self.dir = tmp_path

    def fresh(self, tag: str = "t") -> Endpoints:
        """每个用例一组独立端点（Windows 管道名全局可见，不能重名）。"""
        return Endpoints(self.channel, Path.cwd(), instance=f"test-{tag}-{uuid.uuid4().hex[:10]}")

    async def serve(
        self, role: Role, allowed: set[str] | None = None, endpoints: Endpoints | None = None
    ) -> Port:
        port = Port()
        listener = await serve_role(
            endpoints or self.endpoints, role, allowed or {self.me}, IpcServer(port)
        )
        self.listeners.append(listener)
        return port

    async def connect(self, role: Role, server_account: str | None = None) -> Any:
        return await connect_role(self.endpoints, role, server_account=server_account or self.me)

    async def close(self) -> None:
        for listener in self.listeners:
            listener.close()
            await listener.wait_closed()


@pytest_asyncio.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    r = Rig(tmp_path)
    r.endpoints = Endpoints(r.channel, tmp_path, r.endpoints.instance)
    yield r
    await r.close()


INTENT = {"type": "intent", "intent": "{}"}
COMMAND = {"type": "command", "request_id": "c", "name": "reconcile", "params": {}, "actor": "d"}


# ---------- 各平台都跑 ----------


def test_each_role_has_its_own_endpoint() -> None:
    assert set(ENDPOINTS) == set(Role)
    assert len(set(ENDPOINTS.values())) == len(ENDPOINTS)


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [set(), {""}, {"  "}])
async def test_empty_allow_lists_are_refused(tmp_path: Path, allowed: set[str]) -> None:
    with pytest.raises(TransportError, match="非空"):
        await serve_role(
            Endpoints(UnavailableChannel(), tmp_path), Role.APID, allowed, IpcServer(Port())
        )


@pytest.mark.asyncio
async def test_macos_is_explicitly_not_implemented(tmp_path: Path) -> None:
    mac = MacChannel()
    with pytest.raises(PlatformNotImplementedError, match="P-MAC"):
        mac.current_account()
    with pytest.raises(TransportError, match="P-MAC"):
        await serve_role(Endpoints(mac, tmp_path), Role.APID, {"a"}, IpcServer(Port()))
    with pytest.raises(TransportError, match="P-MAC"):
        await connect_role(Endpoints(mac, tmp_path), Role.APID, server_account="a")


def test_other_platform_adapters_refuse_to_construct() -> None:
    """在错误的平台上实例化另一平台的实现：明确报错，不退化。"""
    from server.platforms.linux import LinuxChannel  # noqa: PLC0415
    from server.platforms.windows import WindowsChannel  # noqa: PLC0415

    built = 0
    for cls in (LinuxChannel, WindowsChannel):
        try:
            cls()
            built += 1
        except PlatformError:
            pass
    assert built <= 1  # 至多当前平台那一个能用


@pytest.mark.parametrize("name", ["", "a/b", "a\\b", "..", "a.b", "A", "a b", "x" * 65, "a:b"])
def test_endpoint_names_follow_one_rule_everywhere(name: str) -> None:
    with pytest.raises(PlatformError, match="端点名不合法"):
        check_endpoint_name(name)
    assert check_endpoint_name("qts-intents") == "qts-intents"


# ---------- 真实平台通道（Windows / Linux） ----------


@real
@pytest.mark.asyncio
async def test_workerd_endpoint_only_takes_intents(rig: Rig) -> None:
    port = await rig.serve(Role.WORKERD)
    client = await rig.connect(Role.WORKERD)
    assert (await client.request(INTENT))["refused"] is True  # 到达意图入口（内容不合法）
    crossed = await client.request(COMMAND)
    assert crossed["error"] == "workerd 不能发送 'command' 请求"
    await client.close()
    assert port.calls == []


@real
@pytest.mark.asyncio
async def test_apid_endpoint_only_takes_commands(rig: Rig) -> None:
    port = await rig.serve(Role.APID)
    client = await rig.connect(Role.APID)
    answer = await client.command("c-1", "reconcile", {}, actor="dylan")
    assert answer == {"ok": False, "error": "替身拒绝", "refused": True}
    crossed = await client.request(INTENT)
    assert crossed["error"] == "apid 不能发送 'intent' 请求"
    await client.close()
    assert port.calls == ["reconcile"]


@real
@pytest.mark.asyncio
async def test_roles_map_to_different_endpoints(rig: Rig) -> None:
    """只开了意图端点时，按 apid 角色连接找不到端点（地址由角色推出，不能错配）。"""
    await rig.serve(Role.WORKERD)
    assert rig.endpoints.address(Role.WORKERD) != rig.endpoints.address(Role.APID)
    with pytest.raises((OSError, TransportError)):
        await rig.connect(Role.APID)


@real
@pytest.mark.asyncio
async def test_unlisted_account_is_disconnected(rig: Rig) -> None:
    port = await rig.serve(Role.APID, allowed={"someone-else"})
    client = await rig.connect(Role.APID)
    with pytest.raises((ConnectionError, ProtocolError, OSError)):
        await asyncio.wait_for(client.request(COMMAND), 5)
    await client.close()
    assert port.calls == []


@real
@pytest.mark.asyncio
async def test_client_refuses_an_impostor_tradingd(rig: Rig) -> None:
    """端点被别的账户抢先占用：客户端核验服务端账户，不把意图或命令交给冒充者。"""
    await rig.serve(Role.WORKERD)
    with pytest.raises(TransportError, match="不是 tradingd"):
        await rig.connect(Role.WORKERD, server_account="someone-else")


@real
@pytest.mark.asyncio
async def test_second_instance_cannot_take_a_live_endpoint(rig: Rig) -> None:
    await rig.serve(Role.WORKERD)
    with pytest.raises(TransportError):
        await rig.serve(Role.WORKERD)
    client = await rig.connect(Role.WORKERD)  # 第一个实例仍可达
    assert (await client.request(INTENT))["refused"] is True
    await client.close()


@real
@pytest.mark.asyncio
async def test_endpoint_serves_several_clients(rig: Rig) -> None:
    await rig.serve(Role.WORKERD)
    clients = [await rig.connect(Role.WORKERD) for _ in range(3)]
    answers = await asyncio.gather(*(c.request(INTENT) for c in clients))
    assert all(a["refused"] is True for a in answers)
    for c in clients:
        await c.close()


@real
@pytest.mark.asyncio
async def test_largest_frame_crosses_the_real_channel(rig: Rig) -> None:
    """最大合法帧走真实管道 / 套接字（Windows 消息模式管道会分多次读）。"""
    await rig.serve(Role.WORKERD)
    client = await rig.connect(Role.WORKERD)
    padding = "x" * (MAX_FRAME - 200)
    answer = await client.request({"type": "intent", "intent": "{}", "pad": padding})
    assert answer["refused"] is True
    await client.close()


@real
@pytest.mark.asyncio
async def test_connecting_to_nothing_fails(rig: Rig) -> None:
    with pytest.raises((OSError, TransportError)):
        await rig.connect(Role.WORKERD)


CHILD_SERVER = """
import asyncio, os, sys
from server.platforms import local_channel

async def main(address):
    channel = local_channel()

    async def on_peer(reader, writer, identity):
        writer.write(str(identity.pid).encode() + bytes([10]))
        await writer.drain()
        writer.close()

    await channel.serve(address, on_peer)
    print("ready", os.getpid(), flush=True)
    await asyncio.sleep(60)

asyncio.run(main(sys.argv[1]))
"""


@real
@pytest.mark.asyncio
async def test_identities_name_the_actual_peer_process(rig: Rig) -> None:
    """跨进程：客户端看到的服务端进程号是子进程，子进程看到的客户端进程号是本进程。"""
    address = rig.endpoints.address(Role.WORKERD)
    root = Path(__file__).resolve().parents[3]
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", CHILD_SERVER, address,
        stdout=asyncio.subprocess.PIPE, cwd=root, env={**os.environ, "PYTHONPATH": str(root)},
    )  # fmt: skip
    try:
        assert child.stdout is not None
        line = await asyncio.wait_for(child.stdout.readline(), 30)
        word, server_pid = line.split()
        assert word == b"ready"  # Windows 的 venv python.exe 是启动器，真正的服务进程号由子进程自报
        reader, writer, identity = await rig.channel.connect(address)
        assert identity.pid == int(server_pid) != os.getpid()
        assert identity.account == rig.me
        seen = await asyncio.wait_for(reader.readline(), 10)
        assert int(seen) == os.getpid()
        writer.close()
    finally:
        child.kill()
        await child.wait()


@real
@pytest.mark.asyncio
async def test_failed_identity_lookup_disconnects(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """服务端取不到对端身份时直接断开，不交给上层（两个平台的实现都有 _client_identity）。"""
    import server.platforms.linux as linux_impl  # noqa: PLC0415
    import server.platforms.windows as windows_impl  # noqa: PLC0415

    def broken(*_a: Any) -> Any:
        raise PlatformError("取不到身份")

    patched = 0
    for module in (windows_impl, linux_impl):
        if hasattr(module, "_client_identity"):
            monkeypatch.setattr(module, "_client_identity", broken)
            patched += 1
    assert patched == 1  # 当前平台的实现确实被替换
    port = await rig.serve(Role.APID)
    reader, writer, _ = await rig.channel.connect(rig.endpoints.address(Role.APID))
    assert await asyncio.wait_for(reader.read(), 5) == b""  # 服务端直接关闭，没有任何回答
    writer.close()
    assert port.calls == []
