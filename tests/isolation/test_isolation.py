"""The two-user isolation test (spec section 5, ADR 0005): one case per registered endpoint.

The registry check that fails an unregistered list endpoint is test_coverage.py, so
it keeps running while this module is skipped for an empty registry.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from .harness import assert_isolated
from .registry import REGISTRY, ListEndpoint

if not REGISTRY:
    pytest.skip("REGISTRY is empty: no list endpoints exist yet", allow_module_level=True)


@pytest.mark.parametrize("endpoint", REGISTRY, ids=[endpoint.path for endpoint in REGISTRY])
async def test_registered_list_endpoint_is_isolated(
    endpoint: ListEndpoint, running_app: FastAPI
) -> None:
    await assert_isolated(running_app, endpoint)
