"""交易名单只追加存储与资格检查（S-14，ADR-16）。

独立存放于 `server/storage/trading_list.py`（仅依赖 `server.domain`），
供 `server/api` 与 `server/risk/trading_list.py` 共同调用，避免经 `risk` 间接依赖 `broker`。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from server.domain.enums import AssetClass
from server.domain.instrument import Instrument

CONFIRM_TRADING_LIST_PHRASE = "CONFIRM ADD TRADING LIST"
TRADING_LIST_NOT_MEMBER = "标的不在生效交易名单内，禁止新开仓（S-14 三层隔离）"


class TradingListState(StrEnum):
    WATCHLIST_ONLY = "WATCHLIST_ONLY"
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    IN_TRADING_LIST = "IN_TRADING_LIST"


class TradingListError(ValueError):
    """交易名单资格检查、权限或确认违约。"""


@dataclass(frozen=True)
class EligibilityCriteria:
    """交易名单准入资格规则与硬上限（默认保守值，最终以受保护配置为准）。"""

    max_list_size: int = 20
    allow_stk: bool = False
    min_avg_dollar_volume: Decimal = Decimal("50000000")  # 近 60 日日均成交额 >= 5000 万美元
    max_median_spread_bps: Decimal = Decimal("15")  # 买卖价差中位数 <= 0.15% (15 bps)
    min_listing_days: int = 365


DEFAULT_CRITERIA = EligibilityCriteria()


@dataclass(frozen=True)
class SymbolProfile:
    """标的资格核验所需的市场与合约事实。"""

    instrument: Instrument
    avg_dollar_volume_60d: Decimal
    median_spread_bps: Decimal
    listing_days: int
    is_leveraged_or_inverse: bool = False
    tradable: bool = True


@dataclass(frozen=True)
class EligibilityResult:
    symbol: str
    eligible: bool
    reasons: tuple[str, ...]


def check_eligibility(
    profile: SymbolProfile,
    *,
    current_count: int,
    criteria: EligibilityCriteria = DEFAULT_CRITERIA,
) -> EligibilityResult:
    """逐项核验标的是否满足进入交易名单的资格与硬上限。"""
    reasons: list[str] = []
    inst = profile.instrument
    if current_count >= criteria.max_list_size:
        reasons.append(f"交易名单数量已达硬上限 {criteria.max_list_size}")
    if not profile.tradable or inst.multiplier != Decimal(1):
        reasons.append("合约不可交易或乘数非 1")
    allowed_assets = {AssetClass.ETF, AssetClass.STK} if criteria.allow_stk else {AssetClass.ETF}
    if inst.asset_class not in allowed_assets:
        reasons.append(f"首期仅限美股 ETF 现货，不支持 {inst.asset_class.value}")
    if profile.is_leveraged_or_inverse:
        reasons.append("禁止杠杆或反向 ETF")
    if profile.avg_dollar_volume_60d < criteria.min_avg_dollar_volume:
        reasons.append(
            f"日均成交额 {profile.avg_dollar_volume_60d} < 门槛 {criteria.min_avg_dollar_volume}"
        )
    if profile.median_spread_bps > criteria.max_median_spread_bps:
        reasons.append(
            f"价差 {profile.median_spread_bps} bps > 门槛 {criteria.max_median_spread_bps} bps"
        )
    if profile.listing_days < criteria.min_listing_days:
        reasons.append(f"上市天数 {profile.listing_days} < 门槛 {criteria.min_listing_days}")
    return EligibilityResult(
        symbol=inst.symbol,
        eligible=len(reasons) == 0,
        reasons=tuple(reasons),
    )


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS trading_list_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL CHECK (
        action IN ('request_add', 'confirm_add', 'cancel_pending', 'remove')
    ),
    actor TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail_json TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS trading_list_events_no_update
BEFORE UPDATE ON trading_list_events
BEGIN
    SELECT RAISE(ABORT, 'trading_list_events is append-only: UPDATE forbidden');
END;

CREATE TRIGGER IF NOT EXISTS trading_list_events_no_delete
BEFORE DELETE ON trading_list_events
BEGIN
    SELECT RAISE(ABORT, 'trading_list_events is append-only: DELETE forbidden');
END;
"""


@dataclass(frozen=True)
class TradingListSnapshot:
    active: frozenset[str]
    pending: frozenset[str]


class TradingListStore:
    """只追加交易名单注册表（ADR-16：加入须待确认+独立确认短语，移出立即生效）。"""

    def __init__(
        self,
        db_path: Path,
        *,
        criteria: EligibilityCriteria = DEFAULT_CRITERIA,
    ) -> None:
        self._path = db_path
        self._criteria = criteria
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA recursive_triggers = ON")
        return conn

    def snapshot(self) -> TradingListSnapshot:
        """从只追加变更记录重放当前生效名单与待确认集合。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT symbol, action FROM trading_list_events ORDER BY seq ASC"
            ).fetchall()
        active: set[str] = set()
        pending: set[str] = set()
        for sym_raw, act_raw in rows:
            sym, act = str(sym_raw), str(act_raw)
            if act == "request_add":
                if sym not in active:
                    pending.add(sym)
            elif act == "confirm_add":
                pending.discard(sym)
                active.add(sym)
            elif act == "cancel_pending":
                pending.discard(sym)
            elif act == "remove":
                pending.discard(sym)
                active.discard(sym)
        return TradingListSnapshot(active=frozenset(active), pending=frozenset(pending))

    def state_of(self, symbol: str) -> TradingListState:
        snap = self.snapshot()
        if symbol in snap.active:
            return TradingListState.IN_TRADING_LIST
        if symbol in snap.pending:
            return TradingListState.PENDING_CONFIRMATION
        return TradingListState.WATCHLIST_ONLY

    def request_add(
        self,
        profile: SymbolProfile,
        *,
        actor: str,
        reason: str,
        at: datetime,
    ) -> EligibilityResult:
        """发起加入交易名单（调松）：服务端先核验资格与硬上限，通过后记入待确认，不立即生效。"""
        _require_owner(actor)
        _require_tz(at)
        if not reason.strip():
            raise TradingListError("加入交易名单必须填写原因")
        snap = self.snapshot()
        sym = profile.instrument.symbol
        if sym in snap.active:
            raise TradingListError(f"{sym} 已在生效交易名单中")
        if sym in snap.pending:
            raise TradingListError(f"{sym} 已有待确认的加入请求")
        elig = check_eligibility(profile, current_count=len(snap.active), criteria=self._criteria)
        if not elig.eligible:
            raise TradingListError(f"{sym} 未通过交易名单资格检查：{'；'.join(elig.reasons)}")
        self._append(
            sym,
            "request_add",
            actor,
            at,
            reason,
            {"asset_class": profile.instrument.asset_class.value},
        )
        return elig

    def confirm_add(
        self,
        symbol: str,
        *,
        actor: str,
        confirmation_phrase: str,
        at: datetime,
    ) -> TradingListSnapshot:
        """二次确认加入交易名单（ADR-16）：必须存在待确认请求、未达硬上限且确认短语精确匹配。"""
        _require_owner(actor)
        _require_tz(at)
        if confirmation_phrase != CONFIRM_TRADING_LIST_PHRASE:
            raise TradingListError(f"确认短语不正确，请输入：{CONFIRM_TRADING_LIST_PHRASE}")
        snap = self.snapshot()
        if symbol not in snap.pending:
            raise TradingListError(f"{symbol} 没有待确认的加入请求")
        if len(snap.active) >= self._criteria.max_list_size:
            raise TradingListError(f"交易名单已达硬上限 {self._criteria.max_list_size}，拒绝确认")
        self._append(symbol, "confirm_add", actor, at, "二次确认生效", {})
        return self.snapshot()

    def cancel_pending(
        self, symbol: str, *, actor: str, reason: str, at: datetime
    ) -> TradingListSnapshot:
        _require_owner(actor)
        _require_tz(at)
        snap = self.snapshot()
        if symbol not in snap.pending:
            raise TradingListError(f"{symbol} 没有可撤回的待确认请求")
        self._append(symbol, "cancel_pending", actor, at, reason or "撤回待确认", {})
        return self.snapshot()

    def remove(self, symbol: str, *, actor: str, reason: str, at: datetime) -> TradingListSnapshot:
        """移出交易名单（调严）：立即生效并写只追加记录；已有持仓减仓与保护性止损照常。"""
        _require_owner(actor)
        _require_tz(at)
        if not reason.strip():
            raise TradingListError("移出交易名单必须填写原因")
        snap = self.snapshot()
        if symbol not in snap.active and symbol not in snap.pending:
            raise TradingListError(f"{symbol} 不在交易名单或待确认列表中")
        self._append(symbol, "remove", actor, at, reason, {})
        return self.snapshot()

    def _append(  # noqa: PLR0913, PLR0917
        self,
        symbol: str,
        action: str,
        actor: str,
        at: datetime,
        reason: str,
        detail: dict[str, Any],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO trading_list_events (
                    symbol, action, actor, occurred_at, reason, detail_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    action,
                    actor,
                    at.astimezone(UTC).isoformat(timespec="microseconds"),
                    reason,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                ),
            )


def _require_owner(actor: str) -> None:
    if not actor.strip():
        raise TradingListError("操作者不能为空")


def _require_tz(at: datetime) -> None:
    if at.tzinfo is None or at.utcoffset() is None:
        raise TradingListError("时间必须带时区")


__all__ = (
    "CONFIRM_TRADING_LIST_PHRASE",
    "TRADING_LIST_NOT_MEMBER",
    "EligibilityCriteria",
    "EligibilityResult",
    "SymbolProfile",
    "TradingListError",
    "TradingListSnapshot",
    "TradingListState",
    "TradingListStore",
    "check_eligibility",
)
