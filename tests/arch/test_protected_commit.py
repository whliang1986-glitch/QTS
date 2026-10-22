"""F-02 验收：git 提交层的第二道防线（scripts/check_protected_paths.py）。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from scripts.check_protected_paths import classify, find_violations

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_protected_paths.py"


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("secrets/a.txt", "secrets"),
        ("server/secrets/a.txt", "secrets"),
        ("config/risk/parameters.yaml", "config"),
        ("Config/Live/accounts.yaml", "config"),
        ("config/README.md", None),
        ("server/risk/rules.py", None),
    ],
)
def test_classify(path: str, kind: str | None) -> None:
    assert classify(path) == kind


def test_secrets_never_allowed_even_with_override() -> None:
    assert find_violations(["secrets/x"], allow_config=True) == ["secrets/x"]


def test_config_allowed_only_with_override() -> None:
    paths = ["config/risk/parameters.yaml"]
    assert find_violations(paths, allow_config=False) == paths
    assert find_violations(paths, allow_config=True) == []


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
def test_script_rejects_staged_protected_file(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    target = tmp_path / "config" / "risk" / "parameters.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("leverage: 1.0\n", encoding="utf-8")
    git("add", "-A")

    env = {k: v for k, v in os.environ.items() if k != "ALLOW_PROTECTED_CONFIG"}
    blocked = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=tmp_path, capture_output=True, check=False, env=env
    )
    assert blocked.returncode == 1
    assert "config/risk/parameters.yaml" in blocked.stderr.decode("utf-8")

    allowed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        env={**env, "ALLOW_PROTECTED_CONFIG": "1"},
    )
    assert allowed.returncode == 0
