"""``/mailboxes``: the Gmail account campaigns send from, and its OAuth flow (spec 11.5; P3-01).

The flow, from the Settings page:

1. ``PUT /mailboxes/oauth/client`` stores the Desktop-app client (Keychain).
2. ``POST /mailboxes/oauth/start`` answers the Google URL to open. Its redirect
   is this server's ``/mailboxes/oauth/callback`` on the host the page used, a
   loopback address (the CSRF middleware refuses any other ``Host``).
3. Google redirects the browser to the callback, the one route a browser
   navigation reaches without ``X-Netkeeper-Client`` (spec 14.2). It checks
   ``state`` against an authorization this user started in this process,
   exchanges the code, asks Gmail whose account it is, stores the token, and
   redirects to ``/settings?gmail=connected`` (or ``gmail=error&reason=<code>``).

No handler holds a session while it talks to Google. The access log never shows
the callback's query string (``netkeeper.logging_setup``), which carries the code.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any
from urllib.parse import urlencode

from anyio import from_thread
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.campaigns import gmail_oauth
from netkeeper.config import Settings
from netkeeper.db import session_scope
from netkeeper.models import Mailbox, MailboxStatus
from netkeeper.services import keychain
from netkeeper.services import mailboxes as service
from netkeeper.services.events import Event, EventBus
from netkeeper.web.deps import AuthProvider, CurrentUser, SessionDep, read_only
from netkeeper.web.schemas import (
    MailboxOut,
    MailboxStatusOut,
    OAuthClientIn,
    OAuthStartIn,
    OAuthStartOut,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["mailboxes"])

CALLBACK_ROUTE = "mailbox_oauth_callback"
SETTINGS_PATH = "/settings"

Responses = dict[int | str, dict[str, Any]]
NOT_FOUND: Responses = {404: {"description": "No such mailbox for this user"}}
NO_CLIENT: Responses = {409: {"description": "No OAuth client is stored yet"}}
INVALID: Responses = {422: {"description": "Not a usable Desktop-app OAuth client"}}
NO_KEYCHAIN: Responses = {503: {"description": "The Keychain refused"}}


@contextmanager
def translate_errors() -> Iterator[None]:
    try:
        yield
    except service.MailboxNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.ClientNotConfigured as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except gmail_oauth.ClientConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except keychain.KeychainUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _endpoints(request: Request) -> gmail_oauth.GoogleEndpoints | None:
    endpoints: gmail_oauth.GoogleEndpoints | None = request.app.state.gmail_endpoints
    return endpoints


def _pending(request: Request) -> service.PendingAuthorizations:
    pending: service.PendingAuthorizations = request.app.state.pending_oauth
    return pending


def _publish(request: Request, event: Event) -> None:
    """Publish from a handler running on the thread pool (the bus lives on the loop)."""
    bus: EventBus = request.app.state.bus
    from_thread.run_sync(bus.publish, event)


def _status_event(mailbox: Mailbox) -> Event:
    return service.status_event(mailbox.id, mailbox.user_id, mailbox.status, mailbox.status_reason)


@router.get("/mailboxes", response_model=list[MailboxOut])
def list_mailboxes(session: SessionDep, user: CurrentUser) -> list[Mailbox]:
    """Every mailbox, disconnected ones included, oldest first."""
    return service.list_mailboxes(session, user)


@router.get("/mailboxes/status", responses=NO_KEYCHAIN)
def mailbox_status(session: SessionDep, user: CurrentUser) -> MailboxStatusOut:
    """Whether a client is set, every mailbox, and whether any needs re-authorizing."""
    with translate_errors():
        client = service.load_client(user.id)
    rows = service.list_mailboxes(session, user)
    return MailboxStatusOut(
        client_configured=client is not None,
        client_id=None if client is None else client.client_id,
        mailboxes=[MailboxOut.model_validate(row) for row in rows],
        reauth_required=any(row.status is MailboxStatus.REAUTH_REQUIRED for row in rows),
    )


@router.put("/mailboxes/oauth/client", responses={**INVALID, **NO_KEYCHAIN})
@read_only  # writes the Keychain, never the database
def set_oauth_client(body: OAuthClientIn, user: CurrentUser) -> MailboxStatusOut:
    """Store the OAuth client's ID and secret in the Keychain."""
    with translate_errors():
        service.save_client(user, gmail_oauth.validate_client(body.client_id, body.client_secret))
    return MailboxStatusOut(
        client_configured=True,
        client_id=body.client_id.strip(),
        mailboxes=[],
        reauth_required=False,
    )


@router.post("/mailboxes/oauth/start", responses={**NOT_FOUND, **NO_CLIENT, **NO_KEYCHAIN})
@read_only  # the authorization waits in memory, never in the database
def start_oauth(
    body: OAuthStartIn, request: Request, session: SessionDep, user: CurrentUser
) -> OAuthStartOut:
    """The Google URL to open. Valid for ten minutes, once."""
    with translate_errors():
        hint = None
        if body.mailbox_id is not None:
            hint = service.get_mailbox(session, user, body.mailbox_id).email
        client = service.require_client(user.id)
        authorization = gmail_oauth.begin(
            client,
            str(request.url_for(CALLBACK_ROUTE)),
            login_hint=hint,
            endpoints=_endpoints(request),
        )
    _pending(request).add(user.id, authorization)
    return OAuthStartOut(authorization_url=authorization.url)


@router.get(
    "/mailboxes/oauth/callback",
    name=CALLBACK_ROUTE,
    status_code=303,
    response_class=RedirectResponse,
    responses={303: {"description": "Back to the Settings page, with the outcome"}},
)
def oauth_callback(
    request: Request,
    state: Annotated[str, Query(max_length=200)] = "",
    code: Annotated[str, Query(max_length=2000)] = "",
    error: Annotated[str, Query(max_length=200)] = "",
) -> RedirectResponse:
    """Where Google sends the browser back. Always redirects to the Settings page."""
    factory: sessionmaker[Session] = request.app.state.session_factory
    auth: AuthProvider = request.app.state.auth
    with session_scope(factory) as session:
        user_id = auth.current_user(request, session).id
    authorization = _pending(request).take(state, user_id) if state else None
    if authorization is None:
        return _back("error", "state_mismatch")
    if error:
        # Google's own code, e.g. access_denied when the person pressed Cancel.
        return _back("error", error if error.replace("_", "").isalnum() else "denied")
    if not code:
        return _back("error", "no_code")
    try:
        client = service.require_client(user_id)
        grant = gmail_oauth.exchange_code(
            client, authorization, code, endpoints=_endpoints(request)
        )
        email = gmail_oauth.fetch_email(grant.access_token, endpoints=_endpoints(request))
    except service.ClientNotConfigured:
        return _back("error", "client_missing")
    except gmail_oauth.OAuthError as exc:
        log.info("gmail authorization failed: %s", exc.code)
        return _back("error", exc.code)
    except keychain.KeychainUnavailable as exc:
        log.warning("gmail authorization failed: %s", exc)
        return _back("error", "keychain")
    settings: Settings = request.app.state.settings
    try:
        with session_scope(factory, write=True) as session:
            user = auth.current_user(request, session)
            mailbox = service.connect(
                session,
                user,
                email,
                grant.refresh_token,
                daily_cap=settings.campaigns.mailbox_daily_cap,
            )
            event = _status_event(mailbox)
    except service.OtherMailboxConnected:
        return _back("error", "other_mailbox_connected")
    except keychain.KeychainUnavailable as exc:
        log.warning("gmail authorization failed: %s", exc)
        return _back("error", "keychain")
    _publish(request, event)
    return _back("connected", None)


@router.post("/mailboxes/{mailbox_id}/check", responses={**NOT_FOUND, **NO_KEYCHAIN})
@read_only  # the check opens its own writer after Google answers, never during
def check_mailbox(mailbox_id: int, request: Request, user: CurrentUser) -> MailboxOut:
    """Refresh the token now, as the background poll does; answers the mailbox after."""
    factory: sessionmaker[Session] = request.app.state.session_factory
    result = service.check_mailbox(factory, user.id, mailbox_id, endpoints=_endpoints(request))
    if result is None:
        raise HTTPException(status_code=404, detail=f"no mailbox {mailbox_id}")
    if result.changed:
        _publish(
            request,
            service.status_event(result.mailbox_id, result.user_id, result.status, result.reason),
        )
    with session_scope(factory) as session, translate_errors():
        return MailboxOut.model_validate(service.get_mailbox(session, user, mailbox_id))


@router.post("/mailboxes/{mailbox_id}/disconnect", responses={**NOT_FOUND, **NO_KEYCHAIN})
def disconnect_mailbox(
    mailbox_id: int, request: Request, session: SessionDep, user: CurrentUser
) -> MailboxOut:
    """Forget the token and disable the mailbox. Its row stays for the campaigns naming it."""
    with translate_errors():
        mailbox = service.disconnect(session, user, service.get_mailbox(session, user, mailbox_id))
    out = MailboxOut.model_validate(mailbox)
    event = _status_event(mailbox)
    session.commit()  # before the event, so a listener that reads again sees it
    _publish(request, event)
    return out


def _back(outcome: str, reason: str | None) -> RedirectResponse:
    query = {"gmail": outcome} if reason is None else {"gmail": outcome, "reason": reason}
    return RedirectResponse(f"{SETTINGS_PATH}?{urlencode(query)}", status_code=303)
