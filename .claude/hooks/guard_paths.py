"""PreToolUse 钩子：拒绝 Claude Code 写入受保护路径（F-02）。

受保护路径（任意层级出现均算）：
- config/risk/、config/live/：只读，只能由 Dylan 手动修改
  （流程见 docs/runbooks/protected-config-change.md）
- secrets/：不可读、不可写

规则：
- Edit / Write / MultiEdit / NotebookEdit：目标路径命中即拒绝。
- Read / Grep / Glob：目标路径命中 secrets 即拒绝。
- Bash / PowerShell：提到 secrets 即拒绝；提到 config/risk、config/live 时，只允许纯只读命令；
  另外拒绝绕过钩子的写法（--no-verify、ALLOW_PROTECTED_CONFIG）。

失败即关闭：任何异常都按拒绝处理（退出码 2）。
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterable
from typing import Any

BLOCK = 2

WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read", "Grep", "Glob"}
SHELL_TOOLS = {"Bash", "PowerShell"}

READ_ONLY_COMMANDS = {
    "cat",
    "head",
    "tail",
    "less",
    "more",
    "ls",
    "dir",
    "grep",
    "rg",
    "wc",
    "file",
    "stat",
    "diff",
    "type",
    "get-content",
    "gc",
    "get-childitem",
    "gci",
    "select-string",
    "sls",
    "test-path",
    "echo",
    "find",
}
READ_ONLY_GIT = {"diff", "log", "show", "status", "blame", "ls-files"}
FIND_WRITE_FLAGS = ("-delete", "-exec", "-execdir", "-ok", "-fprint")

_SEP = r"[\\/]+"
PROTECTED_CONFIG_RE = re.compile(
    rf"(?:^|[^a-z0-9_.-])config{_SEP}(?:risk|live)(?=$|[\\/\s\"'`;|&)*?\[])", re.IGNORECASE
)
# “secrets” 作为一个独立的路径段或单词出现（detect-secrets 这类连字符名称不算）
SECRETS_RE = re.compile(r"(?:^|[^a-z0-9_-])secrets(?=$|[^a-z0-9_-])", re.IGNORECASE)
# cd 进 config 再用相对路径写 risk/ 的绕过写法：进入 config 目录即按受保护命令处理
CD_CONFIG_RE = re.compile(
    r"(?:^|[;&|\s(])(?:cd|pushd|set-location|sl|push-location)\s+[\"']?(?:\.[\\/]+)?config"
    r"(?:[\\/]+(?:risk|live))?[\\/]*[\"']?(?=$|[\s;&|)])",
    re.IGNORECASE,
)
BYPASS_RE = re.compile(r"--no-verify|ALLOW_PROTECTED_CONFIG|\bcommit\b[^|;&]*\s-n\b", re.IGNORECASE)
SEGMENT_SPLIT_RE = re.compile(r"\|\||&&|[|;&\n]")


def _project_root(payload: dict[str, Any]) -> str:
    root = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    return str(root)


def _normalize(path: str, base: str) -> list[str]:
    """返回用于比对的若干种规范化形式（字面 + 解析符号链接），全部小写、正斜杠。"""
    if not os.path.isabs(path):
        path = os.path.join(base, path)
    forms = {os.path.normpath(path), os.path.realpath(path)}
    return [f.replace("\\", "/").casefold() for f in forms]


def _segments_hit(norm: str) -> str | None:
    parts = [p for p in norm.split("/") if p]
    for i, part in enumerate(parts):
        if part == "secrets":
            return "secrets/"
        if part == "config" and i + 1 < len(parts) and parts[i + 1] in {"risk", "live"}:
            return f"config/{parts[i + 1]}/"
    return None


def check_path(path: str, base: str, *, write: bool) -> str | None:
    for norm in _normalize(path, base):
        hit = _segments_hit(norm)
        if hit is None:
            continue
        if hit == "secrets/" or write:
            return hit
    return None


def _segment_is_read_only(segment: str) -> bool:
    tokens = segment.strip().split()
    if not tokens:
        return True
    cmd = os.path.basename(tokens[0].strip("\"'`")).casefold().removesuffix(".exe")
    if cmd == "git":
        return len(tokens) > 1 and tokens[1].casefold() in READ_ONLY_GIT
    if cmd == "find":
        return not any(t.casefold().startswith(FIND_WRITE_FLAGS) for t in tokens[1:])
    if cmd in {"sed", "awk"}:
        return False
    return cmd in READ_ONLY_COMMANDS


def check_command(command: str) -> str | None:
    if BYPASS_RE.search(command):
        return "命令试图绕过提交钩子或受保护路径检查（--no-verify / ALLOW_PROTECTED_CONFIG）"
    if SECRETS_RE.search(command):
        return "命令涉及 secrets/，Claude Code 不得读取或写入密钥目录"
    mentions_config = PROTECTED_CONFIG_RE.search(command) or CD_CONFIG_RE.search(command)
    read_only = ">" not in command and all(
        _segment_is_read_only(s) for s in SEGMENT_SPLIT_RE.split(command)
    )
    if mentions_config and not read_only:
        return "命令可能写入 config/risk 或 config/live，这两个目录只能由 Dylan 修改"
    return None


def _candidate_paths(tool_input: dict[str, Any]) -> Iterable[str]:
    for key in ("file_path", "notebook_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            yield value
    pattern = tool_input.get("pattern")
    if isinstance(pattern, str) and ("/" in pattern or "\\" in pattern):
        yield pattern


def evaluate(payload: dict[str, Any]) -> str | None:
    """返回拒绝原因；None 表示放行。"""
    tool = str(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return "无法解析工具参数"
    base = _project_root(payload)

    if tool in SHELL_TOOLS:
        command = tool_input.get("command", "")
        return check_command(command) if isinstance(command, str) else "无法解析命令"

    if tool in WRITE_TOOLS or tool in READ_TOOLS:
        write = tool in WRITE_TOOLS
        for path in _candidate_paths(tool_input):
            hit = check_path(path, base, write=write)
            if hit:
                action = "写入" if write else "读取"
                return f"拒绝{action}受保护路径 {hit}（{path}）"
    return None


def main() -> int:
    # Windows 下管道默认是 GBK；钩子和 git 读取的输出统一用 UTF-8
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
        if not isinstance(payload, dict):
            raise ValueError("payload 不是 JSON 对象")
        reason = evaluate(payload)
    except Exception as exc:
        print(f"[guard_paths] 钩子内部错误，按拒绝处理：{exc!r}", file=sys.stderr)
        return BLOCK
    if reason:
        print(
            f"[guard_paths] {reason}。如确需修改，请 Dylan 按 "
            "docs/runbooks/protected-config-change.md 手动操作。",
            file=sys.stderr,
        )
        return BLOCK
    return 0


if __name__ == "__main__":
    sys.exit(main())
