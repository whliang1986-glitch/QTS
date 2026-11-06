"""诊断包接口（S-09b）：只给 owner、先预览再下载同一份、审计写不进去就不给、apid 接好真实来源。"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from server.api.app import ApiDeps, create_app
from server.app.apid import ApidSettings, ReadOnlyLedger, diagnostics_builder
from server.app.diagnostics import (
    MAX_LINE_CHARS,
    Bundle,
    DiagnosticsError,
    PreviewStore,
    build_bundle,
    scrub_text,
)
from server.storage.alertsdb import StoredAlert
from server.storage.apidb import ApiDb
from server.storage.migrate import upgrade

from tests.unit.api.test_app import OWNER, VIEWER, Gateway, Tokens

NOW = datetime(2026, 11, 5, 21, 0, tzinfo=UTC)
ROUTE = "/v1/diagnostics/previews"


def sample() -> Bundle:
    return build_bundle(
        sources={"mode": lambda: {"mode": "PAPER", "account": "DU1234567", "api_token": "abc"}},
        clock=lambda: NOW,
    )


class BrokenAudit:
    def audit(self, action: str, actor: str, detail: dict[str, str]) -> None:
        raise OSError("disk")


def client(tmp_path: Path, **kw: Any) -> tuple[TestClient, ApiDb]:
    db = ApiDb(tmp_path / "api.db")
    args: dict[str, Any] = {"diagnostics": PreviewStore(sample), "audit": db}
    args.update(kw)
    deps = ApiDeps(
        authenticator=Tokens(),
        ledger=lambda: None,  # type: ignore[arg-type, return-value]
        gateway=Gateway(),
        **args,
    )
    return TestClient(create_app(deps)), db


def auth(token: str) -> dict[str, str]:
    return {"Authorization": token}


def actions(db: ApiDb) -> list[str]:
    return [row[2] for row in db.recent_audits(50)]


def test_preview_then_download_the_same_copy(tmp_path: Path) -> None:
    api, db = client(tmp_path)
    preview = api.post(ROUTE, headers=auth(OWNER))
    assert preview.status_code == 200
    body = preview.json()
    files = {f["name"]: f["content"] for f in body["files"]}
    assert "DU1234567" not in files["mode.json"] and '"api_token": "***"' in files["mode.json"]
    assert body["expires_in_seconds"] == 600 and body["errors"] == {}
    assert "diagnostics_preview" in actions(db)

    archive = api.get(f"{ROUTE}/{body['preview_id']}/archive", headers=auth(OWNER))
    assert archive.status_code == 200
    assert archive.headers["content-type"] == "application/zip"
    assert archive.headers["cache-control"] == "no-store"
    with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
        assert {n: z.read(n).decode("utf-8") for n in z.namelist()} == files
    assert "diagnostics_export" in actions(db)


def test_only_the_owner_can_use_it(tmp_path: Path) -> None:
    api, db = client(tmp_path)
    assert api.post(ROUTE).status_code == 401
    assert api.post(ROUTE, headers=auth(VIEWER)).status_code == 403
    preview_id = api.post(ROUTE, headers=auth(OWNER)).json()["preview_id"]
    assert api.get(f"{ROUTE}/{preview_id}/archive").status_code == 401
    assert api.get(f"{ROUTE}/{preview_id}/archive", headers=auth(VIEWER)).status_code == 403
    assert "diagnostics_export" not in actions(db)


def test_unknown_preview_is_not_found_and_not_audited_as_exported(tmp_path: Path) -> None:
    api, db = client(tmp_path)
    assert api.get(f"{ROUTE}/nope/archive", headers=auth(OWNER)).status_code == 404
    assert "diagnostics_export" not in actions(db)


def test_nothing_is_given_when_the_audit_cannot_be_written(tmp_path: Path) -> None:
    built: list[int] = []

    def build() -> Bundle:
        built.append(1)
        return sample()

    api, _ = client(tmp_path, audit=BrokenAudit(), diagnostics=PreviewStore(build))
    assert api.post(ROUTE, headers=auth(OWNER)).status_code == 503
    assert built == []  # 审计写不进去就不打包

    store = PreviewStore(sample)
    preview_id, _ = store.create_preview()
    api, _ = client(tmp_path, audit=BrokenAudit(), diagnostics=store)
    assert api.get(f"{ROUTE}/{preview_id}/archive", headers=auth(OWNER)).status_code == 503


def test_unavailable_or_refused_bundles_are_503_without_detail(tmp_path: Path) -> None:
    api, _ = client(tmp_path, diagnostics=None)
    assert api.post(ROUTE, headers=auth(OWNER)).status_code == 503
    assert api.get(f"{ROUTE}/x/archive", headers=auth(OWNER)).status_code == 503

    def refused() -> Bundle:
        raise DiagnosticsError("诊断包不收集像凭据的文件：tws.pem")

    api, _ = client(tmp_path, diagnostics=PreviewStore(refused))
    response = api.post(ROUTE, headers=auth(OWNER))
    assert response.status_code == 503 and "tws.pem" not in response.text


def test_apid_wires_read_only_sources(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.db"
    upgrade(ledger_path)  # 建表
    db = ApiDb(tmp_path / "api.db")
    db.audit("diagnostics_preview", "dylan", {})
    log = tmp_path / "apid.log"
    log.write_text("start password=hunter2\n", encoding="utf-8")
    settings = ApidSettings(
        runtime_dir=tmp_path, ledger_path=ledger_path, api_db=tmp_path / "api.db",
        tradingd_account="svc", diagnostics_logs=(log,),
    )  # fmt: skip
    bundle = diagnostics_builder(settings, ReadOnlyLedger(ledger_path), None, db)()
    assert {"status.json", "recent_orders.json", "api_audit.json", "logs/apid.log"} <= set(
        bundle.files
    )
    assert "hunter2" not in bundle.files["logs/apid.log"]
    assert json.loads(bundle.files["recent_orders.json"]) == []
    assert "alerts" in bundle.errors  # 没配置告警库：如实记下，不中断


class FakeAlerts:
    def recent(self, limit: int) -> list[StoredAlert]:
        return [
            StoredAlert(
                alert_id=1, key="k", level="P1", code="alert_test",
                params={"detail": "连接失败 密码=alert-pass"},
                raised_at="2026-11-05T21:00:00+00:00",
            )
        ]  # fmt: skip


def test_secrets_in_free_text_never_reach_preview_or_zip(tmp_path: Path) -> None:
    """吊销原因、告警参数、HALT 原因都是自由文本：真实来源走到底，预览和 zip 都不能有明文。"""
    upgrade(tmp_path / "ledger.db")
    db = ApiDb(tmp_path / "api.db")
    bare = "qts_" + "a" * 16 + "_" + "b" * 43
    db.audit("token_revoked", "dylan", {
        "reason": f"泄露了 password=hunter2 还有 Bearer abc.def.ghi 令牌：zzz999 以及 {bare}",
    })  # fmt: skip
    settings = ApidSettings(
        runtime_dir=tmp_path, ledger_path=tmp_path / "ledger.db", api_db=tmp_path / "api.db",
        tradingd_account="svc",
    )  # fmt: skip
    build = diagnostics_builder(
        settings,
        ReadOnlyLedger(settings.ledger_path),
        FakeAlerts(),  # type: ignore[arg-type]
        db,
    )
    store = PreviewStore(build)
    api, _ = client(tmp_path, diagnostics=store, audit=db)
    preview = api.post(ROUTE, headers=auth(OWNER))
    assert preview.status_code == 200 and preview.headers["cache-control"] == "no-store"
    body = preview.json()
    archive = api.get(f"{ROUTE}/{body['preview_id']}/archive", headers=auth(OWNER))
    with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
        zipped = " ".join(z.read(n).decode("utf-8") for n in z.namelist())
    for text in (json.dumps(body, ensure_ascii=False), zipped):
        for leaked in ("hunter2", "abc.def.ghi", "zzz999", bare, "alert-pass"):
            assert leaked not in text, leaked
    assert "token_revoked" in zipped and "连接失败" in zipped  # 不是把内容整个扔掉


def test_source_free_text_is_scrubbed_even_for_ordinary_keys() -> None:
    bundle = build_bundle(
        sources={"status": lambda: {"halt": {"reason": "误操作 api_token=s3cr3t 后停机"}}},
        clock=lambda: NOW,
    )
    assert "s3cr3t" not in bundle.files["status.json"]
    assert "误操作" in bundle.files["status.json"]


def test_audit_links_the_export_to_its_preview(tmp_path: Path) -> None:
    api, db = client(tmp_path)
    preview_id = api.post(ROUTE, headers=auth(OWNER)).json()["preview_id"]
    data = api.get(f"{ROUTE}/{preview_id}/archive", headers=auth(OWNER)).content
    rows = {row[2]: row[4] for row in db.recent_audits(50)}
    assert rows["diagnostics_preview_created"] == {"preview_id": preview_id}
    assert rows["diagnostics_export"] == {
        "preview_id": preview_id, "sha256": hashlib.sha256(data).hexdigest(),
    }  # fmt: skip


def test_only_one_bundle_is_built_at_a_time() -> None:
    def reenter() -> Bundle:  # 打包途中又来一个请求
        store.create_preview()
        return sample()

    store = PreviewStore(reenter)
    with pytest.raises(DiagnosticsError, match="正在生成"):
        store.create_preview()


def test_a_failed_build_releases_the_lock() -> None:
    results = iter([OSError("disk"), None])

    def flaky() -> Bundle:
        outcome = next(results)
        if outcome is not None:
            raise outcome
        return sample()

    store = PreviewStore(flaky)  # 同一个实例：第一次失败，第二次要能成功
    with pytest.raises(OSError, match="disk"):
        store.create_preview()
    assert store.create_preview()[0]


class AuditFailsAfter:
    def __init__(self, good: int) -> None:
        self.left = good

    def audit(self, action: str, actor: str, detail: dict[str, str]) -> None:
        if self.left <= 0:
            raise OSError("disk")
        self.left -= 1


def test_nothing_is_returned_if_the_second_preview_audit_fails(tmp_path: Path) -> None:
    api, _ = client(tmp_path, audit=AuditFailsAfter(1))
    response = api.post(ROUTE, headers=auth(OWNER))
    assert response.status_code == 503
    assert "preview_id" not in response.text and "mode.json" not in response.text


@pytest.mark.parametrize(
    ("text", "leaked"),
    [
        ("X-Auth-Token: Bearer abc123", "abc123"),
        ("token=Bearer abc123", "abc123"),
        ("auth: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("密码＝abc123", "abc123"),
    ],
)
def test_key_value_rule_does_not_expose_bearer_tokens(text: str, leaked: str) -> None:
    assert leaked not in scrub_text(text)


def test_keys_that_collide_after_scrubbing_keep_both_values() -> None:
    bundle = build_bundle(
        sources={"odd": lambda: {"note Bearer aaa": "first", "note Bearer bbb": "second"}},
        clock=lambda: NOW,
    )
    text = bundle.files["odd.json"]
    assert "first" in text and "second" in text


def test_very_long_log_lines_are_omitted_not_cut(tmp_path: Path) -> None:
    """截断会让 JSON 解析不了、令牌只剩半截：超长的行整行省略，前后的行照常保留。"""
    blob = "x" * (MAX_LINE_CHARS + 500)
    bare = "qts_" + "a" * 16 + "_" + "b" * 43
    log = tmp_path / "apid.log"
    log.write_text(
        "before\n"
        + json.dumps({"password": ["hunter2"], "blob": blob}) + "\n"
        + "y" * (MAX_LINE_CHARS - 10) + bare + "\n"
        + "after\n",
        encoding="utf-8",
    )  # fmt: skip
    text = build_bundle(sources={}, logs=[log], clock=lambda: NOW).files["logs/apid.log"]
    assert "hunter2" not in text and "qts_" not in text and "xxxx" not in text
    lines = text.splitlines()
    assert lines[0] == "before" and lines[-1] == "after"
    assert "已省略" in lines[1] and "已省略" in lines[2]


def test_bad_entry_names_fail_at_preview_not_at_download() -> None:
    store = PreviewStore(lambda: Bundle(created_at=NOW, files={"a:b.log": "x"}))
    with pytest.raises(DiagnosticsError, match="条目名"):
        store.create_preview()


def test_missing_package_metadata_does_not_disable_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(_name: str) -> str:
        raise PackageNotFoundError

    monkeypatch.setattr("server.app.apid.version", missing)
    settings = ApidSettings(
        runtime_dir=tmp_path, ledger_path=tmp_path / "ledger.db", api_db=tmp_path / "api.db",
        tradingd_account="svc",
    )  # fmt: skip
    bundle = diagnostics_builder(settings, ReadOnlyLedger(settings.ledger_path), None, None)()
    assert json.loads(bundle.files["manifest.json"])["version"] == "unknown"


def test_a_configured_credential_file_refuses_the_whole_bundle(tmp_path: Path) -> None:
    key = tmp_path / "tws.pem"
    key.write_text("x", encoding="utf-8")
    settings = ApidSettings(
        runtime_dir=tmp_path, ledger_path=tmp_path / "ledger.db", api_db=tmp_path / "api.db",
        tradingd_account="svc", diagnostics_logs=(key,),
    )  # fmt: skip
    build = diagnostics_builder(settings, ReadOnlyLedger(settings.ledger_path), None, None)
    with pytest.raises(DiagnosticsError, match="凭据"):
        build()
