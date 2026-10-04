"""``/dashboard``: the reads the dashboard needs that no other resource answers (P3-12).

Everything else on the dashboard comes from the resource that already owns it
(``/linkedin/schedule``, ``/linkedin/budget``, ``/linkedin/heat``,
``/linkedin/status``, ``/linkedin/runs``, ``/mailboxes``), so each number has
one source. See :mod:`netkeeper.services.dashboard`.

Read-only and per-user. Nothing here touches the browser or Gmail: every
answer is a database read.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Query

from netkeeper.models import Contact
from netkeeper.models.base import utcnow
from netkeeper.services import campaign_engine
from netkeeper.services import dashboard as service
from netkeeper.web.deps import CurrentUser, SessionDep
from netkeeper.web.schemas import (
    ChangedJobOut,
    ChangedJobPage,
    InboundOut,
    NextFireOut,
    NextFirePage,
)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

#: A dashboard card lists a handful; the page behind it has the rest.
MAX_LIMIT: Final = 50


def _name(contact: Contact) -> str:
    first = contact.preferred_name or contact.first_name
    return " ".join(part for part in (first, contact.last_name) if part)


@router.get("/next-fires", operation_id="list_next_fires")
def list_next_fires(
    user: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 5,
) -> NextFirePage:
    """The next campaign steps due, soonest first (``next_action_at``, spec 11.4).

    A LinkedIn step is listed too, with ``ready_to_prefill``: a person prefills it
    (P4-09), and the tick never fires it."""
    now = utcnow()
    fires, total = campaign_engine.upcoming(session, user, limit=limit, include_linkedin=True)
    return NextFirePage(
        items=[
            NextFireOut(
                enrollment_id=fire.enrollment.id,
                due=fire.due,
                campaign_id=fire.campaign.id,
                campaign_name=fire.campaign.name,
                step_position=None if fire.step is None else fire.step.position,
                channel=None if fire.step is None else fire.step.channel.value,
                contact_id=fire.contact.id,
                contact_name=_name(fire.contact),
                ready_to_prefill=fire.ready_to_prefill(now),
            )
            for fire in fires
        ],
        total=total,
    )


@router.get("/changed-jobs", operation_id="list_changed_jobs")
def list_changed_jobs(
    user: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 10,
) -> ChangedJobPage:
    """Live contacts netkeeper saw change position in the last 30 days, newest first (spec 9.8).

    A change is one an enrichment visit noticed (a contact snapshot), dated when
    it was noticed, not the start date on the profile (#286).
    """
    rows, total = service.changed_jobs(session, user, now=utcnow(), limit=limit)
    return ChangedJobPage(
        items=[
            ChangedJobOut(
                contact_id=row.contact.id,
                contact_name=_name(row.contact),
                current_title=row.contact.current_title,
                current_company=row.contact.current_company,
                noticed_at=row.noticed_at,
            )
            for row in rows
        ],
        total=total,
        days=service.CHANGED_JOBS_DAYS,
    )


@router.get("/inbound", operation_id="get_inbound_this_week")
def get_inbound(user: CurrentUser, session: SessionDep) -> InboundOut:
    """Inbound messages in the last seven days. Replies are not detected yet (P3-08)."""
    count, since = service.inbound_since(session, user, now=utcnow())
    return InboundOut(count=count, since=since, reply_detection=False)
