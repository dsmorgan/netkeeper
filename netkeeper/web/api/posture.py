"""``/posture``: every protection the LinkedIn extractor has, as data (P2-12).

Spec section 9 spreads the extractor's safety across five modules (browser attach,
pacing, budgets, heat, the session flag); `netkeeper.services.posture` reads through
all of them and answers "what is protecting me, and is any of it off right now?" --
see its own module docstring. `netkeeper posture` has printed this report from a
terminal since P2-11; this is the same report, for the Settings page (the frontend
pages table in docs/architecture.md names it as arriving with P2-12).

Read-only and per-user: `posture()` is called with no browser probe (`probe=None`)
-- a live attach-and-read-the-session check has to await browser work, and CLAUDE.md
forbids that inside a request handler. The LinkedIn session row answers instead from
the last evidence the database holds (#282): what `netkeeper preflight` or `netkeeper
posture --probe` last recorded, or the newest run that read LinkedIn without flagging
the session. With neither, it says unknown rather than assuming healthy. A live check
stays `netkeeper preflight`, a terminal command.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from netkeeper.models.base import utcnow
from netkeeper.services import posture as posture_service
from netkeeper.services.linkedin_accounts import account_id_for
from netkeeper.services.read_failures import ReadFailures
from netkeeper.web.deps import CurrentUser, SessionDep, effective_settings
from netkeeper.web.schemas import PostureOut, ProtectionOut

router = APIRouter(tags=["posture"])


def _unreadable(request: Request, user_id: int) -> list[str]:
    """What the running ``serve`` could not read of this user's settings (#464)."""
    failures: ReadFailures | None = getattr(request.app.state, "read_failures", None)
    return [] if failures is None else failures.describe(user_id)


@router.get("/posture", operation_id="get_posture")
def get_posture(request: Request, user: CurrentUser, session: SessionDep) -> PostureOut:
    settings = effective_settings(request, session, user)
    account_id = account_id_for(session, user)
    report = posture_service.posture(
        session, user, account_id, now=utcnow(), settings=settings, probe=None
    )
    unreadable = _unreadable(request, user.id)
    return PostureOut(
        checked_at=report.checked_at,
        timezone=report.timezone,
        local_time=report.local_time,
        protections=[
            ProtectionOut(
                name=protection.name,
                key=protection.key,
                status=protection.status.value,
                value=protection.value,
                summary=protection.summary,
                warnings=list(protection.warnings),
                notes=list(protection.notes),
            )
            for protection in report.protections
        ],
        warnings=[*report.warnings, *unreadable],
        notes=list(report.notes),
        gaps=list(report.gaps),
        ok=report.ok and not unreadable,
        verdict=posture_service.verdict(report),
    )
