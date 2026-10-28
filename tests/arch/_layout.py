"""架构测试的辅助工具：在临时目录里造一棵与真实仓库同形的 server 包树。"""

from __future__ import annotations

from pathlib import Path

LAYERS = [
    "domain",
    "storage",
    "ipc",
    "broker",
    "broker/ibkr",
    "broker/fake",
    "oms",
    "risk",
    "data",
    "strategy",
    "backtest",
    "ai",
    "api",
    "guard",
    "platforms",
]


def make_server_tree(root: Path, files: dict[str, str] | None = None) -> Path:
    """在 root 下建 server/ 及全部分层空包，再写入 files（相对 root 的路径 → 源码）。"""
    (root / "server").mkdir(parents=True, exist_ok=True)
    (root / "server" / "__init__.py").write_text('"""临时包。"""\n', encoding="utf-8")
    for layer in LAYERS:
        pkg = root / "server" / layer
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "__init__.py").write_text('"""临时包。"""\n', encoding="utf-8")
    for rel, source in (files or {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return root
