"""apid 入口（S-02）：S-03 之前拒绝启动；只听回环地址；只读账本。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from server.api.auth import Principal, Role
from server.app.apid import ApidSettings, ReadOnlyLedger, build_apid_app, run_apid
from server.app.supervisor import EXIT_REFUSED
from server.platforms.base import UnavailableChannel
from server.storage.errors import StorageError
from server.storage.migrate import upgrade

AUTH = {"Authorization": "Bearer t"}


class Tokens:
    def authenticate(self, token: str) -> Principal | None:
        return Principal("dylan", Role.OWNER)


def settings(tmp_path: Path, host: str = "127.0.0.1") -> ApidSettings:
    return ApidSettings(
        runtime_dir=tmp_path, ledger_path=tmp_path / "ledger.db", tradingd_account="x", host=host
    )


@pytest.mark.asyncio
async def test_production_apid_refuses_without_authentication(tmp_path: Path) -> None:
    code = await run_apid(settings(tmp_path), UnavailableChannel(), asyncio.Event())
    assert code == EXIT_REFUSED


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com", "::"])  # noqa: S104
async def test_non_loopback_listen_is_refused_before_tls(tmp_path: Path, host: str) -> None:
    code = await run_apid(
        settings(tmp_path, host), UnavailableChannel(), asyncio.Event(), authenticator=Tokens
    )
    assert code == EXIT_REFUSED


def test_ledger_missing_is_503_then_recovers(tmp_path: Path) -> None:
    client = TestClient(build_apid_app(settings(tmp_path), UnavailableChannel(), Tokens()))
    assert client.get("/v1/status", headers=AUTH).status_code == 503
    upgrade(tmp_path / "ledger.db")
    assert client.get("/v1/status", headers=AUTH).status_code == 200


def test_read_only_ledger_refuses_writes(tmp_path: Path) -> None:
    upgrade(tmp_path / "ledger.db")
    repo = ReadOnlyLedger(tmp_path / "ledger.db")()
    with pytest.raises(StorageError):
        repo.record_command("req-0001", "c", "{}", datetime.now(UTC))


def test_commands_without_tradingd_are_not_sent(tmp_path: Path) -> None:
    upgrade(tmp_path / "ledger.db")
    client = TestClient(build_apid_app(settings(tmp_path), UnavailableChannel(), Tokens()))
    body = {"request_id": "req-0001", "name": "reconcile", "params": {}}
    response = client.post("/v1/commands", json=body, headers=AUTH)
    assert response.status_code == 503
    assert response.json()["retry_same_request_id"] is False
