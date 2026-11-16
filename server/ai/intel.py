"""情报驱动核心：数据源注册表、情报流水线、事件抽取、产业链图谱与影响排名（A-15 / A-11 ~ A-14）。

核心安全与可信原则（V4.1 9.6 & docs/tasks/README.md）：
1. 付费数据源默认禁用，启用须 OWNER 二次确认（`CONFIRM_PAID_INTEL_PHRASE`）；密钥只存凭据库引用名；
2. 外部新闻/网页/社媒一律不可信，先经 `sanitize_untrusted_text` 隔离注入；
3. 单一未核实来源绝不能触发交易（`verified_for_trading=False`），必须多源交叉核验；
4. 产业链图谱每条传导边必须附带数据来源（`evidence_source`），拒绝无依据的凭空关联；
5. 影响传导排名严格按 `as_of` 时间截断，杜绝前视偏差。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from server.ai.schemas import AiError, sanitize_untrusted_text

CONFIRM_PAID_INTEL_PHRASE = "CONFIRM ENABLE PAID INTEL"


class EventCategory(StrEnum):
    SUPPLY_SHOCK = "SUPPLY_SHOCK"
    POLICY_STIMULUS = "POLICY_STIMULUS"
    RATE_DECISION = "RATE_DECISION"
    EARNINGS_SURPRISE = "EARNINGS_SURPRISE"
    GEOPOLITICAL = "GEOPOLITICAL"


# ---------- A-15：资讯数据源平台管理 ----------


@dataclass(frozen=True)
class IntelSourceConfig:
    source_id: str
    name: str
    paid: bool
    enabled: bool = False
    reliability: Decimal = Decimal("0.70")
    credential_ref: str | None = None  # Windows 凭据库 key 引用，绝不放明文密钥
    paid_confirmed: bool = False

    def __post_init__(self) -> None:
        if not self.source_id.strip() or not self.name.strip():
            raise AiError("数据源 source_id 与 name 不能为空")
        if not (Decimal(0) < self.reliability <= Decimal(1)):
            raise AiError("数据源可信度必须在 (0, 1] 内")
        if self.paid and self.enabled and not self.paid_confirmed:
            raise AiError("付费资讯源初始化时必须默认禁用，须由 OWNER 二次确认后启用")
        if self.credential_ref is not None and any(
            bad in self.credential_ref.lower() for bad in ("sk-", "bearer ", "apikey=")
        ):
            raise AiError("credential_ref 只能填写凭据库引用名，禁止写入明文密钥")


class IntelSourceRegistry:
    """A-15 资讯数据源注册表：付费源默认禁用，启用须 OWNER 二次确认。"""

    def __init__(self) -> None:
        self._sources: dict[str, IntelSourceConfig] = {}

    def register(self, cfg: IntelSourceConfig) -> None:
        self._sources[cfg.source_id] = cfg

    def enable_source(
        self,
        source_id: str,
        *,
        actor_role: str,
        confirmation_phrase: str | None = None,
    ) -> IntelSourceConfig:
        if actor_role != "owner":
            raise AiError("只有 owner 可以启用资讯数据源")
        cfg = self._sources.get(source_id)
        if cfg is None:
            raise AiError(f"未知数据源：{source_id}")
        if cfg.paid and confirmation_phrase != CONFIRM_PAID_INTEL_PHRASE:
            raise AiError(f"启用付费数据源须输入二次确认短语：{CONFIRM_PAID_INTEL_PHRASE}")
        updated = IntelSourceConfig(
            source_id=cfg.source_id,
            name=cfg.name,
            paid=cfg.paid,
            enabled=True,
            reliability=cfg.reliability,
            credential_ref=cfg.credential_ref,
            paid_confirmed=cfg.paid,
        )
        self._sources[source_id] = updated
        return updated

    def is_enabled(self, source_id: str) -> bool:
        cfg = self._sources.get(source_id)
        return cfg is not None and cfg.enabled

    def reliability_of(self, source_id: str) -> Decimal:
        cfg = self._sources.get(source_id)
        return cfg.reliability if cfg is not None else Decimal("0")


# ---------- A-11：实时情报流水线 ----------


@dataclass(frozen=True)
class RawIntelItem:
    item_id: str
    source_id: str
    title: str
    body: str
    published_at: datetime
    themes: tuple[str, ...] = ()
    mentioned_symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class CleanIntelItem:
    item_id: str
    source_id: str
    sanitized_text: str
    published_at: datetime
    themes: tuple[str, ...]
    mentioned_symbols: tuple[str, ...]
    content_digest: str


class IntelPipeline:
    """A-11 实时情报清洗、去重与隔离流水线。"""

    def __init__(self, sources: IntelSourceRegistry) -> None:
        self._sources = sources
        self._seen_digests: set[str] = set()
        self._items: list[CleanIntelItem] = []

    def ingest(self, raw: RawIntelItem) -> CleanIntelItem | None:
        if not self._sources.is_enabled(raw.source_id):
            raise AiError(f"数据源 {raw.source_id} 未启用或未登记")
        if raw.published_at.tzinfo is None or raw.published_at.utcoffset() is None:
            raise AiError("published_at 必须带时区")
        sanitized = sanitize_untrusted_text(
            f"{raw.title}\n{raw.body}",
            source_name=raw.source_id,
            strict_reject=True,
        )
        digest = hashlib.sha256(f"{raw.title}|{raw.body}".encode()).hexdigest()[:16]
        if digest in self._seen_digests:
            return None
        self._seen_digests.add(digest)
        clean = CleanIntelItem(
            item_id=raw.item_id,
            source_id=raw.source_id,
            sanitized_text=sanitized,
            published_at=raw.published_at,
            themes=tuple(t.upper() for t in raw.themes),
            mentioned_symbols=tuple(s.upper() for s in raw.mentioned_symbols),
            content_digest=digest,
        )
        self._items.append(clean)
        return clean

    def items_as_of(self, as_of: datetime) -> list[CleanIntelItem]:
        return [it for it in self._items if it.published_at <= as_of]


# ---------- A-12：事件抽取与多源交叉核验 ----------


@dataclass(frozen=True)
class EventSignalInput:
    item: CleanIntelItem
    category: EventCategory
    theme: str
    direction: int  # +1 利好，-1 利空
    magnitude: Decimal  # (0, 1]


@dataclass(frozen=True)
class VerifiedEvent:
    event_key: str
    category: EventCategory
    theme: str
    direction: int
    confidence: Decimal
    source_ids: tuple[str, ...]
    latest_at: datetime
    verified_for_trading: bool


def extract_and_verify_events(
    signals: Sequence[EventSignalInput],
    sources: IntelSourceRegistry,
    *,
    as_of: datetime,
    min_sources_for_trade: int = 2,
) -> list[VerifiedEvent]:
    """A-12 事件汇聚与多源交叉核验：单一来源绝不标记为可交易。"""
    grouped: dict[tuple[EventCategory, str, int], list[EventSignalInput]] = {}
    for sig in signals:
        if sig.item.published_at > as_of:
            continue
        if sig.direction not in (-1, 1):
            raise AiError("事件方向只能为 +1 或 -1")
        key = (sig.category, sig.theme.upper(), sig.direction)
        grouped.setdefault(key, []).append(sig)

    events: list[VerifiedEvent] = []
    for (cat, theme, direction), group in sorted(grouped.items()):
        distinct_sources = sorted({g.item.source_id for g in group})
        weights = [sources.reliability_of(g.item.source_id) * g.magnitude for g in group]
        avg_conf = (sum(weights, Decimal(0)) / Decimal(len(weights))).quantize(Decimal("0.0001"))
        verified = len(distinct_sources) >= min_sources_for_trade
        latest = max(g.item.published_at for g in group)
        events.append(
            VerifiedEvent(
                event_key=f"{cat.value}:{theme}:{direction:+d}",
                category=cat,
                theme=theme,
                direction=direction,
                confidence=min(Decimal(1), avg_conf),
                source_ids=tuple(distinct_sources),
                latest_at=latest,
                verified_for_trading=verified,
            )
        )
    return events


# ---------- A-13：产业链图谱 ----------


@dataclass(frozen=True)
class SupplyChainEdge:
    theme: str
    etf_symbol: str
    transmission_sign: int  # +1 同向受益，-1 反向受损
    weight: Decimal  # (0, 1]
    evidence_source: str  # 必须有真实数据来源

    def __post_init__(self) -> None:
        if not self.theme.strip() or not self.etf_symbol.strip():
            raise AiError("产业链边 theme 与 etf_symbol 不能为空")
        if self.transmission_sign not in (-1, 1):
            raise AiError("transmission_sign 必须为 +1 或 -1")
        if not (Decimal(0) < self.weight <= Decimal(1)):
            raise AiError("传导权重必须在 (0, 1] 内")
        if not self.evidence_source.strip():
            raise AiError("产业链关系必须附带数据来源（evidence_source）")


class SupplyChainGraph:
    """A-13 行业 ETF 产业链图谱：只接受有数据来源的传导关系。"""

    def __init__(self, edges: Sequence[SupplyChainEdge] = ()) -> None:
        self._by_theme: dict[str, list[SupplyChainEdge]] = {}
        for edge in edges:
            self.add_edge(edge)

    def add_edge(self, edge: SupplyChainEdge) -> None:
        self._by_theme.setdefault(edge.theme.upper(), []).append(edge)

    def edges_for(self, theme: str) -> tuple[SupplyChainEdge, ...]:
        return tuple(self._by_theme.get(theme.upper(), ()))


# ---------- A-14：影响传导与选股排名 ----------


@dataclass(frozen=True)
class SymbolImpactScore:
    symbol: str
    score: Decimal  # [-1, +1] 正值利好，负值利空
    supporting_events: tuple[str, ...]
    verified: bool


def rank_symbol_impacts(
    events: Sequence[VerifiedEvent],
    graph: SupplyChainGraph,
    *,
    allowed_universe: frozenset[str] | None = None,
) -> list[SymbolImpactScore]:
    """A-14 沿产业链图谱传导已核验事件，生成行业 ETF 影响排名。"""
    scores: dict[str, Decimal] = {}
    ev_refs: dict[str, list[str]] = {}
    verified_map: dict[str, bool] = {}

    for ev in events:
        if not ev.verified_for_trading:
            continue  # 单一未核实事件不参与可交易影响传导
        for edge in graph.edges_for(ev.theme):
            sym = edge.etf_symbol.upper()
            if allowed_universe is not None and sym not in allowed_universe:
                continue
            delta = Decimal(ev.direction * edge.transmission_sign) * ev.confidence * edge.weight
            scores[sym] = scores.get(sym, Decimal(0)) + delta
            ev_refs.setdefault(sym, []).append(f"{ev.event_key}@{edge.evidence_source}")
            verified_map[sym] = True

    ranked: list[SymbolImpactScore] = []
    for sym, raw_score in scores.items():
        clamped = max(Decimal(-1), min(Decimal(1), raw_score)).quantize(Decimal("0.0001"))
        ranked.append(
            SymbolImpactScore(
                symbol=sym,
                score=clamped,
                supporting_events=tuple(ev_refs[sym]),
                verified=verified_map[sym],
            )
        )
    ranked.sort(key=lambda x: (-x.score, x.symbol))
    return ranked


__all__ = (
    "CONFIRM_PAID_INTEL_PHRASE",
    "CleanIntelItem",
    "EventCategory",
    "EventSignalInput",
    "IntelPipeline",
    "IntelSourceConfig",
    "IntelSourceRegistry",
    "RawIntelItem",
    "SupplyChainEdge",
    "SupplyChainGraph",
    "SymbolImpactScore",
    "VerifiedEvent",
    "extract_and_verify_events",
    "rank_symbol_impacts",
)
