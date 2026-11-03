"""apid 的 FastAPI 应用（S-02，V4.1 10.1）：/v1 查询、命令与事件流。

- 只读：查询都来自账本的只读连接（ledger.db 只有 tradingd 能写，V4.1 5.6）；
  账本读不了一律 503，不拿空列表冒充“没有订单”（失败即关闭）。
- 命令：白名单 + 参数键校验后经本机通道交给 tradingd（commands.IpcCommandGateway）；
  操作者取认证结果；结果语义见 commands 模块与 docs/tasks/S-02.md。
- 认证：每个路由与 WebSocket 都要认证（auth.Authenticator，S-03 实现）；viewer 不能发命令。
- 生产不开放 /docs 与 /openapi.json（expose_docs=False）；OpenAPI 由 scripts/gen_api_types.py 导出。
"""

# 不用 from __future__ import annotations：FastAPI 要在定义时解析路由里局部的 Annotated 依赖别名
import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any

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
    NotDeliveredError,
    OutcomeUnknownError,
    check_command,
)
from server.api.models import (
    CommandCatalog,
    CommandInfo,
    CommandRequest,
    CommandResponse,
    ErrorResponse,
    ExecutionView,
    HaltView,
    HistoryEntry,
    OrderDetail,
    OrderEventView,
    OrderList,
    OrderView,
    StatusView,
    StreamAuth,
    StreamError,
    StreamHello,
    StreamOrderEvent,
    StreamStatus,
    WhoAmI,
)
from server.domain.orders import Execution, OrderIntent
from server.storage.ledger import LedgerRepository, OrderEventRow, OrderSummary
from server.storage.logs import log_event

log = logging.getLogger(__name__)

API_PREFIX = "/v1"
STREAM_BATCH = 200
# WebSocket 关闭码（4000–4999 为应用自定义）
WS_UNAUTHORIZED = 4401
WS_BAD_MESSAGE = 4400


@dataclass(frozen=True)
class ApiDeps:
    authenticator: Authenticator
    ledger: Callable[[], LedgerRepository]  # 只读账本；打开失败抛出（按 503 处理）
    gateway: CommandGateway
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
    code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 422, 503, 504)
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
        token = bearer_token(request.headers.get("authorization"))
        if token is None:
            raise ApiError(401, "缺少令牌")
        found = _authenticate(deps.authenticator, token)
        if found is None:
            raise ApiError(401, "令牌无效")
        return found

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

    @app.post(f"{API_PREFIX}/commands", responses=ERRORS)
    async def command(
        body: CommandRequest, who: Annotated[Principal, Depends(owner)]
    ) -> CommandResponse:
        try:
            check_command(body.name, body.params)
        except CommandError as exc:
            raise ApiError(422, str(exc)) from exc
        try:
            answer = await deps.gateway.send(
                body.request_id, body.name, body.params, actor=who.name
            )
        except NotDeliveredError as exc:
            raise ApiError(503, "tradingd 不可用，命令未发出，可以重试") from exc
        except OutcomeUnknownError as exc:
            raise ApiError(
                504, "命令已发出但结果未知：请用同一 request_id 重试查询原结果", retry_same=True
            ) from exc
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
    try:
        who = await asyncio.to_thread(_authenticate, deps.authenticator, message.token)
    except ApiError:
        who = None
    if who is None:
        await _close_with_error(ws, "令牌无效", WS_UNAUTHORIZED)
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


__all__ = ("API_PREFIX", "ApiDeps", "create_app")
