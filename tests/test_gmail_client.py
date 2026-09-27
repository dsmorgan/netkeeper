"""The Gmail client over googleapiclient: requests, auth, errors, logging (#262, P3-02).

Every test drives the real ``GmailClient`` over :class:`RecordingHttp`, which
answers from a script and records what was sent. Nothing reaches Gmail.
"""

import base64
import email
import inspect
import logging
import re
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP
from typing import Any

import pytest
from gmail_fakes import RecordingHttp, gmail_error

from netkeeper.campaigns import gmail as gmail_module
from netkeeper.campaigns import gmail_oauth
from netkeeper.campaigns.gmail import (
    Gmail,
    GmailAuthError,
    GmailClient,
    GmailConflict,
    GmailNotFound,
    GmailRateLimited,
    GmailRejected,
    GmailTransient,
    Label,
    check_purpose,
    ensure_label,
)

API = "/gmail/v1/users/me"
PURPOSE = "send step 1 for enrollment 7"


class Tokens:
    """A ``refresh`` function that counts its calls and can be told to fail."""

    def __init__(self) -> None:
        self.calls = 0
        self.error: Exception | None = None

    def __call__(self) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return f"access-{self.calls}"


def _client(
    http: RecordingHttp,
    tokens: Tokens | None = None,
    failures: list[GmailAuthError] | None = None,
) -> GmailClient:
    return GmailClient(
        tokens or Tokens(),
        mailbox_id=3,
        http=http,
        on_auth_failure=None if failures is None else failures.append,
    )


def _message(subject: str = "Catching up") -> EmailMessage:
    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["To"] = "ada@example.com"
    message["Subject"] = subject
    message["Message-ID"] = "<step1@example.com>"
    message.set_content("Hello Ada,\n\nIt has been a while.\n")
    return message


def _decode(raw: str) -> EmailMessage:
    parsed = email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=SMTP)
    assert isinstance(parsed, EmailMessage)
    return parsed


def test_both_implementations_have_the_interface() -> None:
    """mypy checks these assignments; the fake's own tests use the same Protocol."""
    from netkeeper.campaigns.gmail_fake import FakeGmail

    implementations: list[Gmail] = [_client(RecordingHttp()), FakeGmail()]
    assert len(implementations) == 2


# --- requests ---------------------------------------------------------------------


def test_send_posts_the_raw_message_into_the_thread() -> None:
    http = RecordingHttp().answer(200, {"id": "m2", "threadId": "t1", "labelIds": ["SENT"]})
    ref = _client(http).send(_message("Re: Catching up"), thread_id="t1", purpose=PURPOSE)

    assert (ref.id, ref.thread_id) == ("m2", "t1")
    [sent] = http.requests
    assert (sent.method, sent.path) == ("POST", f"{API}/messages/send")
    assert sent.headers["authorization"] == "Bearer access-1"
    body = sent.json()
    assert body["threadId"] == "t1"
    raw = _decode(body["raw"])
    assert raw["Subject"] == "Re: Catching up"
    assert raw["Message-ID"] == "<step1@example.com>"
    assert b"\r\n" in base64.urlsafe_b64decode(body["raw"])  # RFC 2822 line ends


def test_send_without_a_thread_names_none() -> None:
    http = RecordingHttp().answer(200, {"id": "m1", "threadId": "m1"})
    _client(http).send(_message(), purpose=PURPOSE)
    assert "threadId" not in http.requests[0].json()


def test_one_token_serves_many_calls() -> None:
    tokens = Tokens()
    http = RecordingHttp().answer(200, {"labels": []}).answer(200, {"labels": []})
    client = _client(http, tokens)
    client.list_labels(purpose="list labels")
    client.list_labels(purpose="list labels")
    assert tokens.calls == 1
    assert [sent.headers["authorization"] for sent in http.requests] == ["Bearer access-1"] * 2


def test_a_401_renews_the_token_once_and_sends_again() -> None:
    tokens = Tokens()
    http = (
        RecordingHttp()
        .answer(401, gmail_error(401, "authError"))
        .answer(200, {"id": "m1", "threadId": "m1"})
    )
    _client(http, tokens).send(_message(), purpose=PURPOSE)

    assert tokens.calls == 2
    first, second = http.requests
    assert first.headers["authorization"] == "Bearer access-1"
    assert second.headers["authorization"] == "Bearer access-2"
    assert first.body == second.body


def test_create_and_get_a_draft() -> None:
    draft = {"id": "r9", "message": {"id": "m5", "threadId": "t1"}}
    http = RecordingHttp().answer(200, draft).answer(200, draft)
    client = _client(http)

    made = client.create_draft(_message(), thread_id="t1", purpose="draft step 2 for enrollment 7")
    got = client.get_draft("r9", purpose="drafts poll for enrollment 7")

    assert made == got
    assert (made.id, made.message.id, made.message.thread_id) == ("r9", "m5", "t1")
    create, get = http.requests
    assert (create.method, create.path) == ("POST", f"{API}/drafts")
    assert create.json()["message"]["threadId"] == "t1"
    assert _decode(create.json()["message"]["raw"])["To"] == "ada@example.com"
    assert (get.method, get.path, get.query["format"]) == ("GET", f"{API}/drafts/r9", ["minimal"])


def test_list_drafts_reads_every_page() -> None:
    http = (
        RecordingHttp()
        .answer(
            200,
            {
                "drafts": [{"id": "r1", "message": {"id": "m1", "threadId": "t1"}}],
                "nextPageToken": "p2",
            },
        )
        .answer(200, {"drafts": [{"id": "r2", "message": {"id": "m2", "threadId": "t2"}}]})
    )
    drafts = _client(http).list_drafts(purpose="drafts poll for mailbox 3")

    assert [(d.id, d.message.id, d.message.thread_id) for d in drafts] == [
        ("r1", "m1", "t1"),
        ("r2", "m2", "t2"),
    ]
    first, second = http.requests
    assert (first.method, first.path) == ("GET", f"{API}/drafts")
    assert "pageToken" not in first.query
    assert second.query["pageToken"] == ["p2"]


def test_list_drafts_with_none_is_empty() -> None:
    http = RecordingHttp().answer(200, {"resultSizeEstimate": 0})
    assert _client(http).list_drafts(purpose="drafts poll for mailbox 3") == []


def test_netkeeper_never_deletes_mail() -> None:
    """ADR 0003: netkeeper deletes nothing in Gmail, drafts included (#273, question 2).
    Neither the interface nor the client has a way to, and the client never builds a
    ``delete``, ``trash`` or ``batchDelete`` request."""
    for cls in (Gmail, GmailClient):
        names = [name.lower() for name in dir(cls) if not name.startswith("__")]
        assert [n for n in names if "delete" in n or "trash" in n] == []
    source = inspect.getsource(gmail_module)
    assert re.search(r"\.(delete|trash|batchDelete|untrash)\(", source) is None


def test_get_message_reads_metadata_never_the_body() -> None:
    body = {
        "id": "m1",
        "threadId": "t1",
        "labelIds": ["INBOX", "UNREAD"],
        "historyId": "4242",
        "internalDate": "1790000000000",
        "snippet": "Thanks, I don&#39;t mind &quot;soon&quot; &amp; &lt;b&gt;happy&lt;/b&gt;",
        "payload": {
            "headers": [
                {"name": "From", "value": "Ada <ada@example.com>"},
                {"name": "Message-Id", "value": "<reply@example.com>"},
            ]
        },
    }
    http = RecordingHttp().answer(200, body)
    message = _client(http).get_message("m1", purpose="reply check for enrollment 7")

    assert message.label_ids == {"INBOX", "UNREAD"}
    assert message.snippet == 'Thanks, I don\'t mind "soon" & <b>happy</b>'  # unescaped (#267)
    assert message.history_id == 4242
    assert message.internal_date == datetime.fromtimestamp(1_790_000_000, tz=UTC)
    assert message.header("message-id") == "<reply@example.com>"
    assert message.header("From") == "Ada <ada@example.com>"
    assert message.header("References") is None
    query = http.requests[0].query
    assert query["format"] == ["metadata"]
    assert {"From", "Message-ID", "In-Reply-To", "References"} <= set(query["metadataHeaders"])


def test_get_thread_keeps_the_message_order() -> None:
    def item(message_id: str, when: int) -> dict[str, Any]:
        return {
            "id": message_id,
            "threadId": "t1",
            "historyId": "10",
            "internalDate": str(when),
            "payload": {"headers": []},
        }

    http = RecordingHttp().answer(
        200, {"id": "t1", "historyId": "12", "messages": [item("a", 1), item("b", 2)]}
    )
    thread = _client(http).get_thread("t1", purpose="threads fallback for enrollment 7")
    assert [message.id for message in thread.messages] == ["a", "b"]
    assert thread.history_id == 12
    assert http.requests[0].path == f"{API}/threads/t1"


def test_search_follows_pages_up_to_the_limit() -> None:
    def page(ids: list[str], token: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {"messages": [{"id": i, "threadId": i} for i in ids]}
        if token:
            body["nextPageToken"] = token
        return body

    http = RecordingHttp().answer(200, page(["a", "b"], "p2")).answer(200, page(["c", "d"], "p3"))
    found = _client(http).search(
        "from:ada@example.com after:1790000000", max_results=3, purpose="reply search"
    )

    assert [ref.id for ref in found] == ["a", "b", "c"]
    first, second = http.requests
    assert first.query["q"] == ["from:ada@example.com after:1790000000"]
    assert first.query["maxResults"] == ["3"]
    assert "pageToken" not in first.query
    assert (second.query["pageToken"], second.query["maxResults"]) == (["p2"], ["1"])


def test_history_reads_every_page_in_order() -> None:
    def added(*ids: str) -> list[dict[str, Any]]:
        return [{"message": {"id": i, "threadId": "t"}} for i in ids]

    http = (
        RecordingHttp()
        .answer(
            200,
            {
                "history": [{"id": "101", "messagesAdded": added("a", "b")}],
                "historyId": "150",
                "nextPageToken": "n",
            },
        )
        .answer(
            200,
            {"history": [{"id": "160", "messagesAdded": added("b", "c")}], "historyId": "170"},
        )
    )
    history = _client(http).history(100, label_id="INBOX", purpose="reply poll")

    assert [ref.id for ref in history.messages_added] == ["a", "b", "c"]
    assert history.history_id == 170
    first = http.requests[0]
    assert first.query["startHistoryId"] == ["100"]
    assert first.query["historyTypes"] == ["messageAdded"]
    assert first.query["labelId"] == ["INBOX"]
    assert http.requests[1].query["pageToken"] == ["n"]


def test_history_with_nothing_new_keeps_gmails_id() -> None:
    http = RecordingHttp().answer(200, {"historyId": "205"})
    history = _client(http).history(200, purpose="reply poll")
    assert history.messages_added == ()
    assert history.history_id == 205


def test_labels_and_modify() -> None:
    http = (
        RecordingHttp()
        .answer(200, {"labels": [{"id": "INBOX", "name": "INBOX", "type": "system"}]})
        .answer(200, {"id": "Label_1", "name": "netkeeper/First 100", "type": "user"})
        .answer(200, {"id": "m1", "threadId": "m1", "labelIds": ["Label_1"]})
        .answer(200, {"emailAddress": "Sender@Example.com", "historyId": "77"})
    )
    client = _client(http)
    assert client.list_labels(purpose="labels") == [Label("INBOX", "INBOX", "system")]
    made = client.create_label("netkeeper/First 100", purpose="label for campaign 2")
    client.modify_labels("m1", add=[made.id], remove=["UNREAD"], purpose="label step 1")
    profile = client.profile(purpose="baseline history for mailbox 3")

    assert made == Label("Label_1", "netkeeper/First 100", "user")
    create, modify = http.requests[1], http.requests[2]
    assert create.json()["name"] == "netkeeper/First 100"
    assert (modify.path, modify.json()) == (
        f"{API}/messages/m1/modify",
        {"addLabelIds": ["Label_1"], "removeLabelIds": ["UNREAD"]},
    )
    assert (profile.email, profile.history_id) == ("sender@example.com", 77)


# --- errors -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "reason", "expected", "code"),
    [
        (429, "rateLimitExceeded", GmailRateLimited, "rateLimitExceeded"),
        (403, "userRateLimitExceeded", GmailRateLimited, "userRateLimitExceeded"),
        (403, "dailyLimitExceeded", GmailRateLimited, "dailyLimitExceeded"),
        (403, "insufficientPermissions", GmailAuthError, "insufficientPermissions"),
        (404, "notFound", GmailNotFound, "notFound"),
        (409, "conflict", GmailConflict, "conflict"),
        (400, "invalidArgument", GmailRejected, "invalidArgument"),
        (500, "backendError", GmailTransient, "backendError"),
        (503, "", GmailTransient, "http_503"),
        (408, "", GmailTransient, "http_408"),
    ],
)
def test_http_errors_map_to_typed_errors(
    status: int, reason: str, expected: type[Exception], code: str
) -> None:
    body: Any = gmail_error(status, reason) if reason else b"<html>unavailable</html>"
    http = RecordingHttp().answer(status, body)
    with pytest.raises(expected) as caught:
        _client(http).get_message("m1", purpose="read message 1")
    assert getattr(caught.value, "code", None) == code


@pytest.mark.parametrize(
    ("status", "api_status", "expected"),
    [
        (403, "RESOURCE_EXHAUSTED", GmailRateLimited),  # a quota, not the token (#267)
        (403, "PERMISSION_DENIED", GmailAuthError),
        (403, None, GmailAuthError),
        (429, "RESOURCE_EXHAUSTED", GmailRateLimited),
    ],
)
def test_a_403_with_no_legacy_reason_is_classed_by_its_status(
    status: int, api_status: str | None, expected: type[Exception]
) -> None:
    error: dict[str, Any] = {"code": status, "message": "a refusal"}
    if api_status is not None:
        error["status"] = api_status
    failures: list[GmailAuthError] = []
    http = RecordingHttp().answer(status, {"error": error})
    with pytest.raises(expected) as caught:
        _client(http, failures=failures).get_message("m1", purpose="read message 1")
    assert getattr(caught.value, "code", None) == f"http_{status}"
    assert len(failures) == (1 if expected is GmailAuthError else 0)


def test_a_quota_status_is_a_limit_whatever_reason_it_names() -> None:
    """As the module docstring says: RESOURCE_EXHAUSTED never marks the mailbox."""
    body = gmail_error(403, "forbidden")
    body["error"]["status"] = "RESOURCE_EXHAUSTED"
    failures: list[GmailAuthError] = []
    with pytest.raises(GmailRateLimited):
        _client(RecordingHttp().answer(403, body), failures=failures).get_message(
            "m1", purpose="read message 1"
        )
    assert failures == []


def test_a_rate_limit_reason_wins_over_a_status_that_is_not_one() -> None:
    body = gmail_error(403, "userRateLimitExceeded")
    body["error"]["status"] = "PERMISSION_DENIED"
    with pytest.raises(GmailRateLimited):
        _client(RecordingHttp().answer(403, body)).get_message("m1", purpose="read message 1")


def test_googles_message_text_is_never_kept() -> None:
    http = RecordingHttp().answer(400, gmail_error(400, "invalidArgument", "Invalid To: ada@x.io"))
    with pytest.raises(GmailRejected) as caught:
        _client(http).send(_message(), purpose=PURPOSE)
    assert "ada@x.io" not in str(caught.value)


def test_a_transient_failure_of_a_write_leaves_the_outcome_unknown() -> None:
    http = RecordingHttp().answer(500, gmail_error(500, "backendError"))
    with pytest.raises(GmailTransient) as caught:
        _client(http).send(_message(), purpose=PURPOSE)
    assert caught.value.outcome_unknown is True


def test_a_transient_failure_of_a_read_does_not() -> None:
    http = RecordingHttp().answer(502, b"bad gateway")
    with pytest.raises(GmailTransient) as caught:
        _client(http).get_thread("t1", purpose="threads fallback")
    assert caught.value.outcome_unknown is False


def test_a_timeout_mid_send_is_transient_with_the_outcome_unknown() -> None:
    http = RecordingHttp().fail(TimeoutError("timed out"))
    with pytest.raises(GmailTransient) as caught:
        _client(http).send(_message(), purpose=PURPOSE)
    assert (caught.value.code, caught.value.outcome_unknown) == ("unavailable", True)


def test_a_dead_grant_is_an_auth_error_and_sends_nothing() -> None:
    tokens = Tokens()
    tokens.error = gmail_oauth.InvalidGrant("refused", code="invalid_grant")
    failures: list[GmailAuthError] = []
    http = RecordingHttp()

    with pytest.raises(GmailAuthError) as caught:
        _client(http, tokens, failures).send(_message(), purpose=PURPOSE)

    assert caught.value.code == "invalid_grant"
    assert failures == [caught.value]
    assert http.requests == []


@pytest.mark.parametrize("code", ["invalid_client", "unauthorized_client"])
def test_a_dead_client_is_an_auth_error(code: str) -> None:
    tokens = Tokens()
    tokens.error = gmail_oauth.OAuthRefused("refused", code=code)
    failures: list[GmailAuthError] = []
    with pytest.raises(GmailAuthError, match="refused"):
        _client(RecordingHttp(), tokens, failures).list_labels(purpose="labels")
    assert [failure.code for failure in failures] == [code]


@pytest.mark.parametrize("code", ["invalid_request", "http_407", "http_403", "no_access_token"])
def test_a_refusal_that_says_nothing_about_the_grant_is_transient(code: str) -> None:
    """Only ``REAUTH_CODES`` pause the mailbox; anything else is tried again later (#256)."""
    tokens = Tokens()
    tokens.error = gmail_oauth.OAuthRefused("refused", code=code)
    failures: list[GmailAuthError] = []
    http = RecordingHttp()
    with pytest.raises(GmailTransient) as caught:
        _client(http, tokens, failures).send(_message(), purpose=PURPOSE)
    assert (caught.value.code, caught.value.outcome_unknown) == (code, False)
    assert failures == []
    assert http.requests == []


def test_google_unreachable_for_the_token_is_transient_and_nothing_was_sent() -> None:
    tokens = Tokens()
    tokens.error = gmail_oauth.OAuthUnavailable("down", code="unavailable")
    failures: list[GmailAuthError] = []
    with pytest.raises(GmailTransient) as caught:
        _client(RecordingHttp(), tokens, failures).send(_message(), purpose=PURPOSE)
    assert caught.value.outcome_unknown is False
    assert failures == []


def test_a_401_that_survives_renewal_is_an_auth_error() -> None:
    failures: list[GmailAuthError] = []
    http = RecordingHttp()
    for _ in range(3):
        http.answer(401, gmail_error(401, "authError"))
    with pytest.raises(GmailAuthError):
        _client(http, failures=failures).list_labels(purpose="labels")
    assert len(failures) == 1


def test_a_403_that_is_not_a_limit_calls_the_auth_hook() -> None:
    failures: list[GmailAuthError] = []
    http = RecordingHttp().answer(403, gmail_error(403, "accessNotConfigured"))
    with pytest.raises(GmailAuthError):
        _client(http, failures=failures).profile(purpose="baseline")
    assert [failure.code for failure in failures] == ["accessNotConfigured"]


def test_a_failing_hook_does_not_hide_the_auth_error(caplog: pytest.LogCaptureFixture) -> None:
    def hook(error: GmailAuthError) -> None:
        raise RuntimeError("database is locked")

    tokens = Tokens()
    tokens.error = gmail_oauth.InvalidGrant("refused", code="invalid_grant")
    client = GmailClient(tokens, mailbox_id=3, http=RecordingHttp(), on_auth_failure=hook)
    with pytest.raises(GmailAuthError):
        client.list_labels(purpose="labels")
    assert "recording the auth failure failed" in caplog.text


# --- labels helper ----------------------------------------------------------------


def test_ensure_label_finds_an_existing_label_without_case() -> None:
    http = RecordingHttp().answer(
        200, {"labels": [{"id": "Label_4", "name": "Netkeeper/First 100", "type": "user"}]}
    )
    label = ensure_label(_client(http), "netkeeper/first 100", purpose="label for campaign 2")
    assert label.id == "Label_4"
    assert len(http.requests) == 1


def test_ensure_label_creates_a_missing_label() -> None:
    http = (
        RecordingHttp()
        .answer(200, {"labels": []})
        .answer(200, {"id": "Label_5", "name": "netkeeper/x", "type": "user"})
    )
    assert ensure_label(_client(http), "netkeeper/x", purpose="label").id == "Label_5"


def test_ensure_label_reads_back_a_label_made_in_between() -> None:
    http = (
        RecordingHttp()
        .answer(200, {"labels": []})
        .answer(409, gmail_error(409, "conflict"))
        .answer(200, {"labels": [{"id": "Label_6", "name": "netkeeper/x", "type": "user"}]})
    )
    assert ensure_label(_client(http), "netkeeper/x", purpose="label").id == "Label_6"


# --- logging ----------------------------------------------------------------------


def test_every_call_logs_its_method_and_purpose(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="netkeeper.campaigns.gmail")
    http = RecordingHttp().answer(200, {"id": "m1", "threadId": "m1"})
    _client(http).send(_message(), purpose="  send step 2 for enrollment 14 ")
    assert "gmail messages.send for mailbox 3: send step 2 for enrollment 14" in caplog.messages


@pytest.mark.parametrize(
    "purpose", ["", "   ", "reply from ada@example.com", "two\nlines", "x" * 121]
)
def test_a_purpose_unfit_to_log_is_refused_before_any_request(purpose: str) -> None:
    http = RecordingHttp()
    with pytest.raises(ValueError):
        _client(http).search("in:inbox", purpose=purpose)
    assert http.requests == []


def test_check_purpose_strips() -> None:
    assert check_purpose("  reply poll ") == "reply poll"


def test_no_address_body_or_token_reaches_any_log(caplog: pytest.LogCaptureFixture) -> None:
    """googleapiclient logs request URLs and page bodies at DEBUG; they stay out."""
    caplog.set_level(logging.DEBUG)
    http = (
        RecordingHttp()
        .answer(200, {"messages": [{"id": "a", "threadId": "a"}], "nextPageToken": "p"})
        .answer(200, {"messages": []})
        .answer(400, gmail_error(400, "invalidArgument", "Invalid To header"))
    )
    client = _client(http)
    client.search("from:ada@example.com", purpose="reply search for enrollment 7")
    with pytest.raises(GmailRejected):
        client.send(_message(), purpose=PURPOSE)

    text = caplog.text
    assert "gmail messages.list for mailbox 3" in text
    for secret in ("ada@example.com", "ada%40example.com", "access-1", "It has been a while"):
        assert secret not in text
    raw = http.requests[-1].json()["raw"]
    assert raw not in text
