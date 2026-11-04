"""字体许可（H-01）：不打包未登记的字体，网页不引用外部字体服务，也不写 @font-face。

规则见 docs/licenses/FONTS.md。登记表里的许可只允许 OFL-1.1、Apache-2.0、MIT。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs" / "licenses" / "FONTS.md"
FONT_SUFFIXES = (".woff", ".woff2", ".ttf", ".otf", ".eot", ".ttc", ".otc", ".pfb")
ALLOWED_LICENSES = frozenset({"OFL-1.1", "Apache-2.0", "MIT"})
WEB_TEXT = (".css", ".scss", ".less", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".html", ".svg")
_FORBIDDEN = re.compile(
    r"@font-face|new\s+FontFace\s*\(|document\.fonts\.add|"
    r"fonts\.googleapis\.com|fonts\.gstatic\.com|use\.typekit\.net|fonts\.bunny\.net|"
    r"fonts\.cdnfonts\.com|fontawesome\.com|@fontsource|"
    r"https?://[^\s\"')]+\.(" + "|".join(x.lstrip(".") for x in FONT_SUFFIXES) + r")\b",
    re.IGNORECASE,
)


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def registered() -> dict[str, str]:
    """登记表：文件路径 → 许可。"""
    rows: dict[str, str] = {}
    for line in REGISTRY.read_text(encoding="utf-8").splitlines():
        cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and cells[0].lower().endswith(FONT_SUFFIXES):
            rows[cells[0]] = cells[1]
    return rows


def test_every_font_file_is_registered_with_a_free_license() -> None:
    fonts = [p for p in tracked_files() if p.lower().endswith(FONT_SUFFIXES)]
    table = registered()
    missing = [p for p in fonts if p not in table]
    assert missing == [], f"字体文件没有登记许可（docs/licenses/FONTS.md）：{missing}"
    bad = {p: lic for p, lic in table.items() if lic not in ALLOWED_LICENSES}
    assert bad == {}, f"字体许可不在允许范围（{sorted(ALLOWED_LICENSES)}）：{bad}"


def test_web_does_not_load_external_fonts_or_declare_font_face() -> None:
    hits = []
    for path in tracked_files():
        if not path.startswith("web/") or not path.lower().endswith(WEB_TEXT):
            continue
        text = (ROOT / path).read_text(encoding="utf-8", errors="replace")
        hits += [f"{path}: {m.group(0)}" for m in _FORBIDDEN.finditer(text)]
    assert hits == [], f"网页引用了外部字体或 @font-face：{hits}"


def test_detector_catches_known_patterns() -> None:
    """检查本身有效：这些写法都要被拦下。"""
    samples = (
        "@font-face { font-family: X; src: url(x.woff2); }",
        '<link href="https://fonts.googleapis.com/css2?family=Inter">',
        "@import url('https://use.typekit.net/abc.css');",
        "const f = new FontFace('X', 'url(x.woff2)'); document.fonts.add(f);",
        "@import '@fontsource/inter';",
        "src: url(https://cdn.jsdelivr.net/npm/some-font/files/x.woff2)",
    )
    assert all(_FORBIDDEN.search(s) for s in samples)
    assert not _FORBIDDEN.search('font-family: "Segoe UI", system-ui, sans-serif;')
