"""Count confirmation tokens (``netkeeper.crm.confirmation``), item P1-05."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from netkeeper.crm.confirmation import (
    TOKEN_TTL,
    InvalidToken,
    Signer,
    selection_digest,
)

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
DIGEST = selection_digest({"filter": {"where": None, "include_archived": False}, "ids": None})
OTHER = selection_digest({"filter": None, "ids": [1, 2, 3]})


@pytest.fixture
def signer() -> Signer:
    return Signer(key=b"a fixed key for the tests" * 2)


def _issue(signer: Signer, **overrides: object) -> str:
    fields: dict[str, object] = {
        "user_id": 1,
        "action": "archive",
        "digest": DIGEST,
        "count": 214,
        "now": NOW,
    }
    fields.update(overrides)
    token, _ = signer.issue(**fields)  # type: ignore[arg-type]
    return token


# --- the round trip ---------------------------------------------------------


def test_a_fresh_token_verifies_and_carries_the_count(signer: Signer) -> None:
    token, expires_at = signer.issue(user_id=1, action="archive", digest=DIGEST, count=214, now=NOW)
    assert expires_at == NOW + TOKEN_TTL
    confirmation = signer.verify(token, user_id=1, action="archive", digest=DIGEST, now=NOW)
    assert confirmation.count == 214
    assert confirmation.user_id == 1
    assert confirmation.action == "archive"
    assert confirmation.expires_at == NOW + TOKEN_TTL


def test_the_lifetime_is_five_minutes() -> None:
    assert timedelta(minutes=5) == TOKEN_TTL


def test_a_token_carries_no_readable_secret(signer: Signer) -> None:
    token = _issue(signer)
    assert signer.key.decode(errors="ignore") not in token
    assert "." in token and token.count(".") == 1


# --- what it refuses --------------------------------------------------------


def test_an_expired_token_is_refused(signer: Signer) -> None:
    token = _issue(signer)
    later = NOW + TOKEN_TTL + timedelta(seconds=1)
    with pytest.raises(InvalidToken) as caught:
        signer.verify(token, user_id=1, action="archive", digest=DIGEST, now=later)
    assert caught.value.reason == "expired"
    assert "ask for the count again" in str(caught.value)


def test_a_token_expires_exactly_at_its_expiry(signer: Signer) -> None:
    token, expires_at = signer.issue(user_id=1, action="archive", digest=DIGEST, count=1, now=NOW)
    with pytest.raises(InvalidToken):
        signer.verify(token, user_id=1, action="archive", digest=DIGEST, now=expires_at)


def test_another_users_token_is_refused(signer: Signer) -> None:
    token = _issue(signer, user_id=1)
    with pytest.raises(InvalidToken) as caught:
        signer.verify(token, user_id=2, action="archive", digest=DIGEST, now=NOW)
    assert caught.value.reason == "user"


def test_a_token_for_another_action_is_refused(signer: Signer) -> None:
    token = _issue(signer, action="archive")
    with pytest.raises(InvalidToken) as caught:
        signer.verify(token, user_id=1, action="set_do_not_contact", digest=DIGEST, now=NOW)
    assert caught.value.reason == "action"
    assert "archive" in str(caught.value)


def test_a_token_for_another_selection_is_refused(signer: Signer) -> None:
    token = _issue(signer, digest=DIGEST)
    with pytest.raises(InvalidToken) as caught:
        signer.verify(token, user_id=1, action="archive", digest=OTHER, now=NOW)
    assert caught.value.reason == "selection"


def test_a_token_from_another_signer_is_refused(signer: Signer) -> None:
    token = _issue(signer)
    stranger = Signer(key=b"a different key entirely!" * 2)
    with pytest.raises(InvalidToken) as caught:
        stranger.verify(token, user_id=1, action="archive", digest=DIGEST, now=NOW)
    assert caught.value.reason == "malformed"


def test_a_tampered_payload_is_refused(signer: Signer) -> None:
    body, _, signature = _issue(signer).partition(".")
    forged, _ = signer.issue(user_id=1, action="archive", digest=DIGEST, count=1, now=NOW)
    with pytest.raises(InvalidToken) as caught:
        signer.verify(
            f"{forged.partition('.')[0]}.{signature}",
            user_id=1,
            action="archive",
            digest=DIGEST,
            now=NOW,
        )
    assert caught.value.reason == "malformed"
    assert body  # the two bodies differ, so the swap is a real tamper


@pytest.mark.parametrize(
    "token",
    ["", ".", "nodot", "a.b", "!!!.???", "." * 40, "eyJ2IjoidjEifQ.short"],
)
def test_garbage_is_refused_without_raising_anything_else(signer: Signer, token: str) -> None:
    with pytest.raises(InvalidToken) as caught:
        signer.verify(token, user_id=1, action="archive", digest=DIGEST, now=NOW)
    assert caught.value.reason == "malformed"


def test_a_token_of_an_unknown_version_is_refused(signer: Signer) -> None:
    import base64
    import json

    body = base64.urlsafe_b64encode(json.dumps({"v": "v99"}).encode()).decode().rstrip("=")
    signature = base64.urlsafe_b64encode(signer._mac(body)).decode().rstrip("=")
    with pytest.raises(InvalidToken) as caught:
        signer.verify(f"{body}.{signature}", user_id=1, action="archive", digest=DIGEST, now=NOW)
    assert caught.value.reason == "malformed"


# --- the digest -------------------------------------------------------------


def test_the_digest_ignores_key_order() -> None:
    assert selection_digest({"a": 1, "b": 2}) == selection_digest({"b": 2, "a": 1})


def test_the_digest_separates_different_selections() -> None:
    assert selection_digest({"ids": [1, 2]}) != selection_digest({"ids": [1, 3]})


def test_generated_signers_do_not_share_a_key() -> None:
    assert Signer.generated().key != Signer.generated().key
