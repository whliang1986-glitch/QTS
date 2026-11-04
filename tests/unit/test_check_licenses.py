"""许可证检查（H-01）：强传染许可禁止、LGPL 需确认、认不出的禁止。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_licenses.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_licenses", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get("check_licenses")
    sys.modules["check_licenses"] = module  # dataclass 要能按模块名找到自己
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop("check_licenses", None)
        else:
            sys.modules["check_licenses"] = previous
    return module


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("MIT", "ok"),
        ("Apache-2.0 OR BSD-2-Clause", "ok"),
        ("MPL-2.0", "ok"),
        ("GPL-3.0-only", "deny"),
        ("AGPL-3.0-or-later", "deny"),
        ("GNU General Public License v3 (GPLv3)", "deny"),
        ("SSPL-1.0", "deny"),
        ("LGPL-2.1-or-later", "warn"),
        ("GNU Lesser General Public License v3 (LGPLv3)", "warn"),
        ("UNKNOWN", "deny"),
        # 评审 P1-1：混合许可里有 GPL 一律禁止
        ("GPL-3.0-only OR LGPL-2.1", "deny"),
        ("GPL-2.0-or-later AND LGPL-2.1+", "deny"),
        # 评审 P1-2：认不出、非自由许可一律禁止
        ("SEE LICENSE IN LICENSE.txt", "deny"),
        ("UNLICENSED", "deny"),
        ("BUSL-1.1", "deny"),
        ("Elastic-2.0", "deny"),
        ("CC-BY-NC-4.0", "deny"),
        ("Copyright (c) 2020 Someone", "deny"),
        ("MIT OR GPL-3.0", "deny"),  # 出现 GPL 字样就要人工判断，不自动放行
        ("BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0", "ok"),
        ("Apache-2.0 OR BSD-2-Clause", "ok"),
        ("OSI Approved", "deny"),  # 只说“OSI 批准”，没说是哪种许可
        ("Foo AND (Bar OR MIT OR Baz)", "deny"),  # 嵌套组合：要人工登记
        ("GPLv3", "deny"),
        ("AGPLv2+", "deny"),
    ],
)
def test_verdicts(text: str, expected: str) -> None:
    mod = load()
    assert mod.verdict(mod.Package("python", "x", "1", text, None)) == expected


def test_gpl_in_the_full_license_text_is_caught() -> None:
    """评审 P1-2：License 字段第一行是版权声明、GPL 在后面几行，也要拦下。"""
    mod = load()
    text = (
        "Copyright (c) 2020 Someone\n\nThis program is free software: GNU General Public License v3"
    )
    assert mod.verdict(
        mod.Package("python", "x", "1", "Copyright (c) 2020 Someone", None, text)
    ) == ("deny")


def test_lgpl_fails_the_check_unless_reviewed(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = load()
    pkg = mod.Package("python", "lib", "1", "LGPL-3.0-only", None)
    monkeypatch.setattr(mod, "python_packages", lambda: [pkg])
    monkeypatch.setattr(mod, "npm_packages", lambda: [])
    assert mod.main([]) == 1
    monkeypatch.setitem(mod.REVIEWED, "lib", "（已核对：只动态链接）")
    assert mod.main([]) == 0


def test_current_dependencies_pass() -> None:
    """仓库当前的依赖没有禁止的许可（加依赖时这里会先失败）。"""
    assert load().main([]) == 0
