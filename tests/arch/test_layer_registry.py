"""新增分层不能默认放行：server/ 下每个分层都必须登记，并被依赖契约完整覆盖（F-03 评审第 6 条）。"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from scripts.arch_scan import BROKER_IMPORT_ALLOWED, KNOWN_LAYERS

ROOT = Path(__file__).resolve().parents[2]


def _contracts() -> dict[str, dict[str, Any]]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return {c["id"]: c for c in data["tool"]["importlinter"]["contracts"]}


def _layers(modules: list[str]) -> set[str]:
    return {m.removeprefix("server.") for m in modules}


def test_every_server_entry_is_a_registered_layer() -> None:
    server = ROOT / "server"
    # 不要求 __init__.py：没有它的命名空间包同样必须登记
    entries = {p.name for p in server.iterdir() if p.is_dir() and p.name != "__pycache__"}
    entries |= {p.stem for p in server.glob("*.py") if p.name != "__init__.py"}
    assert entries == set(KNOWN_LAYERS), (
        "server/ 下的分层与 scripts/arch_scan.py 的 KNOWN_LAYERS 不一致；"
        "新增分层必须先登记并在 pyproject.toml 的契约里补齐依赖规则"
    )


def test_domain_contract_forbids_every_other_layer() -> None:
    forbidden = _layers(_contracts()["domain-independent"]["forbidden_modules"])
    assert forbidden == set(KNOWN_LAYERS) - {"domain"}


def test_broker_allowlist_covers_every_layer() -> None:
    sources = _layers(_contracts()["broker-importers-allowlist"]["source_modules"])
    allowed = {a.split("/", 1)[0] for a in BROKER_IMPORT_ALLOWED}
    assert sources.isdisjoint(allowed)
    assert sources | allowed == set(KNOWN_LAYERS), "有分层既不在禁止列表也不在允许列表"


def test_fake_broker_contract_covers_every_layer_but_broker_and_backtest() -> None:
    sources = _layers(_contracts()["fake-broker-test-only"]["source_modules"])
    assert sources == set(KNOWN_LAYERS) - {"broker", "backtest"}


def test_no_layer_may_depend_on_the_assembly_layer() -> None:
    """S-01c 前置：新增分层时必须同时补进 app-is-top，否则“其他层不得依赖 app”会悄悄失效。"""
    contract = _contracts()["app-is-top"]
    assert _layers(contract["source_modules"]) == set(KNOWN_LAYERS) - {"app"}
    assert contract["forbidden_modules"] == ["server.app"]
    assert not contract.get("allow_indirect_imports", False)


def test_assembly_layer_does_not_import_oms_directly() -> None:
    contract = _contracts()["app-no-direct-oms"]
    assert contract["source_modules"] == ["server.app"]
    assert contract["forbidden_modules"] == ["server.oms"]


def test_platform_leaf_covers_every_other_layer() -> None:
    forbidden = _layers(_contracts()["platforms-leaf"]["forbidden_modules"])
    assert forbidden == set(KNOWN_LAYERS) - {"platforms"}
