"""JSON schema helpers for constrained decoding."""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve local `$ref`s into a self-contained schema (no `$defs`).

    Grammar-based decoders (llama.cpp / Ollama) handle flat schemas most reliably.
    """
    schema = copy.deepcopy(schema)
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = copy.deepcopy(defs[ref.split("/")[-1]])
                extra = {k: v for k, v in node.items() if k != "$ref"}
                target.update(extra)
                return resolve(target)
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


def strict_object_schema(model_cls: type[BaseModel]) -> dict[str, Any]:
    """Flat schema with every property required and no extra properties."""
    schema = inline_refs(model_cls.model_json_schema())

    def tighten(node: Any) -> Any:
        """Tighten one schema node. Only schema annotations named `title`/`default` are
        dropped; a *property* called `title` lives inside `properties` and is kept."""
        if isinstance(node, list):
            return [tighten(v) for v in node]
        if not isinstance(node, dict):
            return node
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("title", "default"):
                continue
            if key in ("properties", "$defs") and isinstance(value, dict):
                out[key] = {name: tighten(sub) for name, sub in value.items()}
            else:
                out[key] = tighten(value)
        if out.get("type") == "object" and "properties" in out:
            out["required"] = list(out["properties"].keys())
            out["additionalProperties"] = False
        return out

    return tighten(schema)
