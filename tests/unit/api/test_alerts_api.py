"""apid 告警与审计接口（S-05a）：读不了是 503 不是空列表；确认只有 owner、操作者取令牌、幂等。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from server.api.app import ApiDeps, create_app
from server.guard.alerts import Alert, AlertHub, Level
from server.storage.alertsdb import (
    AlertsReader,
    AlertStore,
    StoredAlert,
    read_ack_requests,
    write_status,
)
from server.storage.apidb import ApiDb
from tests.unit.api.test_app import OWNER, VIEWER, Gateway, Tokens


class BrokenSource:
    def recent(self, limit: int) -> list[StoredAlert]:
        raise OSError("disk")

    def one(self, alert_id: int) -> tuple[str, StoredAlert | None]:
        raise OSError("disk")


def client(tmp_path: Path, **kw: object) -> tuple[TestClient, ApiDb]:
    db = ApiDb(tmp_path / "api.db")
    deps = ApiDeps(
        authenticator=Tokens(), ledger=lambda: None,  # type: ignore[arg-type, return-value]
        gateway=Gateway(), audit=db, **kw,  # type: ignore[arg-type]
    )  # fmt: skip
    return TestClient(create_app(deps)), db


@pytest.fixture
def setup(tmp_path: Path) -> tuple[TestClient, ApiDb, AlertHub, Path]:
    alerts = tmp_path / "alerts.db"
    hub = AlertHub(None, store=AlertStore(alerts))
    hub.raise_alert(Alert(Level.P0, "tradingd_heartbeat_lost", {"seconds": "90"}, key="hb"))
    hub.raise_alert(Alert(Level.P1, "broker_disconnected", {"detail": "x"}, key="b"))
    hub.resolve("b")
    write_status(alerts, store_ok=True, at=datetime.now(UTC))  # guardd 的健康标记
    db = ApiDb(tmp_path / "api.db")
    deps = ApiDeps(
        authenticator=Tokens(), ledger=lambda: None,  # type: ignore[arg-type, return-value]
        gateway=Gateway(), audit=db, alerts=AlertsReader(alerts), acks=db, audit_log=db,
    )  # fmt: skip
    return TestClient(create_app(deps)), db, hub, tmp_path / "api.db"


@pytest.mark.parametrize("path", ["/v1/alerts", "/v1/audit"])
def test_routes_need_a_token(setup: tuple[TestClient, ApiDb, AlertHub, Path], path: str) -> None:
    assert setup[0].get(path).status_code == 401
    assert setup[0].post("/v1/alerts/1/ack").status_code == 401


def test_alerts_listed_newest_first_with_both_languages(
    setup: tuple[TestClient, ApiDb, AlertHub, Path],
) -> None:
    c = setup[0]
    response = c.get("/v1/alerts", headers={"Authorization": VIEWER})  # viewer 可以看
    assert response.status_code == 200
    alerts = response.json()["alerts"]
    assert [a["key"] for a in alerts] == ["b", "hb"]
    assert alerts[0]["resolved_at"] is not None
    assert "90" in alerts[1]["text"]["zh"]
    assert "heartbeat" in alerts[1]["text"]["en"]
    assert alerts[1]["level"] == "P0"


def test_unreadable_alerts_are_503_not_empty(tmp_path: Path) -> None:
    for kw in ({}, {"alerts": BrokenSource()}, {"alerts": AlertsReader(tmp_path / "none.db")}):
        c, _ = client(tmp_path, **kw)
        response = c.get("/v1/alerts", headers={"Authorization": OWNER})
        assert response.status_code == 503
        assert response.json()["error"] == "无法获取告警"


def test_owner_ack_records_request_with_token_identity(
    setup: tuple[TestClient, ApiDb, AlertHub, Path],
) -> None:
    c, db, hub, api_path = setup
    hb = next(a for a in c.get("/v1/alerts", headers={"Authorization": OWNER}).json()["alerts"]
              if a["key"] == "hb")  # fmt: skip
    response = c.post(
        f"/v1/alerts/{hb['alert_id']}/ack",
        headers={"Authorization": OWNER},
        json={"actor": "mallory"},  # 请求体里的操作者被忽略
    )
    assert response.status_code == 200
    assert response.json() == {"alert_id": hb["alert_id"], "requested": True, "already": False}
    [req] = read_ack_requests(api_path, 0)
    assert (req.alert_id, req.alert_key, req.actor) == (hb["alert_id"], "hb", "dylan")
    assert req.store_id == hub.store_id  # 带上了告警库编号
    assert [a[2] for a in db.audits("alert_ack_requested")] == ["dylan"]
    pending = c.post(f"/v1/alerts/{hb['alert_id']}/ack", headers={"Authorization": OWNER})
    assert pending.json() == {"alert_id": hb["alert_id"], "requested": True, "already": False}
    assert len(read_ack_requests(api_path, 0)) == 1  # 已提交、guardd 未执行：不重复记录
    assert hub.acknowledge("hb", actor="dylan", alert_id=hb["alert_id"], store_id=req.store_id,
                           requested_at=req.at)  # guardd 执行后  # fmt: skip
    again = c.post(f"/v1/alerts/{hb['alert_id']}/ack", headers={"Authorization": OWNER})
    assert again.json()["already"] is True
    assert len(read_ack_requests(api_path, 0)) == 1  # 已确认：不重复记录


def test_viewer_cannot_ack(setup: tuple[TestClient, ApiDb, AlertHub, Path]) -> None:
    c, _, _, api_path = setup
    assert c.post("/v1/alerts/1/ack", headers={"Authorization": VIEWER}).status_code == 403
    assert read_ack_requests(api_path, 0) == []


def test_ack_unknown_or_resolved(setup: tuple[TestClient, ApiDb, AlertHub, Path]) -> None:
    c = setup[0]
    alerts = c.get("/v1/alerts", headers={"Authorization": OWNER}).json()["alerts"]
    resolved = next(a for a in alerts if a["key"] == "b")
    assert c.post("/v1/alerts/999/ack", headers={"Authorization": OWNER}).status_code == 404
    response = c.post(f"/v1/alerts/{resolved['alert_id']}/ack", headers={"Authorization": OWNER})
    assert response.status_code == 409
    assert c.post("/v1/alerts/0/ack", headers={"Authorization": OWNER}).status_code == 422


def test_ack_write_failure_is_503(setup: tuple[TestClient, ApiDb, AlertHub, Path]) -> None:
    c, db, _, _ = setup
    hb = next(a for a in c.get("/v1/alerts", headers={"Authorization": OWNER}).json()["alerts"]
              if a["key"] == "hb")  # fmt: skip
    db.close()
    response = c.post(f"/v1/alerts/{hb['alert_id']}/ack", headers={"Authorization": OWNER})
    assert response.status_code == 503


def test_audit_owner_only_newest_first(setup: tuple[TestClient, ApiDb, AlertHub, Path]) -> None:
    c, db, _, _ = setup
    db.audit("x", "dylan", {"n": "1"})
    db.audit("y", "dylan", {"n": "2"})
    assert c.get("/v1/audit", headers={"Authorization": VIEWER}).status_code == 403
    entries = c.get("/v1/audit?limit=2", headers={"Authorization": OWNER}).json()["entries"]
    assert [e["action"] for e in entries] == ["y", "x"]
    older = c.get(f"/v1/audit?before={entries[-1]['seq']}", headers={"Authorization": OWNER})
    assert all(e["seq"] < entries[-1]["seq"] for e in older.json()["entries"])


@pytest.mark.parametrize("marker", ["missing", "stale", "degraded"])
def test_untrusted_store_is_503(
    setup: tuple[TestClient, ApiDb, AlertHub, Path], tmp_path: Path, marker: str
) -> None:
    """评审 P2-2：guardd 没在运行或告警库已降级时，库里的记录可能不全：一律“无法获取告警”。"""
    alerts = tmp_path / "alerts.db"
    status = alerts.with_name("alerts.status.json")
    if marker == "missing":
        status.unlink()
    else:
        when = datetime.now(UTC) - timedelta(minutes=5 if marker == "stale" else 0)
        write_status(alerts, store_ok=marker != "degraded", at=when)
    c = setup[0]
    assert c.get("/v1/alerts", headers={"Authorization": OWNER}).json()["error"] == "无法获取告警"
    assert c.post("/v1/alerts/1/ack", headers={"Authorization": OWNER}).status_code == 503


def test_corrupt_audit_detail_is_503(setup: tuple[TestClient, ApiDb, AlertHub, Path]) -> None:
    import sqlite3  # noqa: PLC0415

    c, _, _, api_path = setup
    conn = sqlite3.connect(api_path)
    conn.execute(
        "INSERT INTO api_audit (at, action, actor, detail) VALUES ('t', 'a', 'b', '{\"n\": 1}')"
    )
    conn.commit()
    conn.close()
    assert c.get("/v1/audit", headers={"Authorization": OWNER}).status_code == 503


def test_audit_unavailable_is_503(tmp_path: Path) -> None:
    c, _ = client(tmp_path)
    assert c.get("/v1/audit", headers={"Authorization": OWNER}).status_code == 503
