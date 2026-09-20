"""Registered list endpoints. Add one :class:`ListEndpoint` here per list operation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from netkeeper.models import User


@dataclass(frozen=True)
class ListEndpoint:
    """A list endpoint under the isolation test.

    ``path`` is the full path as the OpenAPI schema spells it (``/api/v1/contacts``).
    ``seed`` creates rows for the given user, through the services, and returns how
    many items the list should then show that user; it runs once per user on a
    fresh database. ``count`` extracts the item count from the decoded JSON body
    (:func:`paged_count` and :func:`array_count` cover the two response shapes).
    """

    path: str
    seed: Callable[[Session, User], int]
    count: Callable[[Any], int]


def paged_count(body: Any) -> int:
    """Item count of a paged response: an object with an ``items`` array."""
    items = body["items"]
    assert isinstance(items, list), f"items is not an array: {items!r}"
    return len(items)


def array_count(body: Any) -> int:
    """Item count of a plain array response."""
    assert isinstance(body, list), f"body is not an array: {body!r}"
    return len(body)


REGISTRY: list[ListEndpoint] = []
