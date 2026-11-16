"""ModelGateway、预算控制、回放缓存与降级恢复（A-01 / A-05 / A-09，V4.1 第 9 章）。

核心保障：
1. 统一模型网关：所有 AI 调用必须经 `ModelGateway`，不得散落直调外部 SDK；
2. 预算与频次上限：每日调用次数与 token 预算受控，超限 fail-closed；
3. 确定性回放缓存（A-05）：按 `(provider, model, prompt_hash, schema_version)` 缓存结构化 JSON，
   `replay_only=True` 时绝不发起真实网络调用，保证离线测试与复现 100% 确定性；
4. 熔断与降级恢复（A-09）：主模型连续失败达到阈值自动熔断，切换至备用模型或本地降级状态，
   并支持半开探针恢复。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ValidationError

from server.ai.schemas import AiError


class GatewayHealth(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    OPEN_CIRCUIT = "OPEN_CIRCUIT"


class GatewayError(AiError):
    """模型网关调用、预算、回放缺失或熔断异常。"""


@dataclass(frozen=True)
class ModelRequest:
    task_kind: str
    system_prompt: str
    user_prompt: str
    schema_name: str
    schema_version: str = "1"
    max_tokens: int = 1024

    @property
    def prompt_hash(self) -> str:
        raw = json.dumps(
            {
                "task_kind": self.task_kind,
                "system_prompt": self.system_prompt,
                "user_prompt": self.user_prompt,
                "schema_name": self.schema_name,
                "schema_version": self.schema_version,
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class ModelResponse:
    provider: str
    model: str
    content_json: str
    tokens_used: int
    from_cache: bool = False


class ModelProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    async def complete_json(self, request: ModelRequest) -> ModelResponse: ...


class ReplayCache(Protocol):
    def get_cached(
        self, *, provider: str, model: str, prompt_hash: str, schema_version: str
    ) -> str | None: ...

    def put_cached(  # noqa: PLR0913
        self,
        *,
        provider: str,
        model: str,
        prompt_hash: str,
        schema_version: str,
        content_json: str,
        tokens_used: int,
        at: datetime,
    ) -> None: ...


@dataclass(frozen=True)
class BudgetPolicy:
    max_calls_per_day: int = 200
    max_tokens_per_day: int = 500_000


DEFAULT_BUDGET = BudgetPolicy()


class ModelGateway:
    """统一 AI 模型网关：缓存回放、预算控制、Schema 强制校验、主备降级与自动恢复。"""

    def __init__(  # noqa: PLR0913
        self,
        primary: ModelProvider,
        *,
        fallback: ModelProvider | None = None,
        cache: ReplayCache | None = None,
        budget: BudgetPolicy = DEFAULT_BUDGET,
        failure_threshold: int = 3,
        replay_only: bool = False,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold 必须为正整数")
        self._primary = primary
        self._fallback = fallback
        self._cache = cache
        self._budget = budget
        self._failure_threshold = failure_threshold
        self._replay_only = replay_only
        self._consecutive_failures = 0
        self._health = GatewayHealth.HEALTHY
        self._usage_day: date | None = None
        self._calls_today = 0
        self._tokens_today = 0

    @property
    def health(self) -> GatewayHealth:
        return self._health

    @property
    def usage_today(self) -> tuple[int, int]:
        return (self._calls_today, self._tokens_today)

    def _roll_day(self, now: datetime) -> None:
        today = now.astimezone(UTC).date()
        if self._usage_day != today:
            self._usage_day = today
            self._calls_today = 0
            self._tokens_today = 0

    def _check_budget(self, now: datetime, max_tokens: int) -> None:
        self._roll_day(now)
        if self._calls_today >= self._budget.max_calls_per_day:
            raise GatewayError("今日 AI 调用次数已达预算上限")
        if self._tokens_today + max_tokens > self._budget.max_tokens_per_day:
            raise GatewayError("今日 AI Token 消耗已达预算上限")

    def _record_usage(self, tokens: int) -> None:
        self._calls_today += 1
        self._tokens_today += max(0, tokens)

    async def invoke_structured[TModel: BaseModel](
        self,
        request: ModelRequest,
        schema_cls: type[TModel],
        *,
        now: datetime,
    ) -> tuple[TModel, ModelResponse]:
        """调用模型并强制解析为目标 Pydantic Schema；支持缓存命中与主备降级。"""
        if now.tzinfo is None or now.utcoffset() is None:
            raise GatewayError("now 必须带时区")

        p_name = self._primary.provider_name
        m_name = self._primary.model_name
        p_hash = request.prompt_hash

        # 1. 先查确定性回放缓存
        if self._cache is not None:
            cached = self._cache.get_cached(
                provider=p_name,
                model=m_name,
                prompt_hash=p_hash,
                schema_version=request.schema_version,
            )
            if cached is not None:
                parsed = _parse_schema(cached, schema_cls)
                return parsed, ModelResponse(
                    provider=p_name,
                    model=m_name,
                    content_json=cached,
                    tokens_used=0,
                    from_cache=True,
                )
        if self._replay_only:
            raise GatewayError(f"回放模式下未找到缓存响应（hash={p_hash[:12]}）")

        # 2. 预算检查
        self._check_budget(now, request.max_tokens)

        # 3. 若主模型已熔断，优先尝试备用模型
        if self._health is GatewayHealth.OPEN_CIRCUIT:
            if self._fallback is not None:
                return await self._call_provider(
                    self._fallback, request, schema_cls, now, is_fallback=True
                )
            raise GatewayError("主模型已熔断且无备用模型，AI 网关拒绝调用")

        # 4. 调用主模型，失败则计数并视情况降级到备用模型
        try:
            res_model, resp = await self._call_provider(
                self._primary, request, schema_cls, now, is_fallback=False
            )
            self._consecutive_failures = 0
            self._health = GatewayHealth.HEALTHY
            return res_model, resp
        except Exception as primary_exc:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._failure_threshold:
                self._health = (
                    GatewayHealth.DEGRADED
                    if self._fallback is not None
                    else GatewayHealth.OPEN_CIRCUIT
                )
            if self._fallback is not None:
                try:
                    return await self._call_provider(
                        self._fallback, request, schema_cls, now, is_fallback=True
                    )
                except Exception as fb_exc:
                    self._health = GatewayHealth.OPEN_CIRCUIT
                    raise GatewayError(f"主备模型均失败：{fb_exc}") from fb_exc
            raise GatewayError(f"主模型调用失败：{primary_exc}") from primary_exc

    async def probe_recovery[TModel: BaseModel](
        self, probe_request: ModelRequest, schema_cls: type[TModel], *, now: datetime
    ) -> bool:
        """降级恢复探针（A-09）：尝试主模型，成功则恢复 HEALTHY。"""
        try:
            await self._call_provider(
                self._primary, probe_request, schema_cls, now, is_fallback=False
            )
        except Exception:
            return False
        self._consecutive_failures = 0
        self._health = GatewayHealth.HEALTHY
        return True

    async def _call_provider[TModel: BaseModel](
        self,
        provider: ModelProvider,
        request: ModelRequest,
        schema_cls: type[TModel],
        now: datetime,
        *,
        is_fallback: bool,
    ) -> tuple[TModel, ModelResponse]:
        resp = await provider.complete_json(request)
        parsed = _parse_schema(resp.content_json, schema_cls)
        self._record_usage(resp.tokens_used)
        if self._cache is not None and not is_fallback:
            self._cache.put_cached(
                provider=resp.provider,
                model=resp.model,
                prompt_hash=request.prompt_hash,
                schema_version=request.schema_version,
                content_json=resp.content_json,
                tokens_used=resp.tokens_used,
                at=now,
            )
        return parsed, resp


def _parse_schema[TModel: BaseModel](raw_json: str, schema_cls: type[TModel]) -> TModel:
    try:
        return schema_cls.model_validate_json(raw_json)
    except ValidationError as exc:
        raise GatewayError(f"模型输出不符合结构化协议 {schema_cls.__name__}：{exc}") from exc


__all__ = (
    "BudgetPolicy",
    "GatewayError",
    "GatewayHealth",
    "ModelGateway",
    "ModelProvider",
    "ModelRequest",
    "ModelResponse",
    "ReplayCache",
)
