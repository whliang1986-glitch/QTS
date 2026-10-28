"""guardd 看门狗（S-01c，V4.1 7.6）：tradingd 心跳丢失时，逐单撤销本系统的未成交开仓单。

只撤满足以下**全部**条件的单（任何一条不确定就不撤：guardd 宁可少撤，也不能误撤保护性止损）：
- 券商侧订单类型是限价单（LIMIT）——止损单、认不出的类型一律不碰；
- 有 orderRef，且账本里该 orderRef 对应的意图存在、类别是开仓（OPEN）、方向是买入；
- 账本记录的 broker_order_id 与券商一致、账户一致（防止 orderRef 被复用或冒用）。
不平仓、不下新单、不撤保护性止损、不用全局撤单；撤单不带止损确认参数
（券商适配器对止损单会再拒一次）。
账本只读打开（guardd 不写账本）；撤单结果写自己的日志。心跳恢复后不再动作。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from server.broker.base import BrokerAdapter, BrokerOrderType, OrderRecord
from server.domain.enums import OrderClass, Side
from server.storage.ledger import LedgerRepository
from server.storage.logs import log_event

log = logging.getLogger(__name__)

HEARTBEAT_TIMEOUT = timedelta(seconds=90)  # V4.1 附录：tradingd 心跳超时（guardd）
FUTURE_TOLERANCE = timedelta(seconds=5)  # 心跳时间超前超过它视为不可信


class Heartbeat(Protocol):
    def last(self) -> datetime | None: ...


@dataclass(frozen=True)
class GuardReport:
    alive: bool
    cancelled: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()  # 未成交但不符合条件、没有撤的单
    failed: tuple[str, ...] = ()
    reasons: dict[str, str] = field(default_factory=dict)


class GuardWatchdog:
    def __init__(
        self,
        broker: BrokerAdapter,
        repo: LedgerRepository,
        heartbeat: Heartbeat,
        *,
        timeout: timedelta = HEARTBEAT_TIMEOUT,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if timeout <= timedelta(0):
            raise ValueError("心跳超时必须为正")
        self._broker = broker
        self._repo = repo
        self._heartbeat = heartbeat
        self._timeout = timeout
        self._clock = clock

    def tradingd_alive(self) -> bool:
        beat = self._heartbeat.last()
        if beat is None:
            return False
        age = self._clock() - beat
        return -FUTURE_TOLERANCE <= age <= self._timeout

    async def check(self) -> GuardReport:
        """跑一轮：心跳正常什么都不做；丢失则逐单撤符合条件的开仓单。"""
        if self.tradingd_alive():
            return GuardReport(alive=True)
        log_event(log, logging.ERROR, "tradingd 心跳丢失，guardd 逐单撤销未成交开仓单")
        cancelled: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        reasons: dict[str, str] = {}
        for order in await self._broker.open_orders():
            why = self._why_not(order)
            if why is not None:
                skipped.append(order.broker_order_id)
                reasons[order.broker_order_id] = why
                continue
            try:
                await self._broker.cancel_order(order.broker_order_id)
            except Exception as exc:  # 一张失败不影响其他单；下一轮再试
                failed.append(order.broker_order_id)
                reasons[order.broker_order_id] = type(exc).__name__
                log_event(log, logging.ERROR, "guardd 撤单失败",
                          order=order.broker_order_id, error=type(exc).__name__)  # fmt: skip
                continue
            cancelled.append(order.broker_order_id)
            log_event(log, logging.WARNING, "guardd 已请求撤单", order=order.broker_order_id)
        return GuardReport(False, tuple(cancelled), tuple(skipped), tuple(failed), reasons)

    def _why_not(self, order: OrderRecord) -> str | None:
        """不撤的原因；None 表示可以撤。读账本出错也按“不撤”处理。"""
        if order.order_type is not BrokerOrderType.LIMIT:
            return "不是限价单"
        if order.order_ref is None:
            return "外来订单"
        try:
            intent = self._repo.get_intent(order.order_ref)
            bound = self._repo.intent_for_broker_order(order.broker_order_id)
        except Exception as exc:
            return f"读账本出错：{type(exc).__name__}"
        checks = (
            (intent is not None, "账本里没有这张单"),
            (bound == order.order_ref, "券商订单号与账本不一致"),
            (
                intent is not None
                and intent.order_class is OrderClass.OPEN
                and intent.side is Side.BUY,
                "不是开仓单",
            ),
            (order.side is Side.BUY, "券商侧方向不是买入"),
            (intent is not None and intent.account_id == order.account_id, "账户与账本不一致"),
        )
        return next((why for ok, why in checks if not ok), None)


__all__ = ("FUTURE_TOLERANCE", "HEARTBEAT_TIMEOUT", "GuardReport", "GuardWatchdog", "Heartbeat")
