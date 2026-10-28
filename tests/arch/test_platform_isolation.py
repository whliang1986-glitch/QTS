"""平台隔离（Dylan 2026-10-28：Windows 与 macOS 都要支持）：操作系统判断只能在 server/platforms。"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# 判断操作系统的写法：sys.platform、os.name 等属性；只在某个系统存在的模块；
# 别名（import sys as s）与 getattr / hasattr(sys|os, "字面量") 也算（第二轮评审 P2-6）
OS_ATTRIBUTES = {
    "sys": {"platform", "getwindowsversion", "winver"},
    "os": {"name", "uname", "getuid", "geteuid", "sep", "linesep", "altsep"},
}
OS_MODULES = {
    "platform", "sysconfig", "msvcrt", "winreg", "_winapi", "winsound", "fcntl", "termios",
    "pwd", "grp", "resource", "posix", "nt", "ctypes.wintypes",
}  # fmt: skip


def _aliases(tree: ast.AST) -> dict[str, str]:
    """本文件里指向 sys / os 的名字（含别名）。"""
    names = {"sys": "sys", "os": "os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in OS_ATTRIBUTES:
                    names[a.asname or a.name] = a.name
    return names


def os_checks(source: str) -> list[int]:
    tree = ast.parse(source)
    aliases = _aliases(tree)
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            module = aliases.get(node.value.id)
            if module and node.attr in OS_ATTRIBUTES[module]:
                lines.append(node.lineno)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"getattr", "hasattr"} and len(node.args) >= 2:
                target, attr = node.args[0], node.args[1]
                module = aliases.get(target.id) if isinstance(target, ast.Name) else None
                if module and isinstance(attr, ast.Constant):
                    lines.append(node.lineno)  # 反射取 sys / os 的属性：一律视为平台判断
        elif isinstance(node, ast.Import):
            if any(a.name in OS_MODULES for a in node.names):
                lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module in OS_MODULES:
                lines.append(node.lineno)
            if node.module == "ctypes" and any(a.name == "wintypes" for a in node.names):
                lines.append(node.lineno)
            if node.module in OS_ATTRIBUTES and any(
                a.name in OS_ATTRIBUTES[node.module] for a in node.names
            ):
                lines.append(node.lineno)
    return lines


def test_business_code_does_not_check_the_operating_system() -> None:
    offenders = {}
    for path in (ROOT / "server").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("server/platforms/"):
            continue
        found = os_checks(path.read_text(encoding="utf-8"))
        if found:
            offenders[rel] = found
    assert offenders == {}, "操作系统差异要放进 server/platforms，业务代码不得自己判断"


@pytest.mark.parametrize(
    "source",
    [
        "import sys\nif sys.platform == 'win32': pass\n",
        "import os\nx = os.name\n",
        "import platform\n",
        "from platform import system\n",
        "from sys import platform\n",
        "import os\nos.uname()\n",
        "import sys as s\nif s.platform: pass\n",
        "import sys\ngetattr(sys, 'platform')\n",
        "import os\nhasattr(os, 'getuid')\n",
        "import fcntl\n",
        "import msvcrt\n",
        "from ctypes import wintypes\n",
        "import sysconfig\n",
        "import os\nx = os.sep\n",
        "import sys\nsys.getwindowsversion()\n",
    ],
)
def test_os_check_detector_catches(source: str) -> None:
    assert os_checks(source)


@pytest.mark.parametrize(
    "source",
    [
        "from server.platforms import local_channel\n",
        "from server.platforms import files as platform_files\n",
        "import os\nos.rename('a', 'b')\n",
    ],
)
def test_os_check_detector_allows(source: str) -> None:
    assert not os_checks(source)


def test_platform_layer_is_a_leaf() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    contracts = {c["id"]: c for c in data["tool"]["importlinter"]["contracts"]}
    leaf = contracts["platforms-leaf"]
    assert leaf["source_modules"] == ["server.platforms"]
    others = {
        "domain", "storage", "ipc", "broker", "oms", "risk", "data",
        "strategy", "backtest", "ai", "api", "guard",
    }  # fmt: skip
    assert {m.removeprefix("server.") for m in leaf["forbidden_modules"]} == others
