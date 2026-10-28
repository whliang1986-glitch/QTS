"""交易核心（S-01a）：tradingd 里把 ModeManager、OMS、风控、对账、运行状态机与急停串起来的唯一入口。

- 启动顺序固定（V4.1 3.2 / R-01 / O-05 移交）：取券商账户 → ModeManager 启动校验
  （持久化的 HALT 不自动恢复；校验失败直接抛出，tradingd 必须退出）→ OMS 向券商查单恢复
  → 启动对账 → 评估运行状态。走完才接受意图与命令；对账发现差异会自行 SAFE HALT，
  核心照常就绪（风控按 HALT 拒绝新增）。
- 串行化：意图、会下单的命令（紧急平仓）、券商回报处理、对账共用一把锁（首期单账户、单 tradingd），
  避免两次评估互相看不到对方而超买超卖，也保证对账前回报已处理完。
- 意图入口：只接受已校验账户的意图；拒绝外部意图冒用保护性止损或紧急平仓的身份（类别或来源标识），
  这两类只能由 OMS 随持仓创建、或由急停命令生成（R-04 / R-05 移交）。
- 命令入口：按 request_id 去重（账本命令日志，ADR-14）；同一 request_id 换了参数即冲突；
  撤保护性止损、紧急平仓执行、采用对账基准这三个人工确认入口按 arch_scan 只能在 server/api 调用，
  暂不经交易核心执行（放宽到交易核心须 Dylan 批准，见 S-01a 任务单）；
  命令结果（含被拒原因）写入命令日志，重试返回原结果。
  恢复交易只走 RunStateMachine.resume（六项门槛）。
- 后台例行：处理券商回报、评估运行状态、盘中 / 日终对账（调度在 S-01d 接入）；
  例行任务出错一律按不可信处理。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from server.domain.account import Account
from server.domain.enums import OrderClass
from server.domain.orders import EMERGENCY_FLATTEN_SOURCE, PROTECTIVE_STOP_SOURCE, OrderIntent
from server.oms.manager import OrderManager
from server.risk.context import LedgerContextProvider
from server.risk.emergency import EmergencyControls, EmergencyError
from server.risk.engine import Outcome, RiskEngine
from server.risk.mode_manager import ModeConfig, ModeError, ModeManager
from server.risk.reconcile import Reconciler, ReconError, ReconKind
from server.risk.run_state import RunStateError, RunStateMachine
from server.storage.ledger import LedgerRepository
from server.storage.logs import log_event

log = logging.getLogger(__name__)

RESERVED_SOURCES = frozenset({PROTECTIVE_STOP_SOURCE, EMERGENCY_FLATTEN_SOURCE})
# 内部生成的意图号命名空间（紧急平仓）：外部意图不得抢占
RESERVED_INTENT_PREFIXES = ("flatten-",)
STOP_COMMAND = "stop_new_orders"
# 命令被拒（确认短语不符、门槛不满足等）：结果照样写入命令日志，重试返回同一结果
REFUSALS = (EmergencyError, ReconError, ModeError, RunStateError)


class IntakeError(Exception):
    """意图或命令在进入风控之前就被拒绝。"""


class NotReadyError(IntakeError):
    """交易核心尚未完成启动：暂时性的，调用方可稍后用同一幂等键重试。"""


@dataclass(frozen=True)
class StartupReport:
    recovered: bool  # OMS 是否已向券商查单完毕（否则拒绝新单）
    reconciled: bool  # 启动对账是否通过（否则已 SAFE HALT）
    run_state: str


@dataclass(frozen=True)
class CommandResult:
    request_id: str
    ok: bool
    detail: Mapping[str, Any]
    replayed: bool  # 是否为重试返回的原结果


class TradingCore:
    def __init__(  # noqa: PLR0913 - 组件全部显式注入（装配在 S-01c）
        self,
        repo: LedgerRepository,
        oms: OrderManager,
        modes: ModeManager,
        *,
        mode_config: ModeConfig,
        broker_account: Callable[[], Awaitable[Account]],
        engine: RiskEngine,
        reconciler: Reconciler,
        run_state: RunStateMachine,
        emergency: EmergencyControls,
        context: LedgerContextProvider | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        actor: str = "tradingd",
    ) -> None:
        self._repo = repo
        self._oms = oms
        self._modes = modes
        self._mode_config = mode_config
        self._broker_account = broker_account
        self._engine = engine
        self._reconciler = reconciler
        self._run_state = run_state
        self._emergency = emergency
        self._context = context
        self._clock = clock
        self._actor = actor
        if not engine.has_run_gate:
            raise ValueError("风控引擎没有接入运行状态门禁（run_gate），交易核心拒绝装配")
        self._lock = asyncio.Lock()
        self._command_lock = asyncio.Lock()
        self._ready = False
        self._starting = False

    # ---------- 启动 ----------

    async def start(self) -> StartupReport:
        """按固定顺序启动；ModeManager 启动校验失败直接抛出（tradingd 必须退出，不得重试）。

        各组件应先报告健康再启动，否则关键组件按“从未报告”算故障，启动即 SAFE HALT（须人工恢复）。
        """
        if self._ready or self._starting:
            raise IntakeError("交易核心已启动或正在启动")
        self._starting = True
        account = await self._broker_account()
        self._modes.start(self._mode_config, account)
        async with self._lock:
            recovered = await self._oms.recover()
            report = await self._reconciler.reconcile(ReconKind.STARTUP)
        assessment = self._run_state.assess()
        self._ready = True
        log_event(
            log,
            logging.WARNING,
            "交易核心启动完成",
            recovered=recovered,
            reconciled=report.clean,
            run_state=assessment.state.value,
        )
        return StartupReport(recovered, report.clean, assessment.state.value)

    @property
    def ready(self) -> bool:
        return self._ready

    # ---------- 意图 ----------

    async def submit_intent(self, intent: OrderIntent) -> Outcome:
        """外部（策略经 IPC）提交的意图：先做入口校验，再在串行锁内交给风控。"""
        self._require_ready()
        if (
            intent.order_class is OrderClass.PROTECTIVE_STOP
            or intent.strategy_id in RESERVED_SOURCES
            or intent.intent_id.startswith(RESERVED_INTENT_PREFIXES)
        ):
            raise IntakeError("外部意图不得使用保护性止损或紧急平仓的类别与来源")
        account = self._modes.verified_account()
        if account is None or intent.account_id != account.account_id:
            raise IntakeError("意图账户不是本进程已校验的账户")
        if intent.expires_at is not None and intent.expires_at <= self._clock():
            # 不信任投递方的过期判断，按 tradingd 自己的时钟再查一次（防追单，V4.1 3.4）
            raise IntakeError("意图已过有效期")
        async with self._lock:
            return await self._engine.submit(intent)

    # ---------- 命令 ----------

    async def command(
        self, request_id: str, name: str, params: Mapping[str, str], *, actor: str
    ) -> CommandResult:
        """执行一条命令（apid 经 IPC 转来）：同一 request_id 只执行一次，重试返回原结果。

        急停（stop_new_orders）走最短路径：启动之前也能执行，不排在其他命令与下单后面——
        只读查重后立即持久化 HALT，再记结果。
        """
        _validate_command(request_id, name, params, actor)
        fingerprint = _fingerprint(name, params, actor)
        if name == STOP_COMMAND:
            return await self._stop_now(request_id, fingerprint, params, actor)
        self._require_ready()
        async with self._command_lock:
            replay = self._replay(request_id, fingerprint)
            if replay is not None:
                return replay
            handler = self._handlers().get(name)
            if handler is None:
                raise IntakeError(f"未知命令：{name}")
            try:
                detail = await handler(params, actor)
                ok = True
            except REFUSALS as exc:
                detail, ok = {"error": str(exc)}, False
            return self._record(request_id, fingerprint, name, ok, detail)

    async def _stop_now(
        self, request_id: str, fingerprint: str, params: Mapping[str, str], actor: str
    ) -> CommandResult:
        replay = self._replay(request_id, fingerprint)  # 只读查重，不排队
        if replay is not None:
            return replay  # 之前那次已持久化 HALT
        self._emergency.stop_new_orders(params.get("reason", ""), actor=actor)
        return self._record(request_id, fingerprint, STOP_COMMAND, True, {})

    def _replay(self, request_id: str, fingerprint: str) -> CommandResult | None:
        existing = self._repo.command_result(request_id)
        if existing is None:
            return None
        if existing.command != fingerprint:
            raise IntakeError("同一 request_id 的命令内容或操作者不同")
        stored = json.loads(existing.result)
        return CommandResult(request_id, stored["ok"], stored["detail"], replayed=True)

    def _record(
        self, request_id: str, fingerprint: str, name: str, ok: bool, detail: dict[str, Any]
    ) -> CommandResult:
        result = json.dumps({"ok": ok, "detail": detail}, ensure_ascii=False, sort_keys=True)
        self._repo.record_command(request_id, fingerprint, result, self._clock())
        log_event(log, logging.WARNING, "命令已执行", command=name, ok=ok)
        return CommandResult(request_id, ok, detail, replayed=False)

    def _handlers(self) -> dict[str, Callable[[Mapping[str, str], str], Awaitable[dict[str, Any]]]]:
        return {
            "cancel_open_orders": self._cancel_open_orders,
            "preview_flatten": self._preview_flatten,
            "reconcile": self._reconcile_now,
            "resume": self._resume,
        }

    async def _cancel_open_orders(self, params: Mapping[str, str], actor: str) -> dict[str, Any]:
        del params
        async with self._lock:
            results = await self._emergency.cancel_open_orders(actor=actor)
        return {"orders": [_cancel_view(r) for r in results]}

    async def _preview_flatten(self, params: Mapping[str, str], actor: str) -> dict[str, Any]:
        del params, actor
        async with self._lock:
            preview = await self._emergency.preview_flatten()
        return {
            "preview_id": preview.preview_id,
            "confirmation": preview.confirmation,
            "lines": [
                {
                    "symbol": x.symbol,
                    "quantity": str(x.quantity),
                    "limit_price": str(x.limit_price),
                    "max_slippage": str(x.max_slippage),
                }
                for x in preview.lines
            ],
            "warnings": list(preview.warnings),
        }

    async def _reconcile_now(self, params: Mapping[str, str], actor: str) -> dict[str, Any]:
        del params, actor
        clean = await self.reconcile(ReconKind.STARTUP)
        return {"clean": clean, "settled": self._reconciler.last_clean()}

    async def _resume(self, params: Mapping[str, str], actor: str) -> dict[str, Any]:
        del params
        async with self._lock:  # 六项门槛读到同一时刻的状态
            change = self._run_state.resume(actor=actor)
        if self._context is not None:
            self._context.reset_reports()  # 人工恢复后，同一张外来单再出现要重新报告
        return {"mode": change.mode.value}

    # ---------- 例行任务（调度在 S-01d） ----------

    async def handle_events(self, events: AsyncIterator[Any]) -> None:
        """处理券商回报流（与意图、对账串行）。回报流结束或出错由调用方重连。"""
        async for event in events:
            async with self._lock:
                try:
                    await self._oms.on_broker_event(event)
                except Exception as exc:
                    # 一条回报（可能是成交）没处理成功：本地订单状态不再可信，先停再报
                    self._halt_untrusted(f"券商回报处理出错：{type(exc).__name__}")
                    raise

    def assess(self) -> str:
        """评估运行状态；出错时尽量持久化 SAFE HALT（写不进去时 ModeManager 已闩锁拒绝）。"""
        try:
            return self._run_state.assess().state.value
        except Exception as exc:
            log_event(log, logging.ERROR, "运行状态评估出错", error=type(exc).__name__)
            self._halt_untrusted(f"运行状态评估出错：{type(exc).__name__}")
            return "SAFE_HALT"

    def _halt_untrusted(self, reason: str) -> None:
        """尽量持久化 SAFE HALT；写不进去时 ModeManager 已闩锁为拒绝，只记日志。"""
        try:
            self._modes.safe_halt(reason, actor=self._actor)
        except Exception as exc:
            log_event(log, logging.ERROR, "SAFE HALT 写入失败", error=type(exc).__name__)

    async def reconcile(self, kind: ReconKind) -> bool:
        """对账：与意图、回报串行；盘中先查证尚无结果的改单。返回是否没有未解释差异。"""
        async with self._lock:
            if kind is ReconKind.INTRADAY:
                try:
                    await self._resolve_pending_amendments()
                except Exception as exc:  # 预处理出错不跳过对账：对账自己会把不一致报出来
                    log_event(log, logging.WARNING, "改单查证预处理出错", error=type(exc).__name__)
            report = await self._reconciler.reconcile(kind)
        return report.clean

    async def _resolve_pending_amendments(self) -> None:
        for intent_id in self._repo.submitted_intents():
            if any(a.result in {None, "UNKNOWN"} for a in self._repo.amendments_for(intent_id)):
                try:
                    await self._oms.resolve_amendments(intent_id)
                except Exception as exc:  # 查证失败：对账照常进行，未决改单会被报出
                    log_event(log, logging.WARNING, "改单查证失败", error=type(exc).__name__)

    def _require_ready(self) -> None:
        if not self._ready:
            raise NotReadyError("交易核心尚未完成启动")


def _validate_command(request_id: Any, name: Any, params: Any, actor: Any) -> None:
    """IPC 传来的 JSON 可能类型不对：入口严格校验，不合格直接拒绝（不执行、不记录）。"""
    if not isinstance(request_id, str) or not request_id.strip():
        raise IntakeError("request_id 必须是非空字符串")
    if not isinstance(name, str) or not isinstance(actor, str):
        raise IntakeError("命令名与操作者必须是字符串")
    if not isinstance(params, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in params.items()
    ):
        raise IntakeError("命令参数的键和值都必须是字符串")


def _fingerprint(name: str, params: Mapping[str, str], actor: str) -> str:
    """命令指纹：命令名 + 规范化参数 + 操作者（同一 request_id 换人或换参数都算冲突）。"""
    body = {"command": name, "params": dict(params), "actor": actor}
    return json.dumps(body, sort_keys=True, ensure_ascii=False)


def _cancel_view(result: Any) -> dict[str, Any]:
    return {
        "intent_id": result.intent_id,
        "status": None if result.status is None else result.status.value,
        "error": result.error,
    }


__all__ = ("CommandResult", "IntakeError", "NotReadyError", "StartupReport", "TradingCore")
