"""AI 结构化决策协议、权限矩阵与不可信输入隔离（A-03 / A-04 / A-08，V4.1 第 9 章）。

核心红线：
1. AI 只能提议（IDEA / HYPOTHESIS / 待审批建议），绝不能批准晋级、调松风控、修改交易名单或直接下单；
2. 任何新闻、网页、社交媒体内容一律视为不可信输入（untrusted），必须包裹隔离并检测注入特征；
3. 所有 AI 决策必须输出严格校验的 Pydantic 结构化模型，并附带证据来源（citations）。
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from server.domain.enums import Side

_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"system\s*prompt", re.IGNORECASE),
    re.compile(r"bypass\s+(risk|guard|approval)", re.IGNORECASE),
    re.compile(r"execute\s+order\s+immediately", re.IGNORECASE),
    re.compile(r"忽略(之前|以上)(所有)?指令"),
    re.compile(r"绕过(风控|审批|门禁)"),
)


class AiError(ValueError):
    """AI 子系统基础异常。"""


class AiPermissionError(AiError):
    """AI 试图执行越权操作（如直接下单、晋级、调松风控等）。"""


class PromptInjectionError(AiError):
    """检测到不可信输入中包含提示词注入攻击载荷。"""


class TaskKind(StrEnum):
    RESEARCH_HYPOTHESIS = "RESEARCH_HYPOTHESIS"
    FACTOR_CANDIDATE = "FACTOR_CANDIDATE"
    INTEL_DIGEST = "INTEL_DIGEST"
    STRATEGY_PROPOSAL = "STRATEGY_PROPOSAL"
    RISK_EXPLANATION = "RISK_EXPLANATION"
    PORTFOLIO_REVIEW = "PORTFOLIO_REVIEW"


class AiCapability(StrEnum):
    """AI 操作权限枚举：分为允许（PROPOSE/READ）与严格禁止（FORBIDDEN_*）。"""

    READ_MARKET_AND_LEDGER = "READ_MARKET_AND_LEDGER"
    PROPOSE_HYPOTHESIS = "PROPOSE_HYPOTHESIS"
    PROPOSE_FACTOR_CANDIDATE = "PROPOSE_FACTOR_CANDIDATE"
    PROPOSE_INTEL_TRADE_IDEA = "PROPOSE_INTEL_TRADE_IDEA"
    EXPLAIN_RISK_DECISION = "EXPLAIN_RISK_DECISION"
    # 以下高危操作对 AI 永久禁止（fail closed）
    FORBIDDEN_SUBMIT_ORDER = "FORBIDDEN_SUBMIT_ORDER"
    FORBIDDEN_PROMOTE_STRATEGY = "FORBIDDEN_PROMOTE_STRATEGY"
    FORBIDDEN_PROMOTE_FACTOR = "FORBIDDEN_PROMOTE_FACTOR"
    FORBIDDEN_LOOSEN_RISK = "FORBIDDEN_LOOSEN_RISK"
    FORBIDDEN_MODIFY_TRADING_LIST = "FORBIDDEN_MODIFY_TRADING_LIST"
    FORBIDDEN_TOUCH_LIVE_MODE = "FORBIDDEN_TOUCH_LIVE_MODE"


ALLOWED_AI_CAPABILITIES = frozenset(
    {
        AiCapability.READ_MARKET_AND_LEDGER,
        AiCapability.PROPOSE_HYPOTHESIS,
        AiCapability.PROPOSE_FACTOR_CANDIDATE,
        AiCapability.PROPOSE_INTEL_TRADE_IDEA,
        AiCapability.EXPLAIN_RISK_DECISION,
    }
)


def require_ai_capability(capability: AiCapability) -> None:
    """运行时权限矩阵强制检查：非白名单能力一律拒绝（A-04）。"""
    if capability not in ALLOWED_AI_CAPABILITIES:
        raise AiPermissionError(f"AI 权限矩阵拒绝越权操作：{capability.value}")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Citation(_StrictModel):
    source_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=256)
    published_at: str = Field(min_length=1, max_length=64)
    excerpt: str = Field(min_length=1, max_length=500)


class TradeSuggestion(_StrictModel):
    """AI 产出的交易建议：只能进入 S-10 待审批队列，绝不直接成单。"""

    symbol: str = Field(min_length=1, max_length=32, pattern=r"^[A-Z0-9._-]+$")
    side: Side
    target_weight: Annotated[Decimal, Field(gt=Decimal(0), le=Decimal("0.20"))]
    rationale: str = Field(min_length=1, max_length=500)
    confidence: Annotated[Decimal, Field(ge=Decimal(0), le=Decimal(1))]


class StructuredDecision(_StrictModel):
    """A-03 统一结构化决策输出协议。"""

    decision_id: str = Field(min_length=1, max_length=128)
    task_kind: TaskKind
    created_at: datetime
    summary: str = Field(min_length=1, max_length=1000)
    rationale: str = Field(min_length=1, max_length=2000)
    citations: tuple[Citation, ...] = Field(min_length=1)
    confidence: Annotated[Decimal, Field(ge=Decimal(0), le=Decimal(1))]
    suggestions: tuple[TradeSuggestion, ...] = ()
    requires_human_approval: bool = True


def sanitize_untrusted_text(
    raw_text: str,
    *,
    source_name: str,
    strict_reject: bool = True,
) -> str:
    """隔离并清洗来自新闻/网页/社交媒体的不可信文本（A-08 / V4.1 9.6）。"""
    cleaned = raw_text.replace("\x00", "").strip()
    if not cleaned:
        raise AiError(f"来自 {source_name} 的外部输入为空")
    for pat in _INJECTION_PATTERNS:
        if pat.search(cleaned):
            if strict_reject:
                raise PromptInjectionError(f"外部输入（{source_name}）包含可疑提示词注入指令")
            cleaned = pat.sub("[REDACTED_INJECTION]", cleaned)
    return f"[UNTRUSTED_DATA source={source_name}]\n{cleaned}\n[/UNTRUSTED_DATA]"


__all__ = (
    "ALLOWED_AI_CAPABILITIES",
    "AiCapability",
    "AiError",
    "AiPermissionError",
    "Citation",
    "PromptInjectionError",
    "StructuredDecision",
    "TaskKind",
    "TradeSuggestion",
    "require_ai_capability",
    "sanitize_untrusted_text",
)
