"""急停四级（R-05，V4.1 7.3）：四个独立操作，不合并成一个按钮；每次操作写审计。

1. 停止新增订单：持久化 HALT（ModeManager），重启不自动恢复；
   恢复走 ModeManager.resume 的健康检查与对账门槛。先 HALT 后写审计：审计失败不妨碍 HALT。
   HALT 下风控只放行降低风险的订单：OMS 随持仓创建的保护性止损、本模块的紧急平仓
   （规则照查，见 engine._reduces_risk）；SAFE HALT（状态不可信）下连这两类也不放行。
2. 撤销未成交订单：逐单撤开仓单与减仓单，**保护性止损默认保留**；请求发出不算成功，
   `unconfirmed_cancels()` 列出还没确认终态的订单（只在本进程内跟踪，重启后以 OMS 状态为准）。
   连止损一起撤要另行二次确认（逐字输入确认短语），走 OMS 唯一的止损撤单入口。
3. 减少风险：只按预先批准的规则和流动性约束执行；规则须 Dylan 批准，
   本任务不实现（DEFERRED，见任务单）。
4. 紧急平仓：先预览（每个持仓的数量、保护价、最新价、最大滑点），再逐字输入确认短语执行；
   执行时重新计算：标的或数量与预览不同、任一保护价低于预览时的保护价、或预览过期即拒绝
   （请重新预览）；保护价更高仍满足操作者确认过的价格下限，照常执行。
   用带保护价的限价单：保护价 = 最新价 ×（1 − 当前生效的限价偏离上限），向上取到最小跳动；
   不放开市价单。标的还挂着保护性止损时拒绝平仓（止损与平仓单同时成交会卖成空头）：
   先经第 2 级二次确认撤止损、等撤单确认，再平仓。执行串行化，并扣除已挂的减仓单，
   不会因连点或并发重复卖出。

确认入口（cancel_protective_stops、emergency_flatten）只能在本模块与 server/api 中出现
（arch_scan emergency-confirm）；紧急平仓来源标识只能在 domain 与 risk 中出现
（arch_scan emergency-source）。身份核验在 S 组 API 鉴权时接上；本模块依赖 oms，
api 须经 IPC 调用（import-linter 禁止 api 间接依赖 broker）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from server.domain.enums import OrderClass, OrderType, Side, TimeInForce
from server.domain.order_state import TERMINAL, OrderStatus
from server.domain.orders import EMERGENCY_FLATTEN_SOURCE, OrderIntent
from server.oms.manager import CancelRefusedError, OrderManager
from server.risk.engine import ContextProvider, Outcome, RiskContext, RiskEngine, Subject
from server.risk.mode_manager import ModeManager
from server.risk.parameters import RiskParameters
from server.storage.ledger import LedgerRepository
from server.storage.logs import log_event, record_audit

log = logging.getLogger(__name__)

EMERGENCY_ACTION = "emergency_control"
PROTECTIVE_CANCEL_PHRASE = "确认撤销全部保护性止损"
PREVIEW_TTL = timedelta(seconds=60)


class EmergencyError(Exception):
    """急停操作被拒绝（确认短语不符、计划已变化、上下文不可用等）。"""


@dataclass(frozen=True)
class CancelResult:
    intent_id: str
    order_class: OrderClass
    status: OrderStatus | None  # 撤单请求后的本地状态；None 表示被拒或出错（见 error）
    error: str | None = None


@dataclass(frozen=True)
class FlattenLine:
    symbol: str
    quantity: Decimal
    limit_price: Decimal
    last_price: Decimal

    @property
    def max_slippage(self) -> Decimal:
        """相对最新价的最大不利成交金额（按保护价全数成交估计）。"""
        return (self.last_price - self.limit_price) * self.quantity


@dataclass(frozen=True)
class FlattenPreview:
    preview_id: str
    created_at: datetime
    lines: tuple[FlattenLine, ...]
    confirmation: str  # 执行时必须逐字输入
    warnings: tuple[str, ...]  # 交易时段、流动性、失败风险等提示


@dataclass(frozen=True)
class FlattenResult:
    symbol: str
    outcome: Outcome | None  # 风控结果；None 表示提交时出错（见 error）
    error: str | None = None


WARNINGS = (
    "限价单按保护价挂出，跳空或流动性不足时可能部分成交或不成交",
    "非交易时段挂出的单会等到开盘，届时价格可能已大幅变化",
    "平仓会实现盈亏，且可能产生市场冲击",
    "超过挂单数或每分钟下单上限的标的会被风控拒绝，需稍后重新预览执行剩余部分",
)


class EmergencyControls:
    def __init__(  # noqa: PLR0913 - 依赖全部显式注入
        self,
        repo: LedgerRepository,
        oms: OrderManager,
        modes: ModeManager,
        engine: RiskEngine,
        *,
        context: ContextProvider,
        parameters: Callable[[], RiskParameters | None],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._repo = repo
        self._oms = oms
        self._modes = modes
        self._engine = engine
        self._context = context
        self._parameters = parameters
        self._clock = clock
        self._new_id = new_id
        self._requested: set[str] = set()
        self._previews: dict[str, FlattenPreview] = {}
        self._flatten_lock = asyncio.Lock()

    # ---------- 1. 停止新增订单 ----------

    def stop_new_orders(self, reason: str, *, actor: str) -> None:
        """先持久化 HALT（ModeManager 对输入容错、写入失败即闩锁），再补写急停审计。"""
        try:
            self._modes.halt(reason, actor=actor)
        finally:
            try:
                self._audit("stop_new_orders", actor or "unknown-operator", reason=reason)
            except Exception as exc:  # 审计失败不影响 HALT
                log_event(log, logging.ERROR, "急停审计写入失败", error=type(exc).__name__)

    # ---------- 2. 撤销未成交订单 ----------

    async def cancel_open_orders(self, *, actor: str) -> list[CancelResult]:
        """逐单撤开仓单与减仓单；保护性止损保留。"""
        self._audit("cancel_open_orders", actor)
        return [
            await self._cancel(intent_id, cls)
            for intent_id, cls in self._live_orders()
            if cls is not OrderClass.PROTECTIVE_STOP
        ]

    async def cancel_protective_stops(self, *, actor: str, confirmation: str) -> list[CancelResult]:
        """连保护性止损一起撤：另行二次确认（逐字输入 PROTECTIVE_CANCEL_PHRASE）。"""
        _require_actor(actor)
        if confirmation != PROTECTIVE_CANCEL_PHRASE:
            raise EmergencyError("确认短语不一致，保护性止损未撤销")
        self._audit("cancel_protective_stops", actor)
        return [
            await self._cancel(intent_id, cls)
            for intent_id, cls in self._live_orders()
            if cls is OrderClass.PROTECTIVE_STOP
        ]

    def unconfirmed_cancels(self) -> list[str]:
        """已发出撤单请求、但还没确认终态的订单（请求发出不算成功）。"""
        return [i for i in sorted(self._requested) if self._oms.status(i) not in TERMINAL]

    # ---------- 4. 紧急平仓 ----------

    async def preview_flatten(self) -> FlattenPreview:
        now = self._clock()
        for stale in [k for k, v in self._previews.items() if now - v.created_at > PREVIEW_TTL]:
            del self._previews[stale]
        lines = await self._plan()
        preview_id = self._new_id()
        symbols = "、".join(f"{line.symbol} {line.quantity}" for line in lines) or "无持仓"
        preview = FlattenPreview(
            preview_id=preview_id,
            created_at=self._clock(),
            lines=lines,
            confirmation=f"确认紧急平仓 {symbols}",
            warnings=WARNINGS,
        )
        self._previews[preview_id] = preview
        return preview

    async def emergency_flatten(
        self, preview_id: str, *, actor: str, confirmation: str
    ) -> list[FlattenResult]:
        """按预览执行紧急平仓：确认短语一致、预览未过期、重新计算的计划与预览完全相同。

        串行执行；预览在第一次等待之前即被领取（一次预览只能执行一次，并发重复调用直接拒绝）。
        """
        _require_actor(actor)
        preview = self._previews.get(preview_id)
        if preview is None:
            raise EmergencyError("没有这个平仓预览，请重新预览")
        if confirmation != preview.confirmation:
            raise EmergencyError("确认短语不一致，未执行紧急平仓")
        del self._previews[preview_id]
        if self._clock() - preview.created_at > PREVIEW_TTL:
            raise EmergencyError("平仓预览已过期，请重新预览")
        account_id = self._account_id()
        async with self._flatten_lock:
            fresh = await self._plan()
            if not _still_matches(fresh, preview.lines):
                raise EmergencyError("持仓、挂单或保护价已变化，请重新预览")
            self._audit("emergency_flatten", actor, preview_id=preview_id, lines=str(len(fresh)))
            results = []
            for line in fresh:
                intent = self._flatten_intent(preview_id, account_id, line)
                try:
                    outcome = await self._engine.submit(intent)
                except Exception as exc:
                    log_event(log, logging.ERROR, "紧急平仓提交出错", symbol=line.symbol)
                    results.append(FlattenResult(line.symbol, None, type(exc).__name__))
                else:
                    results.append(FlattenResult(line.symbol, outcome))
            return results

    # ---------- 内部 ----------

    def _live_orders(self) -> list[tuple[str, OrderClass]]:
        out = []
        for intent_id in self._repo.submitted_intents():
            intent = self._repo.get_intent(intent_id)
            if intent is None:  # 外键保证不会发生；真发生了说明账本不可信，不能少算挂单与止损
                raise EmergencyError(f"账本里查不到已签发的意图 {intent_id}，请先对账")
            if self._oms.status(intent_id) not in TERMINAL:
                out.append((intent_id, intent.order_class))
        return out

    async def _cancel(self, intent_id: str, cls: OrderClass) -> CancelResult:
        try:
            if cls is OrderClass.PROTECTIVE_STOP:
                status = await self._oms.request_cancel_protective(intent_id)
            else:
                status = await self._oms.request_cancel(intent_id)
        except CancelRefusedError as exc:
            log_event(log, logging.WARNING, "急停撤单被拒", intent_id=intent_id)
            return CancelResult(intent_id, cls, None, str(exc))
        except Exception as exc:  # 单张出错不中断整轮撤单
            log_event(log, logging.ERROR, "急停撤单出错", intent_id=intent_id)
            return CancelResult(intent_id, cls, None, type(exc).__name__)
        self._requested.add(intent_id)
        return CancelResult(intent_id, cls, status)

    async def _plan(self) -> tuple[FlattenLine, ...]:
        params = self._parameters()
        if params is None:
            raise EmergencyError("风控参数不可用，无法计算平仓保护价")
        account_id = self._account_id()
        context = await self._context.snapshot(Subject.new_order(_snapshot_intent(account_id)))
        if (
            context.positions is None
            or context.open_orders is None
            or context.last_prices is None
            or context.instruments is None
        ):
            raise EmergencyError("持仓、挂单、最新价或标的规格不可用，无法计算平仓计划")
        held: dict[str, Decimal] = {}
        for position in context.positions:
            if position.account_id != account_id:
                raise EmergencyError("持仓不属于已校验的账户，无法计算平仓计划")
            held[position.symbol] = held.get(position.symbol, Decimal(0)) + position.quantity
        if any(quantity < 0 for quantity in held.values()):
            raise EmergencyError("存在负持仓（待对账），不执行紧急平仓")
        stops = self._protected_symbols(context)
        pending = self._pending_reduces(context)
        lines = []
        for symbol in sorted(held):
            quantity = (held[symbol] - pending.get(symbol, Decimal(0))).to_integral_value(
                rounding=ROUND_FLOOR
            )
            if quantity <= 0:
                continue
            if symbol in stops:
                raise EmergencyError(f"{symbol} 还挂着保护性止损：先撤止损并等撤单确认，再平仓")
            last = context.last_prices.get(symbol)
            instrument = context.instruments.get(symbol)
            if last is None or last <= 0 or instrument is None or instrument.tick_size is None:
                raise EmergencyError(f"{symbol} 没有最新价或最小跳动，无法计算保护价")
            raw = last * (Decimal(1) - params["max_limit_deviation"])
            ticks = (raw / instrument.tick_size).to_integral_value(rounding=ROUND_CEILING)
            lines.append(FlattenLine(symbol, quantity, ticks * instrument.tick_size, last))
        return tuple(lines)

    def _protected_symbols(self, context: RiskContext) -> set[str]:
        """还挂着（未确认终态的）保护性止损的标的：券商快照与本地账本取并集。"""
        symbols = {
            o.symbol
            for o in context.open_orders or ()
            if o.order_class is OrderClass.PROTECTIVE_STOP
        }
        for intent_id, cls in self._live_orders():
            intent = self._repo.get_intent(intent_id)
            if cls is OrderClass.PROTECTIVE_STOP and intent is not None:
                symbols.add(intent.symbol)
        return symbols

    def _pending_reduces(self, context: RiskContext) -> dict[str, Decimal]:
        """已挂减仓卖单的剩余数量：快照挂单与本地账本未结束的减仓单按意图去重后合计。"""
        remaining = {
            o.intent_id: (o.symbol, o.remaining_quantity)
            for o in context.open_orders or ()
            if o.order_class is OrderClass.REDUCE
        }
        for intent_id, cls in self._live_orders():
            intent = self._repo.get_intent(intent_id)
            if cls is OrderClass.REDUCE and intent is not None and intent_id not in remaining:
                remaining[intent_id] = (intent.symbol, intent.quantity)  # 保守：按全量未成交计
        pending: dict[str, Decimal] = {}
        for symbol, quantity in remaining.values():
            pending[symbol] = pending.get(symbol, Decimal(0)) + quantity
        return pending

    def _flatten_intent(self, preview_id: str, account_id: str, line: FlattenLine) -> OrderIntent:
        return OrderIntent(
            intent_id=f"flatten-{preview_id}-{line.symbol}",
            created_at=self._clock(),
            strategy_id=EMERGENCY_FLATTEN_SOURCE,
            strategy_version="1",
            account_id=account_id,
            symbol=line.symbol,
            side=Side.SELL,
            quantity=line.quantity,
            order_type=OrderType.LIMIT,
            limit_price=line.limit_price,
            order_class=OrderClass.REDUCE,
            time_in_force=TimeInForce.DAY,
        )

    def _account_id(self) -> str:
        account = self._modes.verified_account()
        if account is None:
            raise EmergencyError("账户未完成启动校验")
        return account.account_id

    def _audit(self, operation: str, actor: str, **detail: str) -> None:
        _require_actor(actor)
        record_audit(
            self._repo,
            action=EMERGENCY_ACTION,
            actor=actor,
            at=self._clock(),
            detail={"operation": operation, **detail},
        )
        log_event(log, logging.WARNING, "急停操作", operation=operation)


def _require_actor(actor: str) -> None:
    if not actor.strip():
        raise EmergencyError("必须写明操作者")


def _still_matches(fresh: tuple[FlattenLine, ...], previewed: tuple[FlattenLine, ...]) -> bool:
    """标的与数量必须完全一致；保护价不得低于预览时（更高仍在操作者确认的下限之上）。"""
    if [(x.symbol, x.quantity) for x in fresh] != [(x.symbol, x.quantity) for x in previewed]:
        return False
    return all(
        new.limit_price >= old.limit_price for new, old in zip(fresh, previewed, strict=True)
    )


def _snapshot_intent(account_id: str) -> OrderIntent:
    """采集账户快照时的占位评估对象（上下文提供者按账户取数，不依赖具体标的）。"""
    return OrderIntent(
        intent_id="emergency-snapshot",
        created_at=datetime(2000, 1, 1, tzinfo=UTC),
        strategy_id=EMERGENCY_FLATTEN_SOURCE,
        strategy_version="1",
        account_id=account_id,
        symbol="SNAPSHOT",
        side=Side.SELL,
        quantity=Decimal(1),
        order_type=OrderType.LIMIT,
        limit_price=Decimal(1),
        order_class=OrderClass.REDUCE,
        time_in_force=TimeInForce.DAY,
    )


__all__: Sequence[str] = (
    "PREVIEW_TTL",
    "PROTECTIVE_CANCEL_PHRASE",
    "CancelResult",
    "EmergencyControls",
    "EmergencyError",
    "FlattenLine",
    "FlattenPreview",
    "FlattenResult",
)
