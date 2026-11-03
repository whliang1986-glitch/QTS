"""API 的请求与响应模型（S-02），也是 TS 类型的来源。请求严格校验、拒绝未知字段（V4.1 4 章）。

金额与数量一律用字符串传十进制（不转浮点，CLAUDE.md）；时间是带时区的 UTC ISO 字符串。
账户号不出现在任何响应里。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from server.api.commands import Tier
from server.domain.enums import Mode, OrderClass, OrderType, Side, TimeInForce
from server.domain.order_state import EventKind, OrderStatus

# 幂等键：客户端生成、重试不换；字符集收窄，避免与日志、审计的分隔符混淆
RequestId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")]
ParamValue = Annotated[str, StringConstraints(max_length=1000)]


class Strict(BaseModel):
    # 输出模型里带默认值的字段总会出现：在 TS 类型里标成必有
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, json_schema_serialization_defaults_required=True
    )


class CommandRequest(Strict):
    request_id: RequestId
    name: str = Field(max_length=64)
    params: dict[Annotated[str, StringConstraints(max_length=64)], ParamValue] = Field(
        default_factory=dict, max_length=8
    )


class CommandResponse(Strict):
    request_id: str
    ok: bool  # 命令执行的结果（被 tradingd 规则拒绝时为 False，原因在 detail.error）
    replayed: bool  # 是否是同一 request_id 之前那次的原结果
    detail: dict[str, Any]


class ErrorResponse(Strict):
    error: str
    retry_same_request_id: bool = False  # 为真时只能用同一 request_id 重试，不得换号


class CommandInfo(Strict):
    name: str
    tier: Tier
    params: list[str]
    summary: str


class CommandCatalog(Strict):
    commands: list[CommandInfo]


class HaltView(Strict):
    halted: bool
    reason: str | None
    since: str | None
    set_by: str | None


class StatusView(Strict):
    mode: Mode | None  # 账本里还没有模式记录时为空（界面按“未知”显示）
    halt: HaltView | None
    changed_at: str | None
    changed_by: str | None


class OrderView(Strict):
    intent_id: str
    created_at: str
    strategy_id: str
    strategy_version: str
    symbol: str
    side: Side
    quantity: str
    order_type: OrderType
    limit_price: str | None
    stop_price: str | None
    order_class: OrderClass
    time_in_force: TimeInForce
    expires_at: str | None
    status: OrderStatus | None  # 尚无订单事件时为空
    updated_at: str | None


class OrderList(Strict):
    orders: list[OrderView]


class OrderEventView(Strict):
    seq: int
    intent_id: str
    kind: EventKind
    status: OrderStatus
    occurred_at: str


class ExecutionView(Strict):
    execution_id: str
    symbol: str
    side: Side
    quantity: str
    price: str
    commission: str
    executed_at: str


class HistoryEntry(Strict):
    kind: EventKind
    status: OrderStatus
    occurred_at: str


class OrderDetail(Strict):
    order: OrderView
    history: list[HistoryEntry]
    executions: list[ExecutionView]  # 有效成交（已去掉被更正取代的）


class WhoAmI(Strict):
    name: str
    role: str


# ---------- WebSocket 消息（/v1/stream） ----------


class StreamAuth(Strict):
    """客户端连上后的第一条消息。after：上次收到的最后一个订单事件 seq，断线重连时续读不漏。"""

    type: Literal["auth"]
    token: str = Field(min_length=1, max_length=4096)
    after: int | None = Field(default=None, ge=0)


class StreamHello(Strict):
    type: Literal["hello"] = "hello"
    name: str
    role: str
    cursor: int  # 从这个 seq 之后开始推送订单事件


class StreamStatus(Strict):
    type: Literal["status"] = "status"
    status: StatusView


class StreamOrderEvent(Strict):
    type: Literal["order_event"] = "order_event"
    event: OrderEventView


class StreamError(Strict):
    """服务端关闭连接前的最后一条消息：界面应把所有状态显示为“未知”。"""

    type: Literal["error"] = "error"
    error: str


STREAM_MODELS = (StreamAuth, StreamHello, StreamStatus, StreamOrderEvent, StreamError)


__all__ = (
    "STREAM_MODELS",
    "CommandCatalog",
    "CommandInfo",
    "CommandRequest",
    "CommandResponse",
    "ErrorResponse",
    "ExecutionView",
    "HaltView",
    "HistoryEntry",
    "OrderDetail",
    "OrderEventView",
    "OrderList",
    "OrderView",
    "StatusView",
    "StreamAuth",
    "StreamError",
    "StreamHello",
    "StreamOrderEvent",
    "StreamStatus",
    "WhoAmI",
)
