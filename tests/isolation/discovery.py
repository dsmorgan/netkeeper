"""Find the list operations in an OpenAPI schema. Pure: a dict in, a set of paths out."""

from __future__ import annotations

from typing import Any

REF_PREFIX = "#/components/schemas/"


def list_operations(openapi: dict[str, Any]) -> set[str]:
    """Paths of every ``GET`` whose ``200`` JSON response is an array or a paged object.

    A paged object has an ``items`` property of type array, the convention for
    paged lists. ``$ref`` schemas are resolved against ``components/schemas``.
    """
    found: set[str] = set()
    for path, item in openapi.get("paths", {}).items():
        operation = item.get("get")
        if operation is None:
            continue
        schema = _response_schema(operation)
        if schema is not None and _is_list(openapi, schema):
            found.add(path)
    return found


def _response_schema(operation: dict[str, Any]) -> dict[str, Any] | None:
    response = operation.get("responses", {}).get("200", {})
    content = response.get("content", {}).get("application/json", {})
    schema = content.get("schema")
    return schema if isinstance(schema, dict) else None


def _is_list(openapi: dict[str, Any], schema: dict[str, Any]) -> bool:
    resolved = _resolve(openapi, schema)
    if resolved.get("type") == "array":
        return True
    if resolved.get("type") != "object":
        return False
    items = resolved.get("properties", {}).get("items")
    return isinstance(items, dict) and _resolve(openapi, items).get("type") == "array"


def _resolve(openapi: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Follow ``$ref`` links until a concrete schema; unknown refs resolve to ``{}``."""
    seen: set[str] = set()
    while "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith(REF_PREFIX) or ref in seen:
            return {}
        seen.add(ref)
        target = openapi.get("components", {}).get("schemas", {}).get(ref[len(REF_PREFIX) :])
        if not isinstance(target, dict):
            return {}
        schema = target
    return schema
