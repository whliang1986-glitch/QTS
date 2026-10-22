"""仓库结构必须真的进了 git：防止 .gitignore 误伤源码（曾因 data/ 规则漏提交 server/data）。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# V4.1 第 4 章规定的服务端分层
REQUIRED_PACKAGES = [
    "server",
    "server/domain",
    "server/storage",
    "server/ipc",
    "server/broker",
    "server/broker/ibkr",
    "server/broker/fake",
    "server/oms",
    "server/risk",
    "server/data",
    "server/strategy",
    "server/backtest",
    "server/ai",
    "server/api",
    "server/guard",
]
SOURCE_DIRS = ["server", "tests", "scripts", "docs", "config", "web", ".claude", ".github"]
CACHE_PARTS = {"__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache", ".hypothesis"}

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or not (ROOT / ".git").exists(), reason="需要 git 工作副本"
)


def _git(*args: str) -> list[str]:
    out = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode("utf-8")
    return [line for line in out.splitlines() if line]


@pytest.mark.parametrize("package", REQUIRED_PACKAGES)
def test_package_init_is_tracked(package: str) -> None:
    init = f"{package}/__init__.py"
    assert _git("ls-files", "--", init) == [init], f"{init} 不在 git 里"


def test_no_source_file_is_ignored() -> None:
    ignored = _git("ls-files", "--others", "--ignored", "--exclude-standard", "--", *SOURCE_DIRS)
    real = [p for p in ignored if not CACHE_PARTS.intersection(p.split("/"))]
    assert real == [], f"这些源码目录下的文件被 .gitignore 忽略了：{real}"
