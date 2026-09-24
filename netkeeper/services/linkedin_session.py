"""The LinkedIn session flag: the record set when a run can no longer trust the
browser's session (spec 8.4, 9.7, 9.10).

This lives here and not under ``netkeeper/linkedin/`` because writing it means
opening a session, and the extractor boundary (ADR 0005, spec 9.10) forbids
that on that side of the line: "Nothing under ``linkedin/`` imports ORM models
or opens a session" (CLAUDE.md). :func:`netkeeper.linkedin.classify.classify`
stays pure and returns an ``Outcome``; :func:`flag_session` is the other half,
on the core side, that a future job (P2-06 onward) calls when that outcome is
one of the two spec 9.7 says should stop the run and raise a banner.
:func:`clear_session_flag` is the way back, for once the session is healthy
again.

Spec 8.4 describes the flag as a column on ``linkedin_account``
(``session_status``, ``session_flag_at``), but that table does not exist yet --
no phase-2 item before this one creates it, and v1 has exactly one LinkedIn
account per user regardless. Keeping it in ``settings_kv``, scoped by user the
same way every other runtime flag in this codebase is (``tags.defaults_seeded``,
``lists.validated_seeded``), needs no migration and is what the issue asks
for. Moving it onto a real ``linkedin_account`` column is a follow-up once that
table lands, not a decision this item is positioned to make well.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from netkeeper.db import is_writer
from netkeeper.linkedin.classify import Outcome
from netkeeper.models import User
from netkeeper.models.base import utcnow
from netkeeper.services.settings_kv import delete_setting, get_setting, set_setting

SESSION_FLAG_KEY: Final = "linkedin.session_flag"
"""The ``settings_kv`` key holding the current :class:`SessionFlag`, or nothing
when the session is not flagged."""

_FLAGGABLE: Final[frozenset[Outcome]] = frozenset({Outcome.CHECKPOINT, Outcome.LOGGED_OUT})
"""The only two outcomes spec 9.7's Action column says set the session flag."""


@dataclass(frozen=True, slots=True)
class SessionFlag:
    """What :data:`SESSION_FLAG_KEY` holds: why the session was flagged, from
    where, and when.

    ``url`` is the *path* of the response that triggered the flag, not the
    full url -- see :func:`flag_session`.
    """

    outcome: Outcome
    url: str
    flagged_at: datetime


def flag_session(session: Session, user: User, outcome: Outcome, *, url: str) -> SessionFlag:
    """Record that ``user``'s LinkedIn session needs attention.

    Only :attr:`Outcome.CHECKPOINT` and :attr:`Outcome.LOGGED_OUT` reach here
    -- every other outcome raises ``ValueError`` rather than silently writing
    a flag spec 9.7 never asked for. This function writes once and returns;
    it does not loop, does not attempt the request again, and has no retry
    path of any kind. "Never retry" for a checkpoint (spec 9.7) is decided in
    :func:`netkeeper.linkedin.classify.is_retryable`, which a job checks
    *before* ever calling this -- this function stops a run that already
    decided not to retry, it does not itself decide that.

    ``url`` is stored with its query string (and any fragment) dropped, kept
    only as its path. A checkpoint or login-wall url routinely carries a
    ``ctx`` or ``sessionRedirect`` token, and that token adds nothing a
    banner or a support conversation needs while adding one more thing
    ``settings_kv`` backups and exports would otherwise carry along.
    """
    if outcome not in _FLAGGABLE:
        raise ValueError(
            f"flag_session() called with {outcome!r}; only {sorted(_FLAGGABLE)} "
            "set the session flag (spec 9.7)"
        )
    if not is_writer(session):
        raise RuntimeError("flag_session() needs a writer session; use session_scope(write=True)")
    flag = SessionFlag(outcome=outcome, url=urlsplit(url).path, flagged_at=utcnow())
    set_setting(
        session,
        user,
        SESSION_FLAG_KEY,
        {
            "outcome": flag.outcome.value,
            "url": flag.url,
            "flagged_at": flag.flagged_at.isoformat(),
        },
    )
    return flag


def clear_session_flag(session: Session, user: User) -> bool:
    """Clear ``user``'s session flag, if one is set. True when a flag was cleared.

    The other half of :func:`flag_session`: once set, nothing else in this
    module removes the flag, so without this a checkpoint or logged-out
    banner would persist even after the session is healthy again.
    ``netkeeper preflight`` calls this now (P2-01, #154), but only after a
    :attr:`~netkeeper.linkedin.classify.Outcome.LOGGED_OUT` flag -- never a
    :attr:`~netkeeper.linkedin.classify.Outcome.CHECKPOINT` one, since a live
    session cookie is not proof a checkpoint is resolved (#168 review, F1). A
    ``Checkpoint`` flag is cleared only by hand, with ``netkeeper linkedin
    clear-flag``; wiring a fresh ``Ok`` classification to also clear one is
    separate follow-up work, deliberately deferred.
    """
    if not is_writer(session):
        raise RuntimeError(
            "clear_session_flag() needs a writer session; use session_scope(write=True)"
        )
    return delete_setting(session, user, SESSION_FLAG_KEY)


def session_flag(session: Session, user: User) -> SessionFlag | None:
    """The current flag for ``user``, or ``None`` when the session is not flagged.

    A value this cannot make sense of -- the key absent, not an object, or an
    object missing a field -- reads as "not flagged" rather than raising, the
    same way an unreadable ``tags.defaults_seeded`` reads as "nothing seeded"
    (:mod:`netkeeper.crm.tags`): a flag is advisory (a UI banner), and a
    session that genuinely is still broken raises its flag again the next
    time a job hits it.
    """
    stored = get_setting(session, user, SESSION_FLAG_KEY)
    if not isinstance(stored, dict):
        return None
    outcome_value = stored.get("outcome")
    url = stored.get("url")
    flagged_at_value = stored.get("flagged_at")
    if not isinstance(outcome_value, str) or not isinstance(url, str):
        return None
    if not isinstance(flagged_at_value, str):
        return None
    try:
        outcome = Outcome(outcome_value)
        flagged_at = datetime.fromisoformat(flagged_at_value)
    except ValueError:
        return None
    return SessionFlag(outcome=outcome, url=url, flagged_at=flagged_at)
