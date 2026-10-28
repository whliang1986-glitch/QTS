"""风控上下文提供者（R-03）：按评估对象所属账户采集一份 RiskContext 快照。

数据来源全部以协议注入（risk 不得 import broker，券商适配器按结构满足这些协议）：
- 持仓、现金：AccountSource（券商事实）；
- 未结束订单：本地账本里已签发、尚未终态的本账户订单（含 SUBMITTING / UNKNOWN 与撤单中），
  数量与价格取保守值（见 _open_order）；券商侧还有本系统不认识的本账户未结束订单时，
  open_orders 给 None（依赖它的规则全部拒绝）并报告状态不可信（调用方接 ModeManager.safe_halt）；
- 报价：QuoteSource（持仓、挂单与本单涉及的标的）；最新价取自报价；
- 账户同步状态、时钟偏差、开盘净值、策略回撤、止损冷却、是否允许延迟行情：由调用方提供。

失败即关闭：任何来源抛错或给不出，就把对应字段置 None（不是 0、不是空映射），由用到它的规则拒绝；
净值 = 现金合计 + 各持仓 × 最新价（首期领域层只允许美元）；任一持仓没有最新价即为 None。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from server.domain.account import CashBalance, Position
from server.domain.enums import Side
from server.domain.instrument import Instrument
from server.domain.market import Quote
from server.domain.order_state import TERMINAL
from server.oms.manager import OrderManager
from server.risk.engine import OpenOrder, RiskContext, StopCooldown, Subject
from server.risk.limits import RATE_WINDOW
from server.storage.ledger import LedgerRepository
from server.storage.logs import log_event

log = logging.getLogger(__name__)

UNKNOWN_BROKER_ORDER = "券商侧有本系统不认识的未结束订单"
BROKER_ORDER_MISMATCH = "券商侧订单的标的或方向与本地记录不一致"


class AccountSource(Protocol):
    async def positions(self) -> list[Position]: ...

    async def cash(self) -> CashBalance: ...


class BrokerOpenOrder(Protocol):
    """券商侧未结束订单（B-03 必须返回账户内全部未结束订单，含 TWS 手工单与别的 clientId 的单）。"""

    @property
    def broker_order_id(self) -> str: ...

    @property
    def order_ref(self) -> str | None: ...

    @property
    def perm_id(self) -> str | None: ...

    @property
    def account_id(self) -> str: ...

    @property
    def symbol(self) -> str: ...

    @property
    def side(self) -> Side: ...

    @property
    def remaining_quantity(self) -> Decimal: ...

    @property
    def price(self) -> Decimal | None: ...


class BrokerOrderSource(Protocol):
    async def open_orders(self) -> Sequence[BrokerOpenOrder]: ...


class QuoteSource(Protocol):
    async def quotes(self, symbols: Collection[str]) -> Mapping[str, Quote]: ...


class LedgerContextProvider:
    def __init__(  # noqa: PLR0913 - 数据来源全部显式注入
        self,
        repo: LedgerRepository,
        oms: OrderManager,
        *,
        account: AccountSource,
        broker_orders: BrokerOrderSource,
        quotes: QuoteSource,
        instruments: Mapping[str, Instrument],
        account_synced: Callable[[], bool | None],
        clock_skew_ms: Callable[[], Awaitable[int | None]],
        start_of_day_nav: Callable[[str], Decimal | None],
        strategy_drawdown: Callable[[], Mapping[str, Decimal] | None],
        stop_cooldown: Callable[[], Mapping[str, StopCooldown] | None],
        delayed_data_allowed: Callable[[], bool],
        on_untrusted: Callable[[str], None],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repo = repo
        self._oms = oms
        self._account = account
        self._broker_orders = broker_orders
        self._quotes = quotes
        self._instruments = instruments
        self._account_synced = account_synced
        self._clock_skew_ms = clock_skew_ms
        self._start_of_day_nav = start_of_day_nav
        self._strategy_drawdown = strategy_drawdown
        self._stop_cooldown = stop_cooldown
        self._delayed_data_allowed = delayed_data_allowed
        self._on_untrusted = on_untrusted
        self._clock = clock
        self._reported: set[tuple[str, tuple[str, ...]]] = set()

    def reset_reports(self) -> None:
        """人工恢复交易后调用：之前报告过的不可信状态再次出现时要重新报告（R-03 移交）。"""
        self._reported.clear()

    async def snapshot(self, subject: Subject) -> RiskContext:
        account_id = subject.intent.account_id
        positions = await self._fetch("positions", self._account.positions)
        if positions is not None:  # 只看本账户：多账户时别的账户的持仓不计入净值与持仓
            positions = [p for p in positions if p.account_id == account_id]
        cash = await self._fetch("cash", self._account.cash)
        if cash is not None and cash.account_id != account_id:
            cash = None  # 别的账户的现金不能用来算本账户的净值与可用现金
        open_orders = await self._open_orders(account_id)
        # 没有规格的标的（如急停快照的占位对象）不取报价：规格缺失本身就会被 DefaultDeny 拒绝
        symbols = {subject.intent.symbol} & set(self._instruments)
        symbols.update(p.symbol for p in positions or ())
        symbols.update(o.symbol for o in open_orders or ())
        quotes = await self._fetch("quotes", lambda: self._quotes.quotes(sorted(symbols)))
        last_prices = (
            None
            if quotes is None
            else {s: q.last for s, q in quotes.items() if q.symbol == s and q.last is not None}
        )
        clock_skew_ms = await self._fetch("clock_skew", self._clock_skew_ms)
        now = self._clock()  # 取完数据再取时间：报价年龄不会少算取数耗时
        return RiskContext(
            now=now,
            positions=None if positions is None else tuple(positions),
            open_orders=open_orders,
            cash=cash,
            instruments=self._instruments,
            last_prices=last_prices,
            nav=_nav(positions, cash, last_prices),
            start_of_day_nav=self._call("start_of_day_nav", self._start_of_day_nav, account_id),
            strategy_drawdown=self._call("strategy_drawdown", self._strategy_drawdown),
            recent_submissions=self._call("recent_submissions", self._recent_submissions, now),
            stop_cooldown=self._call("stop_cooldown", self._stop_cooldown),
            quotes=quotes,
            clock_skew_ms=clock_skew_ms,
            account_synced=self._call("account_synced", self._account_synced),
            delayed_data_allowed=self._call("delayed_data", self._delayed_data_allowed) is True,
        )

    # ---------- 内部 ----------

    async def _fetch[T](self, name: str, source: Callable[[], Awaitable[T]]) -> T | None:
        try:
            return await source()
        except Exception as exc:
            log_event(
                log, logging.WARNING, "风控上下文取数失败", field=name, error=type(exc).__name__
            )
            return None

    def _call[T, *Ts](self, name: str, source: Callable[[*Ts], T], *args: *Ts) -> T | None:
        try:
            return source(*args)
        except Exception as exc:
            log_event(
                log, logging.WARNING, "风控上下文取数失败", field=name, error=type(exc).__name__
            )
            return None

    async def _open_orders(self, account_id: str) -> tuple[OpenOrder, ...] | None:
        """本账户未结束订单（不可信或取不到为 None）。

        先取券商快照、再读账本：OMS 先落签发记录后下单，所以券商上已有的本系统订单
        在随后读到的账本里一定查得到，不会因读取间隙被误判为外来订单。
        """
        try:
            records = await self._broker_orders.open_orders()
            return self._reconcile([o for o in records if o.account_id == account_id], account_id)
        except _UntrustedError as exc:
            self._report(exc.reason, exc.keys)
        except _TransientError:
            # 本地已终态、券商仍显示未结束：多半是回报时差，本次评估拒绝，交给对账（R-07）判断
            log_event(log, logging.WARNING, "券商未结束订单与本地终态不一致，本次评估不放行")
        except Exception as exc:
            _fetch_failed("open_orders", exc)
        return None

    def _reconcile(
        self, at_broker: Sequence[BrokerOpenOrder], account_id: str
    ) -> tuple[OpenOrder, ...]:
        known = set(self._repo.submitted_intents())
        ours = {
            order.intent_id: order
            for intent_id in known
            if (order := self._open_order(intent_id, account_id)) is not None
        }
        merged = dict(ours)
        for intent_id, record in self._claim(at_broker, known).items():
            local = ours.get(intent_id)
            if local is None:
                intent = self._repo.get_intent(intent_id)
                if intent is None or intent.account_id != account_id:  # 账户归属不一致，不是时差
                    raise _UntrustedError(BROKER_ORDER_MISMATCH, [_order_key(record)])
                raise _TransientError
            if record.symbol != local.symbol or record.side is not local.side:
                raise _UntrustedError(BROKER_ORDER_MISMATCH, [_order_key(record)])
            merged[intent_id] = _more_conservative(local, record)
        return tuple(merged[i] for i in sorted(merged))

    def _claim(
        self, at_broker: Sequence[BrokerOpenOrder], known: set[str]
    ) -> dict[str, BrokerOpenOrder]:
        claim = claim_open_orders(self._repo, at_broker, known)
        if claim.foreign:
            raise _UntrustedError(UNKNOWN_BROKER_ORDER, claim.foreign)
        if claim.duplicates:
            raise _UntrustedError(BROKER_ORDER_MISMATCH, claim.duplicates)
        return claim.claimed

    def _open_order(self, intent_id: str, account_id: str) -> OpenOrder | None:
        """本账户一张未终态订单的保守视图：数量取签发与所有未被拒改单中的最大值减去已成交；
        买单价格取其中最高（占用现金按最坏情况算），卖单价格取最低。"""
        intent = self._repo.get_intent(intent_id)
        if intent is None:
            raise LookupError(f"账本里查不到已签发的意图 {intent_id}")
        if intent.account_id != account_id or self._oms.status(intent_id) in TERMINAL:
            return None
        issued = self._repo.issuance_for(intent_id)
        quantities = [intent.quantity if issued is None else issued.quantity]
        prices = [] if intent.limit_price is None else [intent.limit_price]
        for amendment in self._repo.amendments_for(intent_id):
            if amendment.result != "REJECTED":
                quantities.append(amendment.new_quantity)
                prices.append(amendment.new_price)
        remaining = max(max(quantities) - self._oms.filled_quantity(intent_id), Decimal(0))
        price = None
        if prices:
            price = max(prices) if intent.side is Side.BUY else min(prices)
        return OpenOrder(
            intent_id=intent_id,
            symbol=intent.symbol,
            side=intent.side,
            order_class=intent.order_class,
            remaining_quantity=remaining,
            limit_price=price,
        )

    def _recent_submissions(self, now: datetime) -> tuple[datetime, ...]:
        since = now - RATE_WINDOW
        times = []
        for intent_id in self._repo.submitted_intents():
            issued = self._repo.issuance_for(intent_id)
            if issued is not None:
                at = datetime.fromisoformat(issued.issued_at)
                if at > since:
                    times.append(at)
        return tuple(times)

    def _report(self, reason: str, broker_order_ids: list[str]) -> None:
        """报告状态不可信（调用方接 SAFE HALT）。同一原因、同一批券商订单只报一次；
        回调失败不记为已报告，下次评估重试。出现新的订单（例如人工恢复后又来一张外来单）会再报。"""
        log_event(log, logging.ERROR, "风控上下文发现状态不可信", reason=reason)
        key = (reason, tuple(broker_order_ids))
        if key in self._reported:
            return
        try:
            self._on_untrusted(f"{reason}：{'、'.join(broker_order_ids)}")
        except Exception as exc:  # 报告失败不影响本次拒绝（open_orders 已是 None）
            log_event(log, logging.ERROR, "状态不可信报告失败", error=type(exc).__name__)
            return
        self._reported.add(key)


def _more_conservative(local: OpenOrder, record: BrokerOpenOrder) -> OpenOrder:
    """券商事实与本地视图取更保守者：剩余量取大；买单价取高、卖单价取低。

    本地没有限价（止损单）就保持没有；买单券商侧缺价按缺价处理（占用现金无法估计，由规则拒绝）。
    """
    remaining = max(local.remaining_quantity, record.remaining_quantity)
    price = local.limit_price
    if price is not None:
        if record.price is None:
            price = None if local.side is Side.BUY else price
        elif local.side is Side.BUY:
            price = max(price, record.price)
        else:
            price = min(price, record.price)
    if remaining != local.remaining_quantity or price != local.limit_price:
        log_event(
            log,
            logging.WARNING,
            "券商订单数量或价格与本地不一致，按更保守者计",
            intent_id=local.intent_id,
        )
    return OpenOrder(
        intent_id=local.intent_id,
        symbol=local.symbol,
        side=local.side,
        order_class=local.order_class,
        remaining_quantity=remaining,
        limit_price=price,
    )


def owned_by_us(
    repo: LedgerRepository, broker_order_id: str | None, order_ref: str | None, perm_id: str | None
) -> bool:
    """券商侧订单或成交是否属于本系统：与 OMS 回报认领同一口径（manager._intent_for）。

    orderRef 必须存在，且券商订单号登记在同一意图名下；permId 双方都有时必须一致。
    """
    if order_ref is None or broker_order_id is None:
        return False
    if repo.intent_for_broker_order(broker_order_id) != order_ref:
        return False
    known_perm = repo.perm_id_for(order_ref)
    return perm_id is None or known_perm is None or perm_id == known_perm


@dataclass(frozen=True)
class OrderClaim:
    claimed: dict[str, BrokerOpenOrder]  # 意图 → 券商订单
    foreign: list[str]  # 外来订单键（排序）
    duplicates: list[str]  # 同一意图有两张及以上券商订单的订单键（排序）


def claim_open_orders(
    repo: LedgerRepository, at_broker: Sequence[BrokerOpenOrder], known: set[str]
) -> OrderClaim:
    """把券商未结束订单归到本系统已签发的意图；认不出的是外来订单，同一意图多张是不一致。"""
    claimed: dict[str, BrokerOpenOrder] = {}
    foreign: list[str] = []
    duplicates: set[str] = set()
    for record in at_broker:
        ref = record.order_ref
        if (
            ref is None
            or ref not in known
            or not owned_by_us(repo, record.broker_order_id, ref, record.perm_id)
        ):
            foreign.append(_order_key(record))
        elif ref in claimed:
            duplicates |= {_order_key(claimed[ref]), _order_key(record)}
        else:
            claimed[ref] = record
    return OrderClaim(claimed, sorted(foreign), sorted(duplicates))


class _UntrustedError(Exception):
    def __init__(self, reason: str, keys: list[str]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.keys = keys


class _TransientError(Exception):
    pass


def _order_key(record: BrokerOpenOrder) -> str:
    """去重与报告用的券商订单键：订单号加 permId。

    外来单的订单号可能为 0 或与别的 clientId 重复，只用订单号去重会漏报新的外来单。
    """
    if record.perm_id is None:
        return record.broker_order_id
    return f"{record.broker_order_id}/{record.perm_id}"


def _fetch_failed(field: str, exc: Exception) -> None:
    log_event(log, logging.WARNING, "风控上下文取数失败", field=field, error=type(exc).__name__)


def _nav(
    positions: Sequence[Position] | None,
    cash: CashBalance | None,
    last_prices: Mapping[str, Decimal] | None,
) -> Decimal | None:
    if positions is None or cash is None or last_prices is None:
        return None
    total = cash.total
    for position in positions:
        last = last_prices.get(position.symbol)
        if last is None:
            return None
        total += position.quantity * last
    return total


__all__ = (
    "AccountSource",
    "BrokerOpenOrder",
    "BrokerOrderSource",
    "LedgerContextProvider",
    "QuoteSource",
)
