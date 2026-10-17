"""``/health``: liveness for the top-bar indicator and the launchd agent.

``boot_id`` is new for each process, so after **Restart netkeeper** (#597) the UI can
tell the restarted server from the one that was stopping.
"""

from __future__ import annotations

import uuid
from typing import Final

from fastapi import APIRouter

from netkeeper import __version__
from netkeeper.web.schemas import HealthOut

router = APIRouter(tags=["health"])

BOOT_ID: Final = uuid.uuid4().hex
"""This process's id: a re-exec or a launchd restart gets a new one."""


@router.get("/health", operation_id="get_health")
def get_health() -> HealthOut:
    return HealthOut(status="ok", version=__version__, boot_id=BOOT_ID)
