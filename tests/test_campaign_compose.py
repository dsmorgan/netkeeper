"""netkeeper.campaigns.compose: RFC 2822 building, the Message-ID, threading (P3-07, #269)."""

from __future__ import annotations

import email
import hashlib
from datetime import UTC, datetime, timedelta, timezone
from email.message import EmailMessage
from email.policy import SMTP

import pytest

from netkeeper.campaigns import compose
from netkeeper.campaigns.compose import (
    ComposeError,
    build_message,
    campaign_label,
    is_message_id,
    message_id_for,
    reply_subject,
)
from netkeeper.campaigns.gmail_fake import normalize_subject

CREATED = datetime(2026, 9, 29, 14, 0, 0, 123456, tzinfo=UTC)


def _id(**overrides: object) -> str:
    fields: dict[str, object] = {
        "user_id": 1,
        "message_id": 42,
        "created_at": CREATED,
        "address": "me@example.test",
    }
    fields.update(overrides)
    return message_id_for(**fields)  # type: ignore[arg-type]


def _wire(message: EmailMessage) -> EmailMessage:
    parsed = email.message_from_bytes(message.as_bytes(policy=SMTP), policy=SMTP)
    assert isinstance(parsed, EmailMessage)
    return parsed


def test_the_compose_constants_are_pinned() -> None:
    assert compose.MESSAGE_ID_VERSION == "v1"
    assert compose.MESSAGE_ID_HASH_LENGTH == 32
    assert compose.REFERENCES_MAX == 20


# --- the Message-ID -------------------------------------------------------------------


def test_the_message_id_is_the_same_every_time_for_one_row() -> None:
    first = _id()
    assert first == _id()
    assert first == _id(created_at=CREATED.astimezone(timezone(timedelta(hours=-7))))
    assert first == "<" + first[1:33] + "@example.test>"
    assert is_message_id(first)


def test_the_message_id_is_pinned_to_its_derivation() -> None:
    """A change to the derivation would lose every message sent before it: pinned."""
    seed = "netkeeper-message-id:v1:1:42:2026-09-29T14:00:00.123456+00:00"
    expected = hashlib.sha256(seed.encode()).hexdigest()[:32]
    assert _id() == f"<{expected}@example.test>"


@pytest.mark.parametrize(
    "change",
    [
        {"user_id": 2},
        {"message_id": 43},
        {"created_at": CREATED + timedelta(microseconds=1)},
        {"address": "me@other.test"},
    ],
)
def test_the_message_id_differs_for_another_row_user_database_or_mailbox(
    change: dict[str, object],
) -> None:
    """``created_at`` matters: a database made again numbers messages from 1 again."""
    assert _id(**change) != _id()


def test_the_message_id_says_nothing_about_the_row_or_netkeeper() -> None:
    local = _id().split("@")[0]
    assert "netkeeper" not in local and "42" not in local[:3]


@pytest.mark.parametrize("address", ["me", "me@", "me@exa mple.test", "me@bad>domain"])
def test_the_message_id_needs_a_domain(address: str) -> None:
    with pytest.raises(ComposeError):
        _id(address=address)


def test_the_message_id_refuses_a_naive_time() -> None:
    with pytest.raises(ComposeError):
        _id(created_at=CREATED.replace(tzinfo=None))


def test_the_message_id_lowercases_the_domain() -> None:
    assert _id(address="Me@Example.TEST") == _id()


# --- subjects -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Catching up", "Re: Catching up"),
        ("Re: Catching up", "Re: Catching up"),
        ("RE:Catching up", "RE:Catching up"),
        ("  Catching   up ", "Re: Catching up"),
        ("Fwd: Catching up", "Re: Fwd: Catching up"),
        ("", "Re:"),
        (None, "Re:"),
    ],
)
def test_reply_subject(subject: str | None, expected: str) -> None:
    assert reply_subject(subject) == expected


@pytest.mark.parametrize("subject", ["Catching up", "Re: Catching up", "Fwd: News"])
def test_a_reply_subject_threads_by_the_fakes_rule(subject: str) -> None:
    assert normalize_subject(reply_subject(subject)) == normalize_subject(subject)


# --- the message ----------------------------------------------------------------------


def test_a_first_message_has_its_headers_and_no_threading() -> None:
    message = _wire(
        build_message(
            to="ada@example.test", subject="Catching up", body="Hi Ada.\n", message_id=_id()
        )
    )
    assert message["To"] == "ada@example.test"
    assert message["Subject"] == "Catching up"
    assert message["Message-ID"] == _id()
    assert message["From"] is None  # Gmail fills in the account and its name
    assert message["In-Reply-To"] is None and message["References"] is None
    assert message.get_content_type() == "text/plain"
    assert message.get_content().replace("\r\n", "\n") == "Hi Ada.\n"


def test_a_follow_up_cites_the_thread() -> None:
    message = _wire(
        build_message(
            to="ada@example.test",
            subject="Re: Catching up",
            body="Following up.\n",
            message_id="<two@example.test>",
            in_reply_to="<one@example.test>",
            references=["<one@example.test>"],
        )
    )
    assert message["In-Reply-To"] == "<one@example.test>"
    assert message["References"] == "<one@example.test>"


def test_references_keep_the_first_and_the_newest() -> None:
    cites = [f"<m{n}@example.test>" for n in range(30)]
    message = build_message(
        to="ada@example.test",
        subject="Re: x",
        body="b",
        message_id="<new@example.test>",
        in_reply_to=cites[-1],
        references=cites,
    )
    kept = str(message["References"]).split()
    assert len(kept) == 20
    assert kept[0] == "<m0@example.test>"
    assert kept[1:] == cites[-19:]


def test_a_body_in_another_script_survives_the_wire() -> None:
    message = _wire(
        build_message(to="ada@example.test", subject="Olá, Ada", body="Até já.\n", message_id=_id())
    )
    assert message["Subject"] == "Olá, Ada"
    assert message.get_content().replace("\r\n", "\n") == "Até já.\n"


@pytest.mark.parametrize(
    "fields",
    [
        {"to": ""},
        {"to": "nobody"},
        {"to": "ada@example.test\nBcc: eve@example.test"},
        {"subject": "Hi\r\nBcc: eve@example.test"},
        {"message_id": "not-an-id"},
        {"in_reply_to": "<a@b> <c@d>"},
        {"references": ["<ok@example.test>", "bad"]},
    ],
)
def test_nothing_can_add_a_header(fields: dict[str, object]) -> None:
    arguments: dict[str, object] = {
        "to": "ada@example.test",
        "subject": "Catching up",
        "body": "b",
        "message_id": _id(),
    }
    arguments.update(fields)
    with pytest.raises(ComposeError):
        build_message(**arguments)  # type: ignore[arg-type]


# --- labels ---------------------------------------------------------------------------


def test_campaign_label() -> None:
    assert campaign_label("netkeeper", "First 100") == "netkeeper/First 100"
    assert campaign_label("netkeeper/", "  First   100 ") == "netkeeper/First 100"
