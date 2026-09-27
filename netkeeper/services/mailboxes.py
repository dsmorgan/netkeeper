"""Mailboxes: connect, disconnect, check, and report health (spec 8.5, 11.5; item P3-01).

**Secrets.** Two Keychain entries per user, never a database column
(:mod:`netkeeper.services.keychain`): the OAuth client under
:data:`CLIENT_SECRET_NAME`, and each mailbox's refresh token under its
``keychain_ref`` (:func:`token_name`).

**One live mailbox.** Spec 8.5 has one row in v1. Authorizing the address a
user already has re-authorizes it; authorizing a different address while one
is ``ok`` or ``reauth_required`` is refused (:class:`OtherMailboxConnected`)
until that one is disconnected. A disconnected mailbox stays, ``disabled``, for
the campaigns that name it, and authorizing its address again brings it back.

**Health.** :func:`check_mailbox` refreshes the token. Google's ``invalid_grant``,
``invalid_client`` or ``unauthorized_client``
(:data:`~netkeeper.campaigns.gmail_oauth.REAUTH_CODES`), or a token or client
missing from the Keychain, sets the mailbox to ``reauth_required``, which is
what pauses email steps: the guards read :func:`mailbox_health` (P3-06 wires it
into ``ChannelState``). Anything else changes nothing: a network failure, a
5xx, and any other refusal (a proxy's ``407``, an HTML ``403``,
``invalid_request``, an answer with no access token), which says nothing
certain about the grant. :func:`poll_mailboxes`
checks every ``ok`` mailbox of every local user, and ``netkeeper serve`` runs it
every ``[campaigns] reply_poll_minutes`` (:class:`MailboxMonitor`), so a
revoked token shows as the banner within one poll.

**Sessions.** No HTTP call is made while a session is open: a check reads what
it needs, closes the session, calls Google, then opens a writer to record the
result, and records it only when the mailbox is still the one it checked: still
``ok``, and not authorized again since (``generation``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.campaigns import gmail_oauth
from netkeeper.campaigns.gmail import GmailClient
from netkeeper.db import session_scope
from netkeeper.models import Mailbox, MailboxStatus, User, UserKind
from netkeeper.models.base import utcnow
from netkeeper.scoping import get_scoped, scoped
from netkeeper.services import keychain
from netkeeper.services.events import Event, EventBus

log = logging.getLogger(__name__)

#: The Keychain name of a user's OAuth client (``<user_id>/gmail/oauth_client``).
CLIENT_SECRET_NAME: Final = "gmail/oauth_client"  # noqa: S105 - an entry name

#: Spec 11.4: a mailbox never sends to more than this many recipients a day,
#: whatever the config says. Gmail's consumer limit is 500.
MAILBOX_HARD_MAX_PER_DAY: Final = 400

#: The event the UI's banner listens for.
STATUS_EVENT: Final = "mailbox.status"

REASON_TOKEN_MISSING: Final = "token_missing"  # noqa: S105 - a status code
REASON_CLIENT_MISSING: Final = "client_missing"
REASON_DISCONNECTED: Final = "disconnected"


class MailboxNotFound(LookupError):
    """No mailbox with that id belongs to the user."""


class ClientNotConfigured(RuntimeError):
    """The user has not given netkeeper an OAuth client yet."""


class OtherMailboxConnected(RuntimeError):
    """The user authorized a different address while another mailbox is still live."""

    def __init__(self, connected: str, authorized: str) -> None:
        super().__init__(
            f"{connected} is already connected; disconnect it before connecting {authorized}"
        )
        self.connected = connected
        self.authorized = authorized


def token_name(mailbox_id: int) -> str:
    """The Keychain name of a mailbox's refresh token."""
    return f"gmail/mailbox/{mailbox_id}"


# --- the OAuth client --------------------------------------------------------------


def save_client(user: User, client: gmail_oauth.OAuthClient) -> None:
    """Keep ``client`` in the Keychain for ``user``, replacing any before it."""
    keychain.set_secret(user.id, CLIENT_SECRET_NAME, client.to_json())


def load_client(user_id: int) -> gmail_oauth.OAuthClient | None:
    """The user's OAuth client, or None when none is stored (or it is unreadable)."""
    raw = keychain.get_secret(user_id, CLIENT_SECRET_NAME)
    if raw is None:
        return None
    try:
        return gmail_oauth.OAuthClient.from_json(raw)
    except (ValueError, KeyError, TypeError):
        log.warning("the stored OAuth client for user %d is unreadable; set it again", user_id)
        return None


def require_client(user_id: int) -> gmail_oauth.OAuthClient:
    client = load_client(user_id)
    if client is None:
        raise ClientNotConfigured(
            "no Gmail OAuth client is set; add the client ID and secret first (docs/gmail-setup.md)"
        )
    return client


# --- authorizations in flight -----------------------------------------------------

#: How long a person has between starting an authorization and Google's redirect.
PENDING_TTL: Final = timedelta(minutes=10)

#: At most this many authorizations wait at once; starting another drops the oldest.
PENDING_MAX: Final = 8


class PendingAuthorizations:
    """Authorizations started from the Settings page, waiting for Google's redirect.

    In memory, per process, never written down: one that outlives a restart is
    simply started again. Keyed by ``state``; :meth:`take` hands one out at most
    once, and only to the user who started it, within :data:`PENDING_TTL`.
    Thread-safe, since request handlers run on a thread pool.
    """

    def __init__(self, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[int, gmail_oauth.Authorization, datetime]] = {}

    def add(self, user_id: int, authorization: gmail_oauth.Authorization) -> None:
        with self._lock:
            self._expire()
            while len(self._pending) >= PENDING_MAX:
                self._pending.pop(next(iter(self._pending)))
            self._pending[authorization.state] = (
                user_id,
                authorization,
                self._clock() + PENDING_TTL,
            )

    def take(self, state: str, user_id: int) -> gmail_oauth.Authorization | None:
        """The authorization ``state`` names, removed; None when unknown, expired, or not theirs."""
        with self._lock:
            self._expire()
            found = self._pending.pop(state, None)
        if found is None or found[0] != user_id:
            return None
        return found[1]

    def _expire(self) -> None:
        now = self._clock()
        for state in [key for key, (_, _, until) in self._pending.items() if until <= now]:
            del self._pending[state]


# --- rows --------------------------------------------------------------------------


def list_mailboxes(session: Session, user: User) -> list[Mailbox]:
    """The user's mailboxes, oldest first."""
    return list(session.scalars(scoped(user, Mailbox).order_by(Mailbox.id)))


def get_mailbox(session: Session, user: User, mailbox_id: int) -> Mailbox:
    mailbox = get_scoped(session, user, Mailbox, mailbox_id)
    if mailbox is None:
        raise MailboxNotFound(f"no mailbox {mailbox_id}")
    return mailbox


def live_mailbox(session: Session, user: User) -> Mailbox | None:
    """The user's one mailbox that is not disconnected, if any."""
    return session.scalars(
        scoped(user, Mailbox).where(Mailbox.status != MailboxStatus.DISABLED).order_by(Mailbox.id)
    ).first()


def connect(
    session: Session,
    user: User,
    email: str,
    refresh_token: str,
    *,
    daily_cap: int,
    now: datetime | None = None,
) -> Mailbox:
    """Record a successful authorization of ``email``: the row ``ok``, its token stored.

    Needs a writer session. The token is written after the row is flushed; if
    the Keychain refuses, the exception rolls the row back with the transaction.
    """
    email = email.strip().lower()
    live = live_mailbox(session, user)
    if live is not None and live.email != email:
        raise OtherMailboxConnected(live.email, email)
    mailbox = session.scalars(scoped(user, Mailbox).where(Mailbox.email == email)).first()
    if mailbox is None:
        mailbox = Mailbox(
            user_id=user.id,
            email=email,
            keychain_ref="",
            daily_cap=min(max(daily_cap, 0), MAILBOX_HARD_MAX_PER_DAY),
        )
        session.add(mailbox)
        session.flush()
        mailbox.keychain_ref = token_name(mailbox.id)
    mailbox.status = MailboxStatus.OK
    mailbox.status_reason = None
    mailbox.checked_at = now or utcnow()
    mailbox.generation += 1  # a check of the grant before this one no longer applies
    session.flush()
    keychain.set_secret(user.id, mailbox.keychain_ref, refresh_token)
    log.info("mailbox %d connected", mailbox.id)
    return mailbox


def disconnect(session: Session, user: User, mailbox: Mailbox) -> Mailbox:
    """Disable ``mailbox`` and forget its token. Its row stays for the campaigns naming it."""
    keychain.delete_secret(user.id, mailbox.keychain_ref)
    mailbox.status = MailboxStatus.DISABLED
    mailbox.status_reason = REASON_DISCONNECTED
    session.flush()
    log.info("mailbox %d disconnected", mailbox.id)
    return mailbox


def status_event(mailbox_id: int, user_id: int, status: MailboxStatus, reason: str | None) -> Event:
    """The ``mailbox.status`` event the banner listens for."""
    return Event(
        type=STATUS_EVENT,
        data={"mailbox_id": mailbox_id, "status": status.value, "reason": reason},
        user_id=user_id,
    )


# --- health ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MailboxHealth:
    """What the guards need to know about a mailbox (spec 11.9; P3-06 reads it).

    ``healthy`` is ``status == ok``. ``reauth_required`` is the one unhealthy
    state a person fixes by authorizing again; a ``disabled`` mailbox is
    neither healthy nor waiting for re-authorization.
    """

    mailbox_id: int
    healthy: bool
    reauth_required: bool
    daily_cap: int


def mailbox_health(session: Session, user: User, mailbox_id: int) -> MailboxHealth | None:
    """The health of the user's mailbox ``mailbox_id``, read fresh, or None if it is not theirs.

    Reads the row from the database on every call, never from the session's
    identity map, so a status the poll just changed is seen.
    """
    mailbox = session.scalars(
        scoped(user, Mailbox)
        .where(Mailbox.id == mailbox_id)
        .execution_options(populate_existing=True)
    ).first()
    if mailbox is None:
        return None
    return MailboxHealth(
        mailbox_id=mailbox.id,
        healthy=mailbox.status is MailboxStatus.OK,
        reauth_required=mailbox.status is MailboxStatus.REAUTH_REQUIRED,
        daily_cap=mailbox.daily_cap,
    )


@dataclass(frozen=True, slots=True)
class CheckResult:
    """What one check found. ``changed`` is whether it moved the status."""

    mailbox_id: int
    user_id: int
    status: MailboxStatus
    reason: str | None
    changed: bool


def check_mailbox(
    factory: sessionmaker[Session],
    user_id: int,
    mailbox_id: int,
    *,
    endpoints: gmail_oauth.GoogleEndpoints | None = None,
    clock: Callable[[], datetime] = utcnow,
) -> CheckResult | None:
    """Refresh the mailbox's token; mark it ``reauth_required`` when the grant is dead.

    Dead means a code in :data:`~netkeeper.campaigns.gmail_oauth.REAUTH_CODES`
    or a secret missing from the Keychain; any other failure leaves it as it is.

    Only an ``ok`` mailbox is checked; any other is returned as it is. None when
    the user or mailbox is gone. Blocking: call it off the event loop.
    """
    with session_scope(factory) as session:
        user = session.get(User, user_id)
        if user is None:
            return None
        mailbox = get_scoped(session, user, Mailbox, mailbox_id)
        if mailbox is None:
            return None
        if mailbox.status is not MailboxStatus.OK:
            return CheckResult(mailbox.id, user_id, mailbox.status, mailbox.status_reason, False)
        ref = mailbox.keychain_ref
        generation = mailbox.generation

    reason: str | None = None
    try:
        client = load_client(user_id)
        token = keychain.get_secret(user_id, ref)
        if client is None:
            reason = REASON_CLIENT_MISSING
        elif token is None:
            reason = REASON_TOKEN_MISSING
        else:
            gmail_oauth.refresh_access_token(client, token, endpoints=endpoints)
    except gmail_oauth.OAuthUnavailable as exc:
        log.info("mailbox %d not checked: %s", mailbox_id, exc)
        return CheckResult(mailbox_id, user_id, MailboxStatus.OK, None, False)
    except gmail_oauth.OAuthError as exc:
        if not gmail_oauth.needs_reauthorization(exc):
            # Refused, but not in a way that says the grant is dead: try again next poll.
            log.warning("mailbox %d not checked: Google refused (%s)", mailbox_id, exc.code)
            return CheckResult(mailbox_id, user_id, MailboxStatus.OK, None, False)
        reason = exc.code
    except keychain.KeychainUnavailable as exc:
        log.warning("mailbox %d not checked: %s", mailbox_id, exc)
        return CheckResult(mailbox_id, user_id, MailboxStatus.OK, None, False)

    return _record(factory, user_id, mailbox_id, ref, reason, clock)


def mark_reauth_required(
    factory: sessionmaker[Session], user_id: int, mailbox_id: int, ref: str, reason: str
) -> CheckResult | None:
    """Set the mailbox ``reauth_required`` for ``reason``, as a failed check would.

    ``ref`` is the ``keychain_ref`` the caller's token came from: a mailbox a
    person disconnected or re-authorized since then is left alone. None when
    the user or mailbox is gone. Opens a writer, so call it with no session open.
    """
    return _record(factory, user_id, mailbox_id, ref, reason, utcnow)


def _record(
    factory: sessionmaker[Session],
    user_id: int,
    mailbox_id: int,
    ref: str,
    reason: str | None,
    clock: Callable[[], datetime],
) -> CheckResult | None:
    """Write what a check found: ``checked_at`` when ``reason`` is None, else the pause."""
    with session_scope(factory, write=True) as session:
        user = session.get(User, user_id)
        mailbox = None if user is None else get_scoped(session, user, Mailbox, mailbox_id)
        if mailbox is None:
            return None
        if mailbox.status is not MailboxStatus.OK or mailbox.generation != generation:
            # A person disconnected or re-authorized it while we were asking Google.
            return CheckResult(mailbox.id, user_id, mailbox.status, mailbox.status_reason, False)
        if reason is None:
            mailbox.checked_at = clock()
            return CheckResult(mailbox.id, user_id, MailboxStatus.OK, None, False)
        mailbox.status = MailboxStatus.REAUTH_REQUIRED
        mailbox.status_reason = reason
        log.warning("mailbox %d needs re-authorization (%s); email steps pause", mailbox.id, reason)
        return CheckResult(mailbox.id, user_id, mailbox.status, reason, True)


class MailboxNotReady(RuntimeError):
    """The mailbox cannot send: it is not ``ok``, or its secrets are gone. ``code`` says why."""

    def __init__(self, mailbox_id: int, code: str) -> None:
        super().__init__(f"mailbox {mailbox_id} is not ready to use ({code})")
        self.mailbox_id = mailbox_id
        self.code = code


def open_gmail(
    factory: sessionmaker[Session],
    user_id: int,
    mailbox_id: int,
    *,
    endpoints: gmail_oauth.GoogleEndpoints | None = None,
    on_status_change: Callable[[CheckResult], None] | None = None,
    http: Any = None,
) -> GmailClient:
    """A :class:`GmailClient` for the user's ``ok`` mailbox (item P3-02).

    The client renews its access token with P3-01's
    :func:`~netkeeper.campaigns.gmail_oauth.refresh_access_token`, from the
    OAuth client and refresh token in the Keychain. When Gmail or Google
    refuses the grant, the mailbox goes ``reauth_required`` by the same path as
    :func:`check_mailbox`, and ``on_status_change`` hears of it (it runs on the
    calling thread: an async caller hands :func:`status_event` to the loop).

    :class:`MailboxNotFound` when it is not the user's; :class:`MailboxNotReady`
    when it is not ``ok``, a secret is missing (which marks the mailbox, as a
    check would), or the Keychain is locked (which does not). Reads a session
    and closes it before returning.
    """
    with session_scope(factory) as session:
        user = session.get(User, user_id)
        mailbox = None if user is None else get_scoped(session, user, Mailbox, mailbox_id)
        if mailbox is None:
            raise MailboxNotFound(f"no mailbox {mailbox_id}")
        if mailbox.status is not MailboxStatus.OK:
            raise MailboxNotReady(mailbox_id, mailbox.status.value)
        ref = mailbox.keychain_ref

    def mark(reason: str, used: str | None = None) -> None:
        if used is not None:
            try:
                current = keychain.get_secret(user_id, ref)
            except keychain.KeychainUnavailable:
                current = used  # cannot tell; pausing is the safe side
            if current != used:
                # Re-authorized since this client read its token: the new grant is fine.
                log.info("mailbox %d was re-authorized meanwhile; not marked", mailbox_id)
                return
        result = mark_reauth_required(factory, user_id, mailbox_id, ref, reason)
        if result is not None and result.changed and on_status_change is not None:
            on_status_change(result)

    try:
        client = load_client(user_id)
        token = keychain.get_secret(user_id, ref)
    except keychain.KeychainUnavailable as exc:
        # Nothing is known about the grant, so nothing is marked (as in a check).
        raise MailboxNotReady(mailbox_id, "keychain_unavailable") from exc
    if client is None:
        mark(REASON_CLIENT_MISSING)
        raise MailboxNotReady(mailbox_id, REASON_CLIENT_MISSING)
    if token is None:
        mark(REASON_TOKEN_MISSING)
        raise MailboxNotReady(mailbox_id, REASON_TOKEN_MISSING)

    def refresh() -> str:
        return gmail_oauth.refresh_access_token(client, token, endpoints=endpoints)

    return GmailClient(
        refresh,
        mailbox_id=mailbox_id,
        on_auth_failure=lambda error: mark(error.code, token),
        http=http,
    )


def poll_mailboxes(
    factory: sessionmaker[Session],
    *,
    endpoints: gmail_oauth.GoogleEndpoints | None = None,
    clock: Callable[[], datetime] = utcnow,
) -> list[CheckResult]:
    """Check every ``ok`` mailbox of every local user. Blocking: run it off the event loop."""
    with session_scope(factory) as session:
        users = session.scalars(
            select(User).where(User.kind == UserKind.LOCAL).order_by(User.id)
        ).all()
        due = [
            (user.id, mailbox.id)
            for user in users
            for mailbox in session.scalars(
                scoped(user, Mailbox).where(Mailbox.status == MailboxStatus.OK).order_by(Mailbox.id)
            )
        ]
    results: list[CheckResult] = []
    for user_id, mailbox_id in due:
        result = check_mailbox(factory, user_id, mailbox_id, endpoints=endpoints, clock=clock)
        if result is not None:
            results.append(result)
    return results


class MailboxMonitor:
    """The background poll ``netkeeper serve`` runs: :func:`poll_mailboxes` every interval.

    Each status change goes out on the bus as ``mailbox.status``, which the
    banner listens for. The first poll runs one interval after start.
    """

    def __init__(
        self,
        factory: sessionmaker[Session],
        bus: EventBus,
        *,
        interval_s: float,
        endpoints: gmail_oauth.GoogleEndpoints | None = None,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("the mailbox poll interval must be positive")
        self._factory = factory
        self._bus = bus
        self._interval_s = interval_s
        self._endpoints = endpoints
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="mailbox-poll")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def poll_once(self) -> list[CheckResult]:
        """One poll, off the loop, then the events for whatever changed."""
        results = await asyncio.to_thread(poll_mailboxes, self._factory, endpoints=self._endpoints)
        for result in results:
            if result.changed:
                self._bus.publish(
                    status_event(result.mailbox_id, result.user_id, result.status, result.reason)
                )
        return results

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_s)
            try:
                await self.poll_once()
            except Exception:
                log.exception("mailbox poll failed; trying again next interval")
