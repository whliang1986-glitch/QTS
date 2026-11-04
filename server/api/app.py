"""apid 的 FastAPI 应用（S-02，V4.1 10.1）：/v1 查询、命令与事件流。

- 只读：查询都来自账本的只读连接（ledger.db 只有 tradingd 能写，V4.1 5.6）；
  账本读不了一律 503，不拿空列表冒充“没有订单”（失败即关闭）。
- 命令：白名单 + 参数键校验后经本机通道交给 tradingd（commands.IpcCommandGateway）；
  操作者取认证结果；结果语义见 commands 模块与 docs/tasks/S-02.md。
- 认证：每个路由与 WebSocket 都要认证（auth.Authenticator；生产用 api.db 令牌，S-03）；
  viewer 不能发命令；同一来源认证失败过多即限流（429）。
- 高危命令须先申请二次确认（security.ConfirmationBook，S-03），tradingd 的确认短语另行核对。
- 审计（S-03）：认证失败、限流、确认签发与拒绝、命令提交与结果写入 api.db；
  命令提交的审计写不进去就不转发（503，失败即关闭）。审计里不记参数值（可能含确认短语）。
- 告警与审计（S-05a）：告警只读自 alerts.db（guardd 写），读不了 503（界面显示“无法获取告警”，
  不显示“没有告警”）；确认只有 owner 能发，操作者取令牌身份，确认请求与审计同一事务写入 api.db，
  由 guardd 执行；已确认的再确认不重复记录，已解除的返回 409。审计查询只给 owner。
- 生产不开放 /docs 与 /openapi.json（expose_docs=False）；OpenAPI 由 scripts/gen_api_types.py 导出。
"""

# 不用 from __future__ import annotations：FastAPI 要在定义时解析路由里局部的 Annotated 依赖别名
import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Annotated, Any, Protocol

from fastapi import Depends, FastAPI, Path, Query, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect, WebSocketState

from server.api.auth import Authenticator, Principal, Role, bearer_token
from server.api.commands import (
    CATALOG,
    CommandError,
    CommandGateway,
    CommandSpec,
    NotDeliveredError,
    OutcomeUnknownError,
    Tier,
    check_command,
)
from server.api.models import (
    AckResponse,
    AlertList,
    AlertView,
    AuditEntry,
    AuditList,
    CommandCatalog,
    CommandInfo,
    CommandRequest,
    CommandResponse,
    ConfirmationRequest,
    ConfirmationResponse,
    ErrorResponse,
    ExecutionView,
    HaltView,
    HistoryEntry,
    OrderDetail,
    OrderEventView,
    OrderList,
    OrderView,
    RevokeRequest,
    RevokeResponse,
    StatusView,
    StreamAuth,
    StreamError,
    StreamHello,
    StreamOrderEvent,
    StreamStatus,
    WhoAmI,
)
from server.api.security import (
    Audit,
    ConfirmationBook,
    ConfirmationError,
    FailureLimiter,
    TokenAdmin,
)
from server.domain.orders import Execution, OrderIntent
from server.guard.alerts import CODES, LANGS, Alert, Level, render
from server.storage.alertsdb import StoredAlert
from server.storage.apidb import ApiDbError, TokenNotFoundError
from server.storage.ledger import LedgerRepository, OrderEventRow, OrderSummary
from server.storage.logs import log_event

log = logging.getLogger(__name__)

API_PREFIX = "/v1"
STREAM_BATCH = 200
# WebSocket 关闭码（4000–4999 为应用自定义）
WS_UNAUTHORIZED = 4401
WS_BAD_MESSAGE = 4400


class AckRequests(Protocol):
    """记一条告警确认请求，同一事务写审计；写不进去必须抛出。"""

    def request_ack(self, alert_id: int, alert_key: str, store_id: str, *, actor: str) -> None: ...

    def ack_requested(self, alert_id: int, store_id: str) -> bool: ...


class AlertSource(Protocol):
    """只读告警记录（alerts.db）；读不了必须抛出。"""

    def recent(self, limit: int) -> list[StoredAlert]: ...

    def one(self, alert_id: int) -> tuple[str, StoredAlert | None]:
        """(alerts.db 的库编号, 那次发生)。"""
        ...


class AuditLog(Protocol):
    def recent_audits(
        self, limit: int, before: int | None = None
    ) -> list[tuple[int, str, str, str, dict[str, str]]]: ...


@dataclass(frozen=True)
class ApiDeps:
    authenticator: Authenticator
    ledger: Callable[[], LedgerRepository]  # 只读账本；打开失败抛出（按 503 处理）
    gateway: CommandGateway
    audit: Audit  # 生产用 api.db；必须显式给出（不默认落到内存里）
    tokens: TokenAdmin | None = None  # 在线吊销；None 表示不提供
    alerts: AlertSource | None = None
    acks: AckRequests | None = None
    audit_log: AuditLog | None = None
    limiter: FailureLimiter = field(default_factory=FailureLimiter)
    confirmations: ConfirmationBook = field(default_factory=ConfirmationBook)
    stream_interval: float = 1.0  # 事件流轮询账本的间隔（秒）
    auth_timeout: float = 5.0  # WebSocket 连上后必须在这段时间内认证
    expose_docs: bool = False


class ApiError(Exception):
    def __init__(self, status: int, error: str, *, retry_same: bool = False) -> None:
        super().__init__(error)
        self.status = status
        self.error = error
        self.retry_same = retry_same


def _error(status: int, error: str, *, retry_same: bool = False) -> JSONResponse:
    body = ErrorResponse(error=error, retry_same_request_id=retry_same)
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return JSONResponse(body.model_dump(), status_code=status, headers=headers)


ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 422, 429, 503, 504)
}


def create_app(deps: ApiDeps) -> FastAPI:  # noqa: PLR0915 - 路由集中在一处，便于审查
    docs = "/docs" if deps.expose_docs else None
    app = FastAPI(
        title="QTS apid",
        version="1",
        docs_url=docs,
        redoc_url=None,
        openapi_url="/openapi.json" if deps.expose_docs else None,
    )
    bearer = HTTPBearer(auto_error=False)  # 只为在 OpenAPI 里声明认证方式；校验在 principal()

    @app.exception_handler(ApiError)
    async def on_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return _error(exc.status, exc.error, retry_same=exc.retry_same)

    @app.exception_handler(RequestValidationError)
    async def on_invalid(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # 只给出出错位置，不回显请求内容（里面可能有确认短语）
        where = sorted({".".join(str(p) for p in e.get("loc", ())) for e in exc.errors()})
        return _error(422, f"请求格式不合法：{', '.join(where)}")

    def principal(request: Request, _scheme: Annotated[Any, Depends(bearer)] = None) -> Principal:
        source = request.client.host if request.client is not None else "unknown"
        return _login(deps, source, bearer_token(request.headers.get("authorization")))

    def owner(who: Annotated[Principal, Depends(principal)]) -> Principal:
        if who.role is not Role.OWNER:
            raise ApiError(403, "只有 owner 能发命令")
        return who

    def ledger() -> LedgerRepository:
        try:
            return deps.ledger()
        except Exception as exc:
            _ledger_failed(exc)
            raise ApiError(503, "账本不可读") from exc

    Who = Annotated[Principal, Depends(principal)]  # noqa: N806 - 类型别名
    Ledger = Annotated[LedgerRepository, Depends(ledger)]  # noqa: N806

    @app.get(f"{API_PREFIX}/whoami", responses=ERRORS)
    def whoami(who: Who) -> WhoAmI:
        return WhoAmI(name=who.name, role=who.role.value)

    @app.get(f"{API_PREFIX}/status", responses=ERRORS)
    def status(_who: Who, repo: Ledger) -> StatusView:
        return _read(lambda: status_view(repo))

    @app.get(f"{API_PREFIX}/orders", responses=ERRORS)
    def orders(
        _who: Who, repo: Ledger, limit: Annotated[int, Query(ge=1, le=500)] = 50
    ) -> OrderList:
        return _read(lambda: OrderList(orders=[order_view(s) for s in repo.recent_orders(limit)]))

    @app.get(f"{API_PREFIX}/orders/{{intent_id}}", responses=ERRORS)
    def order(
        _who: Who,
        repo: Ledger,
        intent_id: Annotated[str, Path(pattern=r"^\S{1,128}$")],
    ) -> OrderDetail:
        detail = _read(lambda: order_detail(repo, intent_id))
        if detail is None:
            raise ApiError(404, "没有这个订单")
        return detail

    @app.get(f"{API_PREFIX}/commands/catalog", responses=ERRORS)
    def catalog(_who: Who) -> CommandCatalog:
        return CommandCatalog(
            commands=[
                CommandInfo(name=s.name, tier=s.tier, params=sorted(s.params), summary=s.summary)
                for s in CATALOG.values()
            ]
        )

    Owner = Annotated[Principal, Depends(owner)]  # noqa: N806

    @app.post(f"{API_PREFIX}/commands/confirmations", responses=ERRORS)
    def confirm(body: ConfirmationRequest, who: Owner) -> ConfirmationResponse:
        """高危命令的二次确认：返回短时有效的确认号，绑定使用者、命令名与参数。"""
        spec = _checked(body.name, body.params)
        if spec.tier is not Tier.HIGH:
            raise ApiError(422, "只有高危命令需要二次确认")
        try:
            confirmation_id, expires = deps.confirmations.issue(who, body.name, body.params)
        except ConfirmationError as exc:
            raise ApiError(429, str(exc)) from exc
        _audit_required(deps.audit, "confirmation_issued", who.name, {"name": body.name})
        return ConfirmationResponse(
            confirmation_id=confirmation_id, name=body.name, expires_at=expires.isoformat()
        )

    @app.post(f"{API_PREFIX}/tokens/{{token_id}}/revoke", responses=ERRORS)
    def revoke(
        body: RevokeRequest,
        who: Owner,
        token_id: Annotated[str, Path(pattern=r"^[0-9a-f]{16}$")],
    ) -> RevokeResponse:
        """在线吊销令牌（owner）：立即生效，可以吊销自己的令牌。"""
        if deps.tokens is None:
            raise ApiError(404, "未启用在线吊销")
        try:
            deps.tokens.revoke(token_id, actor=who.name, reason=body.reason)
        except TokenNotFoundError as exc:
            raise ApiError(404, "没有这个令牌") from exc
        except ApiDbError as exc:
            log_event(log, logging.ERROR, "在线吊销失败", error=type(exc).__name__)
            raise ApiError(503, "吊销未完成：api.db 写入失败，请重试") from exc
        return RevokeResponse(token_id=token_id, revoked=True)

    @app.post(f"{API_PREFIX}/commands", responses=ERRORS)
    async def command(body: CommandRequest, who: Owner) -> CommandResponse:
        spec = _checked(body.name, body.params)
        if spec.tier is Tier.HIGH:
            await asyncio.to_thread(_confirmed, deps, body, who)
        elif body.confirmation_id is not None:
            raise ApiError(422, "这条命令不需要二次确认")
        summary = {"request_id": body.request_id, "name": body.name,
                   "param_keys": ",".join(sorted(body.params))}  # fmt: skip
        await asyncio.to_thread(_audit_required, deps.audit, "command_submitted", who.name, summary)
        outcome = "error"
        try:
            answer = await deps.gateway.send(
                body.request_id, body.name, body.params, actor=who.name
            )
            outcome = _outcome(answer.executed, answer.ok, answer.refused)
        except NotDeliveredError as exc:
            outcome = "not_delivered"
            raise ApiError(503, "tradingd 不可用，命令未发出，可以重试") from exc
        except OutcomeUnknownError as exc:
            outcome = "unknown"
            raise ApiError(
                504, "命令已发出但结果未知：请用同一 request_id 重试查询原结果", retry_same=True
            ) from exc
        finally:
            await asyncio.to_thread(
                _audit_best_effort, deps.audit, "command_outcome", who.name,
                {**summary, "outcome": outcome},
            )  # fmt: skip
        if not answer.executed:
            if answer.refused:
                raise ApiError(409, answer.error or "命令被 tradingd 拒绝")
            raise ApiError(
                503,
                f"tradingd 未确认执行结果（{answer.error}）："
                "请先查看状态，再用同一 request_id 重试",
                retry_same=True,
            )
        return CommandResponse(
            request_id=body.request_id, ok=answer.ok, replayed=answer.replayed,
            detail=dict(answer.detail),
        )  # fmt: skip

    def stored[T](query: Callable[[AlertSource], T]) -> T:
        if deps.alerts is None:
            raise ApiError(503, "无法获取告警")
        try:
            return query(deps.alerts)
        except Exception as exc:  # 读不了就 503：不能拿空列表冒充“没有告警”
            log_event(log, logging.ERROR, "读取告警失败", error=type(exc).__name__)
            raise ApiError(503, "无法获取告警") from exc

    @app.get(f"{API_PREFIX}/alerts", responses=ERRORS)
    def alerts(_who: Who, limit: Annotated[int, Query(ge=1, le=500)] = 200) -> AlertList:
        return AlertList(alerts=[alert_view(a) for a in stored(lambda s: s.recent(limit))])

    @app.post(f"{API_PREFIX}/alerts/{{alert_id}}/ack", responses=ERRORS)
    def ack(who: Owner, alert_id: Annotated[int, Path(ge=1)]) -> AckResponse:
        """确认告警：只停止重复提醒，不解除风险（V4.1 10.3）。操作者取令牌身份。"""
        if deps.acks is None:
            raise ApiError(404, "未启用网页确认")
        store_id, found = stored(lambda s: s.one(alert_id))
        if found is None:
            raise ApiError(404, "没有这条告警")
        if found.resolved_at is not None:
            raise ApiError(409, "告警已解除，无需确认")
        if found.acked_by is not None:
            return AckResponse(alert_id=alert_id, requested=False, already=True)
        try:
            if deps.acks.ack_requested(alert_id, store_id):  # 已提交、guardd 还没执行：不重复记录
                return AckResponse(alert_id=alert_id, requested=True, already=False)
            deps.acks.request_ack(alert_id, found.key, store_id, actor=who.name)
        except Exception as exc:
            log_event(log, logging.ERROR, "告警确认请求写入失败", error=type(exc).__name__)
            raise ApiError(503, "确认未记录：api.db 写入失败，请重试") from exc
        return AckResponse(alert_id=alert_id, requested=True, already=False)

    @app.get(f"{API_PREFIX}/audit", responses=ERRORS)
    def audit(
        _who: Owner,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        before: Annotated[int | None, Query(ge=1)] = None,
    ) -> AuditList:
        if deps.audit_log is None:
            raise ApiError(503, "审计记录不可用")
        try:  # 记录损坏（详情不是字符串等）同样按 503，不返回部分结果
            return AuditList(entries=[
                AuditEntry(seq=s, at=at, action=a, actor=who, detail=d)
                for s, at, a, who, d in deps.audit_log.recent_audits(limit, before)
            ])  # fmt: skip
        except Exception as exc:
            log_event(log, logging.ERROR, "读取审计失败", error=type(exc).__name__)
            raise ApiError(503, "无法获取审计记录") from exc

    @app.websocket(f"{API_PREFIX}/stream")
    async def stream(ws: WebSocket) -> None:
        await ws.accept()
        try:
            await _stream(ws, deps)
        except WebSocketDisconnect:
            return
        except Exception as exc:  # 发送途中对端断开等：关闭连接，不影响其他连接
            log_event(log, logging.WARNING, "事件流异常结束", error=type(exc).__name__)
            await _close_with_error(ws, "事件流出错", 1011)

    return app


def alert_view(item: StoredAlert) -> AlertView:
    text = {lang: _text(item, lang) for lang in LANGS}
    level = item.level if item.level in {"P0", "P1", "P2", "P3"} else "P0"
    return AlertView(
        alert_id=item.alert_id, key=item.key, level=level, code=item.code,  # type: ignore[arg-type]
        text=text, raised_at=item.raised_at, acked_by=item.acked_by, acked_at=item.acked_at,
        resolved_at=item.resolved_at,
    )  # fmt: skip


def _text(item: StoredAlert, lang: str) -> str:
    if item.code not in CODES or item.level not in Level.__members__:
        return f"[{item.level}] {item.code} {dict(item.params)}"  # 代码已下线：原样显示，不丢
    return render(Alert(Level[item.level], item.code, item.params, key=item.key), lang)


def _checked(name: str, params: dict[str, str]) -> CommandSpec:
    try:
        return check_command(name, params)
    except CommandError as exc:
        raise ApiError(422, str(exc)) from exc


def _confirmed(deps: ApiDeps, body: CommandRequest, who: Principal) -> None:
    if body.confirmation_id is None:
        _audit_best_effort(
            deps.audit, "confirmation_refused", who.name,
            {"request_id": body.request_id, "name": body.name, "reason": "缺少二次确认"},
        )  # fmt: skip
        raise ApiError(403, "高危命令须先申请二次确认（/v1/commands/confirmations）")
    try:
        deps.confirmations.use(body.confirmation_id, who, body.name, body.params, body.request_id)
    except ConfirmationError as exc:
        _audit_best_effort(
            deps.audit, "confirmation_refused", who.name,
            {"request_id": body.request_id, "name": body.name, "reason": str(exc)},
        )  # fmt: skip
        raise ApiError(403, str(exc)) from exc


def _outcome(executed: bool, ok: bool, refused: bool) -> str:
    if executed:
        return "executed_ok" if ok else "executed_refused_by_rules"
    return "refused" if refused else "temporary_failure"


def _audit_required(audit: Audit, action: str, actor: str, detail: dict[str, str]) -> None:
    """必须落盘的审计：写不进去即拒绝（503）。"""
    try:
        audit.audit(action, actor, detail)
    except Exception as exc:
        log_event(log, logging.ERROR, "审计写入失败，拒绝操作", action=action,
                  error=type(exc).__name__)  # fmt: skip
        raise ApiError(503, "审计不可用，操作未执行") from exc


def _audit_best_effort(audit: Audit, action: str, actor: str, detail: dict[str, str]) -> None:
    """事后的审计（命令已发出、认证已拒绝）：写不进去只记日志，不改变结果。"""
    try:
        audit.audit(action, actor, detail)
    except Exception as exc:
        log_event(log, logging.ERROR, "审计写入失败", action=action, error=type(exc).__name__)


def _login(deps: ApiDeps, source: str, token: str | None) -> Principal:
    """认证一次请求：限流 → 令牌 → 失败计数与审计。令牌本身不进日志与审计。"""
    if deps.limiter.blocked(source):
        raise ApiError(429, "认证失败次数过多，请稍后再试")
    found = None if token is None else _authenticate(deps.authenticator, token)
    if found is not None:
        return found
    reason = "缺少令牌" if token is None else "令牌无效"
    throttled = deps.limiter.fail(source)
    _audit_best_effort(deps.audit, "auth_failed", "anonymous", {"source": source, "reason": reason})
    if throttled:
        _audit_best_effort(deps.audit, "auth_throttled", "anonymous", {"source": source})
    raise ApiError(401, reason)


def _authenticate(authenticator: Authenticator, token: str) -> Principal | None:
    try:
        found = authenticator.authenticate(token)
    except Exception as exc:  # 认证服务出错：拒绝（不当作通过）
        log_event(log, logging.ERROR, "认证出错，拒绝", error=type(exc).__name__)
        raise ApiError(503, "认证服务不可用") from exc
    return found if isinstance(found, Principal) else None


def _ledger_failed(exc: BaseException) -> None:
    log_event(log, logging.ERROR, "读取账本失败", error=type(exc).__name__)


def _read[T](query: Callable[[], T]) -> T:
    try:
        return query()
    except ApiError:
        raise
    except Exception as exc:  # 账本读错、记录损坏：503，不返回部分结果
        _ledger_failed(exc)
        raise ApiError(503, "账本不可读") from exc


# ---------- 视图 ----------


def _dec(value: Decimal) -> str:
    """十进制定点字符串，不出现指数形式（1E+2 → "100"）。"""
    return format(value, "f")


def status_view(repo: LedgerRepository) -> StatusView:
    change = repo.latest_mode()
    if change is None:
        return StatusView(mode=None, halt=None, changed_at=None, changed_by=None)
    halt = change.halt
    return StatusView(
        mode=change.mode,
        halt=HaltView(
            halted=halt.halted, reason=halt.reason,
            since=None if halt.since is None else halt.since.isoformat(), set_by=halt.set_by,
        ),
        changed_at=change.changed_at,
        changed_by=change.actor,
    )  # fmt: skip


def _intent_view(intent: OrderIntent, status: Any, updated_at: str | None) -> OrderView:
    return OrderView(
        intent_id=intent.intent_id, created_at=intent.created_at.isoformat(),
        strategy_id=intent.strategy_id, strategy_version=intent.strategy_version,
        symbol=intent.symbol, side=intent.side, quantity=_dec(intent.quantity),
        order_type=intent.order_type,
        limit_price=None if intent.limit_price is None else _dec(intent.limit_price),
        stop_price=None if intent.stop_price is None else _dec(intent.stop_price),
        order_class=intent.order_class, time_in_force=intent.time_in_force,
        expires_at=None if intent.expires_at is None else intent.expires_at.isoformat(),
        status=status, updated_at=updated_at,
    )  # fmt: skip


def order_view(summary: OrderSummary) -> OrderView:
    return _intent_view(summary.intent, summary.status, summary.updated_at)


def _execution_view(e: Execution) -> ExecutionView:
    return ExecutionView(
        execution_id=e.execution_id, symbol=e.symbol, side=e.side, quantity=_dec(e.quantity),
        price=_dec(e.price), commission=_dec(e.commission), executed_at=e.executed_at.isoformat(),
    )  # fmt: skip


def order_detail(repo: LedgerRepository, intent_id: str) -> OrderDetail | None:
    intent = repo.get_intent(intent_id)
    if intent is None:
        return None
    history = repo.order_history(intent_id)
    entries = [
        HistoryEntry(kind=event.kind, status=state, occurred_at=event.occurred_at.isoformat())
        for event, state in history
    ]
    last = entries[-1] if entries else None
    return OrderDetail(
        order=_intent_view(
            intent,
            None if last is None else last.status,
            None if last is None else last.occurred_at,
        ),
        history=entries,
        executions=[_execution_view(e) for e in repo.effective_executions_for(intent_id)],
    )


def event_view(row: OrderEventRow) -> OrderEventView:
    return OrderEventView(
        seq=row.seq, intent_id=row.intent_id, kind=row.kind, status=row.status,
        occurred_at=row.occurred_at,
    )  # fmt: skip


# ---------- 事件流 ----------


async def _stream(ws: WebSocket, deps: ApiDeps) -> None:
    """认证 → hello → 每隔 stream_interval 推送变化的状态与新订单事件。

    每轮都重新核验令牌（吊销即断开，S-03）；账本读不了先发 error 再关闭（界面显示“未知”）。
    """
    auth = await _stream_auth(ws, deps)
    if auth is None:
        return
    token, who, after = auth
    try:
        repo = await asyncio.to_thread(deps.ledger)
        latest = await asyncio.to_thread(repo.last_order_event_seq)
    except Exception as exc:
        _ledger_failed(exc)
        await _close_with_error(ws, "账本不可读", 1011)
        return
    if after is not None and after > latest:
        # 游标超过账本现有事件（例如账本从备份恢复、连错实例）：不能静默跳过之后的事件
        await _close_with_error(ws, "续读游标超过账本现有事件，请重新加载", WS_BAD_MESSAGE)
        return
    cursor = latest if after is None else after
    await ws.send_json(
        StreamHello(name=who.name, role=who.role.value, cursor=cursor).model_dump(mode="json")
    )
    disconnected = asyncio.ensure_future(_wait_disconnect(ws))
    last: StatusView | None = None
    try:
        while True:
            try:
                # 认证与账本查询都放到线程里：慢盘或认证 I/O 不阻塞事件循环（命令路由共用它）
                again = await asyncio.to_thread(_authenticate, deps.authenticator, token)
            except ApiError:
                again = None
            if again != who:
                await _close_with_error(ws, "令牌已失效", WS_UNAUTHORIZED)
                return
            try:
                current = await asyncio.to_thread(status_view, repo)
                rows = await asyncio.to_thread(repo.order_events_after, cursor, STREAM_BATCH)
            except Exception as exc:
                _ledger_failed(exc)
                await _close_with_error(ws, "账本不可读", 1011)
                return
            if current != last:
                await ws.send_json(StreamStatus(status=current).model_dump(mode="json"))
                last = current
            for row in rows:
                event = StreamOrderEvent(event=event_view(row))
                await ws.send_json(event.model_dump(mode="json"))
                cursor = row.seq
            if len(rows) == STREAM_BATCH:
                continue  # 积压：不等间隔，接着读
            done, _ = await asyncio.wait({disconnected}, timeout=deps.stream_interval)
            if done:
                return
    finally:
        disconnected.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await disconnected


async def _stream_auth(ws: WebSocket, deps: ApiDeps) -> tuple[str, Principal, int | None] | None:
    try:
        raw = await asyncio.wait_for(ws.receive_text(), deps.auth_timeout)
    except TimeoutError:
        await _close_with_error(ws, "未在限定时间内认证", WS_UNAUTHORIZED)
        return None
    try:
        message = StreamAuth.model_validate(json.loads(raw))
    except (ValueError, ValidationError):  # JSONDecodeError 是 ValueError
        await _close_with_error(ws, "第一条消息必须是认证消息", WS_BAD_MESSAGE)
        return None
    source = ws.client.host if ws.client is not None else "unknown"
    try:
        who = await asyncio.to_thread(_login, deps, source, message.token)
    except ApiError as exc:
        await _close_with_error(ws, exc.error, WS_UNAUTHORIZED)
        return None
    return message.token, who, message.after


async def _wait_disconnect(ws: WebSocket) -> None:
    """认证之后客户端不该再发消息：收到断开就结束；收到其他消息忽略。"""
    while True:
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            return


async def _close_with_error(ws: WebSocket, error: str, code: int) -> None:
    if ws.application_state is not WebSocketState.CONNECTED:
        return
    with contextlib.suppress(Exception):
        await ws.send_json(StreamError(error=error).model_dump(mode="json"))
    with contextlib.suppress(Exception):
        await ws.close(code)


__all__ = (
    "API_PREFIX",
    "AckRequests",
    "AlertSource",
    "ApiDeps",
    "AuditLog",
    "alert_view",
    "create_app",
)
