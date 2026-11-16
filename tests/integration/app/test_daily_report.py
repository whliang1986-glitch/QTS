"""日终报告（S-09d）：真实账本、真实对账记录；取不到、被截断都如实标出，不写成 0。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from server.api.app import ApiDeps, create_app
from server.app.apid import ApidSettings, ReadOnlyLedger, build_apid_app
from server.app.daily_report import FutureDayError, build_daily_report
from server.domain.enums import Mode
from server.domain.modes import HaltState
from server.domain.orders import Execution
from server.risk.reconcile import ReconKind as K
from server.storage.alertsdb import StoredAlert
from server.storage.apidb import ApiDb
from server.storage.ledger import MAX_VIEW_ROWS, LedgerRepository

from tests.integration.risk.test_reconcile import Rig
from tests.support.oms import T0, Env
from tests.unit.api.test_app import OWNER, VIEWER, Gateway, Tokens

DAY = T0.date()
LATER = T0 + timedelta(days=2)


def later() -> datetime:
    return LATER


def report(env: Env, alerts: Any = None, clock: Any = later) -> Any:
    return build_daily_report(DAY, ledger=lambda: env.repo, alerts=alerts, clock=clock)


class Alerts:
    def __init__(self, rows: list[StoredAlert] | None = None) -> None:
        self.rows = rows or []

    def recent(self, limit: int) -> list[StoredAlert]:
        return self.rows[:limit]


def stored(alert_id: int, at: datetime, **kw: Any) -> StoredAlert:
    return StoredAlert(
        alert_id=alert_id, key=f"k{alert_id}", level=kw.pop("level", "P1"), code="alert_test",
        params=kw.pop("params", {}), raised_at=at.isoformat(), **kw,
    )  # fmt: skip


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> Env:
    e = Env(tmp_path)
    await e.broker.connect()
    await e.pump()
    return e


async def _trading_day(env: Env) -> Rig:
    """基准、下单、部分成交、日终对账。"""
    rig = Rig(env)
    await rig.baseline()
    await env.submit("r-1", quantity=Decimal(10), limit_price=Decimal("101.00"))
    env.server.fill(env.oid("r-1"), Decimal(4), Decimal("100.50"))
    await env.pump()
    await rig.recon.reconcile(K.END_OF_DAY)
    return rig


@pytest.mark.asyncio
async def test_a_trading_day_is_summed_from_the_ledger(env: Env) -> None:
    await _trading_day(env)
    r = report(env, Alerts([stored(1, T0, level="P0"), stored(2, T0 - timedelta(days=1))]))
    assert r.final and r.complete
    assert r.data["orders"]["count"] == 1
    assert r.data["orders"]["by_class"] == {"OPEN": 1}
    assert r.data["executions"]["count"] == 1
    spy = r.data["executions"]["by_symbol"]["SPY"]
    assert Decimal(spy["buy_quantity"]) == Decimal(4)
    assert Decimal(spy["buy_amount"]) == Decimal("402.00")
    assert isinstance(spy["buy_amount"], str)  # 金额不是浮点数
    assert r.data["recon_result"]["end_of_day"] == "clean"
    assert r.data["recon_result"]["settled"] is True
    assert r.data["recon_result"]["not_checked"][0]["subject"] == "cash_commission"
    assert r.data["alerts"]["count"] == 1  # 昨天那条不算
    assert r.data["alerts"]["by_level"] == {"P0": 1}
    assert r.data["alerts"]["unresolved"] == 1
    json.dumps(r.data, allow_nan=False)  # 是严格的 JSON
    assert "402.0" in r.markdown and "现金" in r.markdown
    assert "不完整" not in r.markdown


@pytest.mark.asyncio
async def test_superseded_executions_are_not_counted(env: Env) -> None:
    await _trading_day(env)
    original = env.repo.effective_executions_for("r-1")[0]
    data = original.model_dump()
    data.update(execution_id="fix-1", quantity=Decimal(3))
    assert env.repo.add_correction(Execution.model_validate(data), original.execution_id, T0)
    r = report(env, Alerts())
    assert r.data["executions"]["count"] == 1
    assert Decimal(r.data["executions"]["by_symbol"]["SPY"]["buy_quantity"]) == Decimal(3)


def test_the_window_is_the_utc_day_and_its_edges_are_exact(env: Env) -> None:
    start = datetime(DAY.year, DAY.month, DAY.day, tzinfo=UTC)
    env.repo.add_intent(env.make_intent("before", created_at=start - timedelta(microseconds=1)))
    env.repo.add_intent(env.make_intent("first", created_at=start))
    env.repo.add_intent(env.make_intent("last", created_at=start + timedelta(hours=23, minutes=59)))
    env.repo.add_intent(env.make_intent("next", created_at=start + timedelta(days=1)))
    assert report(env, Alerts()).data["orders"]["count"] == 2


def test_no_end_of_day_record_is_said_plainly_not_passed(env: Env) -> None:
    r = report(env, Alerts())
    assert r.data["recon_result"]["end_of_day"] == "missing"
    assert "没有日终对账记录" in r.markdown
    assert "通过" not in r.markdown.split("## 日终对账")[1].split("##")[0].split("盘中")[0]


@pytest.mark.asyncio
async def test_a_not_clean_end_of_day_is_reported_with_its_items(env: Env) -> None:
    rig = await _trading_day(env)
    env.server.apply_split("SPY", Decimal(2), Decimal(1))
    await rig.recon.reconcile(K.END_OF_DAY)
    r = report(env, Alerts())
    assert r.data["recon_result"]["end_of_day"] == "not_clean"
    assert any(i["subject"] == "SPY" for i in r.data["recon_result"]["items"])
    assert "未通过" in r.markdown
    # 对账不干净进入 SAFE HALT：HALT 也要出现在报告里
    assert r.data["modes"]["halted_at_day_end"] is True
    assert "日末是否 HALT：是" in r.markdown


def test_halts_and_the_mode_at_day_end_are_reported(env: Env) -> None:
    halt = HaltState(halted=True, reason="急停 密码=halt-pass", since=T0, set_by="dylan")
    env.repo.append_mode_change("mc-1", Mode.PAPER, HaltState(halted=False), T0, "system")
    env.repo.append_mode_change("mc-2", Mode.HALT, halt, T0 + timedelta(hours=1), "dylan")
    env.repo.append_mode_change("mc-3", Mode.PAPER, HaltState(halted=False), LATER, "dylan")
    r = report(env, Alerts())
    assert r.data["modes"]["at_day_end"] == "HALT"  # 第二天恢复的不算
    assert [c["mode"] for c in r.data["modes"]["changes"]] == ["PAPER", "HALT"]
    assert r.data["modes"]["changes"][1]["reason"].startswith("急停")
    assert "halt-pass" not in json.dumps(r.data, ensure_ascii=False)
    assert "halt-pass" not in r.markdown


def test_a_halt_carried_over_from_yesterday_still_shows(env: Env) -> None:
    halt = HaltState(halted=True, reason="昨天的", since=T0, set_by="dylan")
    env.repo.append_mode_change("mc-1", Mode.HALT, halt, T0 - timedelta(days=1), "dylan")
    r = report(env, Alerts())
    assert r.data["modes"]["changes"] == []
    assert r.data["modes"]["halted_at_day_end"] is True


def test_no_mode_record_is_unknown_not_normal(env: Env) -> None:
    r = report(env, Alerts())
    assert r.data["modes"]["at_day_end"] is None
    assert r.data["modes"]["halted_at_day_end"] is None
    assert "日末是否 HALT：未知" in r.markdown


class Broken:
    def recent(self, limit: int) -> list[StoredAlert]:
        raise OSError("password=alert-secret 读不到")


def test_a_missing_section_is_not_zero(env: Env) -> None:
    r = report(env, Broken())
    assert not r.complete
    assert r.data["alerts"] is None
    assert "alerts" in r.data["errors"]
    assert "alert-secret" not in json.dumps(r.data, ensure_ascii=False)
    assert "alert-secret" not in r.markdown
    assert "报告不完整" in r.markdown and "取不到" in r.markdown
    assert r.data["orders"]["count"] == 0  # 其他部分照常


def test_no_alert_database_is_not_zero_alerts(env: Env) -> None:
    r = report(env, None)
    assert not r.complete and r.data["alerts"] is None
    assert "没有配置告警库" in r.data["errors"]["alerts"]


def test_a_broken_ledger_marks_every_ledger_section(env: Env) -> None:
    def broken() -> LedgerRepository:
        raise OSError("打不开")

    r = build_daily_report(DAY, ledger=broken, alerts=Alerts(), clock=later)
    assert not r.complete
    for name in ("orders", "executions", "recon_result", "modes"):
        assert r.data[name] is None and name in r.data["errors"]
    assert r.data["alerts"] is not None


def test_unreadable_recon_records_are_not_a_missing_record(env: Env) -> None:
    class BadRecords(LedgerRepository):
        def audits_between(self, *_a: Any, **_k: Any) -> list[Any]:
            raise ValueError("读不懂")

    repo = BadRecords(env.repo._engine)
    r = build_daily_report(DAY, ledger=lambda: repo, alerts=Alerts(), clock=later)
    assert r.data["recon_result"] is None
    assert "这一天没有日终对账记录" not in r.markdown


def test_truncated_orders_are_marked_incomplete(env: Env) -> None:
    for i in range(MAX_VIEW_ROWS):
        env.repo.add_intent(env.make_intent(f"bulk-{i}"))
    r = report(env, Alerts())
    assert not r.complete and r.data["truncated"] == ["orders"]
    assert "条数到了上限" in r.markdown


def test_truncated_alerts_are_marked_only_if_today_may_be_cut(env: Env) -> None:
    today = [stored(i, T0) for i in range(MAX_VIEW_ROWS)]
    assert report(env, Alerts(today)).data["truncated"] == ["alerts"]
    older = [
        *[stored(i, T0) for i in range(MAX_VIEW_ROWS - 1)],
        stored(9999, T0 - timedelta(days=3)),
    ]
    assert report(env, Alerts(older)).complete


def test_truncated_recon_without_end_of_day_is_unknown_not_missing(env: Env) -> None:
    class Many(LedgerRepository):
        def audits_between(self, *_a: Any, **_k: Any) -> list[Any]:
            return [_intraday_audit(i) for i in range(MAX_VIEW_ROWS)]

    repo = Many(env.repo._engine)
    r = build_daily_report(DAY, ledger=lambda: repo, alerts=Alerts(), clock=later)
    assert r.data["recon_result"]["end_of_day"] == "unknown"
    assert "recon_result" in r.data["truncated"]


def _intraday_audit(i: int) -> Any:
    from server.domain.modes import AuditEvent  # noqa: PLC0415

    return AuditEvent(
        event_id=f"a{i}", occurred_at=T0, actor="system", action="x", correlation_id="c",
        detail=(("recon_id", json.dumps(f"r{i}")), ("kind", json.dumps("INTRADAY")),
                ("clean", json.dumps("True")), ("settled", json.dumps("True")),
                ("items", json.dumps("[]"))),
    )  # fmt: skip


def test_today_is_marked_not_final_and_tomorrow_is_refused(env: Env) -> None:
    r = report(env, Alerts(), clock=lambda: T0)
    assert not r.final
    assert "还没结束" in r.markdown
    with pytest.raises(FutureDayError):
        report(env, Alerts(), clock=lambda: T0 - timedelta(days=1))
    with pytest.raises(ValueError, match="时区"):
        report(env, Alerts(), clock=lambda: datetime(2026, 10, 25, 14, 0))  # noqa: DTZ001
    with pytest.raises(ValueError, match="date"):
        build_daily_report(T0, ledger=lambda: env.repo, alerts=Alerts(), clock=later)
    assert report(env, Alerts(), clock=lambda: datetime(DAY.year, DAY.month, DAY.day, tzinfo=UTC))


def test_refuses_to_change_anything(env: Env) -> None:
    before = env.repo.last_order_event_seq()
    report(env, Alerts())
    assert env.repo.last_order_event_seq() == before


# ---------- 接口 ----------


def _client(tmp_path: Path, env: Env, **kw: Any) -> TestClient:
    args: dict[str, Any] = {
        "daily_reports": lambda day: build_daily_report(
            day, ledger=lambda: env.repo, alerts=Alerts(), clock=later
        )
    }
    args.update(kw)
    deps = ApiDeps(
        authenticator=Tokens(), ledger=lambda: env.repo, gateway=Gateway(),
        audit=ApiDb(tmp_path / "api.db"), **args,
    )  # fmt: skip
    return TestClient(create_app(deps))


ROUTE = "/v1/reports/daily"
AUTH_OWNER = {"Authorization": OWNER}
AUTH_VIEWER = {"Authorization": VIEWER}


def test_api_gives_both_formats_to_the_owner_only(env: Env, tmp_path: Path) -> None:
    client = _client(tmp_path, env)
    answer = client.get(ROUTE, params={"day": DAY.isoformat()}, headers=AUTH_OWNER)
    assert answer.status_code == 200
    body = answer.json()
    assert body["day"] == DAY.isoformat() and body["complete"] is True and body["final"] is True
    assert body["data"]["orders"]["count"] == 0
    assert body["markdown"].startswith(f"# 日终报告 {DAY}")
    assert answer.headers["Cache-Control"] == "no-store"
    assert (
        client.get(ROUTE, params={"day": DAY.isoformat()}, headers=AUTH_VIEWER).status_code == 403
    )
    assert client.get(ROUTE, params={"day": DAY.isoformat()}).status_code == 401


@pytest.mark.parametrize("day", ["", "2026-13-01", "not-a-date", "2026-10-23T00:00:00Z"])
def test_api_refuses_bad_days(env: Env, tmp_path: Path, day: str) -> None:
    answer = _client(tmp_path, env).get(ROUTE, params={"day": day}, headers=AUTH_OWNER)
    assert answer.status_code == 422


def test_api_refuses_future_days_and_hides_failures(env: Env, tmp_path: Path) -> None:
    tomorrow = (LATER + timedelta(days=1)).date().isoformat()
    client = _client(tmp_path, env)
    assert client.get(ROUTE, params={"day": tomorrow}, headers=AUTH_OWNER).status_code == 400

    def boom(_day: Any) -> Any:
        raise OSError("C:/secret/path")

    failing = _client(tmp_path, env, daily_reports=boom)
    answer = failing.get(ROUTE, params={"day": DAY.isoformat()}, headers=AUTH_OWNER)
    assert answer.status_code == 503 and "secret" not in answer.text
    missing = _client(tmp_path, env, daily_reports=None)
    assert (
        missing.get(ROUTE, params={"day": DAY.isoformat()}, headers=AUTH_OWNER).status_code == 503
    )


def test_apid_wires_the_report_read_only(env: Env, tmp_path: Path) -> None:
    settings = ApidSettings(
        runtime_dir=tmp_path, ledger_path=tmp_path / "ledger.db", api_db=tmp_path / "api.db",
        tradingd_account="svc",
    )  # fmt: skip
    ledger = ReadOnlyLedger(settings.ledger_path)
    r = build_daily_report(DAY, ledger=ledger, alerts=None, clock=later)
    assert r.data["orders"]["count"] == 0
    assert callable(build_apid_app)
