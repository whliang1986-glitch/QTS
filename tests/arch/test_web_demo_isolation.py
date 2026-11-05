"""演示模式与生产构建隔离（UI-02；协调决定：生产构建不含随机数、硬编码余额、伪造新闻）。

静态检查（不需要 node）：
- 生产入口 index.html 与 src/main.tsx 不引用 demo；src/demo 之外没有任何文件 import demo 模块；
- Vite 配置不把 demo.html 加进构建入口（默认只构建 index.html）；
- demo 模块不用随机数，demo 页的 CSP 禁止一切网络连接（connect-src 'none'）；
- 指挥中枢的面板（src/console）不自己生成样例数据：不引用 demo、不用随机数。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
SRC = WEB / "src"
DEMO_IMPORT = re.compile(  # from "…demo…"、import("…demo…")、只为副作用的 import "…demo…"
    r"""from\s+["'][^"']*\bdemo\b[^"']*["']|import\(\s*["'][^"']*\bdemo\b|import\s+["'][^"']*\bdemo\b"""
)
RANDOM = re.compile(r"Math\.random|crypto\.getRandomValues|faker")


def _sources(folder: Path) -> list[Path]:
    return [p for p in folder.rglob("*") if p.suffix in {".ts", ".tsx"} and p.is_file()]


def test_production_entry_does_not_reference_demo() -> None:
    assert "demo" not in (WEB / "index.html").read_text(encoding="utf-8").lower()
    assert "demo" not in (SRC / "main.tsx").read_text(encoding="utf-8").lower()


def test_nothing_outside_demo_imports_demo() -> None:
    offenders = [
        str(p.relative_to(WEB)) for p in _sources(SRC)
        if SRC / "demo" not in p.parents and DEMO_IMPORT.search(p.read_text(encoding="utf-8"))
    ]  # fmt: skip
    assert offenders == []


def test_build_does_not_include_the_demo_page() -> None:
    config = (WEB / "vite.config.ts").read_text(encoding="utf-8")
    assert "demo" not in config.lower()  # 没有把 demo.html 加进 rollupOptions.input
    assert "rollupOptions" not in config  # 默认入口只有 index.html


def test_demo_uses_fixed_samples_and_cannot_reach_the_network() -> None:
    for path in _sources(SRC / "demo"):
        assert not RANDOM.search(path.read_text(encoding="utf-8")), path
    page = (WEB / "demo.html").read_text(encoding="utf-8")
    assert "connect-src 'none'" in page


def test_console_panels_do_not_generate_data() -> None:
    for path in _sources(SRC / "console"):
        text = path.read_text(encoding="utf-8")
        assert not RANDOM.search(text), path
        assert not DEMO_IMPORT.search(text), path
