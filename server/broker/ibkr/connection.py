"""IBKR 连接安全规则与连接监督器（B-03a，ADR-06）。

硬性规则（失败即关闭，任何一条不满足都不连接或立即断开）：
- 只连本机 127.0.0.1；
- 端口按运行模式白名单：PAPER 只允许 TWS 模拟盘端口 7497，实盘端口（TWS 7496、Gateway 4001）
  任何模式下都拒绝；G7 之前不存在任何实盘模式的连接（V4.1 红线）；
- 连上后先核验账户：券商只管理这一个账户、账户 ID 与登记的模拟账户一致（PAPER 必须以 DU 开头）、
  账户类型与登记一致；不一致立即断开并停止重连（AccountMismatchError，由装配层进入 SAFE HALT，
  需 Dylan 人工处理）；
- B-03 只读阶段：连接必须以只读方式建立，监督器本身不暴露任何下单接口。

监督器：断线后带指数退避重连；券商每日重启窗口内不连接；任何一次（重）连接之后，
必须由调用方对账成功（mark_reconciled）才允许新增订单（V4.1 7.5）。

ib_async 的绑定在 B-03b：这里只依赖窄接口 IbClient，测试用桩实现。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from server.broker.base import AccountMismatchError, ConnectionEvent, ConnectionState
from server.broker.ibkr.mapping import BROKER_LOST, BROKER_RESTORED
from server.domain.account import Account
from server.domain.enums import LIVE_MODES, AccountType, Mode

LOCALHOST = "127.0.0.1"
PAPER_PORTS = frozenset({7497})  # TWS 模拟盘
LIVE_PORTS = frozenset({7496, 4001})  # TWS 实盘、IB Gateway 实盘：任何模式都不连
PAPER_ACCOUNT_PREFIX = "DU"

# IBKR 账户摘要里的 AccountType 取值 → 本系统账户类型（未实测，B-03b 核对）
_ACCOUNT_TYPES = {"INDIVIDUAL": None, "CASH": AccountType.CASH, "MARGIN": AccountType.MARGIN}


class ConnectionPolicyError(Exception):
    """连接参数违反安全规则：拒绝连接。"""


class IbClient(Protocol):
    """B-03a 需要的最窄接口（B-03b 用 ib_async.IB 实现）。"""

    async def connect(self, host: str, port: int, client_id: int, *, readonly: bool) -> None: ...

    async def disconnect(self) -> None:
        """必须幂等：未连接、半连上（握手未完成）时调用也要释放 socket 与 clientId。"""
        ...

    def is_connected(self) -> bool: ...

    async def managed_accounts(self) -> Sequence[str]: ...

    async def account_summary(self, account_id: str) -> Mapping[str, str]:
        """账户摘要的标签 → 取值（至少含 AccountType、Currency 或 BaseCurrency）。"""
        ...


@dataclass(frozen=True)
class ConnectionPolicy:
    mode: Mode
    host: str
    port: int
    client_id: int
    readonly: bool
    expected_account_id: str
    expected_account_type: AccountType

    def __post_init__(self) -> None:
        check_policy(self)


def check_policy(policy: ConnectionPolicy) -> None:
    """连接参数的硬性规则（构造时检查，监督器真正连接前再检查一次：纵深防御）。"""
    if type(policy) is not ConnectionPolicy:
        raise ConnectionPolicyError("只接受 ConnectionPolicy 本身（不接受子类）")
    if policy.mode in LIVE_MODES:
        raise ConnectionPolicyError("G7 之前不允许任何实盘模式的券商连接")
    if policy.mode is not Mode.PAPER:
        raise ConnectionPolicyError(f"{policy.mode} 模式不连接券商")
    if policy.host != LOCALHOST:
        raise ConnectionPolicyError(f"只允许连接本机 {LOCALHOST}")
    if type(policy.port) is not int or policy.port in LIVE_PORTS or policy.port not in PAPER_PORTS:
        raise ConnectionPolicyError(f"PAPER 模式只允许模拟盘端口 {sorted(PAPER_PORTS)}")
    if policy.readonly is not True:
        raise ConnectionPolicyError("B-03 只读阶段：连接必须是只读的（下单在 B-05 放开）")
    # clientId 0 是 TWS 主客户端，能接管 TWS 手工单：不使用
    if type(policy.client_id) is not int or policy.client_id <= 0:
        raise ConnectionPolicyError("clientId 必须是正整数（0 是 TWS 主客户端，不使用）")
    if not str(policy.expected_account_id).startswith(PAPER_ACCOUNT_PREFIX):
        raise ConnectionPolicyError(f"登记的账户不是模拟账户（应以 {PAPER_ACCOUNT_PREFIX} 开头）")


async def verify_account(client: IbClient, policy: ConnectionPolicy) -> Account:
    """连上后先核验账户；不一致抛 AccountMismatchError（调用方断开并进入 SAFE HALT）。

    报错信息不回显账户号（账户号不进日志）。
    """
    accounts = list(await client.managed_accounts())
    if accounts != [policy.expected_account_id]:
        raise AccountMismatchError(
            f"券商管理的账户与登记不一致（{len(accounts)} 个账户）：只允许登记的那一个模拟账户"
        )
    summary = await client.account_summary(policy.expected_account_id)
    raw_type = summary.get("AccountType", "").strip().upper()
    if raw_type not in _ACCOUNT_TYPES or _ACCOUNT_TYPES[raw_type] is None:
        # INDIVIDUAL 等是开户主体类型而非现金 / 保证金：取不到明确类型即不可信
        raise AccountMismatchError(f"无法确定账户类型（{raw_type or '空'}）")
    account_type = _ACCOUNT_TYPES[raw_type]
    if account_type is not policy.expected_account_type:
        raise AccountMismatchError(
            f"账户类型与登记不一致：登记 {policy.expected_account_type.value}，券商 {raw_type}"
        )
    currency = (summary.get("BaseCurrency") or summary.get("Currency") or "").strip().upper()
    if currency != "USD":
        raise AccountMismatchError(f"账户基础货币不是 USD（{currency or '空'}）")
    return Account(
        account_id=policy.expected_account_id, account_type=account_type, base_currency="USD"
    )


class SupervisorState(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTED = "CONNECTED"
    BROKER_DOWN = "BROKER_DOWN"  # API 连接在，但 TWS 与 IB 服务器断开（1100）：不允许交易
    RESTART_WINDOW = "RESTART_WINDOW"
    HALTED = "HALTED"  # 账户核验失败：停止重连，等人工处理（事件流上表现为 DISCONNECTED）


@dataclass(frozen=True)
class Backoff:
    initial: timedelta
    maximum: timedelta

    def __post_init__(self) -> None:
        if self.initial <= timedelta(0) or self.maximum < self.initial:
            raise ValueError("退避必须为正，且上限不小于初始值")

    def delay(self, failures: int) -> float:
        """第 failures 次连续失败后的等待秒数：initial × 2^(failures−1)，封顶 maximum。"""
        exponent = min(max(failures - 1, 0), 30)
        delay: float = self.initial.total_seconds() * (2**exponent)
        return min(delay, self.maximum.total_seconds())


class ConnectionSupervisor:
    """维持一个只读 IBKR 连接：自动重连、遵守重启窗口、核验账户、恢复后要求先对账。

    每次进入 CONNECTED 都换一个连接代号（epoch）：对账必须带上开始对账时的代号，
    代号不一致（期间断开或重连过）即拒绝，避免旧连接的对账结果放行新连接。
    HALTED 只通过异常（AccountMismatchError）与 halt_reason 传达，事件流上是 DISCONNECTED。
    """

    def __init__(
        self,
        client: IbClient,
        policy: ConnectionPolicy,
        *,
        backoff: Backoff,
        broker_window: Callable[[datetime], bool],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        check_policy(policy)
        self._client = client
        self._policy = policy
        self._backoff = backoff
        self._window = broker_window
        self._clock = clock
        self._state = SupervisorState.DISCONNECTED
        self._account: Account | None = None
        self._epoch = 0
        self._reconciled_epoch: int | None = None
        self._failures = 0
        self._broker_lost = False  # 1100 之后、1101 / 1102 之前：TWS 与 IB 服务器断开
        self.disconnect_failures = 0  # 断开连续失败的次数：大于 0 时装配层应告警
        self.events: asyncio.Queue[ConnectionEvent] = asyncio.Queue()
        self.halt_reason: str | None = None

    @property
    def state(self) -> SupervisorState:
        return self._state

    @property
    def account(self) -> Account | None:
        return self._account

    @property
    def epoch(self) -> int:
        """当前连接代号：对账开始前读取，对账成功后原样传给 mark_reconciled。"""
        return self._epoch

    def trading_allowed(self) -> bool:
        """已连接、账户已核验、本次连接之后已对账、底层连接仍在，才允许新增订单。"""
        try:
            socket_up = self._client.is_connected()
        except Exception:  # 查询连接状态出错：按不可交易处理（失败即关闭）
            return False
        return (
            self._state is SupervisorState.CONNECTED
            and not self._broker_lost
            and self._account is not None
            and self._reconciled_epoch == self._epoch
            and socket_up is True
        )

    def mark_reconciled(self, epoch: int) -> None:
        """调用方在本次连接之后对账成功时调用；代号必须是开始对账时读到的那个。"""
        if self._state is not SupervisorState.CONNECTED or not self._client.is_connected():
            raise ConnectionPolicyError("未连接时不能标记已对账")
        if epoch != self._epoch:
            raise ConnectionPolicyError("对账开始后连接已断开或重连过，需要重新对账")
        self._reconciled_epoch = epoch

    def note_broker_error(self, code: int) -> None:
        """接收 IBKR 连接类代码：1100 停止交易；1101 / 1102 恢复后要求重新对账（换新代号）。

        “券商侧断开”是独立于状态机的标志：核验途中到达的 1100 也会被记住，
        核验通过后直接进入 BROKER_DOWN；只有 1101 / 1102 能清除它。
        """
        if type(code) is not int:
            raise TypeError(f"IBKR 代码必须是 int：{code!r}")
        if self._state is SupervisorState.HALTED:
            return
        if code == BROKER_LOST:
            self._broker_lost = True
            self._reconciled_epoch = None
            if self._state is SupervisorState.CONNECTED:
                self._set(SupervisorState.BROKER_DOWN)
        elif code in BROKER_RESTORED and self._broker_lost:
            self._broker_lost = False
            if self._state is SupervisorState.BROKER_DOWN:
                self._set(SupervisorState.CONNECTED)

    def _emit(self, state: ConnectionState) -> None:
        self.events.put_nowait(ConnectionEvent(state=state, at=self._clock()))

    def _set(self, state: SupervisorState) -> None:
        if state is self._state:
            return
        self._state = state
        self._reconciled_epoch = None
        if state is SupervisorState.CONNECTED:
            self._epoch += 1
        mapping = {
            SupervisorState.CONNECTED: ConnectionState.CONNECTED,
            SupervisorState.RESTART_WINDOW: ConnectionState.RESTART_WINDOW,
        }
        self._emit(mapping.get(state, ConnectionState.DISCONNECTED))

    async def _safe_disconnect(self) -> None:
        """无条件断开（disconnect 必须幂等）。断开出错不掩盖调用方正在处理的异常，
        但会累计 disconnect_failures 供告警；状态已由调用方设为非 CONNECTED，下一步会再试。"""
        try:
            await self._client.disconnect()
        except Exception:  # 不能盖掉原因；失败次数对外可见
            self.disconnect_failures += 1
        else:
            self.disconnect_failures = 0

    async def step(self) -> float:
        """推进一步，返回下一次检查前应等待的秒数（run 循环用；测试可逐步驱动）。"""
        if self._state is SupervisorState.HALTED:
            await self._safe_disconnect()  # 持续确保断开（可能连着错误的账户）
            return self._backoff.maximum.total_seconds()
        now = self._clock()
        if self._window(now):
            self._account = None
            self._set(SupervisorState.RESTART_WINDOW)
            await self._safe_disconnect()
            return self._backoff.initial.total_seconds()
        connected_states = (SupervisorState.CONNECTED, SupervisorState.BROKER_DOWN)
        try:
            socket_up = self._client.is_connected() is True
        except Exception:  # 查不到连接状态：按连接丢失处理
            socket_up = False
        if self._state in connected_states and socket_up:
            return self._backoff.initial.total_seconds()
        if self._state in connected_states:  # 连接丢失
            self._account = None
            self._set(SupervisorState.DISCONNECTED)
            await self._safe_disconnect()  # 先清理残留的 socket 与 clientId，再重连（避免 326）
        return await self._try_connect()

    async def _try_connect(self) -> float:
        check_policy(self._policy)  # 纵深防御：真正连接前再查一次
        p = self._policy
        # 不在重连时清除 1100 标志：TWS 可能仍与 IB 断开，新连接上未必补发 1100（未实测）；
        # 只有 1101 / 1102 能清除（失败即关闭；B-03b 实测后再决定）
        try:
            await self._client.connect(LOCALHOST, p.port, p.client_id, readonly=True)
        except (OSError, TimeoutError, ConnectionError):
            await self._safe_disconnect()  # 底层可能半连上
            self._failures += 1
            self._set(SupervisorState.DISCONNECTED)
            return self._backoff.delay(self._failures)
        except BaseException:
            await self._safe_disconnect()
            self._set(SupervisorState.DISCONNECTED)
            raise
        try:
            account = await verify_account(self._client, p)
        except AccountMismatchError as exc:
            self._account = None
            self.halt_reason = str(exc)
            self._set(SupervisorState.HALTED)  # 先停机，再断开：断开失败也不会再重连
            await self._safe_disconnect()
            raise
        except (OSError, TimeoutError, ConnectionError):
            await self._safe_disconnect()
            self._failures += 1
            self._set(SupervisorState.DISCONNECTED)
            return self._backoff.delay(self._failures)
        except BaseException:
            await self._safe_disconnect()  # 意外异常（含取消）：不留下只核验了一半的连接
            self._set(SupervisorState.DISCONNECTED)
            raise
        self._failures = 0
        self._account = account
        # 核验期间到达过 1100：直接进入 BROKER_DOWN，等 1101 / 1102
        self._set(SupervisorState.BROKER_DOWN if self._broker_lost else SupervisorState.CONNECTED)
        return self._backoff.initial.total_seconds()

    async def run(self, stop: asyncio.Event) -> None:
        """持续监督直到 stop；账户核验失败时抛 AccountMismatchError（调用方进入 SAFE HALT）。"""
        try:
            while not stop.is_set():
                delay = await self.step()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    continue
        finally:
            # 先撤销“已连接”的状态：即使断开失败，退出后也不会在无人监督的连接上放行
            self._account = None
            try:
                if self._state is not SupervisorState.HALTED:
                    self._set(SupervisorState.DISCONNECTED)
            finally:
                await self._safe_disconnect()  # 发事件出错也要断开；断开失败不掩盖原异常
