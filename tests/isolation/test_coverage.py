"""Every list operation the real app exposes is registered for the isolation test."""

from __future__ import annotations

from fastapi import FastAPI

from .discovery import list_operations
from .registry import REGISTRY


def test_every_list_operation_is_registered(app: FastAPI) -> None:
    discovered = list_operations(app.openapi())
    registered = {endpoint.path for endpoint in REGISTRY}
    missing = sorted(discovered - registered)
    assert not missing, (
        f"list endpoints without an isolation test: {missing}. Add a ListEndpoint for "
        "each to REGISTRY in tests/isolation/registry.py; every list endpoint has a "
        "two-user isolation test (ADR 0005)."
    )
    stale = sorted(registered - discovered)
    assert not stale, f"registered paths that are not list operations in the schema: {stale}"
