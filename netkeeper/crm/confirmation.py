"""Count confirmation tokens for bulk actions (spec 10.1; item P1-05).

A bulk action applies to a *filter*, not to a list of ids, so the person never
sees the rows it will touch — they see a count: "archive 214 contacts?". The
token is what makes that count binding. The client asks for a count, gets a
token back, and must send the token with the action. The server re-counts inside
the writer transaction and refuses when the count has moved. Refusing a stale
selection is the whole point: between the dialog and the click a sync may have
run, another tab may have archived rows, or the filter's own relative window
(``last_contacted within 30 days``) may have rolled over.

A token is a signed value, not a row. There is nothing to clean up, nothing to
replicate, and no write on the read path that mints it — a database row would
turn "how many match?" into a writer request, which is the bug #62 is about.

What is signed
--------------
The user, the action, a digest of the selection, the count, and an expiry.

- **User**: a token one user minted is refused for another, so a leaked token is
  not a way into someone else's address book.
- **Action**: a token minted for "how many would I archive?" cannot execute
  ``set_do_not_contact``. The two counts can differ (a filter without
  ``include_archived`` counts different rows once some are archived), and even
  when they agree the person confirmed one sentence, not the other.
- **Selection digest**: the canonical JSON of the filter tree, or of the sorted
  id list, hashed. A token is bound to the selection it was minted for, so one
  filter's token cannot execute a different filter that happens to match the
  same number of contacts.
- **Count**: what the dialog showed. Checked against a fresh count at execution.
- **Expiry**: see :data:`TOKEN_TTL`.

Lifetime
--------
:data:`TOKEN_TTL` is five minutes. It is a confirmation dialog's lifetime, not a
session's: long enough that a person can read the sentence, scroll the preview,
and answer the door, short enough that a token left in a tab overnight is gone
by morning. The count check is the real guard — an expired token and a stale one
both send the client back to :func:`issue` — so the expiry only bounds how long
a signature is worth replaying.

The signing key is :func:`secrets.token_bytes`, made once per process by
:meth:`Signer.generated` and never written down. Restarting the server therefore
invalidates outstanding tokens, which for a local app whose tokens live five
minutes costs a re-count and no data. Nothing here is a credential: a token
authorizes no contact the caller cannot already reach through the API, it only
binds an action to a count the caller was shown.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from netkeeper.models.base import utcnow

log = logging.getLogger(__name__)

TOKEN_TTL: Final[timedelta] = timedelta(minutes=5)
"""How long a count confirmation token stays valid. See "Lifetime" above."""

VERSION: Final[str] = "v1"

InvalidReason = Literal["malformed", "expired", "user", "action", "selection"]
"""Why :meth:`Signer.verify` refused, from the least to the most specific."""


class InvalidToken(Exception):
    """A confirmation token that does not hold up. ``reason`` says which check failed."""

    def __init__(self, reason: InvalidReason, message: str) -> None:
        self.reason: InvalidReason = reason
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Confirmation:
    """What a verified token says: who confirmed which action on which selection, and how many."""

    user_id: int
    action: str
    selection_digest: str
    count: int
    expires_at: datetime


def selection_digest(payload: Any) -> str:
    """A stable digest of a selection's JSON, for binding a token to it.

    Keys are sorted and separators fixed, so the same tree digests the same
    whichever way the client spelled it out. Truncated to 128 bits: this is a
    binding, not a secret, and a collision only lets one selection execute
    another's token at the same count, which the count check already narrows.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class Signer:
    """Signs and verifies confirmation tokens with an HMAC-SHA256 key.

    ``create_app`` makes one per process with :meth:`generated` and keeps it on
    ``app.state``; tests make one with a fixed key and a short ``ttl``.

    ``key`` is kept out of the repr: the signer sits on ``app.state``, which
    turns up in tracebacks and debug dumps, and a signing key is a secret like
    any other (CLAUDE.md).
    """

    key: bytes = field(repr=False)
    ttl: timedelta = TOKEN_TTL

    @classmethod
    def generated(cls, ttl: timedelta = TOKEN_TTL) -> Signer:
        """A signer on a fresh random key. The key lives in memory for this process only."""
        return cls(secrets.token_bytes(32), ttl)

    def issue(
        self,
        *,
        user_id: int,
        action: str,
        digest: str,
        count: int,
        now: datetime | None = None,
    ) -> tuple[str, datetime]:
        """A token for ``count`` rows of ``digest`` under ``action``, and when it expires."""
        expires_at = (utcnow() if now is None else now) + self.ttl
        payload = {
            "v": VERSION,
            "u": user_id,
            "a": action,
            "s": digest,
            "n": count,
            "e": int(expires_at.timestamp()),
        }
        body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
        return f"{body}.{_b64(self._mac(body))}", expires_at

    def verify(
        self,
        token: str,
        *,
        user_id: int,
        action: str,
        digest: str,
        now: datetime | None = None,
    ) -> Confirmation:
        """The :class:`Confirmation` ``token`` carries, or :class:`InvalidToken`.

        Signature first, then expiry, then the user, the action, and the
        selection: a forged token never reaches the comparisons, and the reason
        given back never depends on data the caller did not send.
        """
        confirmation = self._open(token)
        moment = utcnow() if now is None else now
        if confirmation.expires_at <= moment:
            raise InvalidToken("expired", "the confirmation has expired; ask for the count again")
        if confirmation.user_id != user_id:
            log.warning("confirmation token of user %d presented by another", confirmation.user_id)
            raise InvalidToken("user", "the confirmation was not issued to you")
        if confirmation.action != action:
            raise InvalidToken(
                "action",
                f"the confirmation is for {confirmation.action}, not {action}",
            )
        if not hmac.compare_digest(confirmation.selection_digest, digest):
            raise InvalidToken(
                "selection", "the confirmation is for a different selection of contacts"
            )
        return confirmation

    def _open(self, token: str) -> Confirmation:
        body, _, signature = token.partition(".")
        if not body or not signature:
            raise InvalidToken("malformed", "the confirmation token is not readable")
        try:
            given = _unb64(signature)
            if not hmac.compare_digest(self._mac(body), given):
                raise InvalidToken("malformed", "the confirmation token is not readable")
            payload = json.loads(_unb64(body))
        except InvalidToken:
            raise
        except (ValueError, TypeError) as exc:
            raise InvalidToken("malformed", "the confirmation token is not readable") from exc
        if not isinstance(payload, dict) or payload.get("v") != VERSION:
            raise InvalidToken("malformed", "the confirmation token is not readable")
        try:
            return Confirmation(
                user_id=int(payload["u"]),
                action=str(payload["a"]),
                selection_digest=str(payload["s"]),
                count=int(payload["n"]),
                expires_at=datetime.fromtimestamp(int(payload["e"]), tz=UTC),
            )
        except (KeyError, TypeError, ValueError, OverflowError, OSError) as exc:
            raise InvalidToken("malformed", "the confirmation token is not readable") from exc

    def _mac(self, body: str) -> bytes:
        return hmac.new(self.key, body.encode(), hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
