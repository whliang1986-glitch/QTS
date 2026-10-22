"""git pre-commit 第二道防线：拒绝提交受保护路径的改动（F-02）。

- secrets/：任何情况下都拒绝提交。
- config/risk/、config/live/：默认拒绝；Dylan 本人修改时，按
  docs/runbooks/protected-config-change.md 在自己的终端里设置 ALLOW_PROTECTED_CONFIG=1 后提交。
  Claude Code 的命令里出现该变量会被 .claude/hooks/guard_paths.py 拦截。
"""

from __future__ import annotations

import os
import subprocess
import sys


def classify(path: str) -> str | None:
    parts = [p.casefold() for p in path.replace("\\", "/").split("/") if p]
    for i, part in enumerate(parts):
        if part == "secrets":
            return "secrets"
        if part == "config" and i + 1 < len(parts) and parts[i + 1] in {"risk", "live"}:
            return "config"
    return None


def staged_paths() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--no-renames", "-z"],
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    return [p for p in out.split("\0") if p]


def find_violations(paths: list[str], *, allow_config: bool) -> list[str]:
    bad = []
    for path in paths:
        kind = classify(path)
        if kind == "secrets" or (kind == "config" and not allow_config):
            bad.append(path)
    return bad


def main() -> int:
    # Windows 下管道默认是 GBK；钩子和 git 读取的输出统一用 UTF-8
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    allow_config = os.environ.get("ALLOW_PROTECTED_CONFIG") == "1"
    violations = find_violations(staged_paths(), allow_config=allow_config)
    if not violations:
        return 0
    print("拒绝提交受保护路径：", file=sys.stderr)
    for path in violations:
        print(f"  - {path}", file=sys.stderr)
    print(
        "secrets/ 永不入库；config/risk、config/live 的修改流程见 "
        "docs/runbooks/protected-config-change.md",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
