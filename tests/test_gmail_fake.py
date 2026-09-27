"""The in-memory Gmail the engine's tests run against (#262, P3-02).

These pin the behaviors P3-07 and P3-08 rely on matching Gmail: threading by
headers and subject, draft-to-sent ids, history ordering and expiry, search.
"""

import logging
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest

from netkeeper.campaigns.gmail import (
    Gmail,
    GmailNotFound,
    GmailRateLimited,
    GmailRejected,
    GmailTransient,
    ensure_label,
)
from netkeeper.campaigns.gmail_fake import MAILER_DAEMON, FakeGmail, normalize_subject

T0 = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
ME = "sender@example.com"
ADA = "ada@example.com"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, minutes: int) -> None:
        self.now += timedelta(minutes=minutes)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def gmail(clock: Clock) -> FakeGmail:
    return FakeGmail(ME, mailbox_id=5, clock=clock)


def _mail(
    subject: str = "Catching up",
    *,
    to: str = ADA,
    msgid: str | None = "<s1@example.com>",
    reply_to: str | None = None,
) -> EmailMessage:
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    if msgid:
        message["Message-ID"] = msgid
    if reply_to:
        message["In-Reply-To"] = reply_to
        message["References"] = reply_to
    message.set_content("Hello Ada, it has been a while since Lisbon.")
    return message


def _inbound(subject: str, *, sender: str = ADA, reply_to: str | None = None) -> EmailMessage:
    message = _mail(subject, to=ME, msgid=None, reply_to=reply_to)
    message["From"] = sender
    return message


def test_the_fake_has_the_interface(gmail: FakeGmail) -> None:
    implementation: Gmail = gmail
    assert implementation.profile(purpose="baseline").email == ME


# --- sending and threading --------------------------------------------------------


def test_a_first_message_starts_its_own_thread(gmail: FakeGmail, clock: Clock) -> None:
    ref = gmail.send(_mail(msgid=None), purpose="send step 1 for enrollment 1")

    assert ref.thread_id == ref.id
    message = gmail.get_message(ref.id, purpose="read")
    assert message.label_ids == {"SENT"}
    assert message.header("From") == ME  # Gmail fills in the account
    assert (message.header("Message-ID") or "").endswith("@mail.gmail.com>")
    assert message.internal_date == clock.now
    assert message.snippet.startswith("Hello Ada")


def test_a_follow_up_with_the_right_headers_joins_the_thread(
    gmail: FakeGmail, clock: Clock
) -> None:
    first = gmail.send(_mail(), purpose="send step 1")
    clock.advance(60 * 24 * 3)
    second = gmail.send(
        _mail("Re: Catching up", msgid="<s2@example.com>", reply_to="<s1@example.com>"),
        thread_id=first.thread_id,
        purpose="send step 2",
    )

    assert second.thread_id == first.thread_id
    thread = gmail.get_thread(first.thread_id, purpose="read")
    assert [message.id for message in thread.messages] == [first.id, second.id]
    assert thread.messages[1].header("In-Reply-To") == "<s1@example.com>"


@pytest.mark.parametrize(
    ("subject", "reply_to"),
    [
        ("Re: Catching up", None),  # no In-Reply-To or References
        ("Something else", "<s1@example.com>"),  # the subject does not match
        ("Re: Catching up", "<other@example.com>"),  # cites a message not in the thread
    ],
)
def test_a_follow_up_gmail_would_not_thread_starts_a_new_one(
    gmail: FakeGmail, subject: str, reply_to: str | None
) -> None:
    first = gmail.send(_mail(), purpose="send step 1")
    second = gmail.send(
        _mail(subject, msgid="<s2@example.com>", reply_to=reply_to),
        thread_id=first.thread_id,
        purpose="send step 2",
    )
    assert second.thread_id != first.thread_id
    assert second.thread_id == second.id


def test_a_send_without_a_thread_id_starts_a_new_thread_whatever_it_cites(
    gmail: FakeGmail,
) -> None:
    """Gmail threads a sent message only into the ``threadId`` it is given (#267)."""
    first = gmail.send(_mail(), purpose="send step 1")
    second = gmail.send(
        _mail("Re: Catching up", msgid="<s2@example.com>", reply_to="<s1@example.com>"),
        purpose="send step 2",
    )
    assert second.thread_id != first.thread_id
    assert second.thread_id == second.id
    assert len(gmail.get_thread(first.thread_id, purpose="read").messages) == 1


def test_an_unknown_thread_is_not_found(gmail: FakeGmail) -> None:
    with pytest.raises(GmailNotFound):
        gmail.send(_mail(), thread_id="nope", purpose="send step 2")


def test_a_message_with_no_recipient_is_rejected(gmail: FakeGmail) -> None:
    message = _mail()
    del message["To"]
    with pytest.raises(GmailRejected):
        gmail.send(message, purpose="send step 1")
    assert gmail.sent() == []


def test_a_draft_with_no_recipient_is_kept_but_cannot_be_sent(gmail: FakeGmail) -> None:
    """Gmail accepts such a draft; only pressing Send on it fails (#267)."""
    message = _mail()
    del message["To"]
    draft = gmail.create_draft(message, purpose="draft step 1")
    assert gmail.get_draft(draft.id, purpose="check draft") == draft
    with pytest.raises(GmailRejected):
        gmail.send_draft(draft.id)
    assert gmail.drafts() == {draft.id: draft.message}
    assert gmail.sent() == []


@pytest.mark.parametrize(
    ("sender", "kept"),
    [
        (ME, True),
        (f"Sam Sender <{ME.upper()}>", True),
        ("Sam <alias@example.com>", True),  # a verified alias
        ("Someone Else <other@example.com>", False),
        (f"{ME}, other@example.com", False),
    ],
)
def test_a_from_gmail_would_not_send_as_is_rewritten(sender: str, kept: bool) -> None:
    gmail = FakeGmail(ME, aliases=["Alias@example.com"])
    message = _mail()
    message["From"] = sender
    ref = gmail.send(message, purpose="send step 1")
    assert gmail.get_message(ref.id, purpose="read").header("From") == (sender if kept else ME)


def test_an_inbound_from_is_kept(gmail: FakeGmail) -> None:
    ref = gmail.deliver(_inbound("Hi", sender="Ada <ada@example.com>"))
    assert gmail.get_message(ref.id, purpose="read").header("From") == "Ada <ada@example.com>"


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Re: Catching up", "catching up"),
        ("RE: re:  Fwd: Catching   up", "catching up"),
        ("Fw: AW: Catching up", "catching up"),
        ("Remember me", "remember me"),
        (None, ""),
    ],
)
def test_normalize_subject(subject: str | None, expected: str) -> None:
    assert normalize_subject(subject) == expected


# --- drafts -----------------------------------------------------------------------


def test_a_draft_sent_by_the_person_becomes_a_sent_message_with_a_new_id(
    gmail: FakeGmail,
) -> None:
    first = gmail.send(_mail(), purpose="send step 1")
    draft = gmail.create_draft(
        _mail("Re: Catching up", msgid="<s2@example.com>", reply_to="<s1@example.com>"),
        thread_id=first.thread_id,
        purpose="draft step 2",
    )
    assert draft.message.thread_id == first.thread_id
    assert gmail.get_draft(draft.id, purpose="drafts poll") == draft
    assert gmail.get_message(draft.message.id, purpose="read").label_ids == {"DRAFT"}

    sent = gmail.send_draft(draft.id)

    with pytest.raises(GmailNotFound):
        gmail.get_draft(draft.id, purpose="drafts poll")
    with pytest.raises(GmailNotFound):
        gmail.get_message(draft.message.id, purpose="read")
    assert sent.id != draft.message.id
    thread = gmail.get_thread(first.thread_id, purpose="drafts poll")
    assert [(m.id, m.label_ids) for m in thread.messages] == [
        (first.id, frozenset({"SENT"})),
        (sent.id, frozenset({"SENT"})),
    ]


def test_a_discarded_draft_is_gone_with_its_message(gmail: FakeGmail) -> None:
    draft = gmail.create_draft(_mail(), purpose="draft step 1")
    gmail.discard_draft(draft.id)
    with pytest.raises(GmailNotFound):
        gmail.get_draft(draft.id, purpose="drafts poll")
    with pytest.raises(GmailNotFound):
        gmail.get_thread(draft.message.thread_id, purpose="drafts poll")
    assert gmail.drafts() == {}


# --- inbound, replies, bounces ----------------------------------------------------


def test_a_reply_lands_in_the_thread(gmail: FakeGmail, clock: Clock) -> None:
    first = gmail.send(_mail(), purpose="send step 1")
    clock.advance(90)
    reply = gmail.reply(first, sender=f"Ada Lovelace <{ADA}>")

    assert reply.thread_id == first.thread_id
    message = gmail.get_message(reply.id, purpose="read")
    assert message.label_ids == {"INBOX", "UNREAD"}
    assert message.header("In-Reply-To") == "<s1@example.com>"
    assert message.internal_date == clock.now


def test_a_fresh_email_from_the_contact_is_its_own_thread_and_search_finds_it(
    gmail: FakeGmail, clock: Clock
) -> None:
    first = gmail.send(_mail(), purpose="send step 1")
    clock.advance(60)
    fresh = gmail.deliver(_inbound("Saw your note"))
    assert fresh.thread_id not in {first.thread_id}

    after = int((T0 + timedelta(minutes=1)).timestamp())
    found = gmail.search(f"from:{ADA} after:{after}", purpose="reply search for enrollment 1")
    assert found == [fresh]
    assert gmail.search(f"from:{ADA} after:{int(clock.now.timestamp())}", purpose="s") == []


def test_a_bounce_lands_in_the_thread_from_the_mailer_daemon(gmail: FakeGmail) -> None:
    first = gmail.send(_mail(), purpose="send step 1")
    bounce = gmail.bounce(first)
    assert bounce.thread_id == first.thread_id
    message = gmail.get_message(bounce.id, purpose="bounce check")
    assert MAILER_DAEMON in (message.header("From") or "")


def test_a_snippet_is_escaped_as_gmail_sends_it_and_read_as_text(gmail: FakeGmail) -> None:
    message = _mail()
    message.set_content('I don\'t mind "soon" & <soon>.')
    ref = gmail.send(message, purpose="send step 1")

    assert gmail.wire_snippet(ref.id) == "I don&#39;t mind &quot;soon&quot; &amp; &lt;soon&gt;."
    assert gmail.get_message(ref.id, purpose="read").snippet == 'I don\'t mind "soon" & <soon>.'


def test_reading_a_message_never_carries_its_body(gmail: FakeGmail) -> None:
    ref = gmail.send(_mail(), purpose="send step 1")
    message = gmail.get_message(ref.id, purpose="read")
    assert all("Lisbon" not in value for _, value in message.headers)
    assert "Lisbon" in str(gmail.raw(ref.id).get_body())


# --- history ----------------------------------------------------------------------


def test_history_returns_what_was_added_after_the_start_in_order(
    gmail: FakeGmail, clock: Clock
) -> None:
    baseline = gmail.profile(purpose="baseline").history_id
    first = gmail.send(_mail(), purpose="send step 1")
    reply = gmail.reply(first, sender=ADA)
    middle = gmail.get_message(first.id, purpose="read").history_id
    other = gmail.deliver(_inbound("Unrelated", sender="bob@example.com"))

    history = gmail.history(baseline, purpose="reply poll")
    assert history.messages_added == (first, reply, other)
    assert history.history_id == gmail.history_id
    assert gmail.history(middle, purpose="reply poll").messages_added == (reply, other)
    assert gmail.history(history.history_id, purpose="reply poll").messages_added == ()


def test_history_ids_only_grow(gmail: FakeGmail) -> None:
    seen = [gmail.history_id]
    ref = gmail.send(_mail(), purpose="send step 1")
    seen.append(gmail.history_id)
    label = gmail.create_label("netkeeper/x", purpose="label")
    gmail.modify_labels(ref.id, add=[label.id], purpose="label step 1")
    seen.append(gmail.history_id)
    draft = gmail.create_draft(_mail(msgid="<d@example.com>"), purpose="draft")
    seen.append(gmail.history_id)
    gmail.send_draft(draft.id)
    seen.append(gmail.history_id)
    assert seen == sorted(set(seen))
    assert gmail.get_message(ref.id, purpose="read").history_id == seen[2]


def test_history_filters_by_label(gmail: FakeGmail) -> None:
    baseline = gmail.history_id
    gmail.send(_mail(), purpose="send step 1")
    inbound = gmail.deliver(_inbound("Hi"))
    assert gmail.history(baseline, label_id="INBOX", purpose="poll").messages_added == (inbound,)


def test_history_keeps_a_message_deleted_since_and_reading_it_is_not_found(
    gmail: FakeGmail,
) -> None:
    """Gmail keeps ``messageAdded`` for a message deleted since; P3-08 must cope (#267)."""
    baseline = gmail.history_id
    first = gmail.send(_mail(), purpose="send step 1")
    reply = gmail.reply(first, sender=ADA)
    gmail.delete(reply)

    history = gmail.history(baseline, purpose="reply poll")
    assert history.messages_added == (first, reply)
    assert gmail.history(baseline, label_id="INBOX", purpose="poll").messages_added == (reply,)
    with pytest.raises(GmailNotFound):
        gmail.get_message(reply.id, purpose="read reply")
    thread = gmail.get_thread(first.thread_id, purpose="read")
    assert [message.id for message in thread.messages] == [first.id]


def test_history_keeps_a_draft_sent_or_discarded_since(gmail: FakeGmail) -> None:
    baseline = gmail.history_id
    sent_draft = gmail.create_draft(_mail(msgid="<d1@example.com>"), purpose="draft 1")
    discarded = gmail.create_draft(_mail(msgid="<d2@example.com>"), purpose="draft 2")
    sent = gmail.send_draft(sent_draft.id)
    gmail.discard_draft(discarded.id)

    added = gmail.history(baseline, purpose="poll").messages_added
    assert added == (sent_draft.message, discarded.message, sent)
    for gone in (sent_draft.message, discarded.message):
        with pytest.raises(GmailNotFound):
            gmail.get_message(gone.id, purpose="read")


def test_deleting_a_drafts_message_removes_the_draft(gmail: FakeGmail) -> None:
    draft = gmail.create_draft(_mail(), purpose="draft")
    gmail.delete(draft.message)
    assert gmail.drafts() == {}
    with pytest.raises(GmailNotFound):
        gmail.get_draft(draft.id, purpose="check draft")


def test_history_older_than_gmail_keeps_is_not_found(gmail: FakeGmail) -> None:
    baseline = gmail.history_id
    gmail.send(_mail(), purpose="send step 1")
    gmail.forget_history()
    with pytest.raises(GmailNotFound):
        gmail.history(baseline, purpose="reply poll")
    assert gmail.history(gmail.history_id, purpose="reply poll").messages_added == ()


# --- search -----------------------------------------------------------------------


def test_search_operators(gmail: FakeGmail, clock: Clock) -> None:
    first = gmail.send(_mail(), purpose="send step 1")
    clock.advance(5)
    second = gmail.send(_mail(to="bob@example.com", msgid="<b@example.com>"), purpose="send")
    clock.advance(5)
    inbound = gmail.deliver(_inbound("Hello"))
    spam = gmail.deliver(_inbound("Win", sender="spam@example.com"), labels=["SPAM"])
    label = gmail.create_label("netkeeper/First 100", purpose="label")
    gmail.modify_labels(first.id, add=[label.id], purpose="label step 1")

    def search(query: str) -> list[str]:
        return [ref.id for ref in gmail.search(query, purpose="search")]

    assert search("in:sent") == [second.id, first.id]  # newest first
    assert search(f"to:{ADA}") == [first.id]
    assert search("rfc822msgid:<b@example.com>") == [second.id]
    assert search("rfc822msgid:b@example.com") == [second.id]
    assert search('label:"netkeeper/First 100"') == [first.id]
    assert search("in:inbox") == [inbound.id]
    assert spam.id in search("in:anywhere")  # the one search with spam and trash (#267)
    assert spam.id not in search("from:spam@example.com")
    assert search(f"before:{int((T0 + timedelta(minutes=1)).timestamp())}") == [first.id]
    assert len(gmail.search("in:anywhere", max_results=2, purpose="s")) == 2


def test_trash_is_searched_only_in_anywhere(gmail: FakeGmail) -> None:
    trashed = gmail.deliver(_inbound("Old news"), labels=["TRASH"])
    assert gmail.search(f"from:{ADA}", purpose="s") == []
    assert gmail.search(f"from:{ADA} in:anywhere", purpose="s") == [trashed]


@pytest.mark.parametrize(
    ("query", "found"),
    [
        ("from:ada@example.com", True),
        ("from:ada", True),  # a whole word of the address
        ("from:example.com", True),
        ("from:lovelace", True),  # a word of the display name
        ("from:ad", False),  # part of a word
        ("from:da@example.com", False),
        ("from:ada@example.co", False),
        ("from:bada@example.com", False),
    ],
)
def test_from_matches_whole_words_as_gmail_does(gmail: FakeGmail, query: str, found: bool) -> None:
    ref = gmail.deliver(_inbound("Hi", sender="Ada Lovelace <ada@example.com>"))
    assert gmail.search(query, purpose="s") == ([ref] if found else [])


def test_to_does_not_match_an_address_that_only_contains_the_one_asked_for(
    gmail: FakeGmail,
) -> None:
    gmail.send(_mail(to="bada@example.com"), purpose="send")
    wanted = gmail.send(_mail(to=ADA, msgid="<s2@example.com>"), purpose="send")
    assert gmail.search(f"to:{ADA}", purpose="s") == [wanted]


@pytest.mark.parametrize("query", ["hello", "after:2026/10/06", "subject:hi", "in:chats"])
def test_search_refuses_what_it_cannot_read_as_gmail_would(gmail: FakeGmail, query: str) -> None:
    with pytest.raises(ValueError):
        gmail.search(query, purpose="search")


# --- labels -----------------------------------------------------------------------


def test_labels(gmail: FakeGmail) -> None:
    made = ensure_label(gmail, "netkeeper/First 100", purpose="label for campaign 1")
    assert ensure_label(gmail, "NETKEEPER/first 100", purpose="label for campaign 1") == made
    assert made.type == "user"
    assert {"INBOX", "SENT", "DRAFT"} <= {label.id for label in gmail.list_labels(purpose="l")}

    ref = gmail.send(_mail(), purpose="send step 1")
    with pytest.raises(GmailRejected):
        gmail.modify_labels(ref.id, add=["Label_404"], purpose="label step 1")
    with pytest.raises(GmailNotFound):
        gmail.modify_labels("nope", add=[made.id], purpose="label step 1")


@pytest.mark.parametrize(
    ("add", "remove"), [(["DRAFT"], []), (["SENT"], []), ([], ["SENT"]), ([], ["DRAFT"])]
)
def test_modify_labels_refuses_draft_and_sent(
    gmail: FakeGmail, add: list[str], remove: list[str]
) -> None:
    ref = gmail.send(_mail(), purpose="send step 1")
    with pytest.raises(GmailRejected):
        gmail.modify_labels(ref.id, add=add, remove=remove, purpose="label step 1")
    assert gmail.get_message(ref.id, purpose="read").label_ids == {"SENT"}


def test_modify_labels_accepts_the_labels_a_client_may_change(gmail: FakeGmail) -> None:
    ref = gmail.deliver(_inbound("Hi"))
    gmail.modify_labels(ref.id, add=["IMPORTANT"], remove=["UNREAD", "INBOX"], purpose="triage")
    assert gmail.get_message(ref.id, purpose="read").label_ids == {"IMPORTANT"}


# --- scripting, purposes ----------------------------------------------------------


def test_fail_next_fails_once_in_order(gmail: FakeGmail) -> None:
    gmail.fail_next("messages.send", GmailRateLimited("slow down", code="rateLimitExceeded"))
    gmail.fail_next("messages.send", GmailTransient("down", code="backendError"))
    with pytest.raises(GmailRateLimited):
        gmail.send(_mail(), purpose="send step 1")
    with pytest.raises(GmailTransient):
        gmail.send(_mail(), purpose="send step 1")
    gmail.send(_mail(), purpose="send step 1")
    assert len(gmail.sent()) == 1


def test_calls_and_purposes_are_recorded_and_logged(
    gmail: FakeGmail, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="netkeeper.campaigns.gmail")
    gmail.send(_mail(), purpose="send step 1 for enrollment 14")
    gmail.list_labels(purpose="labels for campaign 2")
    assert gmail.calls == [
        ("messages.send", "send step 1 for enrollment 14"),
        ("labels.list", "labels for campaign 2"),
    ]
    assert "gmail messages.send for mailbox 5: send step 1 for enrollment 14" in caplog.messages


def test_the_fake_refuses_a_purpose_the_client_would(gmail: FakeGmail) -> None:
    with pytest.raises(ValueError):
        gmail.send(_mail(), purpose=f"reply to {ADA}")
    assert gmail.sent() == []
