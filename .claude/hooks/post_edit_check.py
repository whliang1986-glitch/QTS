"""PostToolUse 钩子：Claude Code 每次编辑 Python 文件后跑 ruff 与 mypy（F-02）。

发现问题时以退出码 2 把输出回传给 Claude，要求它当场修复；不阻止编辑本身。
虚拟环境不存在时跳过（先运行 uv sync）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

FEEDBACK = 2


def _venv_python(root: Path) -> Path | None:
    for candidate in (root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python"):
        if candidate.exists():
            return candidate
    return None


def main() -> int:
    # Windows 下管道默认是 GBK；钩子和 git 读取的输出统一用 UTF-8
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    except ValueError:
        return 0
    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if not isinstance(file_path, str) or not file_path.endswith(".py"):
        return 0

    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd())
    python = _venv_python(root)
    target = Path(file_path)
    if python is None or not target.exists():
        return 0

    checks = [
        [str(python), "-m", "ruff", "check", "--fix", str(target)],
        [str(python), "-m", "ruff", "format", str(target)],
        [str(python), "-m", "mypy", "--no-error-summary", str(target)],
    ]
    problems: list[str] = []
    for cmd in checks:
        result = subprocess.run(
            cmd, cwd=root, capture_output=True, text=True, encoding="utf-8", check=False
        )
        if result.returncode != 0:
            problems.append(f"$ {' '.join(cmd[1:])}\n{result.stdout}{result.stderr}".strip())
    if problems:
        report = "\n\n".join(problems)
        print(f"[post_edit_check] 编辑后检查未通过，请修复：\n{report}", file=sys.stderr)
        return FEEDBACK
    return 0


if __name__ == "__main__":
    sys.exit(main())
