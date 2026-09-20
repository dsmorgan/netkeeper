"""``discovery.list_operations`` on hand-written schemas."""

from __future__ import annotations

from typing import Any

from .discovery import list_operations


def _get_json(schema: dict[str, Any]) -> dict[str, Any]:
    return {"get": {"responses": {"200": {"content": {"application/json": {"schema": schema}}}}}}


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


HANDWRITTEN: dict[str, Any] = {
    "paths": {
        "/api/v1/contacts": _get_json(_ref("ContactPage")),
        "/api/v1/tags": _get_json({"type": "array", "items": _ref("Tag")}),
        "/api/v1/lists": _get_json(_ref("ListPageAlias")),
        "/api/v1/me": _get_json(_ref("User")),
        "/api/v1/contacts/{id}": {
            **_get_json(_ref("Contact")),
            "delete": {"responses": {"204": {"description": "Deleted"}}},
        },
        "/api/v1/contacts/import": {
            "post": {
                "responses": {
                    "200": {"content": {"application/json": {"schema": {"type": "array"}}}}
                }
            }
        },
        "/api/v1/stats": _get_json(
            {"type": "object", "properties": {"items": {"type": "integer"}}}
        ),
        "/api/v1/events": {"get": {"responses": {"200": {"content": {"text/event-stream": {}}}}}},
        "/api/v1/broken": _get_json(_ref("Missing")),
        "/api/v1/health": {"get": {"responses": {"200": {"description": "no content"}}}},
    },
    "components": {
        "schemas": {
            "ContactPage": {
                "type": "object",
                "properties": {
                    "items": {"type": "array", "items": _ref("Contact")},
                    "total": {"type": "integer"},
                },
            },
            "ListPageAlias": _ref("ListPage"),
            "ListPage": {"type": "object", "properties": {"items": _ref("ListItems")}},
            "ListItems": {"type": "array", "items": {"type": "string"}},
            "Contact": {"type": "object", "properties": {"id": {"type": "integer"}}},
            "Tag": {"type": "object"},
            "User": {"type": "object"},
        }
    },
}


def test_list_operations_on_a_handwritten_schema() -> None:
    assert list_operations(HANDWRITTEN) == {"/api/v1/contacts", "/api/v1/tags", "/api/v1/lists"}


def test_list_operations_on_an_empty_schema() -> None:
    assert list_operations({}) == set()
    assert list_operations({"paths": {}}) == set()


def test_list_operations_survives_a_ref_cycle() -> None:
    schema = {
        "paths": {"/loop": _get_json(_ref("A"))},
        "components": {"schemas": {"A": _ref("B"), "B": _ref("A")}},
    }
    assert list_operations(schema) == set()
