"""导出 apid 的 OpenAPI 与 TypeScript 类型（S-02）。

    uv run python scripts/gen_api_types.py          # 重新生成
    uv run python scripts/gen_api_types.py --check  # 只比对，不一致退出码 1（CI / 测试用）

- docs/api/openapi.json：REST 接口（FastAPI 生成）；
- web/src/api/types.gen.ts：REST 模型 + WebSocket 消息模型的 TS 类型。
只覆盖本项目用到的 JSON Schema 子集；遇到不认识的结构直接报错，不生成含糊的类型。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic.json_schema import models_json_schema
from server.api.app import ApiDeps, create_app
from server.api.models import STREAM_MODELS, StreamAuth
from server.api.security import MemoryAudit

ROOT = Path(__file__).resolve().parents[1]
OPENAPI = ROOT / "docs" / "api" / "openapi.json"
TYPES = ROOT / "web" / "src" / "api" / "types.gen.ts"
HEADER = (
    "// 由 scripts/gen_api_types.py 生成，不要手改（S-02）。\n"
    "// 金额与数量是十进制字符串；时间是带时区的 UTC ISO 字符串。\n"
)


class SchemaError(Exception):
    pass


class _Unused:
    def authenticate(self, token: str) -> None:
        raise AssertionError("只用于生成文档")

    async def send(self, *_a: Any, **_kw: Any) -> Any:
        raise AssertionError("只用于生成文档")

    def __call__(self) -> Any:
        raise AssertionError("只用于生成文档")


def openapi() -> dict[str, Any]:
    unused = _Unused()
    app = create_app(
        ApiDeps(authenticator=unused, ledger=unused, gateway=unused, audit=MemoryAudit())
    )
    return app.openapi()


def _ref(ref: str) -> str:
    prefixes = ("#/components/schemas/", "#/$defs/")
    for prefix in prefixes:
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    raise SchemaError(f"不认识的引用：{ref}")


def ts_type(schema: Mapping[str, Any]) -> str:  # noqa: PLR0911 - 逐种结构映射
    if "$ref" in schema:
        return _ref(schema["$ref"])
    if "const" in schema:
        return json.dumps(schema["const"], ensure_ascii=False)
    if "enum" in schema:
        return " | ".join(json.dumps(v, ensure_ascii=False) for v in schema["enum"])
    if "anyOf" in schema:
        return " | ".join(ts_type(s) for s in schema["anyOf"])
    kind = schema.get("type")
    if kind == "string":
        return "string"
    if kind in {"integer", "number"}:
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind == "null":
        return "null"
    if kind == "array":
        inner = ts_type(schema.get("items", {}))
        return f"({inner})[]" if "|" in inner else f"{inner}[]"
    if kind == "object":
        if "properties" in schema:
            return _object(schema)
        extra = schema.get("additionalProperties", True)
        value = "unknown" if extra is True or extra == {} else ts_type(extra)
        return f"Record<string, {value}>"
    if not schema:
        return "unknown"
    raise SchemaError(f"不认识的结构：{json.dumps(schema, ensure_ascii=False)[:200]}")


def _object(schema: Mapping[str, Any], indent: str = "  ") -> str:
    required = set(schema.get("required", ()))
    lines = ["{"]
    for name, prop in schema["properties"].items():
        mark = "" if name in required else "?"
        lines.append(f"{indent}{name}{mark}: {ts_type(prop)};")
    lines.append("}")
    return "\n".join(lines)


def _render(schema: Mapping[str, Any]) -> str:
    if schema.get("type") == "object" and "properties" in schema:
        return _object(schema)
    return ts_type(schema)


def typescript(schemas: Mapping[str, Mapping[str, Any]]) -> str:
    parts = [HEADER]
    for name in sorted(schemas):
        schema = schemas[name]
        if schema.get("type") == "object" and "properties" in schema:
            parts.append(f"export interface {name} {_object(schema)}\n")
        else:
            parts.append(f"export type {name} = {ts_type(schema)};\n")
    stream = " | ".join(m.__name__ for m in STREAM_MODELS if m.__name__ != "StreamAuth")
    parts.append(f"/** /v1/stream 服务端推送的消息 */\nexport type StreamMessage = {stream};\n")
    return "\n".join(parts)


def all_schemas(spec: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    schemas: dict[str, Mapping[str, Any]] = dict(spec["components"]["schemas"])
    _, extra = models_json_schema(
        [(m, "validation" if m is StreamAuth else "serialization") for m in STREAM_MODELS]
    )
    for name, schema in extra.get("$defs", {}).items():
        # 同名不同构（引用前缀不同不算）：TS 类型会有歧义
        if name in schemas and _render(schemas[name]) != _render(schema):
            raise SchemaError(f"模型 {name} 在 OpenAPI 与事件流里结构不同")
        schemas[name] = schema
    return schemas


def generate() -> dict[Path, str]:
    spec = openapi()
    return {
        OPENAPI: json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        TYPES: typescript(all_schemas(spec)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    stale = []
    for path, text in generate().items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == text:
            continue
        if args.check:
            stale.append(path.relative_to(ROOT).as_posix())
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
    if stale:
        sys.stderr.write(f"接口类型过期，请运行 scripts/gen_api_types.py：{stale}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
