"""接口清单（H-01）：docs/api/ROUTES.md 与实际路由完全一致，每行都写明使用方、测试与归属。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.routing import APIWebSocketRoute
from server.api.app import create_app
from tests.unit.api.test_app import Gateway, Tokens

ROOT = Path(__file__).resolve().parents[3]
INVENTORY = ROOT / "docs" / "api" / "ROUTES.md"
OPENAPI = ROOT / "docs" / "api" / "openapi.json"


def rows() -> dict[tuple[str, str], list[str]]:
    table: dict[tuple[str, str], list[str]] = {}
    for line in INVENTORY.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 7 and cells[1].startswith("/v1/"):
            table[(cells[0], cells[1])] = cells[2:]
    return table


def actual() -> set[tuple[str, str]]:
    spec = json.loads(OPENAPI.read_text(encoding="utf-8"))
    found = {(m.upper(), p) for p, ops in spec["paths"].items() for m in ops}
    return found | websockets()


def websockets() -> set[tuple[str, str]]:
    """OpenAPI 不含 WebSocket：直接从应用的路由表里取（评审 P2-3，新增 WS 路由也会被发现）。"""
    from server.api.app import ApiDeps  # noqa: PLC0415
    from server.api.security import MemoryAudit  # noqa: PLC0415

    app = create_app(ApiDeps(authenticator=Tokens(), ledger=lambda: None,  # type: ignore[arg-type, return-value]
                             gateway=Gateway(), audit=MemoryAudit()))  # fmt: skip
    return {("WS", r.path) for r in app.routes if isinstance(r, APIWebSocketRoute)}


def test_inventory_matches_routes() -> None:
    listed = set(rows())
    assert actual() - listed == set(), "有接口没登记进 docs/api/ROUTES.md"
    assert listed - actual() == set(), "docs/api/ROUTES.md 里有已经不存在的接口"


def test_every_route_has_user_test_and_owner() -> None:
    for route, (purpose, user, test, web, owner) in rows().items():
        assert purpose and user and owner, route
        assert web in {"是", "否"}, route
        assert (ROOT / test).is_file(), f"{route} 的测试文件不存在：{test}"
