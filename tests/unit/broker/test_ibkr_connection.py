"""IBKR 连接安全规则、账户核验与连接监督器（B-03a）。用桩客户端，不连 TWS。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from server.broker.base import AccountMismatchError, ConnectionState
from server.broker.ibkr.connection import (
    Backoff,
    ConnectionPolicy,
    ConnectionPolicyError,
    ConnectionSupervisor,
    SupervisorState,
    verify_account,
)
from server.domain.enums import AccountType, Mode

ACCOUNT = "DU0000001"  # 虚构的模拟账户号
POLICY = ConnectionPolicy(
    mode=Mode.PAPER, host="127.0.0.1", port=7497, client_id=1, readonly=True,
    expected_account_id=ACCOUNT, expected_account_type=AccountType.CASH,
)  # fmt: skip
NOW = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
BACKOFF = Backoff(initial=timedelta(seconds=1), maximum=timedelta(seconds=30))


class StubClient:
    def __init__(
        self,
        accounts: Sequence[str] = (ACCOUNT,),
        summary: Mapping[str, str] | None = None,
    ) -> None:
        self.accounts = list(accounts)
        self.summary = dict(summary or {"AccountType": "CASH", "BaseCurrency": "USD"})
        self.connected = False
        self.connects: list[tuple[str, int, int, bool]] = []
        self.disconnects = 0
        self.fail_connects = 0
        self.fail_summary = False

    async def connect(self, host: str, port: int, client_id: int, *, readonly: bool) -> None:
        self.connects.append((host, port, client_id, readonly))
        if self.fail_connects:
            self.fail_connects -= 1
            raise ConnectionRefusedError("TWS 没开")
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnects += 1
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    async def managed_accounts(self) -> Sequence[str]:
        return self.accounts

    async def account_summary(self, account_id: str) -> Mapping[str, str]:
        if self.fail_summary:
            raise TimeoutError("账户摘要超时")
        return self.summary


def _supervisor(
    client: StubClient, *, window: bool = False, policy: ConnectionPolicy = POLICY
) -> ConnectionSupervisor:
    return ConnectionSupervisor(
        client, policy, backoff=BACKOFF, broker_window=lambda _: window, clock=lambda: NOW
    )


def _state(supervisor: ConnectionSupervisor) -> SupervisorState:
    return supervisor.state  # 经函数读取：避免类型检查器把属性收窄成常量


def _drain(supervisor: ConnectionSupervisor) -> list[ConnectionState]:
    out = []
    while not supervisor.events.empty():
        out.append(supervisor.events.get_nowait().state)
    return out


# ---------- 连接参数 ----------


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"port": 7496}, "模拟盘端口"),  # TWS 实盘端口：绝不连接
        ({"port": 4001}, "模拟盘端口"),  # Gateway 实盘端口
        ({"port": 4002}, "模拟盘端口"),  # 不在白名单的端口一律拒绝
        ({"host": "192.168.1.10"}, "本机"),
        ({"host": "localhost"}, "本机"),  # 只认字面的 127.0.0.1
        ({"mode": Mode.ASSISTED_LIVE}, "实盘"),
        ({"mode": Mode.LIMITED_AUTO_LIVE}, "实盘"),
        ({"mode": Mode.BACKTEST}, "不连接券商"),
        ({"mode": Mode.HALT}, "不连接券商"),
        ({"readonly": False}, "只读"),
        ({"expected_account_id": "U1234567"}, "模拟账户"),  # 实盘账户号格式
        ({"client_id": -1}, "clientId"),
        ({"client_id": 0}, "clientId"),  # TWS 主客户端
        ({"readonly": 1}, "只读"),  # 只认 True 本身
    ],
)
def test_policy_refuses_unsafe_parameters(changes: dict[str, object], match: str) -> None:
    with pytest.raises(ConnectionPolicyError, match=match):
        replace(POLICY, **changes)  # type: ignore[arg-type]


# ---------- 账户核验 ----------


@pytest.mark.asyncio
async def test_matching_paper_account_is_verified() -> None:
    account = await verify_account(StubClient(), POLICY)
    assert (account.account_id, account.account_type) == (ACCOUNT, AccountType.CASH)


@pytest.mark.parametrize(
    ("client", "match"),
    [
        (StubClient(accounts=["DU0000002"]), "账户与登记不一致"),
        (StubClient(accounts=[ACCOUNT, "DU0000002"]), "2 个账户"),
        (StubClient(accounts=[]), "0 个账户"),
        (StubClient(summary={"AccountType": "MARGIN", "BaseCurrency": "USD"}), "账户类型"),
        (StubClient(summary={"AccountType": "INDIVIDUAL", "BaseCurrency": "USD"}), "无法确定"),
        (StubClient(summary={"BaseCurrency": "USD"}), "无法确定"),
        (StubClient(summary={"AccountType": "CASH", "BaseCurrency": "EUR"}), "USD"),
        (StubClient(summary={"AccountType": "CASH"}), "USD"),
    ],
)
@pytest.mark.asyncio
async def test_account_mismatches_are_refused(client: StubClient, match: str) -> None:
    with pytest.raises(AccountMismatchError, match=match):
        await verify_account(client, POLICY)


@pytest.mark.asyncio
async def test_mismatch_message_does_not_echo_account_numbers() -> None:
    with pytest.raises(AccountMismatchError) as caught:
        await verify_account(StubClient(accounts=["DU7654321"]), POLICY)
    assert "DU7654321" not in str(caught.value)
    assert ACCOUNT not in str(caught.value)


# ---------- 监督器 ----------


@pytest.mark.asyncio
async def test_connects_read_only_and_requires_reconciliation_before_trading() -> None:
    client = StubClient()
    sup = _supervisor(client)
    await sup.step()
    assert client.connects == [("127.0.0.1", 7497, 1, True)]
    assert _state(sup) is SupervisorState.CONNECTED
    assert _drain(sup) == [ConnectionState.CONNECTED]
    assert not sup.trading_allowed()  # 连上后必须先对账
    sup.mark_reconciled(sup.epoch)
    assert sup.trading_allowed()


@pytest.mark.asyncio
async def test_reconnects_with_backoff_and_resets_reconciliation() -> None:
    client = StubClient()
    sup = _supervisor(client)
    await sup.step()
    sup.mark_reconciled(sup.epoch)
    client.connected = False  # 连接丢失
    client.fail_connects = 3
    delays = [await sup.step() for _ in range(3)]
    assert delays == [1.0, 2.0, 4.0]
    assert _state(sup) is SupervisorState.DISCONNECTED
    assert not sup.trading_allowed()
    await sup.step()
    assert _state(sup) is SupervisorState.CONNECTED
    assert not sup.trading_allowed()  # 重连后要重新对账
    assert _drain(sup) == [
        ConnectionState.CONNECTED, ConnectionState.DISCONNECTED, ConnectionState.CONNECTED,
    ]  # fmt: skip
    assert await sup.step() == 1.0  # 连续失败计数已清零


def test_backoff_is_capped() -> None:
    assert [BACKOFF.delay(n) for n in (1, 2, 5, 6, 7, 100)] == [1, 2, 16, 30, 30, 30]
    with pytest.raises(ValueError, match="退避"):
        Backoff(initial=timedelta(0), maximum=timedelta(seconds=1))
    with pytest.raises(ValueError, match="退避"):
        Backoff(initial=timedelta(seconds=2), maximum=timedelta(seconds=1))


@pytest.mark.asyncio
async def test_account_mismatch_disconnects_and_halts_without_retrying() -> None:
    client = StubClient(accounts=["DU0000002"])
    sup = _supervisor(client)
    with pytest.raises(AccountMismatchError):
        await sup.step()
    assert _state(sup) is SupervisorState.HALTED
    assert not client.connected
    assert client.disconnects == 1
    assert sup.halt_reason is not None
    await sup.step()
    assert len(client.connects) == 1  # 停止重连，等人工处理
    assert not sup.trading_allowed()


@pytest.mark.asyncio
async def test_verification_timeout_disconnects_and_retries() -> None:
    client = StubClient()
    client.fail_summary = True
    sup = _supervisor(client)
    assert await sup.step() == 1.0
    assert _state(sup) is SupervisorState.DISCONNECTED
    assert not client.connected  # 核验没完成就断开，不留半核验的连接
    client.fail_summary = False
    await sup.step()
    assert _state(sup) is SupervisorState.CONNECTED


@pytest.mark.asyncio
async def test_restart_window_disconnects_and_blocks_trading() -> None:
    client = StubClient()
    in_window = [False]
    sup = ConnectionSupervisor(
        client, POLICY, backoff=BACKOFF, broker_window=lambda _: in_window[0], clock=lambda: NOW
    )
    await sup.step()
    sup.mark_reconciled(sup.epoch)
    in_window[0] = True
    await sup.step()
    assert _state(sup) is SupervisorState.RESTART_WINDOW
    assert not client.connected
    assert not sup.trading_allowed()
    await sup.step()
    assert len(client.connects) == 1  # 窗口内不重连
    in_window[0] = False
    await sup.step()
    assert _state(sup) is SupervisorState.CONNECTED
    assert not sup.trading_allowed()
    assert _drain(sup) == [
        ConnectionState.CONNECTED, ConnectionState.RESTART_WINDOW, ConnectionState.CONNECTED,
    ]  # fmt: skip


@pytest.mark.asyncio
async def test_trading_requires_the_socket_to_be_up() -> None:
    client = StubClient()
    sup = _supervisor(client)
    await sup.step()
    sup.mark_reconciled(sup.epoch)
    client.connected = False  # 监督器还没察觉的断线
    assert not sup.trading_allowed()


def test_mark_reconciled_requires_a_connection() -> None:
    with pytest.raises(ConnectionPolicyError, match="未连接"):
        _supervisor(StubClient()).mark_reconciled(0)


@pytest.mark.asyncio
async def test_run_stops_and_disconnects() -> None:
    client = StubClient()
    sup = _supervisor(client)
    stop = asyncio.Event()
    task = asyncio.create_task(sup.run(stop))
    await asyncio.sleep(0.05)
    assert client.connected
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert not client.connected


@pytest.mark.asyncio
async def test_run_raises_on_account_mismatch() -> None:
    sup = _supervisor(StubClient(accounts=["DU0000002"]))
    with pytest.raises(AccountMismatchError):
        await asyncio.wait_for(sup.run(asyncio.Event()), timeout=2)


# ---------- 评审补充 ----------


@pytest.mark.asyncio
async def test_unexpected_error_during_verification_disconnects() -> None:
    class _Buggy(StubClient):
        async def account_summary(self, account_id: str) -> Mapping[str, str]:
            raise RuntimeError("库缺陷")

    client = _Buggy()
    sup = _supervisor(client)
    with pytest.raises(RuntimeError, match="库缺陷"):
        await sup.step()
    assert not client.connected  # 不留下只核验了一半的连接
    assert _state(sup) is SupervisorState.DISCONNECTED


@pytest.mark.asyncio
async def test_mismatch_still_halts_when_disconnect_fails() -> None:
    class _StuckSocket(StubClient):
        async def disconnect(self) -> None:
            self.disconnects += 1
            raise OSError("断开失败")

    client = _StuckSocket(accounts=["DU0000002"])
    sup = _supervisor(client)
    with pytest.raises(AccountMismatchError):  # 抛出的是账户不一致，而不是断开失败
        await sup.step()
    assert _state(sup) is SupervisorState.HALTED
    await sup.step()
    assert len(client.connects) == 1  # 不会再连那个错误的账户
    assert client.disconnects == 2  # 停机后仍持续尝试断开


@pytest.mark.asyncio
async def test_reconciliation_from_a_previous_connection_is_refused() -> None:
    """对账进行中连接断开又重连：旧的对账结果不能放行新连接（评审 P1-3）。"""
    client = StubClient()
    sup = _supervisor(client)
    await sup.step()
    started = sup.epoch  # 开始对账
    client.connected = False
    await sup.step()  # 察觉断开并重连
    assert _state(sup) is SupervisorState.CONNECTED
    with pytest.raises(ConnectionPolicyError, match="重新对账"):
        sup.mark_reconciled(started)
    assert not sup.trading_allowed()
    sup.mark_reconciled(sup.epoch)
    assert sup.trading_allowed()


@pytest.mark.asyncio
async def test_broker_side_disconnect_blocks_trading_until_reconciled_again() -> None:
    """TWS 与 IB 服务器断开（1100）时 API 连接仍在：停止交易；恢复（1101/1102）后要求重新对账。"""
    client = StubClient()
    sup = _supervisor(client)
    await sup.step()
    sup.mark_reconciled(sup.epoch)
    before = sup.epoch
    sup.note_broker_error(1100)
    assert _state(sup) is SupervisorState.BROKER_DOWN
    assert not sup.trading_allowed()
    assert await sup.step() == 1.0  # API 连接还在：不重连，等恢复
    assert len(client.connects) == 1
    sup.note_broker_error(1102)
    assert _state(sup) is SupervisorState.CONNECTED
    assert sup.epoch == before + 1
    assert not sup.trading_allowed()
    sup.mark_reconciled(sup.epoch)
    assert sup.trading_allowed()
    sup.note_broker_error(2104)  # 信息类代码不影响状态
    assert sup.trading_allowed()


@pytest.mark.asyncio
async def test_policy_is_rechecked_before_connecting() -> None:
    """纵深防御：被篡改的连接参数在真正连接前被拒，子类不被接受（评审 P1-6）。"""
    client = StubClient()
    tampered = replace(POLICY)
    object.__setattr__(tampered, "port", 7496)  # 模拟绕过 frozen 的篡改
    sup = _supervisor(client)
    sup._policy = tampered  # 测试纵深防御：绕过构造校验后替换
    with pytest.raises(ConnectionPolicyError, match="模拟盘端口"):
        await sup.step()
    assert client.connects == []

    class _Sub(ConnectionPolicy):
        pass

    with pytest.raises(ConnectionPolicyError, match="子类"):
        _Sub(**{f: getattr(POLICY, f) for f in POLICY.__dataclass_fields__})


@pytest.mark.asyncio
async def test_connect_failure_with_a_half_open_socket_is_cleaned_up() -> None:
    class _HalfOpen(StubClient):
        async def connect(self, host: str, port: int, client_id: int, *, readonly: bool) -> None:
            self.connects.append((host, port, client_id, readonly))
            self.connected = True
            raise TimeoutError("握手超时")

    client = _HalfOpen()
    sup = _supervisor(client)
    assert await sup.step() == 1.0
    assert not client.connected
    assert _state(sup) is SupervisorState.DISCONNECTED


@pytest.mark.asyncio
async def test_halt_emits_disconnected() -> None:
    sup = _supervisor(StubClient(accounts=["DU0000002"]))
    with pytest.raises(AccountMismatchError):
        await sup.step()
    assert _drain(sup) == [ConnectionState.DISCONNECTED]  # 停机在事件流上表现为断开


# ---------- 第二轮评审补充 ----------


@pytest.mark.asyncio
async def test_run_exit_revokes_trading_even_if_disconnect_fails() -> None:
    """run 退出后不能在无人监督的连接上放行（复审 P1-A）。"""

    class _Sticky(StubClient):
        async def disconnect(self) -> None:
            self.disconnects += 1
            raise OSError("断开失败")

    client = _Sticky()
    sup = _supervisor(client)
    stop = asyncio.Event()
    task = asyncio.create_task(sup.run(stop))
    await asyncio.sleep(0.05)
    sup.mark_reconciled(sup.epoch)
    assert sup.trading_allowed()
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert client.connected  # 断开失败：底层还连着
    assert not sup.trading_allowed()
    assert _state(sup) is SupervisorState.DISCONNECTED
    assert _drain(sup)[-1] is ConnectionState.DISCONNECTED
    assert sup.disconnect_failures == 1


@pytest.mark.asyncio
async def test_broker_loss_during_verification_is_remembered() -> None:
    """核验途中到达的 1100 不被丢掉：核验通过后进入 BROKER_DOWN（复审 P1-B）。"""
    sup: ConnectionSupervisor

    class _LossDuringVerify(StubClient):
        async def account_summary(self, account_id: str) -> Mapping[str, str]:
            sup.note_broker_error(1100)
            return await super().account_summary(account_id)

    client = _LossDuringVerify()
    sup = _supervisor(client)
    await sup.step()
    assert _state(sup) is SupervisorState.BROKER_DOWN
    with pytest.raises(ConnectionPolicyError):
        sup.mark_reconciled(sup.epoch)
    assert not sup.trading_allowed()
    sup.note_broker_error(1102)
    assert _state(sup) is SupervisorState.CONNECTED
    sup.mark_reconciled(sup.epoch)
    assert sup.trading_allowed()


@pytest.mark.asyncio
async def test_broker_down_then_socket_lost_stays_down_until_restored() -> None:
    """1100 之后 API 也断开、重连成功：TWS 可能仍与 IB 断开。

    保持 BROKER_DOWN 直到 1101/1102（复审 P1-C）。
    """
    client = StubClient()
    sup = _supervisor(client)
    await sup.step()
    first = sup.epoch
    sup.note_broker_error(1100)
    client.connected = False
    await sup.step()  # 察觉断开并重连
    assert len(client.connects) == 2
    assert _state(sup) is SupervisorState.BROKER_DOWN
    assert not sup.trading_allowed()
    sup.note_broker_error(1102)
    assert _state(sup) is SupervisorState.CONNECTED
    assert sup.epoch > first
    assert not sup.trading_allowed()  # 仍需对账
    sup.mark_reconciled(sup.epoch)
    assert sup.trading_allowed()


@pytest.mark.asyncio
async def test_restart_window_while_broker_down_and_restore_codes() -> None:
    client = StubClient()
    in_window = [False]
    sup = ConnectionSupervisor(
        client, POLICY, backoff=BACKOFF, broker_window=lambda _: in_window[0], clock=lambda: NOW
    )
    await sup.step()
    sup.note_broker_error(1100)
    in_window[0] = True
    await sup.step()
    assert _state(sup) is SupervisorState.RESTART_WINDOW
    assert not client.connected
    sup.note_broker_error(1101)  # 窗口内收到恢复：不进入 CONNECTED
    assert _state(sup) is SupervisorState.RESTART_WINDOW
    with pytest.raises(TypeError):
        sup.note_broker_error("1100")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_cancellation_during_verification_disconnects() -> None:
    class _Slow(StubClient):
        async def account_summary(self, account_id: str) -> Mapping[str, str]:
            await asyncio.sleep(10)
            return {}

    client = _Slow()
    sup = _supervisor(client)
    task = asyncio.create_task(sup.step())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not client.connected


@pytest.mark.asyncio
async def test_unexpected_connect_error_disconnects_and_status_query_errors_fail_closed() -> None:
    class _Weird(StubClient):
        async def connect(self, host: str, port: int, client_id: int, *, readonly: bool) -> None:
            self.connected = True
            raise RuntimeError("库缺陷")

    client = _Weird()
    sup = _supervisor(client)
    with pytest.raises(RuntimeError):
        await sup.step()
    assert not client.connected

    class _Unknowable(StubClient):
        def is_connected(self) -> bool:
            raise RuntimeError("状态查询失败")

    assert not _supervisor(_Unknowable()).trading_allowed()


@pytest.mark.asyncio
async def test_status_query_error_in_step_is_treated_as_lost() -> None:
    class _Flaky(StubClient):
        broken = False

        def is_connected(self) -> bool:
            if self.broken:
                raise RuntimeError("状态查询失败")
            return self.connected

    client = _Flaky()
    sup = _supervisor(client)
    await sup.step()
    first = sup.epoch
    client.broken = True
    await sup.step()  # 查不到状态：按连接丢失处理，先断开再重连
    assert not sup.trading_allowed()
    assert len(client.connects) == 2
    assert ConnectionState.DISCONNECTED in _drain(sup)
    client.broken = False
    assert sup.epoch > first
    with pytest.raises(ConnectionPolicyError):
        sup.mark_reconciled(first)
