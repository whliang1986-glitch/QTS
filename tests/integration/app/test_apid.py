"""apid 入口（S-02 / S-03）：监听与 TLS 检查、单写者锁、api.db 令牌认证与审计、令牌管理命令。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from server.api.security import TokenAuthenticator
from server.app.apid import (
    ApidSettings,
    ReadOnlyLedger,
    api_db_lock,
    build_apid_app,
    check_listen,
    run_apid,
    uvicorn_config,
)
from server.app.supervisor import EXIT_BUSY, EXIT_OK, EXIT_REFUSED
from server.app.tokens import token_main
from server.platforms.base import UnavailableChannel
from server.platforms.files import InstanceLock
from server.storage.apidb import ApiDb
from server.storage.errors import StorageError
from server.storage.migrate import upgrade

APID_TOML = """[apid]
host = "127.0.0.1"
port = 8730
api_db = "data/api.db"
tls_cert = ""
tls_key = ""
confirmation_seconds = 60
"""


def settings(tmp_path: Path, host: str = "127.0.0.1", **kw: object) -> ApidSettings:
    return ApidSettings(
        runtime_dir=tmp_path, ledger_path=tmp_path / "ledger.db", api_db=tmp_path / "api.db",
        tradingd_account="x", host=host, lock_wait_seconds=0.2, **kw,  # type: ignore[arg-type]
    )  # fmt: skip


def app_with_token(tmp_path: Path, role: str = "owner") -> tuple[TestClient, ApiDb, str]:
    db = ApiDb(tmp_path / "api.db")
    _, token = db.issue("dylan", role, days=30, actor="test")
    app = build_apid_app(settings(tmp_path), UnavailableChannel(), TokenAuthenticator(db), db, db)
    return TestClient(app), db, token


# ---------- 监听与 TLS ----------


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com", "::"])  # noqa: S104
def test_non_loopback_without_tls_is_refused(tmp_path: Path, host: str) -> None:
    assert check_listen(settings(tmp_path, host)) is not None


@pytest.mark.parametrize("host", ["localhost", "my-server.local"])
def test_host_names_are_not_trusted_as_loopback(tmp_path: Path, host: str) -> None:
    assert check_listen(settings(tmp_path, host)) is not None  # hosts 文件可能被改


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_loopback_without_tls_is_allowed(tmp_path: Path, host: str) -> None:
    assert check_listen(settings(tmp_path, host)) is None


def test_tls_needs_both_files_present(tmp_path: Path) -> None:
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    assert "同时配置" in (check_listen(settings(tmp_path, "0.0.0.0", tls_cert=cert)) or "")  # noqa: S104
    assert "不存在" in (
        check_listen(settings(tmp_path, "0.0.0.0", tls_cert=cert, tls_key=key)) or ""  # noqa: S104
    )
    cert.write_text("x", encoding="utf-8")
    key.write_text("x", encoding="utf-8")
    broken = check_listen(settings(tmp_path, "0.0.0.0", tls_cert=cert, tls_key=key))  # noqa: S104
    assert broken is not None and "无法加载" in broken  # 证书坏了启动前就拒绝


@pytest.mark.asyncio
async def test_run_refuses_insecure_listen(tmp_path: Path) -> None:
    code = await run_apid(settings(tmp_path, "0.0.0.0"), UnavailableChannel(), asyncio.Event())  # noqa: S104
    assert code == EXIT_REFUSED
    assert not (tmp_path / "api.db").exists()  # 拒绝在打开 api.db 之前


@pytest.mark.asyncio
async def test_second_apid_is_busy(tmp_path: Path) -> None:
    with InstanceLock(api_db_lock(tmp_path / "api.db")):
        code = await run_apid(settings(tmp_path), UnavailableChannel(), asyncio.Event())
    assert code == EXIT_BUSY


@pytest.mark.asyncio
async def test_broken_api_db_is_refused(tmp_path: Path) -> None:
    (tmp_path / "api.db").write_bytes(b"not a database" * 100)
    code = await run_apid(settings(tmp_path), UnavailableChannel(), asyncio.Event())
    assert code == EXIT_REFUSED


@pytest.mark.asyncio
async def test_apid_starts_and_stops_cleanly(tmp_path: Path) -> None:
    stop = asyncio.Event()
    task = asyncio.ensure_future(run_apid(settings(tmp_path, port=0), UnavailableChannel(), stop))
    await asyncio.sleep(0.5)
    stop.set()
    assert await asyncio.wait_for(task, 10) == EXIT_OK


# ---------- api.db 认证与审计 ----------


def test_issued_token_authenticates_and_is_audited(tmp_path: Path) -> None:
    upgrade(tmp_path / "ledger.db")
    client, db, token = app_with_token(tmp_path)
    response = client.get("/v1/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.json() == {"name": "dylan", "role": "owner"}
    assert client.get("/v1/whoami", headers={"Authorization": "Bearer qts_bad"}).status_code == 401
    failed = db.audits("auth_failed")
    assert failed and failed[-1][3]["reason"] == "令牌无效"
    assert all(token not in str(a) for a in db.audits())  # 明文不进审计


def test_revoked_token_is_rejected_immediately(tmp_path: Path) -> None:
    client, db, token = app_with_token(tmp_path)
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/v1/whoami", headers=headers).status_code == 200
    db.revoke(db.tokens()[0].token_id, actor="test", reason="演练")
    assert client.get("/v1/whoami", headers=headers).status_code == 401


def test_command_is_audited_and_not_sent_without_tradingd(tmp_path: Path) -> None:
    upgrade(tmp_path / "ledger.db")
    client, db, token = app_with_token(tmp_path)
    body = {"request_id": "req-0001", "name": "reconcile", "params": {}}
    response = client.post("/v1/commands", json=body, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 503
    assert response.json()["retry_same_request_id"] is False
    outcome = db.audits("command_outcome")[-1][3]
    assert outcome["outcome"] == "not_delivered" and outcome["request_id"] == "req-0001"
    assert db.audits("command_submitted")


def test_ledger_missing_is_503_then_recovers(tmp_path: Path) -> None:
    client, _, token = app_with_token(tmp_path, role="viewer")
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/v1/status", headers=headers).status_code == 503
    upgrade(tmp_path / "ledger.db")
    assert client.get("/v1/status", headers=headers).status_code == 200


def test_read_only_ledger_refuses_writes(tmp_path: Path) -> None:
    upgrade(tmp_path / "ledger.db")
    repo = ReadOnlyLedger(tmp_path / "ledger.db")()
    with pytest.raises(StorageError):
        repo.record_command("req-0001", "c", "{}", datetime.now(UTC))


# ---------- 令牌管理命令 ----------


def cli(tmp_path: Path, *args: str) -> int:
    config = tmp_path / "apid.toml"
    config.write_text(APID_TOML, encoding="utf-8")
    return token_main(["--apid-config", str(config), "--root", str(tmp_path), *args],
                      default_config=config, root=tmp_path)  # fmt: skip


def test_cli_issue_list_revoke(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(tmp_path, "issue", "--name", "dylan", "--role", "owner", "--days", "90") == EXIT_OK
    out = capsys.readouterr().out
    token = out.split("：")[-1].strip()
    db = ApiDb(tmp_path / "data" / "api.db")
    assert db.verify(token) is not None
    token_id = db.tokens()[0].token_id
    db.close()
    assert cli(tmp_path, "list") == EXIT_OK
    listing = capsys.readouterr().out
    assert token_id in listing and token not in listing
    assert cli(tmp_path, "revoke", "--id", token_id, "--reason", "换新") == EXIT_OK
    db = ApiDb(tmp_path / "data" / "api.db")
    assert db.verify(token) is None
    db.close()


@pytest.mark.parametrize("days", ["0", "366", "-1"])
def test_cli_refuses_bad_lifetime(tmp_path: Path, days: str) -> None:
    code = cli(tmp_path, "issue", "--name", "dylan", "--role", "owner", "--days", days)
    assert code == EXIT_REFUSED


def test_cli_refuses_while_apid_runs(tmp_path: Path) -> None:
    with InstanceLock(api_db_lock(tmp_path / "data" / "api.db")):
        code = cli(tmp_path, "issue", "--name", "dylan", "--role", "owner", "--days", "30")
    assert code == EXIT_BUSY


def test_expired_tokens_fail(tmp_path: Path) -> None:
    now = [datetime(2026, 11, 3, tzinfo=UTC)]
    db = ApiDb(tmp_path / "api.db", clock=lambda: now[0])
    _, token = db.issue("dylan", "owner", days=1, actor="t")
    assert db.verify(token) is not None
    now[0] += timedelta(days=1)
    assert db.verify(token) is None


def test_proxy_headers_are_never_trusted(tmp_path: Path) -> None:
    """限流与审计的来源只取 TCP 对端地址：X-Forwarded-For 之类的头一律不认。"""
    client, _, _ = app_with_token(tmp_path)
    config = uvicorn_config(client.app, settings(tmp_path))  # type: ignore[arg-type]
    assert config.proxy_headers is False
    assert config.forwarded_allow_ips in ("", [], None)


def test_forwarded_header_does_not_change_the_source(tmp_path: Path) -> None:
    """应用层不读转发头。注意 TestClient 不经过 uvicorn：uvicorn 层的保证由上一条测试守着。"""
    from server.api.security import FailureLimiter  # noqa: PLC0415

    db = ApiDb(tmp_path / "api.db")
    from server.api.app import ApiDeps, create_app  # noqa: PLC0415
    from server.api.commands import IpcCommandGateway  # noqa: PLC0415

    async def nowhere() -> object:
        raise ConnectionRefusedError

    deps = ApiDeps(
        authenticator=TokenAuthenticator(db), ledger=ReadOnlyLedger(tmp_path / "l.db"),
        gateway=IpcCommandGateway(nowhere), audit=db,  # type: ignore[arg-type]
        limiter=FailureLimiter(max_failures=3),
    )  # fmt: skip
    client = TestClient(create_app(deps))
    codes = [
        client.get(
            "/v1/whoami", headers={"Authorization": "Bearer x", "X-Forwarded-For": f"10.0.0.{i}"}
        ).status_code
        for i in range(4)
    ]
    assert codes == [401, 401, 401, 429]


def test_online_revoke_takes_effect_immediately(tmp_path: Path) -> None:
    client, db, token = app_with_token(tmp_path)
    headers = {"Authorization": f"Bearer {token}"}
    _, other = db.issue("guest", "viewer", days=10, actor="t")
    other_id = db.tokens()[1].token_id
    body = {"reason": "疑似泄露"}
    assert (
        client.post(f"/v1/tokens/{other_id}/revoke", json=body, headers=headers).status_code == 200
    )
    assert client.get("/v1/whoami", headers={"Authorization": f"Bearer {other}"}).status_code == 401
    assert db.audits("token_revoked")[-1][2] == "dylan"
    missing = client.post("/v1/tokens/0123456789abcdef/revoke", json=body, headers=headers)
    assert missing.status_code == 404
    blank = client.post(f"/v1/tokens/{other_id}/revoke", json={"reason": "  "}, headers=headers)
    assert blank.status_code == 422
    again = client.post(f"/v1/tokens/{other_id}/revoke", json=body, headers=headers)
    assert again.status_code == 200  # 已吊销：幂等


def test_viewer_cannot_revoke(tmp_path: Path) -> None:
    client, db, token = app_with_token(tmp_path, role="viewer")
    token_id = db.tokens()[0].token_id
    response = client.post(
        f"/v1/tokens/{token_id}/revoke", json={"reason": "x"},
        headers={"Authorization": f"Bearer {token}"},
    )  # fmt: skip
    assert response.status_code == 403


def test_stream_refuses_expired_and_revoked_tokens(tmp_path: Path) -> None:
    from starlette.websockets import WebSocketDisconnect  # noqa: PLC0415

    upgrade(tmp_path / "ledger.db")
    now = [datetime.now(UTC)]
    db = ApiDb(tmp_path / "api.db", clock=lambda: now[0])
    _, short = db.issue("dylan", "owner", days=1, actor="t")
    _, revoked = db.issue("guest", "viewer", days=10, actor="t")
    db.revoke(db.tokens()[1].token_id, actor="t", reason="x")
    now[0] += timedelta(days=2)
    app = build_apid_app(settings(tmp_path), UnavailableChannel(), TokenAuthenticator(db), db)
    client = TestClient(app)
    for token in (short, revoked):
        with client.websocket_connect("/v1/stream") as ws:
            ws.send_json({"type": "auth", "token": token})
            assert ws.receive_json()["type"] == "error"
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 4401


def test_tws_port_stays_on_loopback() -> None:
    """V4.1 10.2：TWS API 端口只绑本机。规则在 B-03a 的连接策略里，这里做回归引用。"""
    from server.broker.ibkr.connection import ConnectionPolicy, ConnectionPolicyError  # noqa: PLC0415
    from server.domain.enums import AccountType, Mode  # noqa: PLC0415

    with pytest.raises(ConnectionPolicyError, match="本机"):
        ConnectionPolicy(
            mode=Mode.PAPER, host="192.168.1.10", port=7497, client_id=1, readonly=True,
            expected_account_id="DU0000001", expected_account_type=AccountType.CASH,
        )  # fmt: skip
