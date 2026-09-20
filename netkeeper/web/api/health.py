"""``/health``: liveness for the top-bar indicator and the launchd agent."""

from __future__ import annotations

from fastapi import APIRouter

from netkeeper import __version__
from netkeeper.web.schemas import HealthOut

router = APIRouter(tags=["health"])


@router.get("/health", operation_id="get_health")
def get_health() -> HealthOut:
    return HealthOut(status="ok", version=__version__)
