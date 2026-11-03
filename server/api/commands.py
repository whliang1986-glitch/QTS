"""命令目录与转发（S-02，V4.1 10.1 / ADR-14）。

- 目录：apid 只转发白名单里的命令，参数键必须与目录完全一致；tradingd 还会再严格校验一遍。
- 分级：控制 / 高危（V4.1 10.1）。高危命令必须带 tradingd 给出的确认短语
  （短语由 tradingd 逐字核对）；二次确认令牌在 S-03 加。限额与风控参数没有任何命令入口。
- 转发：每条命令新开一条本机连接，发完即关，命令之间互不排队（急停不会堵在别的命令后面）。
  apid 不保存命令状态，也不自行重发：
  - 连接没建立：命令确定没发出（NotDelivered）；
  - 已发出但断开、超时或回答不合规：结果未知（OutcomeUnknown），只能由客户端用**同一** request_id
    重试，tradingd 按 request_id 返回原结果。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from server.storage.logs import log_event

log = logging.getLogger(__name__)


class Tier(StrEnum):
    CONTROL = "control"
    HIGH = "high"


@dataclass(frozen=True)
class CommandSpec:
    name: str
    tier: Tier
    params: frozenset[str]  # 必须恰好是这些键
    summary: str


CATALOG: dict[str, CommandSpec] = {
    spec.name: spec
    for spec in (
        CommandSpec("stop_new_orders", Tier.CONTROL, frozenset({"reason"}),
                    "停止新增风险（持久化 HALT，不撤保护性止损）"),
        CommandSpec("cancel_open_orders", Tier.CONTROL, frozenset(), "逐单撤销开仓单"),
        CommandSpec("preview_flatten", Tier.CONTROL, frozenset(),
                    "生成紧急平仓预览与确认短语（不下单）"),
        CommandSpec("reconcile", Tier.CONTROL, frozenset(), "立即与券商对账"),
        CommandSpec("resume", Tier.CONTROL, frozenset(), "恢复交易（须满足六项门槛）"),
        CommandSpec("cancel_protective_stops", Tier.HIGH, frozenset({"confirmation"}),
                    "撤销保护性止损（须确认短语）"),
        CommandSpec("emergency_flatten", Tier.HIGH, frozenset({"preview_id", "confirmation"}),
                    "按预览紧急平仓（须确认短语）"),
        CommandSpec("adopt_baseline", Tier.HIGH, frozenset({"confirmation"}),
                    "采用券商持仓为对账基准（须确认短语）"),
    )
}  # fmt: skip


class CommandError(Exception):
    """命令在 apid 入口就不合规：不转发。"""


def check_command(name: str, params: Mapping[str, str]) -> CommandSpec:
    spec = CATALOG.get(name)
    if spec is None:
        raise CommandError(f"未知命令：{name}")
    if set(params) != spec.params:
        raise CommandError(f"命令 {name} 的参数必须恰好是：{sorted(spec.params)}")
    empty = sorted(k for k, v in params.items() if not v.strip())
    if empty:
        raise CommandError(f"参数不能为空：{empty}")
    return spec


class NotDeliveredError(Exception):
    """没连上 tradingd：命令确定没有发出，可以重试。"""


class OutcomeUnknownError(Exception):
    """命令已发出但没拿到合规的回答：结果未知，必须用同一 request_id 重试。"""


@dataclass(frozen=True)
class Answer:
    """tradingd 的回答。executed 为真表示命令已执行（ok 是执行结果）；否则见 refused。"""

    executed: bool
    ok: bool
    detail: Mapping[str, Any]
    replayed: bool
    refused: bool  # 未执行时：True 入口拒绝（不必重试），False 暂时性失败（同号重试）
    error: str | None


class CommandClient(Protocol):
    async def command(
        self, request_id: str, name: str, params: Mapping[str, str], *, actor: str
    ) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class CommandGateway(Protocol):
    async def send(
        self, request_id: str, name: str, params: Mapping[str, str], *, actor: str
    ) -> Answer: ...


class IpcCommandGateway:
    """经本机通道把命令交给 tradingd。connect 负责核验对端是 tradingd 账户（connect_role）。"""

    def __init__(
        self,
        connect: Callable[[], Awaitable[CommandClient]],
        *,
        connect_timeout: float = 5.0,
        timeout: float = 30.0,
    ) -> None:
        if connect_timeout <= 0 or timeout <= 0:
            raise ValueError("超时必须为正")
        self._connect = connect
        self._connect_timeout = connect_timeout
        self._timeout = timeout

    async def send(
        self, request_id: str, name: str, params: Mapping[str, str], *, actor: str
    ) -> Answer:
        try:
            client = await asyncio.wait_for(self._connect(), self._connect_timeout)
        except Exception as exc:  # 任何连接失败：还没写出任何字节
            log_event(log, logging.WARNING, "连不上 tradingd，命令未发出",
                      command=name, error=type(exc).__name__)  # fmt: skip
            raise NotDeliveredError("tradingd 不可用，命令未发出") from exc
        try:
            raw = await asyncio.wait_for(
                client.command(request_id, name, dict(params), actor=actor), self._timeout
            )
            answer = _parse(raw)
        except Exception as exc:  # 已发出：断开、超时、回答不合规都按结果未知处理
            log_event(log, logging.ERROR, "命令结果未知，须用同一 request_id 重试",
                      command=name, error=type(exc).__name__)  # fmt: skip
            raise OutcomeUnknownError("命令已发出但结果未知") from exc
        finally:
            with contextlib.suppress(Exception):
                await client.close()
        return answer


def _parse(raw: Any) -> Answer:
    """严格解析 IPC 回答（protocol.IpcServer 的格式）；不合规抛 ValueError。"""
    if not isinstance(raw, dict) or not isinstance(raw.get("ok"), bool):
        raise ValueError("回答格式不合法")
    if not raw["ok"]:
        error, refused = raw.get("error"), raw.get("refused")
        if not isinstance(error, str) or not isinstance(refused, bool):
            raise ValueError("拒绝回答格式不合法")
        return Answer(False, False, {}, False, refused, error)
    result = raw.get("result")
    if not (
        isinstance(result, dict)
        and isinstance(result.get("ok"), bool)
        and isinstance(result.get("detail"), dict)
        and isinstance(result.get("replayed"), bool)
    ):
        raise ValueError("执行结果格式不合法")
    return Answer(True, result["ok"], result["detail"], result["replayed"], False, None)


__all__ = (
    "CATALOG",
    "Answer",
    "CommandClient",
    "CommandError",
    "CommandGateway",
    "CommandSpec",
    "IpcCommandGateway",
    "NotDeliveredError",
    "OutcomeUnknownError",
    "Tier",
    "check_command",
)
