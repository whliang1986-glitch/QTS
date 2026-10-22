"""F-02 验收：受保护路径写入被拒（PreToolUse 钩子 guard_paths.py）。"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / ".claude" / "hooks" / "guard_paths.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("guard_paths", HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load()


def _payload(tool: str, **tool_input: Any) -> dict[str, Any]:
    return {"tool_name": tool, "tool_input": tool_input, "cwd": str(ROOT)}


def _run_hook(payload: dict[str, Any] | str) -> subprocess.CompletedProcess[bytes]:
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=data.encode("utf-8"),
        capture_output=True,
        check=False,
        env={"CLAUDE_PROJECT_DIR": str(ROOT), "SYSTEMROOT": "C:\\Windows"},
    )


# ---------- 写入类工具 ----------

BLOCKED_WRITE_PATHS = [
    "config/risk/parameters.yaml",
    "config/live/accounts.yaml",
    "secrets/ibkr.txt",
    "CONFIG/Risk/parameters.yaml",
    "config\\risk\\parameters.yaml",
    "./config//risk/x.yaml",
    "server/../config/risk/x.yaml",
    "docs/../config/live/x",
    str(ROOT / "config" / "risk" / "parameters.yaml"),
    str(ROOT / "secrets" / "key"),
    "config/risk",
]


@pytest.mark.parametrize("path", BLOCKED_WRITE_PATHS)
@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit"])
def test_write_to_protected_path_is_blocked(tool: str, path: str) -> None:
    assert guard.evaluate(_payload(tool, file_path=path)) is not None


def test_notebook_edit_is_blocked() -> None:
    assert guard.evaluate(_payload("NotebookEdit", notebook_path="secrets/a.ipynb")) is not None


ALLOWED_WRITE_PATHS = [
    "server/risk/rules.py",  # 风控代码本身不在受保护配置目录下
    "config/README.md",
    "config/parameters.example.yaml",
    "docs/runbooks/protected-config-change.md",
    "tests/arch/test_config_risk_like_name.py",
    "server/broker/fake/live_like.py",
]


@pytest.mark.parametrize("path", ALLOWED_WRITE_PATHS)
def test_write_to_normal_path_is_allowed(path: str) -> None:
    assert guard.evaluate(_payload("Write", file_path=path)) is None


# ---------- 读取类工具 ----------


def test_read_risk_config_is_allowed() -> None:
    assert guard.evaluate(_payload("Read", file_path="config/risk/parameters.yaml")) is None


@pytest.mark.parametrize(
    "payload",
    [
        _payload("Read", file_path="secrets/ibkr.txt"),
        _payload("Grep", pattern="password", path="secrets"),
        _payload("Glob", pattern="secrets/**/*"),
    ],
)
def test_read_secrets_is_blocked(payload: dict[str, Any]) -> None:
    assert guard.evaluate(payload) is not None


# ---------- Shell ----------

BLOCKED_COMMANDS = [
    "echo 1 > config/risk/parameters.yaml",
    "cat a >> config/live/x",
    "cp tmp.yaml config/risk/parameters.yaml",
    "mv x config\\live\\y",
    "rm config/risk/parameters.yaml",
    "sed -i s/5/50/ config/risk/parameters.yaml",
    "python -c \"open('config/risk/p.yaml','w').write('x')\"",
    "git checkout -- config/risk/parameters.yaml",
    "cat config/risk/a.yaml | tee config/risk/b.yaml",
    "cd config && echo 1 > risk/parameters.yaml",
    "cd ./config/risk; touch x",
    "Set-Content -Path config/risk/p.yaml -Value 1",
    "cat secrets/ibkr.txt",
    "ls secrets",
    "git commit --no-verify -m x",
    "git commit -n -m x",
    "git commit -an -m x",
    "git -c core.x=y commit -m x -n",
    "cd repo && git commit -nm x",
    "git -c alias.ci=commit ci -n",
    "git -c alias.x='commit -n' x",
    "git -c core.hooksPath=/dev/null commit -m x",
    "SKIP=arch-scan,lint-imports git commit -m x",
    "ALLOW_PROTECTED_CONFIG=1 git commit -m x",
    "find config/risk -delete",
]


@pytest.mark.parametrize("command", BLOCKED_COMMANDS)
@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
def test_dangerous_command_is_blocked(tool: str, command: str) -> None:
    assert guard.evaluate(_payload(tool, command=command)) is not None


ALLOWED_COMMANDS = [
    "uv run pytest -q",
    "cat config/risk/parameters.yaml",
    "git diff config/risk/parameters.yaml",
    "git log -- config/live",
    "grep -n leverage config/risk/parameters.yaml | head -5",
    "uv run pre-commit run detect-secrets",  # 连字符名称不算 secrets 路径
    "git status",
    # 曾误报：文件名含 commit，同一命令里另有 sed -n
    "python fix.py .pre-commit-config.yaml; sed -n 1,5p a.py",
    "git commit -m 'F-03: 架构扫描'",
    "pre-commit run --all-files && git log -n 3",
    "python -c 'pd.read_csv(f, skip=1)'",  # SKIP= 只认大写的环境变量
]


@pytest.mark.parametrize("command", ALLOWED_COMMANDS)
def test_safe_command_is_allowed(command: str) -> None:
    assert guard.evaluate(_payload("Bash", command=command)) is None


def test_unrelated_tool_is_allowed() -> None:
    assert guard.evaluate(_payload("WebSearch", query="config/risk")) is None


# ---------- 进程级行为：退出码与失败即关闭 ----------


def test_hook_process_blocks_with_exit_code_2() -> None:
    result = _run_hook(_payload("Write", file_path="config/risk/parameters.yaml"))
    assert result.returncode == 2
    assert "config/risk/" in result.stderr.decode("utf-8")


def test_hook_process_allows_with_exit_code_0() -> None:
    result = _run_hook(_payload("Write", file_path="server/oms/state.py"))
    assert result.returncode == 0, result.stderr.decode("utf-8")


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]", '{"tool_name": "Edit", "tool_input": 3}'])
def test_hook_fails_closed_on_bad_input(raw: str) -> None:
    assert _run_hook(raw).returncode == 2


def test_settings_wire_the_hook() -> None:
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    pre = settings["hooks"]["PreToolUse"]
    matchers = " ".join(entry["matcher"] for entry in pre)
    commands = " ".join(h["command"] for entry in pre for h in entry["hooks"])
    for tool in ("Edit", "Write", "MultiEdit", "Bash", "PowerShell", "Read"):
        assert tool in matchers
    assert "guard_paths.py" in commands
    deny = settings["permissions"]["deny"]
    for rule in ("Write(/config/risk/**)", "Write(/config/live/**)", "Read(/secrets/**)"):
        assert rule in deny
