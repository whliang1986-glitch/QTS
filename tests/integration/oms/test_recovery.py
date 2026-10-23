"""O-05：崩溃恢复与重连对账（V4.1 11.2 场景 6、7、12 的 OMS 部分）。

进程崩溃用“旧连接断开 + 同一账本上新建 OrderManager 与新客户端”模拟（`Env.restart`）；
断线漂移用 FakeBroker 的 `drop_client`：断线期间的回报全部丢失，只能靠重连后的查单对账补回。
"""

from __future__ import annotations

import shutil
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from server.broker.base import (
    BrokerOrderState,
    BrokerUnavailableError,
    ConnectionEvent,
    ConnectionState,
    OrderAmendment,
    OrderLookup,
    OrderTicket,
    UnknownOrderError,
)
from server.broker.fake.broker import FakeBroker
from server.domain.order_state import EventKind, OrderStatus
from server.oms.manager import AmendRefusedError, OrderManager, SubmissionRefusedError
from server.storage.ledger import LedgerRepository, open_ledger
from tests.support.oms import T0, Env

S = OrderStatus


class Crash(BaseException):
    """模拟进程在调用券商期间死掉（不是 Exception：OMS 的异常处理不会接住它）。"""


class CrashBeforePlace(FakeBroker):
    async def place_order(self, ticket: OrderTicket) -> None:
        raise Crash


class CrashAfterPlace(FakeBroker):
    async def place_order(self, ticket: OrderTicket) -> None:
        await super().place_order(ticket)
        raise Crash


class CrashAfterModify(FakeBroker):
    async def modify_order(self, amendment: OrderAmendment) -> None:
        await super().modify_order(amendment)
        raise Crash


class LookupDown(FakeBroker):
    async def lookup_order(self, order_ref: str) -> OrderLookup:
        raise BrokerUnavailableError("lookup down")


class LookupEmpty(FakeBroker):
    async def lookup_order(self, order_ref: str) -> OrderLookup:
        return OrderLookup(order_ref=order_ref)


def broker_orders(env: Env, intent_id: str = "i-1") -> int:
    return len(env.server._lookup(intent_id).orders)


def sent(env: Env, intent_id: str = "i-1") -> int:
    return sum(e.kind is EventKind.SUBMIT_SENT for e, _ in env.repo.order_history(intent_id))


async def amend(env: Env, amendment_id: str, price: str, qty: int) -> OrderStatus:
    intent = env.repo.get_intent("i-1")
    assert intent is not None
    later = T0 + timedelta(seconds=len(env.repo.risk_decisions_for("i-1")))  # 晚于已用过的决定
    decision = env.allow(intent, f"d-{amendment_id}", decided_at=later)
    return await env.om.request_amend(
        "i-1",
        amendment_id=amendment_id,
        new_price=Decimal(price),
        new_quantity=Decimal(qty),
        decision=decision,
    )


async def restart(env: Env) -> None:
    env.restart()
    await env.broker.connect()
    await env.pump()  # CONNECTED → 启动对账


# ---------- 场景 6：提交后进程崩溃 ----------


@pytest.mark.asyncio
async def test_crash_before_order_reached_broker_stays_unknown_and_never_resends(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    env.use(CrashBeforePlace(env.server))
    await env.broker.connect()
    with pytest.raises(Crash):
        await env.submit()
    assert env.om.status("i-1") is S.SUBMITTING  # 签发与 SUBMIT_SENT 已落账
    await restart(env)
    assert env.om.status("i-1") is S.UNKNOWN  # 先查单：查无此单，不映射为 REJECTED
    assert "recovered_submitting" in env.discrepancies()
    intent = env.repo.get_intent("i-1")
    assert intent is not None
    await env.om.submit_approved_intent(intent, env.repo.risk_decisions_for("i-1")[0])
    for _ in range(3):
        await env.om.resolve_unknown("i-1")
    assert broker_orders(env) == 0 and sent(env) == 1  # 从不重发
    assert "i-1" in env.unresolved


@pytest.mark.asyncio
async def test_crash_after_order_reached_broker_recovers_its_status(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.use(CrashAfterPlace(env.server))
    await env.broker.connect()
    with pytest.raises(Crash):
        await env.submit()
    await restart(env)
    assert env.om.status("i-1") is S.SUBMITTED
    assert broker_orders(env) == 1 and sent(env) == 1


@pytest.mark.asyncio
async def test_fills_while_process_was_down_are_booked_on_restart(tmp_path: Path) -> None:
    env = Env(tmp_path)
    await env.broker.connect()
    await env.submit()
    await env.pump()
    env.server.drop_client(env.broker.client_id)  # 进程死掉
    env.server.fill(env.oid(), Decimal(4), Decimal(100))
    env.server.fill(env.oid(), Decimal(6), Decimal(100))
    await restart(env)
    assert env.om.status("i-1") is S.FILLED
    assert env.om.filled_quantity("i-1") == Decimal(10)


@pytest.mark.asyncio
async def test_parked_cancel_survives_restart(tmp_path: Path) -> None:
    env = Env(tmp_path)
    await env.broker.connect()
    env.server.lose_next_ack()
    await env.submit()
    env.clock.advance(11)
    env.om.check_timeouts()
    assert await env.om.request_cancel("i-1") is S.UNKNOWN  # 挂起
    await restart(env)  # 内存里的挂起撤单丢了，账本里的记录还在
    assert env.om.status("i-1") is S.CANCELLED
    assert env.server._lookup("i-1").orders[0].state is BrokerOrderState.CANCELLED


@pytest.mark.asyncio
async def test_amendment_in_flight_at_crash_is_resolved_on_restart(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.use(CrashAfterModify(env.server))
    await env.broker.connect()
    await env.submit()
    await env.pump()
    with pytest.raises(Crash):
        await amend(env, "am-1", "99.50", 10)
    assert env.repo.amendments_for("i-1")[-1].result is None
    await restart(env)
    assert env.repo.amendments_for("i-1")[-1].result == "ACCEPTED"


@pytest.mark.asyncio
async def test_new_orders_refused_until_recovery_succeeds(tmp_path: Path) -> None:
    env = Env(tmp_path)
    await env.broker.connect()
    await env.submit()
    await env.pump()
    env.server.drop_client(env.broker.client_id)
    env.use(LookupDown(env.server))  # 新进程查不了单：对账做不完
    await env.broker.connect()
    await env.pump()
    with pytest.raises(SubmissionRefusedError):
        await env.submit("i-2")
    assert env.repo.issuance_for("i-2") is None and broker_orders(env, "i-2") == 0
    with pytest.raises(AmendRefusedError):
        await amend(env, "am-1", "99.50", 6)
    assert "reconcile_failed" in env.discrepancies()


@pytest.mark.asyncio
async def test_live_order_missing_at_broker_is_escalated(tmp_path: Path) -> None:
    env = Env(tmp_path)
    await env.broker.connect()
    await env.submit()
    await env.pump()
    env.server.drop_client(env.broker.client_id)
    env.use(LookupEmpty(env.server))
    await env.broker.connect()
    await env.pump()
    assert env.om.status("i-1") is S.SUBMITTED  # 不擅自改状态
    assert "reconcile_order_missing" in env.discrepancies()
    assert env.unresolved == ["i-1"]


# ---------- 场景 7：断线重连期间状态漂移 ----------


async def drop_and_reconnect(env: Env) -> None:
    await env.pump()  # 断线事件
    await env.broker.connect()
    await env.pump()  # CONNECTED → 重连对账


@pytest.mark.asyncio
async def test_fills_lost_during_disconnect_are_recovered(tmp_path: Path) -> None:
    env = Env(tmp_path)
    await env.broker.connect()
    await env.submit()
    await env.pump()
    env.server.drop_client(env.broker.client_id)
    env.server.fill(env.oid(), Decimal(3), Decimal(100))
    await drop_and_reconnect(env)
    assert env.om.status("i-1") is S.PARTIALLY_FILLED
    assert env.om.filled_quantity("i-1") == Decimal(3)


@pytest.mark.parametrize(
    ("drift", "expected"),
    [("broker_cancel", S.CANCELLED), ("expire", S.EXPIRED)],
)
@pytest.mark.asyncio
async def test_terminal_state_reached_during_disconnect(
    tmp_path: Path, drift: str, expected: OrderStatus
) -> None:
    env = Env(tmp_path)
    await env.broker.connect()
    await env.submit()
    await env.pump()
    env.server.drop_client(env.broker.client_id)
    if drift == "broker_cancel":
        env.server.broker_side_cancel(env.oid())
    else:
        env.server.expire_day_orders()
    await drop_and_reconnect(env)
    assert env.om.status("i-1") is expected


@pytest.mark.asyncio
async def test_cancel_sent_while_disconnected_is_resent_after_reconnect(tmp_path: Path) -> None:
    env = Env(tmp_path)
    await env.broker.connect()
    await env.submit()
    await env.pump()
    env.server.drop_client(env.broker.client_id)
    await env.pump()
    assert await env.om.request_cancel("i-1") is S.CANCEL_PENDING  # 请求没到券商
    await env.broker.connect()
    await env.pump()
    assert "cancel_resent" in env.discrepancies()
    assert env.om.status("i-1") is S.CANCELLED


# ---------- 场景 12：备份恢复后首次启动 ----------


@pytest.mark.asyncio
async def test_restored_backup_is_reconciled_before_new_orders(tmp_path: Path) -> None:
    env = Env(tmp_path)
    await env.broker.connect()
    await env.submit()
    await env.pump()
    backup = tmp_path / "backup.db"
    shutil.copyfile(tmp_path / "ledger.db", backup)  # 备份之后订单又成交了
    env.server.fill(env.oid(), Decimal(10), Decimal(100))
    await env.pump()
    env.server.drop_client(env.broker.client_id)
    env.repo = LedgerRepository(open_ledger(backup))  # 从备份恢复
    env.use(FakeBroker(env.server))
    assert env.om.status("i-1") is S.SUBMITTED and env.om.filled_quantity("i-1") == 0  # 回到旧状态
    await env.broker.connect()
    await env.pump()
    assert env.om.status("i-1") is S.FILLED  # 先对账：按券商事实补记成交
    assert env.om.filled_quantity("i-1") == Decimal(10)
    await env.submit(
        "i-2"
    )  # 对账完成后才放行新单（HALT 的持久化由 ModeManager 负责，见 C-04 测试）
    assert broker_orders(env, "i-2") == 1


# ---------- O-05 评审补充：对账门禁必须失败即关闭 ----------


class DisconnectDuringLookup(FakeBroker):
    """对账查单期间连接中断又很快重连，事件泵先处理了断线事件（与对账交错）。

    重连之后对账结束时仍连着线：只有连接代数的检查能判出这次对账作废。
    """

    om: OrderManager | None = None
    armed = True

    async def lookup_order(self, order_ref: str) -> OrderLookup:
        result = await super().lookup_order(order_ref)
        if self.armed and self.om is not None:
            self.armed = False
            self.server.drop_client(self.client_id)
            await self.om.on_broker_event(
                ConnectionEvent(state=ConnectionState.DISCONNECTED, at=self.server._now())
            )
            await self.connect()
        return result


@pytest.mark.asyncio
async def test_disconnect_during_recovery_voids_it(tmp_path: Path) -> None:
    env = Env(tmp_path)
    await env.broker.connect()
    await env.submit()
    await env.pump()
    env.server.drop_client(env.broker.client_id)
    broker = DisconnectDuringLookup(env.server)
    env.use(broker)
    broker.om = env.om
    await env.broker.connect()
    assert await env.om.recover() is False  # 对账期间断过线：结果作废（此时已重新连上）
    assert env.broker.connected
    assert not env.om.recovered and "reconcile_interrupted" in env.discrepancies()
    await env.pump()  # 排队中的 CONNECTED → 重新对账
    assert env.om.recovered


@pytest.mark.asyncio
async def test_lookup_failure_for_unknown_order_fails_recovery(tmp_path: Path) -> None:
    env = Env(tmp_path)
    await env.broker.connect()
    env.server.lose_next_ack()
    await env.submit()
    env.clock.advance(11)
    env.om.check_timeouts()
    env.server.drop_client(env.broker.client_id)
    env.use(LookupDown(env.server))  # 账本里只有一张 UNKNOWN 订单，查单失败
    await env.broker.connect()
    await env.pump()
    assert not env.om.recovered and "reconcile_failed" in env.discrepancies()
    with pytest.raises(SubmissionRefusedError):
        await env.submit("i-2")


@pytest.mark.asyncio
async def test_backup_without_issuance_does_not_resubmit(tmp_path: Path) -> None:
    env = Env(tmp_path)
    await env.broker.connect()
    intent = env.intent()
    decision = env.allow(intent)
    backup = tmp_path / "backup.db"
    shutil.copyfile(tmp_path / "ledger.db", backup)  # 备份里有意图与决定，没有之后的签发
    await env.om.submit_approved_intent(intent, decision)
    await env.pump()
    env.server.drop_client(env.broker.client_id)
    env.repo = LedgerRepository(open_ledger(backup))
    env.use(FakeBroker(env.server))
    await env.broker.connect()
    await env.pump()
    assert env.repo.issuance_for("i-1") is None
    with pytest.raises(SubmissionRefusedError):  # 上游重放同一意图：券商已有这张单，拒绝签发
        await env.om.submit_approved_intent(intent, decision)
    assert broker_orders(env) == 1
    assert "order_at_broker_without_issuance" in env.discrepancies()
    assert env.unresolved == ["i-1"]


class LookupWithoutExecutions(FakeBroker):
    """查单只返回订单、不返回成交（IBKR reqExecutions 只给当天成交，跨日停机即如此）。"""

    async def lookup_order(self, order_ref: str) -> OrderLookup:
        found = await super().lookup_order(order_ref)
        return OrderLookup(order_ref=order_ref, orders=found.orders)


@pytest.mark.asyncio
async def test_fill_quantity_mismatch_fails_recovery_and_escalates(tmp_path: Path) -> None:
    env = Env(tmp_path)
    await env.broker.connect()
    await env.submit()
    await env.pump()
    env.server.drop_client(env.broker.client_id)
    env.server.fill(env.oid(), Decimal(10), Decimal(100))
    env.use(LookupWithoutExecutions(env.server))
    await env.broker.connect()
    await env.pump()
    assert not env.om.recovered
    assert "reconcile_fill_mismatch" in env.discrepancies() and env.unresolved == ["i-1"]


class CancelSaysUnknown(FakeBroker):
    async def cancel_order(
        self, broker_order_id: str, *, allow_protective_stop: bool = False
    ) -> None:
        raise UnknownOrderError(broker_order_id)


@pytest.mark.asyncio
async def test_resent_cancel_refused_by_broker_escalates(tmp_path: Path) -> None:
    env = Env(tmp_path)
    await env.broker.connect()
    await env.submit()
    await env.pump()
    env.server.drop_client(env.broker.client_id)
    await env.pump()
    await env.om.request_cancel("i-1")  # 断线时发出，没到券商
    env.use(CancelSaysUnknown(env.server))
    await env.broker.connect()
    await env.pump()
    assert not env.om.recovered  # 查单说活着、撤单说不存在：事实矛盾
    assert "cancel_unknown_order" in env.discrepancies() and env.unresolved == ["i-1"]


class ReconnectWhileIssuing(FakeBroker):
    """签发取订单号期间断线又重连，且对账已重新完成（事件泵交错）：这张单仍不得落账。"""

    om: OrderManager | None = None
    armed = False

    async def next_order_id(self) -> str:
        oid = await super().next_order_id()
        if self.armed and self.om is not None:
            self.armed = False
            self.server.drop_client(self.client_id)
            await self.om.on_broker_event(
                ConnectionEvent(state=ConnectionState.DISCONNECTED, at=self.server._now())
            )
            await self.connect()
            assert await self.om.recover()
        return oid


@pytest.mark.asyncio
async def test_disconnect_while_issuing_refuses_the_submission(tmp_path: Path) -> None:
    env = Env(tmp_path)
    broker = ReconnectWhileIssuing(env.server)
    env.use(broker)
    broker.om = env.om
    await env.broker.connect()
    await env.pump()
    broker.armed = True
    with pytest.raises(SubmissionRefusedError):
        await env.submit()
    assert env.om.recovered  # 对账已重新完成，靠连接代数判出这张单跨过了断线
    assert env.repo.issuance_for("i-1") is None and broker_orders(env) == 0


@pytest.mark.asyncio
async def test_one_failing_order_does_not_block_cancels_of_others(tmp_path: Path) -> None:
    env = Env(tmp_path)
    await env.broker.connect()
    await env.submit("i-1")
    await env.submit("i-2")
    await env.pump()
    env.server.drop_client(env.broker.client_id)
    await env.pump()
    env.server.fill(env.oid("i-1"), Decimal(10), Decimal(100))  # 第一张：查不到成交 → 成交量不符
    await env.om.request_cancel("i-2")  # 第二张：断线时发出的撤单
    env.use(LookupWithoutExecutions(env.server))
    await env.broker.connect()
    await env.pump()
    assert not env.om.recovered
    assert "reconcile_fill_mismatch" in env.discrepancies()
    assert env.unresolved == ["i-1"]  # 失败记在出问题的那张单上
    assert env.om.status("i-2") is S.CANCELLED  # 撤单照常补发
