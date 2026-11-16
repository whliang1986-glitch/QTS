"""S-14（自选标的与交易名单三层隔离）与 S-10（待审批队列）集成测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from server.api.app import ApiDeps, create_app
from server.api.security import MemoryAudit
from server.domain.enums import AssetClass, OrderClass, RiskVerdict, Side
from server.domain.instrument import Instrument
from server.risk.engine import RiskContext, Subject
from server.risk.trading_list import (
    CONFIRM_TRADING_LIST_PHRASE,
    TRADING_LIST_NOT_MEMBER,
    EligibilityCriteria,
    SymbolProfile,
    TradingListError,
    TradingListRule,
    TradingListState,
    TradingListStore,
)
from server.storage.approval_queue import (
    ApprovalError,
    ApprovalKind,
    ApprovalQueue,
    ApprovalStatus,
)
from server.storage.watchlist import QuoteSubscriptionStatus, WatchlistStore
from tests.support.oms import SPY, Env
from tests.unit.api.test_app import Gateway, Tokens

T0 = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
QQQ = Instrument(
    symbol="QQQ",
    asset_class=AssetClass.ETF,
    exchange="NASDAQ",
    currency="USD",
    tick_size=Decimal("0.01"),
    trading_calendar="XNYS",
)
AAPL = Instrument(
    symbol="AAPL",
    asset_class=AssetClass.STK,
    exchange="NASDAQ",
    currency="USD",
    tick_size=Decimal("0.01"),
    trading_calendar="XNYS",
)


def _profile(
    inst: Instrument,
    *,
    adv: Decimal = Decimal("100000000"),
    spread: Decimal = Decimal("5"),
    days: int = 1000,
    leveraged: bool = False,
) -> SymbolProfile:
    return SymbolProfile(
        instrument=inst,
        avg_dollar_volume_60d=adv,
        median_spread_bps=spread,
        listing_days=days,
        is_leveraged_or_inverse=leveraged,
    )


def test_s14_three_layer_isolation_eligibility_and_two_step_confirm(tmp_path: Path) -> None:
    """S-14 验收项 2、3、4、5、6：三层隔离、资格检查、硬上限、二次确认生效与移出立即生效。"""
    env = Env(tmp_path, positions={})
    w_store = WatchlistStore(tmp_path / "watchlist.db", max_subscriptions=2)
    t_store = TradingListStore(
        tmp_path / "trading_list.db",
        criteria=EligibilityCriteria(max_list_size=1),
    )
    rule = TradingListRule(lambda: t_store.snapshot().active)

    # 1. 加入自选不会开放交易权限：TradingListRule 依然拒绝 OPEN 买单
    w_store.upsert("SPY", group_name="core", tags=("etf",), note="核心宽基", actor="dylan", at=T0)
    open_intent = env.make_intent("i-open-1", symbol="SPY", order_class=OrderClass.OPEN)
    ctx = RiskContext(now=T0, positions=(), open_orders=(), quotes={}, last_prices={})
    res_before = rule.check(Subject.new_order(open_intent), ctx)
    assert res_before is not None
    assert res_before.verdict is RiskVerdict.REJECT
    assert res_before.message == TRADING_LIST_NOT_MEMBER

    # 2. 资格检查不合格（如个股 / 杠杆 ETF / 流动性不足）：服务端拒绝加入交易名单
    with pytest.raises(TradingListError, match="仅限美股 ETF 现货"):
        t_store.request_add(_profile(AAPL), actor="dylan", reason="尝试加个股", at=T0)
    with pytest.raises(TradingListError, match="杠杆或反向"):
        t_store.request_add(_profile(QQQ, leveraged=True), actor="dylan", reason="杠杆", at=T0)
    with pytest.raises(TradingListError, match="日均成交额"):
        t_store.request_add(
            _profile(QQQ, adv=Decimal("1000000")), actor="dylan", reason="低流动性", at=T0
        )

    # 3. 发起加入 SPY（生成待确认），确认前仍按“不在名单”拒绝
    t_store.request_add(_profile(SPY), actor="dylan", reason="基准宽基", at=T0)
    assert t_store.state_of("SPY") is TradingListState.PENDING_CONFIRMATION
    assert rule.check(Subject.new_order(open_intent), ctx) is not None

    # 4. 错误确认短语被拒绝，正确确认短语才生效
    with pytest.raises(TradingListError, match="确认短语不正确"):
        t_store.confirm_add("SPY", actor="dylan", confirmation_phrase="WRONG", at=T0)
    t_store.confirm_add(
        "SPY", actor="dylan", confirmation_phrase=CONFIRM_TRADING_LIST_PHRASE, at=T0
    )
    assert t_store.state_of("SPY") is TradingListState.IN_TRADING_LIST
    assert rule.check(Subject.new_order(open_intent), ctx) is None

    # 5. 达到硬上限 max_list_size=1 后，不能再加入 QQQ
    with pytest.raises(TradingListError, match="硬上限 1"):
        t_store.request_add(_profile(QQQ), actor="dylan", reason="超上限测试", at=T0)

    # 6. 移出交易名单立即生效；移出后新开仓被拒，但减仓与保护性止损照常放行
    t_store.remove("SPY", actor="dylan", reason="移出观察", at=T0)
    assert rule.check(Subject.new_order(open_intent), ctx) is not None
    reduce_intent = env.make_intent(
        "i-red-1", symbol="SPY", side=Side.SELL, order_class=OrderClass.REDUCE
    )
    assert rule.check(Subject.new_order(reduce_intent), ctx) is None


def test_s14_and_s10_api_permissions_watchlist_quotes_and_approvals(tmp_path: Path) -> None:
    """S-14 验收项 1、8 与 S-10：API 权限、订阅上限与延迟标注、待审批队列批准/拒绝/过期。"""
    w_store = WatchlistStore(tmp_path / "watchlist.db", max_subscriptions=2)
    t_store = TradingListStore(tmp_path / "trading_list.db")
    q_store = ApprovalQueue(tmp_path / "approvals.db")
    audit = MemoryAudit()
    profiles = {
        "SPY": _profile(SPY),
        "QQQ": _profile(QQQ),
        "AAPL": _profile(AAPL),
    }

    app = create_app(
        ApiDeps(
            authenticator=Tokens(),
            ledger=lambda: None,  # type: ignore[arg-type, return-value]
            gateway=Gateway(),
            audit=audit,
            watchlist=w_store,
            trading_list=t_store,
            symbol_profiles=profiles.get,
            quote_delayed_map=lambda: {"SPY": False, "QQQ": True, "AAPL": False},
            approvals=q_store,
        )
    )
    client = TestClient(app)
    owner_hdr = {"Authorization": "Bearer owner-token"}
    viewer_hdr = {"Authorization": "Bearer viewer-token"}

    # viewer 不能改自选或发交易名单请求（403）
    assert (
        client.post("/v1/watchlist", headers=viewer_hdr, json={"symbol": "SPY"}).status_code == 403
    )

    # owner 添加 3 只自选（订阅上限为 2）：SPY 实时、QQQ 延迟、AAPL 超限未订阅
    for sym in ("SPY", "QQQ", "AAPL"):
        r = client.post("/v1/watchlist", headers=owner_hdr, json={"symbol": sym})
        assert r.status_code == 200
    wl = client.get("/v1/watchlist", headers=viewer_hdr).json()["items"]
    by_sym = {it["symbol"]: it for it in wl}
    assert by_sym["SPY"]["quote_status"] == QuoteSubscriptionStatus.REALTIME.value
    assert by_sym["QQQ"]["quote_status"] == QuoteSubscriptionStatus.DELAYED.value
    assert by_sym["AAPL"]["quote_status"] == QuoteSubscriptionStatus.UNSUBSCRIBED.value
    assert by_sym["AAPL"]["eligible"] is False
    assert "仅限美股 ETF 现货" in by_sym["AAPL"]["eligibility_reasons"][0]

    # 交易名单请求 -> 撤回 -> 再请求 -> 确认 -> 移出
    assert (
        client.post(
            "/v1/trading-list/requests",
            headers=owner_hdr,
            json={"symbol": "SPY", "reason": "加入基准"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/v1/trading-list/requests/SPY/cancel",
            headers=owner_hdr,
            json={"reason": "先撤回"},
        ).json()["pending"]
        == []
    )
    client.post(
        "/v1/trading-list/requests",
        headers=owner_hdr,
        json={"symbol": "SPY", "reason": "再次加入"},
    )
    r_conf = client.post(
        "/v1/trading-list/requests/SPY/confirm",
        headers=owner_hdr,
        json={"confirmation_phrase": CONFIRM_TRADING_LIST_PHRASE},
    )
    assert r_conf.status_code == 200
    assert r_conf.json()["active"] == ["SPY"]
    assert (
        client.post(
            "/v1/trading-list/SPY/remove", headers=owner_hdr, json={"reason": "移出"}
        ).json()["active"]
        == []
    )

    # 删除自选 AAPL
    assert client.delete("/v1/watchlist/AAPL", headers=owner_hdr).status_code == 200

    # S-10 待审批队列：提交 2 条，viewer 无权审批，owner 批准一条，过期一条不可再批
    now = datetime.now(UTC)
    q_store.submit(
        approval_id="apr-1",
        kind=ApprovalKind.ORDER_INTENT,
        title="买入 SPY 10 股",
        summary="事件驱动建议",
        proposer="AI:intel_agent",
        payload={"symbol": "SPY", "qty": "10"},
        at=now,
        expires_at=now + timedelta(hours=2),
    )
    q_store.submit(
        approval_id="apr-2",
        kind=ApprovalKind.FACTOR_PROMOTE,
        title="因子动量 v1 转正",
        summary="验证通过",
        proposer="AI:factor_bot",
        payload={"factor_id": "mom"},
        at=now,
        expires_at=now + timedelta(hours=2),
    )
    assert len(client.get("/v1/approvals", headers=viewer_hdr).json()["items"]) == 2
    assert (
        client.post(
            "/v1/approvals/apr-1/decide",
            headers=viewer_hdr,
            json={"approve": True, "reason": "ok"},
        ).status_code
        == 403
    )
    r_apr = client.post(
        "/v1/approvals/apr-1/decide",
        headers=owner_hdr,
        json={"approve": True, "reason": "确认执行"},
    )
    assert r_apr.status_code == 200
    assert r_apr.json()["status"] == ApprovalStatus.APPROVED.value

    # 过期审批项无法批准
    q_store.submit(
        approval_id="apr-exp",
        kind=ApprovalKind.STRATEGY_PROMOTE,
        title="过期提案",
        summary="超时",
        proposer="AI:bot",
        payload={},
        at=T0 - timedelta(hours=3),
        expires_at=T0 - timedelta(hours=1),
    )
    with pytest.raises(ApprovalError, match="已过期"):
        q_store.decide(
            "apr-exp",
            approve=True,
            actor_role="owner",
            actor_name="dylan",
            reason="ok",
            at=T0,
        )


def test_strategy_and_ai_layers_do_not_import_trading_list_or_approval() -> None:
    """S-14 验收项 7：策略与 AI 层不得调用交易名单变更或审批确认入口。"""
    server_root = Path(__file__).resolve().parents[3] / "server"
    for layer in ("strategy", "ai"):
        for py in (server_root / layer).rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            assert "trading_list" not in text, f"{py} 不得引用 trading_list"
            assert "CONFIRM_TRADING_LIST_PHRASE" not in text
