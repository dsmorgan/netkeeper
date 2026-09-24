"""``/posture``: every protection the LinkedIn extractor has, as data (P2-12).

Spec section 9 spreads the extractor's safety across five modules (browser attach,
pacing, budgets, heat, the session flag); `netkeeper.services.posture` reads through
all of them and answers "what is protecting me, and is any of it off right now?" --
see its own module docstring. `netkeeper posture` has printed this report from a
terminal since P2-11; this is the same report, for the Settings page (the frontend
pages table in docs/architecture.md names it as arriving with P2-12).

Read-only and per-user: `posture()` is called with no browser probe (`probe=None`),
which the service reports as an unknown protection rather than assuming healthy --
a live attach-and-read-the-session check has to await browser work, and CLAUDE.md
forbids that inside a request handler. A live check stays `netkeeper preflight`, a
terminal command.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from netkeeper.config import Settings
from netkeeper.models.base import utcnow
from netkeeper.services import posture as posture_service
from netkeeper.services.linkedin_accounts import account_id_for
from netkeeper.web.deps import CurrentUser, SessionDep
from netkeeper.web.schemas import PostureOut, ProtectionOut

router = APIRouter(tags=["posture"])


@router.get("/posture", operation_id="get_posture")
def get_posture(request: Request, user: CurrentUser, session: SessionDep) -> PostureOut:
    settings: Settings = request.app.state.settings
    account_id = account_id_for(session, user)
    report = posture_service.posture(
        session, user, account_id, now=utcnow(), settings=settings, probe=None
    )
    return PostureOut(
        checked_at=report.checked_at,
        timezone=report.timezone,
        local_time=report.local_time,
        protections=[
            ProtectionOut(
                name=protection.name,
                status=protection.status.value,
                value=protection.value,
                warnings=list(protection.warnings),
            )
            for protection in report.protections
        ],
        warnings=list(report.warnings),
        gaps=list(report.gaps),
        ok=report.ok,
        verdict=posture_service.verdict(report),
    )
