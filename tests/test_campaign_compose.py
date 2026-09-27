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


#: ``To`` values that name more than one recipient, or another one (#269).
NOT_ONE_ADDRESS = [
    "ada@example.test, eve@example.test",
    "ada@example.test; eve@example.test",
    "ada@example.test eve@example.test",
    "friends: ada@example.test, eve@example.test;",
    "Eve <eve@example.test>",
    '"Ada Quill" <ada@example.test>',
    '"ada@example.test"@example.test',
    "ada(comment)@example.test",
    "ada@example.test (Ada)",
    "ada@[192.0.2.1]",
    "ada@@example.test",
    "@example.test",
    "ada@",
]


@pytest.mark.parametrize("to", NOT_ONE_ADDRESS)
def test_to_is_exactly_one_bare_address(to: str) -> None:
    with pytest.raises(ComposeError, match="one bare recipient"):
        build_message(to=to, subject="Catching up", body="b", message_id=_id())


def test_one_bare_address_with_space_around_it_is_sent_to() -> None:
    message = _wire(
        build_message(to="  ada.quill+work@example.test ", subject="s", body="b", message_id=_id())
    )
    assert message["To"] == "ada.quill+work@example.test"
    assert message.get_all("To") == ["ada.quill+work@example.test"]
    assert message["Cc"] is None and message["Bcc"] is None


# --- labels ---------------------------------------------------------------------------


def test_campaign_label() -> None:
    assert campaign_label("netkeeper", "First 100") == "netkeeper/First 100"
    assert campaign_label("netkeeper/", "  First   100 ") == "netkeeper/First 100"


# --- #280: Unicode domains, and a subject that is one line -----------------------------


@pytest.mark.parametrize(
    ("to", "expected"),
    [
        ("ada@ü.example", "ada@xn--tda.example"),
        ("ada@bücher.example", "ada@xn--bcher-kva.example"),
        ("ada@straße.example", "ada@xn--strae-oqa.example"),  # IDNA 2008, not "strasse"
        ("ada@BÜCHER.example", "ada@xn--bcher-kva.example"),
        ("ada@xn--tda.example", "ada@xn--tda.example"),
        ("Ada@Example.TEST", "Ada@Example.TEST"),  # ASCII is left as it is
    ],
)
def test_a_unicode_domain_goes_into_to_as_idna(to: str, expected: str) -> None:
    """#280: the header's own encoding made ``a@ü.com`` ``a@=?utf-8?q?=C3=BC?=.com``."""
    message = build_message(to=to, subject="s", body="b", message_id=_id())
    assert message["To"] == expected
    raw = message.as_bytes(policy=SMTP)
    assert f"To: {expected}\r\n".encode() in raw
    assert b"=?" not in raw.split(b"\r\n\r\n")[0]
    assert _wire(message).get_all("To") == [expected]


@pytest.mark.parametrize(
    "to", ["ada@-bü.example", "ada@ü..example", "ada@ü_x.example", "ü@example.test"]
)
def test_an_address_that_cannot_go_into_to_is_refused(to: str) -> None:
    with pytest.raises(ComposeError):
        build_message(to=to, subject="s", body="b", message_id=_id())


SUBJECT_BREAKS = [
    "\x0b",  # VT
    "\x0c",  # FF
    "\x1c",
    "\x1d",
    "\x1e",
    "\x85",  # NEL
    "\u2028",  # LINE SEPARATOR
    "\u2029",  # PARAGRAPH SEPARATOR
    "\x00",  # NUL
    "\x07",
    "\x7f",
    "\r",
    "\n",
]


@pytest.mark.parametrize(
    "separator", SUBJECT_BREAKS, ids=[f"U+{ord(c):04X}" for c in SUBJECT_BREAKS]
)
def test_a_subject_with_a_line_separator_or_control_is_a_compose_error(separator: str) -> None:
    """#280: these raised a bare ValueError (or passed, for NUL), and the step read as an
    unknown outcome that failed hours later."""
    with pytest.raises(ComposeError, match="one line"):
        build_message(
            to="ada@example.test", subject=f"Hi{separator}Bcc: x", body="b", message_id=_id()
        )


@pytest.mark.parametrize(
    "subject", ["=?utf-8?q?Hello?=", "Re: =?UTF-8?B?SGk=?= there", "a =?x?y?z?= b"]
)
def test_a_subject_with_an_encoded_word_is_refused(subject: str) -> None:
    """The header would decode it, so the recipient would read something else (#280)."""
    with pytest.raises(ComposeError, match="encoded word"):
        build_message(to="ada@example.test", subject=subject, body="b", message_id=_id())


@pytest.mark.parametrize("subject", ["Tab\there", "Olá, Ada", "Is 2+2=4?", "=? not one ?"])
def test_an_ordinary_subject_is_kept(subject: str) -> None:
    message = build_message(to="ada@example.test", subject=subject, body="b", message_id=_id())
    assert _wire(message)["Subject"] == subject


def test_whatever_the_email_package_refuses_is_a_compose_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal the checks above missed still fails the step at once, never as an
    unknown outcome (#280)."""

    def refuse(self: EmailMessage, *args: object, **kwargs: object) -> None:
        raise ValueError("refused")

    monkeypatch.setattr(EmailMessage, "set_content", refuse)
    with pytest.raises(ComposeError, match="email package refused"):
        build_message(to="ada@example.test", subject="s", body="b", message_id=_id())
