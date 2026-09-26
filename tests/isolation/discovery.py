"""Find the list operations in an OpenAPI schema. Pure: a dict in, a set of paths out."""

from __future__ import annotations

from typing import Any

REF_PREFIX = "#/components/schemas/"


def list_operations(openapi: dict[str, Any]) -> set[str]:
    """Paths of every list operation: a ``GET`` returning rows, or a ``POST`` returning a page.

    A ``GET`` counts when its ``200`` JSON response is an array, a paged object
    — an object with an ``items`` property of type array, the convention for
    paged lists — or an object made of nothing but arrays, which is a list split
    into sections (``GET /imports/presets`` answers ``builtin`` and ``saved``,
    #78). An object with any non-array property is a record that happens to
    carry arrays (a contact and its emails) and does not count.

    A ``POST`` counts only when that response is a *page*. A query whose filter
    does not fit a query string (``POST /contacts/query``) is a list like any
    other and has to have an isolation test; a command that happens to answer
    with an array (``POST /autotag-rules/reorder`` returns the new order) is
    not one, and the harness, which only reads, could not test it anyway. So the
    convention is that a query pages. A create answers ``201``, so it is never
    mistaken for either.

    ``$ref`` schemas are resolved against ``components/schemas``.
    """
    found: set[str] = set()
    for path, item in openapi.get("paths", {}).items():
        lists = _is_list(openapi, _response_schema(item.get("get")), pages_only=False) or _is_list(
            openapi, _response_schema(item.get("post")), pages_only=True
        )
        if lists:
            found.add(path)
    return found


def _response_schema(operation: dict[str, Any] | None) -> dict[str, Any] | None:
    if operation is None:
        return None
    response = operation.get("responses", {}).get("200", {})
    content = response.get("content", {}).get("application/json", {})
    schema = content.get("schema")
    return schema if isinstance(schema, dict) else None


def _is_list(openapi: dict[str, Any], schema: dict[str, Any] | None, *, pages_only: bool) -> bool:
    if schema is None:
        return False
    resolved = _resolve(openapi, schema)
    if resolved.get("type") == "array":
        return not pages_only
    if resolved.get("type") != "object":
        return False
    properties = resolved.get("properties", {})
    items = properties.get("items")
    if isinstance(items, dict) and _resolve(openapi, items).get("type") == "array":
        return True
    return bool(properties) and all(
        isinstance(value, dict) and _resolve(openapi, value).get("type") == "array"
        for value in properties.values()
    )


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
