"""The messaging fixtures (#374): invented, consistent with the shape note, and not copied.

The last test checks that no fixture value appears anywhere in the maintainer's
private capture. It is opt-in: it runs only when ``NETKEEPER_CAPTURE_DIR`` names the
capture folder, and skips otherwise, even on a machine that has the folder. It never
looks at a default path, so an ordinary ``make check`` never reads the private folder.
Only the maintainer, or an analysis session he approves, sets the variable::

    NETKEEPER_CAPTURE_DIR=~/code/netkeeper-private/messaging-capture \
        .venv/bin/python -m pytest tests/test_messaging_pages.py

A failure names where the fixture value sits, never the value: if it matched, it is
real data.
"""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import messaging_pages as mp
import pytest

#: The opt-in: the capture folder to check against (#374). Unset, the check skips.
CAPTURE_ENV = "NETKEEPER_CAPTURE_DIR"


def _capture_dir() -> Path | None:
    raw = os.environ.get(CAPTURE_ENV, "").strip()
    return Path(raw).expanduser() if raw else None


_PEOPLE = (mp.OWNER, mp.ZEPHYRINE, mp.THADDEUS, mp.MARISOL, mp.BRIXTON, mp.QUILLON, mp.SAFFRON)


def _payloads() -> dict[str, str]:
    """Every JSON answer and request body the fixtures build, by name."""
    first, older = mp.INBOX_FIRST_PAGE, mp.INBOX_OLDER_PAGE
    token = mp.origin_token(1)
    return {
        "conversations_by_sync_token": mp.conversations_by_sync_token(first),
        "conversations_by_sync_token(refresh)": mp.conversations_by_sync_token(
            first[:1], deleted_urns=[mp.conversation_urn(99)]
        ),
        "conversations_by_category": mp.conversations_by_category(
            older, next_cursor="invented-cursor-0002"
        ),
        "conversations_by_ids": mp.conversations_by_ids(first[:1]),
        "conversations_by_recipients": mp.conversations_by_recipients(),
        "actorless_list_item": json.dumps(mp.actorless_list_item()),
        "messages_by_sync_token": mp.messages_by_sync_token(mp.THREAD_ONE_TO_ONE),
        "messages_by_anchor": mp.messages_by_anchor(mp.THREAD_ONE_TO_ONE[:1]),
        "compose_option_answer(existing)": mp.compose_option_answer(
            mp.ZEPHYRINE, existing_conversation=11
        ),
        "compose_option_answer(new)": mp.compose_option_answer(
            mp.THADDEUS, existing_conversation=None
        ),
        "compose_view_contexts_answer": mp.compose_view_contexts_answer(),
        "typing_body": mp.typing_body(11),
        "create_message_request": mp.create_message_request(11, "Invented sent text.", token=token),
        "create_message_response": mp.create_message_response(
            11, 9, "Invented sent text.", token=token, at_ms=mp.T0 + 99 * mp.MINUTE_MS
        ),
    }


def _urls() -> dict[str, str]:
    return {
        "conversations_sync_url": mp.conversations_sync_url("invented-sync-token-0003"),
        "conversations_category_url": mp.conversations_category_url(last_updated_before=mp.T0),
        "conversations_category_url(cursor)": mp.conversations_category_url(
            next_cursor="invented-cursor-0002"
        ),
        "conversations_ids_url": mp.conversations_ids_url(11),
        "conversations_recipients_url": mp.conversations_recipients_url(mp.THADDEUS),
        "messages_sync_url": mp.messages_sync_url(11, "invented-sync-token-0004"),
        "messages_anchor_url": mp.messages_anchor_url(11, mp.T0),
        "compose_option_url": mp.compose_option_url(mp.ZEPHYRINE),
        "compose_view_contexts_url": mp.compose_view_contexts_url(
            mp.ZEPHYRINE, existing_conversation=11
        ),
    }


def _html() -> dict[str, str]:
    return {
        "message_control_html": mp.message_control_html(mp.ZEPHYRINE),
        "profile_message_controls_html": mp.profile_message_controls_html(
            mp.ZEPHYRINE, decoy=mp.SAFFRON
        ),
        "existing_bubble_html": mp.existing_bubble_html(
            mp.ZEPHYRINE, mp.THREAD_ONE_TO_ONE, draft="Invented draft"
        ),
        "never_messaged_bubble_html": mp.never_messaged_bubble_html([mp.THADDEUS]),
    }


def _json_values(value: Any, where: str) -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _json_values(item, f"{where}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _json_values(item, f"{where}[{i}]")
    elif isinstance(value, str):
        yield where, value
    elif isinstance(value, int) and not isinstance(value, bool) and value >= 10**8:
        yield where, str(value)


class _HtmlValues(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.values.extend(v for _, v in attrs if v)

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.values.append(data.strip())


def fixture_values() -> list[tuple[str, str]]:
    """Every value the fixtures hold that is data, not structure, with where it sits."""
    found: list[tuple[str, str]] = []
    for name, body in _payloads().items():
        found.extend(_json_values(json.loads(body), name))
    for name, url in _urls().items():
        found.append((name, url))
        for key, value in parse_qsl(urlsplit(url).query):
            found.append((f"{name}?{key}", value))
        path = unquote(urlsplit(url).path)
        if "urn:li:" in path:  # the endpoint paths themselves are structure
            found.append((f"{name}<path>", path))
    for name, html in _html().items():
        parser = _HtmlValues()
        parser.feed(html)
        found.extend((name, v) for v in parser.values)
    for person in _PEOPLE:
        for attr in (
            "profile_id",
            "urn",
            "slug",
            "member_urn",
            "first",
            "last",
            "name",
            "headline",
        ):
            found.append((f"Member({person.n}).{attr}", str(getattr(person, attr))))
    for n in (11, 12, 13, 14, 15, 16, 17, 18):
        found.append((f"thread_id({n})", mp.thread_id(n)))
    for m in mp.THREAD_ONE_TO_ONE:
        found.append(
            (f"message_id({m.conversation},{m.k})", mp.message_id(m.conversation, m.k, m.at_ms))
        )
    return [(where, v) for where, v in found if not mp.is_structural(v)]


# --- always: the fixtures are invented and hang together --------------------------------


def test_the_capture_date_is_pinned() -> None:
    assert mp.CAPTURE_DATE == "2026-10-05"


def test_every_profile_id_is_invented_and_the_captured_length() -> None:
    for person in _PEOPLE:
        assert person.profile_id.startswith("ACoAAInvented")
        assert len(person.profile_id) == 39
    ids = [v for _, v in fixture_values() for v in re.findall(r"ACo[A-Za-z0-9_-]+", v)]
    assert ids and all(i.startswith("ACoAAInvented") for i in ids)


def test_the_list_carries_one_message_per_conversation() -> None:
    """The finding P4-01's thread rule rests on: the list holds the last message only."""
    page = json.loads(mp.conversations_by_sync_token(mp.INBOX_FIRST_PAGE))["data"][mp.BY_SYNC_TOKEN]
    for item in page["elements"]:
        assert len(item["messages"]["elements"]) == 1
        (last,) = item["messages"]["elements"]
        assert last["deliveredAt"] == item["lastActivityAt"]
        assert last["conversation"]["entityUrn"] == item["entityUrn"]
    times = [item["lastActivityAt"] for item in page["elements"]]
    assert times == sorted(times, reverse=True)


def test_the_urns_nest_as_captured() -> None:
    item = json.loads(mp.conversations_by_sync_token([mp.ONE_TO_ONE_INBOUND]))["data"][
        mp.BY_SYNC_TOKEN
    ]["elements"][0]
    tid = mp.thread_id(mp.ONE_TO_ONE_INBOUND.n)
    assert item["entityUrn"] == f"urn:li:msg_conversation:({mp.OWNER.urn},{tid})"
    assert item["backendUrn"] == f"urn:li:messagingThread:{tid}"
    assert item["conversationUrl"] == f"https://www.linkedin.com/messaging/thread/{tid}/"
    owner, other = item["conversationParticipants"]
    assert owner["participantType"]["member"]["distance"] == "SELF"
    assert other["hostIdentityUrn"] == mp.ZEPHYRINE.urn
    assert other["entityUrn"] == f"urn:li:msg_messagingParticipant:{mp.ZEPHYRINE.urn}"
    assert other["participantType"]["member"]["profileUrl"].endswith(
        f"/in/{mp.ZEPHYRINE.profile_id}"
    )
    assert mp.compose_option_urn(mp.ZEPHYRINE).startswith(
        f"urn:li:fsd_composeOption:({mp.ZEPHYRINE.profile_id},"
    )
    assert mp.fsd_conversation_urn(11) == f"urn:li:fsd_conversation:{mp.thread_id(11)}"


def test_only_the_owners_messages_carry_an_origin_token() -> None:
    thread = json.loads(mp.messages_by_sync_token(mp.THREAD_ONE_TO_ONE))["data"][
        mp.MESSAGES_BY_SYNC_TOKEN
    ]
    for item in thread["elements"]:
        outbound = item["sender"]["hostIdentityUrn"] == mp.OWNER.urn
        assert (item["originToken"] is not None) == outbound


def test_the_send_answer_echoes_what_the_page_sent() -> None:
    token = mp.origin_token(7)
    sent = json.loads(mp.create_message_request(11, "Invented text.", token=token))
    got = json.loads(mp.create_message_response(11, 9, "Invented text.", token=token, at_ms=mp.T0))[
        "value"
    ]
    assert got["originToken"] == sent["message"]["originToken"]
    assert got["conversationUrn"] == sent["message"]["conversationUrn"]
    assert got["senderUrn"] == mp.OWNER.participant_urn


def test_the_compose_answers_name_the_recipient_where_captured() -> None:
    existing = json.loads(mp.compose_option_answer(mp.ZEPHYRINE, existing_conversation=11))["data"]
    new = json.loads(mp.compose_option_answer(mp.THADDEUS, existing_conversation=None))["data"]
    assert existing["composeNavigationContext"]["recipientUrns"] == [mp.ZEPHYRINE.urn]
    assert existing["composeNavigationContext"][
        "existingConversationUrn"
    ] == mp.fsd_conversation_urn(11)
    assert new["composeNavigationContext"]["recipientUrns"] == [mp.THADDEUS.urn]
    assert "existingConversationUrn" not in new["composeNavigationContext"]
    assert mp.ZEPHYRINE.profile_id not in mp.compose_view_contexts_answer()


def test_the_bubbles_have_the_captured_controls() -> None:
    existing = mp.existing_bubble_html(mp.ZEPHYRINE, mp.THREAD_ONE_TO_ONE)
    assert 'role="dialog"' in existing and 'aria-label="Messaging"' in existing
    assert f'<h2 tabindex="-1"><a href="/in/{mp.ZEPHYRINE.profile_id}/">' in existing
    assert existing.count('role="textbox"') == 1 and "Write a message…" in existing
    assert '<button type="submit">Send</button>' in existing
    new = mp.never_messaged_bubble_html([mp.THADDEUS])
    assert f'aria-label="Remove {mp.THADDEUS.name}"' in new and 'role="combobox"' in new
    assert '<button disabled type="submit">Send</button>' in new
    assert '<button type="submit">Send</button>' in mp.never_messaged_bubble_html(
        [mp.THADDEUS], draft="x"
    )


def test_the_profile_has_three_identical_message_controls() -> None:
    page = mp.profile_message_controls_html(mp.ZEPHYRINE)
    assert page.count(">Message</span>") == 3
    assert page.count(f"recipient={mp.ZEPHYRINE.profile_id}") == 3


# --- on the maintainer's machine: nothing was copied ------------------------------------


def _har_text(path: Path) -> Iterator[str]:
    entries = json.loads(path.read_text(encoding="utf-8"))["log"]["entries"]
    for entry in entries:
        yield entry["request"]["url"]
        yield (entry["request"].get("postData") or {}).get("text") or ""
        content = entry["response"].get("content") or {}
        text = content.get("text") or ""
        if content.get("encoding") == "base64":
            text = base64.b64decode(text).decode("utf-8", "replace")
        yield text


def _capture_corpus(capture_dir: Path) -> bytes:
    parts: list[str] = []
    for path in sorted(capture_dir.iterdir()):
        if path.suffix == ".har":
            parts.extend(_har_text(path))
        elif path.is_file():
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
    corpus = "\n".join(parts)
    # A url or a body may carry a value percent-encoded; search the decoded form too.
    return (corpus + "\n" + unquote(corpus)).encode("utf-8")


@pytest.mark.slow
def test_no_fixture_value_appears_in_the_capture() -> None:
    capture_dir = _capture_dir()
    if capture_dir is None:
        pytest.skip(f"opt-in: set {CAPTURE_ENV} to the capture folder to run it")
    if not capture_dir.is_dir():
        pytest.fail(f"{CAPTURE_ENV} is set but is not a folder")
    corpus = _capture_corpus(capture_dir)
    # Every alphanumeric run of a value that occurs in the corpus is inside one of the
    # corpus's runs, so this cheap check rules most values out before the full search.
    runs = b"\n".join(set(re.findall(rb"[A-Za-z0-9]+", corpus)))
    found = []
    for where, value in fixture_values():
        needle = value.encode("utf-8")
        parts = re.findall(rb"[A-Za-z0-9]+", needle)
        if all(p in runs for p in parts) and needle in corpus:
            found.append(where)
    assert not found, f"fixture values that appear in the capture, at: {sorted(set(found))}"
