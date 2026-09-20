"""The two-user isolation harness (spec section 5, ADR 0005).

Every list endpoint registers in :data:`registry.REGISTRY`. ``test_isolation.py``
checks that the registry covers every list operation the OpenAPI schema exposes
and that each registered endpoint shows a user only its own rows.
"""
