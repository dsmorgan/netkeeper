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
from netkeeper.models import SyncRun, SyncRunKind, SyncRunStatus, User
from netkeeper.models.base import utcnow
from netkeeper.scoping import scoped
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


# --- the last evidence about the session (#282) ---------------------------------------

SESSION_EVIDENCE_KEY: Final = "linkedin.session_evidence"
"""The ``settings_kv`` key holding the last :class:`SessionEvidence` a browser
check recorded (``netkeeper preflight``, ``netkeeper posture --probe``)."""

#: How many of the account's newest ended runs :func:`run_evidence` looks through
#: for one that read LinkedIn. A handful of refused or empty runs in a row is
#: ordinary (a flag, heat, a budget spent); more than this and the evidence is
#: stale anyway.
RUN_EVIDENCE_LOOKBACK: Final = 20

_FLAG_OUTCOME_VALUES: Final = frozenset(outcome.value for outcome in _FLAGGABLE)


@dataclass(frozen=True, slots=True)
class SessionEvidence:
    """What the last look at the LinkedIn session found, when, and who looked.

    ``source`` is ``"preflight"``, ``"posture --probe"``, or ``"run <id>"``.
    Cookie *names* only, never a value: this is what a web page shows when it
    cannot probe the browser itself (a request handler may not, spec 9.9), and it
    must not carry more than preflight read.
    """

    logged_in: bool
    source: str
    observed_at: datetime
    cookie_names: tuple[str, ...] = ()


def record_session_evidence(
    session: Session,
    user: User,
    *,
    logged_in: bool,
    source: str,
    cookie_names: tuple[str, ...] = (),
    now: datetime | None = None,
) -> SessionEvidence:
    """Store what a browser check just found about ``user``'s LinkedIn session.

    Called by ``netkeeper preflight`` and ``netkeeper posture --probe`` after
    they read the cookie jar, so a later page load (which never probes) can say
    what they found. Needs a writer session. Replaces any earlier record: only
    the latest one is kept.
    """
    if not is_writer(session):
        raise RuntimeError(
            "record_session_evidence() needs a writer session; use session_scope(write=True)"
        )
    evidence = SessionEvidence(
        logged_in=logged_in,
        source=source,
        observed_at=utcnow() if now is None else now,
        cookie_names=tuple(cookie_names),
    )
    set_setting(
        session,
        user,
        SESSION_EVIDENCE_KEY,
        {
            "logged_in": evidence.logged_in,
            "source": evidence.source,
            "observed_at": evidence.observed_at.isoformat(),
            "cookie_names": list(evidence.cookie_names),
        },
    )
    return evidence


def recorded_evidence(session: Session, user: User) -> SessionEvidence | None:
    """The last evidence a browser check stored, or ``None``. Read-only.

    Anything unreadable reads as "nothing recorded", the same way
    :func:`session_flag` treats a value it cannot parse: the posture row then
    says unknown, which is the honest answer.
    """
    stored = get_setting(session, user, SESSION_EVIDENCE_KEY)
    if not isinstance(stored, dict):
        return None
    logged_in = stored.get("logged_in")
    source = stored.get("source")
    observed_at_value = stored.get("observed_at")
    names = stored.get("cookie_names", [])
    if not isinstance(logged_in, bool) or not isinstance(source, str):
        return None
    if not isinstance(observed_at_value, str) or not isinstance(names, list):
        return None
    try:
        observed_at = datetime.fromisoformat(observed_at_value)
    except ValueError:
        return None
    return SessionEvidence(
        logged_in=logged_in,
        source=source,
        observed_at=observed_at,
        cookie_names=tuple(name for name in names if isinstance(name, str)),
    )


def run_evidence(session: Session, user: User, account_id: int) -> SessionEvidence | None:
    """The newest run on ``account_id`` that shows the session worked, as evidence. Read-only.

    A run counts when it ended (``completed`` or ``aborted``), flagged nothing,
    was not stopped by a checkpoint or a login wall, and actually read LinkedIn:
    a connections sync that read at least one connection, or an enrichment that
    harvested at least one profile. A logged-out page is classified and flags
    the session, so a run that read something and flagged nothing was logged in
    when it did. Refused runs, empty runs, and failed runs say nothing either way
    and are skipped.
    """
    rows = session.scalars(
        scoped(user, SyncRun)
        .where(
            SyncRun.linkedin_account_id == account_id,
            SyncRun.status.in_((SyncRunStatus.COMPLETED, SyncRunStatus.ABORTED)),
            SyncRun.completed_at.is_not(None),
        )
        .order_by(SyncRun.completed_at.desc(), SyncRun.id.desc())
        .limit(RUN_EVIDENCE_LOOKBACK)
    ).all()
    for run in rows:
        if run.completed_at is not None and _read_linkedin(run):
            return SessionEvidence(
                logged_in=True, source=f"run {run.id}", observed_at=run.completed_at
            )
    return None


def _read_linkedin(run: SyncRun) -> bool:
    counts = run.counts_json
    if not isinstance(counts, dict) or counts.get("session_flagged") is not False:
        return False
    if counts.get("outcome") in _FLAG_OUTCOME_VALUES:
        return False
    read = counts.get("completed") if run.kind is SyncRunKind.ENRICH else counts.get("connections")
    return isinstance(read, int) and not isinstance(read, bool) and read > 0


def last_session_evidence(session: Session, user: User, account_id: int) -> SessionEvidence | None:
    """The newer of :func:`recorded_evidence` and :func:`run_evidence`. Read-only.

    On a tie, a "no session" record wins over a run that read LinkedIn.
    """
    candidates = [
        found
        for found in (recorded_evidence(session, user), run_evidence(session, user, account_id))
        if found is not None
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda found: (found.observed_at, not found.logged_in))
