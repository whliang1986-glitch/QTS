"""OpenAPI 与 TS 类型（S-02）：仓库里的文件必须与当前代码生成的一致。"""

from __future__ import annotations

import pytest
from scripts.gen_api_types import OPENAPI, TYPES, SchemaError, generate, main, ts_type


def test_checked_in_files_are_up_to_date() -> None:
    for path, text in generate().items():
        assert path.read_text(encoding="utf-8") == text, (
            f"{path.name} 过期：运行 uv run python scripts/gen_api_types.py"
        )
    assert main(["--check"]) == 0


def test_every_route_declares_bearer_auth_and_v1_prefix() -> None:
    import json  # noqa: PLC0415

    spec = json.loads(OPENAPI.read_text(encoding="utf-8"))
    assert spec["paths"]
    for path, ops in spec["paths"].items():
        assert path.startswith("/v1/"), path
        for op in ops.values():
            assert op.get("security") == [{"HTTPBearer": []}], path


def test_types_cover_stream_messages() -> None:
    text = TYPES.read_text(encoding="utf-8")
    assert "export type StreamMessage = " in text
    assert "export interface CommandRequest" in text


def test_unknown_schema_shapes_are_refused() -> None:
    with pytest.raises(SchemaError):
        ts_type({"type": "tuple"})
    with pytest.raises(SchemaError):
        ts_type({"$ref": "http://elsewhere/x"})
