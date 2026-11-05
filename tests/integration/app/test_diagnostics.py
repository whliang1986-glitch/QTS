"""诊断包（S-09a）：先预览再导出、全部脱敏、不收集密钥、配置只收哈希、取数失败不中断。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from server.app.diagnostics import MAX_LOG_LINES, DiagnosticsError, build_bundle, write_zip
from server.storage.credentials import SensitiveValue

NOW = datetime(2026, 11, 5, 21, 0, tzinfo=UTC)
KEY_DIR = "sec" + "rets"


def bundle(tmp_path: Path, **kw: object):  # type: ignore[no-untyped-def]
    args: dict[str, object] = {"sources": {}, "clock": lambda: NOW, "version": "abc123"}
    args.update(kw)
    return build_bundle(**args)  # type: ignore[arg-type]


def test_sources_are_redacted_and_previewable(tmp_path: Path) -> None:
    b = bundle(
        tmp_path,
        sources={
            "mode": lambda: {
                "mode": "PAPER",
                "account": "DU1234567",
                "api_token": "abc",
                "nav": Decimal("1.50"),
            },
            "audit": lambda: [
                {"actor": "dylan", "detail": "连接 U7654321 失败", "key": SensitiveValue("x")}
            ],
        },
    )
    names = [name for name, _ in b.preview()]
    assert names == sorted(names)
    assert {"mode.json", "audit.json", "manifest.json", "errors.json", "configs.json"} <= set(names)
    text = "".join(content for _, content in b.preview())
    assert "DU1234567" not in text and "U7654321" not in text
    assert "DU****567" in text and "U****321" in text
    assert '"api_token": "***"' in text and '"key": "***"' in text
    assert '"nav": "1.50"' in text  # Decimal 原样成字符串，不变成 float


def test_failing_source_is_recorded_without_stopping(tmp_path: Path) -> None:
    def broken() -> object:
        raise RuntimeError("账本打不开：DU1234567")

    b = bundle(tmp_path, sources={"orders": broken, "mode": lambda: {"mode": "HALT"}})
    assert "orders.json" not in b.files and "mode.json" in b.files
    assert "RuntimeError" in b.errors["orders"] and "DU1234567" not in b.errors["orders"]
    assert "DU****567" in json.loads(b.files["errors.json"])["orders"]


def test_logs_keep_only_the_redacted_tail(tmp_path: Path) -> None:
    log = tmp_path / "tradingd.log"
    log.write_text(
        "".join(f"line {i} DU1234567\n" for i in range(MAX_LOG_LINES + 50)), encoding="utf-8"
    )
    text = bundle(tmp_path, logs=[log]).files["logs/tradingd.log"]
    lines = text.splitlines()
    assert len(lines) == MAX_LOG_LINES and lines[-1].startswith(f"line {MAX_LOG_LINES + 49}")
    assert "DU1234567" not in text


def test_configs_contribute_only_hashes(tmp_path: Path) -> None:
    cfg = tmp_path / "limits.toml"
    cfg.write_text("max_position_pct = 10\n", encoding="utf-8")
    text = bundle(tmp_path, configs=[cfg]).files["configs.json"]
    assert hashlib.sha256(cfg.read_bytes()).hexdigest() in text
    assert "max_position_pct" not in text


@pytest.mark.parametrize("name", ["tws.pem", "api.key", "password.txt", "token.json", ".env"])
def test_credential_like_files_are_refused(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    path.write_text("x", encoding="utf-8")
    with pytest.raises(DiagnosticsError, match="凭据"):
        bundle(tmp_path, logs=[path])


def test_key_directory_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / KEY_DIR
    folder.mkdir()
    path = folder / "notes.log"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(DiagnosticsError, match="密钥目录"):
        bundle(tmp_path, configs=[path])
    with pytest.raises(DiagnosticsError, match="密钥目录"):
        write_zip(bundle(tmp_path), folder / "diag.zip")


def test_bad_source_names_and_naive_clock_are_refused(tmp_path: Path) -> None:
    with pytest.raises(DiagnosticsError, match="标识符"):
        bundle(tmp_path, sources={"../escape": dict})
    with pytest.raises(DiagnosticsError, match="时区"):
        bundle(tmp_path, clock=lambda: datetime(2026, 11, 5, 21, 0))  # noqa: DTZ001


def test_zip_matches_the_preview_and_is_reproducible(tmp_path: Path) -> None:
    b = bundle(tmp_path, sources={"mode": lambda: {"mode": "PAPER"}})
    first = write_zip(b, tmp_path / "a.zip")
    second = write_zip(b, tmp_path / "b.zip")
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert {n: archive.read(n).decode("utf-8") for n in archive.namelist()} == dict(b.files)
    with pytest.raises(DiagnosticsError, match="不覆盖"):
        write_zip(b, first)


def test_manifest_lists_every_file(tmp_path: Path) -> None:
    b = bundle(tmp_path, sources={"mode": lambda: {}})
    manifest = json.loads(b.files["manifest.json"])
    assert sorted(manifest["files"]) == sorted(b.files)
    assert manifest["version"] == "abc123" and manifest["created_at"].endswith("+00:00")


# ---------- 评审补充（S-09a 第一轮） ----------


def test_symlink_into_the_key_directory_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / KEY_DIR
    folder.mkdir()
    (folder / "tws.log").write_text("password=hunter2\n", encoding="utf-8")
    link = tmp_path / "tradingd.log"
    try:
        link.symlink_to(folder / "tws.log")
    except OSError:
        pytest.skip("这台机器没有建符号链接的权限（Windows 需要开发者模式）")
    with pytest.raises(DiagnosticsError, match="链接"):
        bundle(tmp_path, logs=[link])


@pytest.mark.skipif(sys.platform != "win32", reason="目录联接（junction）是 Windows 特有的")
def test_directory_junction_into_the_key_directory_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / KEY_DIR
    folder.mkdir()
    (folder / "tws.log").write_text("password=hunter2\n", encoding="utf-8")
    junction = tmp_path / "logs"
    # mklink /J 不需要管理员权限：这条测试在 Windows 上一定会真正执行
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(folder)], check=True, capture_output=True
    )
    with pytest.raises(DiagnosticsError, match="链接"):
        bundle(tmp_path, logs=[junction / "tws.log"])


def test_account_numbers_in_file_names_are_masked(tmp_path: Path) -> None:
    log = tmp_path / "tradingd-DU1234567.log"
    log.write_text("ok\n", encoding="utf-8")
    cfg = tmp_path / "U7654321.toml"
    cfg.write_text("x = 1\n", encoding="utf-8")
    b = bundle(tmp_path, logs=[log], configs=[cfg])
    every = " ".join([*b.files, *b.errors, *b.files.values()])
    assert "DU1234567" not in every and "U7654321" not in every
    assert "logs/tradingd-DU****567.log" in b.files
    assert sorted(json.loads(b.files["manifest.json"])["files"]) == sorted(b.files)


def test_secrets_inside_log_lines_are_scrubbed(tmp_path: Path) -> None:
    log = tmp_path / "apid.log"
    log.write_text(
        "connect password=hunter2 user=dylan\n"
        "GET /v1/status Authorization: Bearer abc.def.ghi\n"
        '{"event": "login", "api_token": "s3cr3t", "account": "DU1234567"}\n'
        "callback?token=xyz&page=2\n",
        encoding="utf-8",
    )
    text = bundle(tmp_path, logs=[log]).files["logs/apid.log"]
    for secret in ("hunter2", "abc.def.ghi", "s3cr3t", "xyz", "DU1234567"):
        assert secret not in text
    assert "user=dylan" in text and "page=2" in text


@pytest.mark.parametrize("name", ["id_rsa", ".netrc", "deploy.ppk", "store.jks", "vpn.ovpn"])
def test_more_credential_files_are_refused(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    path.write_text("x", encoding="utf-8")
    with pytest.raises(DiagnosticsError):
        bundle(tmp_path, logs=[path])


def test_logs_must_be_whitelisted_files_outside_config(tmp_path: Path) -> None:
    protected = tmp_path / "config" / "risk"
    protected.mkdir(parents=True)
    limits = protected / "limits.txt"
    limits.write_text("max = 1\n", encoding="utf-8")
    with pytest.raises(DiagnosticsError, match="config/"):
        bundle(tmp_path, logs=[limits])
    other = tmp_path / "notes.toml"
    other.write_text("x", encoding="utf-8")
    with pytest.raises(DiagnosticsError, match="日志只接受"):
        bundle(tmp_path, logs=[other])
    folder = tmp_path / "dir.log"
    folder.mkdir()
    with pytest.raises(DiagnosticsError, match="普通文件"):
        bundle(tmp_path, logs=[folder])


def test_reserved_and_duplicate_names_are_refused(tmp_path: Path) -> None:
    for reserved in ("manifest", "errors", "configs"):
        with pytest.raises(DiagnosticsError, match="保留名"):
            bundle(tmp_path, sources={reserved: dict})
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    for folder in ("a", "b"):
        (tmp_path / folder / "tradingd.log").write_text("x\n", encoding="utf-8")
    with pytest.raises(DiagnosticsError, match="重名"):
        bundle(tmp_path, logs=[tmp_path / "a" / "tradingd.log", tmp_path / "b" / "tradingd.log"])


def test_naive_times_and_nan_stay_valid_json(tmp_path: Path) -> None:
    naive = datetime(2026, 11, 5, 21, 0)  # noqa: DTZ001 - 故意不带时区
    b = bundle(tmp_path, sources={"odd": lambda: {"t": naive, "x": float("nan")}})
    data = json.loads(b.files["odd.json"])
    assert data == {"t": "<naive>", "x": "nan"}


def test_bundle_is_read_only_and_bad_entries_are_not_written(tmp_path: Path) -> None:
    from server.app.diagnostics import Bundle  # noqa: PLC0415

    b = bundle(tmp_path)
    with pytest.raises(TypeError):
        b.files["x"] = "y"  # type: ignore[index,unused-ignore]
    forged = Bundle(created_at=NOW, files={"../escape.txt": "x"})
    with pytest.raises(DiagnosticsError, match="条目名"):
        write_zip(forged, tmp_path / "forged.zip")
    assert not (tmp_path / "forged.zip").exists()
    assert not list(tmp_path.glob(".*.partial"))


def test_zip_cannot_be_written_into_config(tmp_path: Path) -> None:
    target = tmp_path / "config" / "live"
    target.mkdir(parents=True)
    with pytest.raises(DiagnosticsError, match="config/"):
        write_zip(bundle(tmp_path), target / "diag.zip")


# ---------- 评审补充（S-09a 第二轮） ----------


def test_quoted_keys_and_secrets_inside_json_strings_are_scrubbed(tmp_path: Path) -> None:
    log = tmp_path / "workerd.log"
    log.write_text(
        '2026-11-05 INFO payload={"api_token": "s3cr3t", "page": 2}\n'
        "{'password': 'hunter2', 'user': 'dylan'}\n"
        'note "secret": "hun\\"ter2" end\n'
        '{"message": "login failed password=hunter2 for DU1234567"}\n',
        encoding="utf-8",
    )
    lines = bundle(tmp_path, logs=[log]).files["logs/workerd.log"].splitlines()
    text = "\n".join(lines)
    for secret in ("s3cr3t", "hunter2", "DU1234567", "ter2"):
        assert secret not in text
    assert '"page": 2' in lines[0] and "'user': 'dylan'" in lines[1]
    assert lines[2].endswith(" end")
    assert json.loads(lines[3])["message"].startswith("login failed password=***")


def test_failed_write_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = bundle(tmp_path, sources={"mode": lambda: {"mode": "PAPER"}})

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("磁盘满")

    monkeypatch.setattr(zipfile.ZipFile, "writestr", boom)
    with pytest.raises(OSError, match="磁盘满"):
        write_zip(b, tmp_path / "diag.zip")
    assert not (tmp_path / "diag.zip").exists()
    assert not list(tmp_path.glob(".*.partial"))


def test_someone_elses_partial_file_is_not_deleted(tmp_path: Path) -> None:
    other = tmp_path / ".diag.zip.partial"
    other.write_bytes(b"in progress")
    with pytest.raises(FileExistsError):
        write_zip(bundle(tmp_path), tmp_path / "diag.zip")
    assert other.read_bytes() == b"in progress"
