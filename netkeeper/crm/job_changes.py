"""What counts as a job change, in one place (spec 9.8, #286, #313, #323).

A job change is a ``contact_snapshot`` marked ``position_changed`` with source
``sync``: an enrichment replaced a current title or company that an earlier
enrichment recorded (:func:`netkeeper.crm.identity.apply` sets the flag). A
contact's first enrichment never counts, and neither does a title or company
from the connections list, the LinkedIn archive, a CSV, or a person.

The dashboard's "changed jobs" card (:func:`netkeeper.services.dashboard.changed_jobs`)
and the filter predicate ``changed_jobs_within_days``
(:mod:`netkeeper.crm.filters`) both select through :func:`job_change`, so the
two always agree on which contacts changed jobs in a window.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ColumnElement, and_

from netkeeper.models import ContactSnapshot, ContactSource, User


def job_change(user: User, *, since: datetime, until: datetime) -> ColumnElement[bool]:
    """``user``'s snapshots that record a job change noticed from ``since`` to ``until``.

    Both ends are inclusive. Use it as the ``WHERE`` clause of a statement over
    :class:`~netkeeper.models.ContactSnapshot`.
    """
    return and_(
        ContactSnapshot.user_id == user.id,
        ContactSnapshot.position_changed.is_(True),
        ContactSnapshot.source == ContactSource.SYNC,
        ContactSnapshot.observed_at >= since,
        ContactSnapshot.observed_at <= until,
    )
