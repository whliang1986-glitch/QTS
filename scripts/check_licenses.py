"""第三方依赖许可证清单与检查（H-01）。

- Python：读当前环境里已安装的包（`uv sync` 之后就是 uv.lock 的内容），取 License-Expression、
  License 分类或 License 字段。
- 前端：读 web/package-lock.json 里每个包登记的 license（npm 锁文件 v3 自带，不用装 node_modules）。
- 强传染许可（GPL、AGPL、SSPL 等）一律报错：这类许可要求整个系统开源，不能用。
  LGPL 只提醒（动态链接一般可用，但要逐个确认）；
  MPL-2.0 是文件级传染，不改它的源码就可以用，记入清单。
  认不出许可的包也报错，要人工确认后登记在 REVIEWED 里，写明确认结果。

用法：
    uv run python scripts/check_licenses.py           # 只检查，有问题退出码 1
    uv run python scripts/check_licenses.py --write   # 同时重写 docs/licenses/THIRD_PARTY.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs" / "licenses" / "THIRD_PARTY.md"
LOCKFILE = ROOT / "web" / "package-lock.json"
OWN_PACKAGES = frozenset({"quant", "qts-web"})

# 判定顺序（H-01 评审 P1-1、P1-2）：先在许可全文里找强传染许可（去掉 LGPL 字样后再找 GPL，
# 混合许可“GPL OR LGPL”也会被拦下）；再找 LGPL；最后要求每一项都在白名单里，认不出的一律禁止。
_LESSER = re.compile(r"\bLGPL[-\w.+]*|Lesser General Public License|Library General Public License",
                     re.IGNORECASE)  # fmt: skip
_DENY = re.compile(
    r"\bA?GPL(v?\d|\b)|\b(SSPL|EUPL|OSL|CPAL|RPL|BUSL|Business Source|Elastic|Commons Clause|"
    r"Proprietary|UNLICENSED|SEE LICENSE|CC-BY-NC|Non-?Commercial)\b|General Public License",
    re.IGNORECASE,
)
# 白名单：可免费商用、允许再分发的许可（SPDX 标识或 PyPI 分类名里的关键词）
_ALLOWED = re.compile(
    r"^(MIT(-0)?|MIT License|(0|[23]-Clause )?BSD([- ][0-3]-Clause)?( License)?|"
    r"BSD-[23]-Clause|0BSD|"
    r"Apache(-2\.0| Software License| License 2\.0)?|ISC( License)?|MPL-2\.0|"
    r"Mozilla Public License 2\.0 \(MPL 2\.0\)|PSF-2\.0|Python Software Foundation License|"
    r"Python-2\.0|Unlicense|The Unlicense \(Unlicense\)|CC0-1\.0|CC-BY-4\.0|Zlib|BlueOak-1\.0\.0)$",
    re.IGNORECASE,
)

# 认不出许可、人工核对过的包：包名 → 核对结果（写明许可与核对依据）
REVIEWED: dict[str, str] = {}


@dataclass(frozen=True)
class Package:
    ecosystem: str  # python / npm
    name: str
    version: str
    license: str
    dev: bool | None  # npm 锁文件能区分开发依赖；Python 环境里不区分（None）
    full_text: str = ""  # 许可全文（Python 的 License 字段）：强传染许可的检查看全文


def _python_license(dist: metadata.Distribution) -> tuple[str, str]:
    """(显示用的许可, 用于检查的全文)。"""
    md = dist.metadata
    raw = str(md.get("License") or "").strip()
    classifiers = [c.split("::")[-1].strip() for c in md.get_all("Classifier") or []
                   if c.startswith("License ::")]  # fmt: skip
    expression = str(md.get("License-Expression") or "")
    full = "\n".join([expression, *classifiers, raw])
    if expression:
        return expression, full
    if classifiers:
        return " / ".join(sorted(set(classifiers))), full
    return (raw.splitlines()[0][:80] if raw else "UNKNOWN"), full


def python_packages() -> list[Package]:
    found: dict[str, Package] = {}
    for dist in metadata.distributions():
        name = str(dist.metadata["Name"]).lower()
        if name in OWN_PACKAGES:
            continue
        shown, full = _python_license(dist)
        found[name] = Package("python", name, dist.version, shown, None, full)
    return sorted(found.values(), key=lambda p: p.name)


def npm_packages() -> list[Package]:
    if not LOCKFILE.is_file():
        return []
    lock = json.loads(LOCKFILE.read_text(encoding="utf-8"))
    result = []
    for path, info in lock.get("packages", {}).items():
        if not path:  # 根项目本身
            continue
        name = path.split("node_modules/")[-1]
        result.append(Package(
            "npm", name, str(info.get("version", "?")), str(info.get("license") or "UNKNOWN"),
            bool(info.get("dev", False)),
        ))  # fmt: skip
    return sorted(result, key=lambda p: p.name)


def _unwrap(text: str) -> str:
    """去掉整体包着的一层括号："(MIT OR X)" → "MIT OR X"；"MPL 2.0 (MPL 2.0)" 不动。"""
    text = text.strip()
    return text[1:-1].strip() if text.startswith("(") and text.endswith(")") else text


def _allowed(expression: str) -> bool:
    """SPDX 式表达式：OR 只要一项可用；AND 与“ / ”（多个分类）要求每一项都可用。"""
    cleaned = _unwrap(expression)
    if re.search(r"\s(AND|OR)\s.*\(", cleaned):  # 嵌套括号的组合表达式：不自动判断，要人工登记
        return False
    if re.search(r"\s+OR\s+", cleaned):
        return any(_allowed(part) for part in re.split(r"\s+OR\s+", cleaned))
    parts = [_unwrap(p) for p in re.split(r"\s+AND\s+|\s+/\s+", cleaned)]
    return bool(parts) and all(_ALLOWED.match(p) for p in parts if p)


def verdict(pkg: Package) -> str:
    """ok / warn / deny。warn 与 deny 都让检查失败（登记在 REVIEWED 里的除外）。"""
    if pkg.name in REVIEWED:
        return "ok"
    text = "\n".join([pkg.license, pkg.full_text])
    if _DENY.search(_LESSER.sub(" ", text)):
        return "deny"
    if _LESSER.search(text):
        return "warn"
    return "ok" if _allowed(pkg.license) else "deny"


def render(packages: list[Package]) -> str:
    lines = [
        "# 第三方依赖许可证清单",
        "",
        "由 `uv run python scripts/check_licenses.py --write` 生成，不要手改。",
        "Python 部分来自生成时的本机环境（Windows），Linux 上会多出或少几个平台相关的包。",
        "",
        "规则：GPL、AGPL、SSPL 等强传染许可禁止使用；LGPL 需逐个确认；",
        "认不出许可的包要人工核对后登记。",
        "MPL-2.0 是文件级传染：只要不修改这些包的源码就可以使用。",
        "",
        "| 生态 | 包 | 版本 | 许可 | 开发依赖 | 结论 |",
        "|---|---|---|---|---|---|",
    ]
    for p in packages:
        dev = "—" if p.dev is None else ("是" if p.dev else "否")
        note = REVIEWED.get(p.name, "")
        result = {"ok": "可用", "warn": "需确认", "deny": "禁止"}[verdict(p)]
        lic = p.license.replace("|", "/")
        lines.append(f"| {p.ecosystem} | {p.name} | {p.version} | {lic} | {dev} | {result}{note} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="重写 docs/licenses/THIRD_PARTY.md")
    args = parser.parse_args(argv)
    packages = python_packages() + npm_packages()
    if args.write:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(render(packages), encoding="utf-8", newline="\n")
    denied = [p for p in packages if verdict(p) == "deny"]
    warned = [p for p in packages if verdict(p) == "warn"]
    for p in warned:
        print(f"需确认：{p.ecosystem} {p.name} {p.version}：{p.license}")
    for p in denied:
        print(f"禁止：{p.ecosystem} {p.name} {p.version}：{p.license}")
    print(f"共 {len(packages)} 个依赖；禁止 {len(denied)}，需确认 {len(warned)}")
    return 1 if denied or warned else 0


if __name__ == "__main__":
    sys.exit(main())
