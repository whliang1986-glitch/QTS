"""F-01 验收：gitleaks 能拦截测试密钥。

假密钥在运行时拼接生成，避免仓库源码本身触发密钥扫描。
本机或 CI 没有 gitleaks 时跳过；CI 的 secret-scan 任务会单独做全历史扫描。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

GITLEAKS = shutil.which("gitleaks")
pytestmark = pytest.mark.skipif(GITLEAKS is None, reason="未安装 gitleaks")


def _fake_github_token() -> str:
    return "ghp" + "_" + "R7xQ2mVt9LpZ4kHs8NbWc3YdJf6Ug1Ae5Ko0"


def _scan(directory: Path) -> subprocess.CompletedProcess[str]:
    assert GITLEAKS is not None
    return subprocess.run(
        [GITLEAKS, "dir", str(directory), "--no-banner", "--redact", "--exit-code", "1"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_gitleaks_detects_fake_token(tmp_path: Path) -> None:
    (tmp_path / "settings.py").write_text(f'TOKEN = "{_fake_github_token()}"\n', encoding="utf-8")
    result = _scan(tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr


def test_gitleaks_passes_clean_dir(tmp_path: Path) -> None:
    (tmp_path / "settings.py").write_text('TOKEN = "从系统凭据存储读取"\n', encoding="utf-8")
    result = _scan(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
