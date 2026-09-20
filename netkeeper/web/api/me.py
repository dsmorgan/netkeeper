"""``/me``: the current user, as the ``CurrentUser`` dependency resolves it (spec 14.1)."""

from __future__ import annotations

from fastapi import APIRouter

from netkeeper.web.deps import CurrentUser
from netkeeper.web.schemas import UserOut

router = APIRouter(tags=["me"])


@router.get("/me", operation_id="get_me")
def get_me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)
