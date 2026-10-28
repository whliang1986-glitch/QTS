"""进程间通信协议（S-01b，ADR-14）：apid、workerd 经本机通道把命令与意图交给 tradingd。

帧格式：4 字节大端长度 + UTF-8 JSON；单帧上限 MAX_FRAME，
超限、截断、不是 JSON 对象一律断开（失败即关闭）。
一问一答：请求 {"type": "intent" | "command", ...}，回答 {"ok": bool, "result" | "error": ...}。

权限按对端身份（由传输层在连接时核验，见 transport）区分，不信任消息里的自报身份：
- workerd：只能提交意图（策略只能提交意图，ADR-14）；
- apid：只能提交命令（操作者身份由 apid 鉴权后填写，S-03）；
- 其他身份一律拒绝。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from pydantic import ValidationError

from server.domain.orders import OrderIntent
from server.storage.logs import log_event

log = logging.getLogger(__name__)

MAX_FRAME = 1 << 20  # 1 MiB：意图与命令都很小，超大帧按异常断开
HEADER = 4


class Role(StrEnum):
    WORKERD = "workerd"
    APID = "apid"


class ProtocolError(Exception):
    """帧或消息格式不合法：断开连接。"""


@dataclass(frozen=True)
class Peer:
    """传输层核验过的对端身份。"""

    role: Role
    detail: str  # 例如进程号与可执行文件路径，只用于日志与审计


class DecisionView(Protocol):
    @property
    def intent_id(self) -> str: ...

    @property
    def verdict(self) -> Any: ...


class OutcomeView(Protocol):
    @property
    def decision(self) -> DecisionView: ...

    @property
    def status(self) -> Any: ...


class CommandView(Protocol):
    @property
    def ok(self) -> bool: ...

    @property
    def detail(self) -> Mapping[str, Any]: ...

    @property
    def replayed(self) -> bool: ...


class TradingPort(Protocol):
    """tradingd 交易核心对外的两个入口（server.risk.core.TradingCore 按结构满足）。"""

    async def submit_intent(self, intent: OrderIntent) -> OutcomeView: ...

    async def command(
        self, request_id: str, name: str, params: Mapping[str, str], *, actor: str
    ) -> CommandView: ...


# 入口拒绝（意图或命令不合规）的异常类型名：回答里如实给出，调用方据此判断不必重试。
# NotReadyError（尚未完成启动）不在其中：是暂时性的，回答 refused=False，调用方稍后重试。
REFUSAL_ERRORS = frozenset({"IntakeError"})


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """读一帧；对端正常关闭返回 None，格式不合法抛 ProtocolError。"""
    try:
        header = await reader.readexactly(HEADER)
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None
        raise ProtocolError("帧头被截断") from exc
    size = int.from_bytes(header, "big")
    if size <= 0 or size > MAX_FRAME:
        raise ProtocolError(f"帧长度不合法：{size}")
    try:
        body = await reader.readexactly(size)
    except asyncio.IncompleteReadError as exc:
        raise ProtocolError("帧体被截断") from exc
    try:
        message = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("帧不是合法的 UTF-8 JSON") from exc
    if not isinstance(message, dict):
        raise ProtocolError("消息必须是 JSON 对象")
    return message


async def write_frame(writer: asyncio.StreamWriter, message: Mapping[str, Any]) -> None:
    body = json.dumps(message, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise ProtocolError("要发送的消息超过帧上限")
    writer.write(len(body).to_bytes(HEADER, "big") + body)
    await writer.drain()


class IpcServer:
    """tradingd 侧：按对端身份分发到交易核心。一个连接内请求按顺序处理。"""

    def __init__(self, port: TradingPort) -> None:
        self._port = port

    async def serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, peer: Peer
    ) -> None:
        try:
            while True:
                message = await read_frame(reader)
                if message is None:
                    return
                await write_frame(writer, await self.dispatch(message, peer))
        except ProtocolError as exc:
            log_event(log, logging.WARNING, "进程间消息格式不合法，断开",
                      role=peer.role.value, reason=str(exc))  # fmt: skip
        except ConnectionError:  # 对端中途断开：意图仍在对方的 outbox 里，下次重投
            log_event(log, logging.INFO, "进程间连接中断", role=peer.role.value)
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def dispatch(self, message: Mapping[str, Any], peer: Peer) -> dict[str, Any]:
        kind = message.get("type")
        try:
            if kind == "intent" and peer.role is Role.WORKERD:
                return await self._intent(message)
            if kind == "command" and peer.role is Role.APID:
                return await self._command(message)
        except ValidationError:
            return {"ok": False, "error": "意图格式不合法", "refused": True}
        except Exception as exc:
            refused = type(exc).__name__ in REFUSAL_ERRORS
            if not refused:
                log_event(log, logging.ERROR, "进程间请求处理出错", error=type(exc).__name__)
            return {"ok": False, "error": str(exc) if refused else type(exc).__name__,
                    "refused": refused}  # fmt: skip
        return {"ok": False, "error": f"{peer.role.value} 不能发送 {kind!r} 请求", "refused": True}

    async def _intent(self, message: Mapping[str, Any]) -> dict[str, Any]:
        raw = message.get("intent")
        if not isinstance(raw, str):
            return {"ok": False, "error": "缺少意图", "refused": True}
        intent = OrderIntent.model_validate_json(raw)
        outcome = await self._port.submit_intent(intent)
        return {
            "ok": True,
            "result": {
                "intent_id": outcome.decision.intent_id,
                "verdict": str(outcome.decision.verdict.value),
                "status": None if outcome.status is None else str(outcome.status.value),
            },
        }

    async def _command(self, message: Mapping[str, Any]) -> dict[str, Any]:
        result = await self._port.command(
            message.get("request_id"),  # type: ignore[arg-type]  # 由交易核心严格校验类型
            message.get("name"),  # type: ignore[arg-type]
            message.get("params"),  # type: ignore[arg-type]
            actor=message.get("actor"),  # type: ignore[arg-type]
        )
        return {
            "ok": True,
            "result": {"ok": result.ok, "detail": dict(result.detail), "replayed": result.replayed},
        }


class IpcClient:
    """apid / workerd 侧：一问一答。连接断开或格式错误抛出，由调用方重连（意图留在 outbox）。"""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._lock = asyncio.Lock()

    async def request(self, message: Mapping[str, Any]) -> dict[str, Any]:
        async with self._lock:
            await write_frame(self._writer, message)
            answer = await read_frame(self._reader)
        if answer is None:
            raise ConnectionError("tradingd 关闭了连接")
        return answer

    async def submit_intent(self, intent: OrderIntent) -> dict[str, Any]:
        return await self.request({"type": "intent", "intent": intent.model_dump_json()})

    async def command(
        self, request_id: str, name: str, params: Mapping[str, str], *, actor: str
    ) -> dict[str, Any]:
        return await self.request(
            {"type": "command", "request_id": request_id, "name": name, "params": dict(params),
             "actor": actor}
        )  # fmt: skip

    async def close(self) -> None:
        self._writer.close()
        with contextlib.suppress(OSError):
            await self._writer.wait_closed()


__all__ = (
    "MAX_FRAME",
    "IpcClient",
    "IpcServer",
    "Peer",
    "ProtocolError",
    "Role",
    "TradingPort",
    "read_frame",
    "write_frame",
)
