"""The one test that talks to real Gmail (#262, P3-02). Skipped unless asked for.

It runs only with ``NETKEEPER_GMAIL_TESTS=1``, which CI never sets. It uses the
mailbox ``netkeeper gmail login`` connected, reading the OAuth client and token
from the real Keychain, and checks what the fake has to match: a follow-up with
the right headers joins the thread, a draft lands in it, search and history see
the messages. It sends two short messages from the account to itself and
leaves them, and a draft, under the label ``netkeeper/live-test``. Run it by
hand as ``docs/gmail-setup.md`` ("Checking the client against your account")
describes.
"""

import os
import time
import uuid
from email.message import EmailMessage
from email.utils import make_msgid

import keyring
import keyring.core
import pytest

from netkeeper.campaigns import gmail_oauth
from netkeeper.campaigns.gmail import GmailClient, ensure_label
from netkeeper.services import keychain
from netkeeper.services import mailboxes as service

LIVE_ENV = "NETKEEPER_GMAIL_TESTS"

pytestmark = pytest.mark.skipif(
    os.environ.get(LIVE_ENV) != "1", reason=f"talks to real Gmail; set {LIVE_ENV}=1 to run"
)


@pytest.fixture
def live() -> GmailClient:
    """A client for the connected mailbox, over the real Keychain and Google."""
    keyring.set_keyring(keyring.core.init_backend())  # conftest restores its own afterwards
    user_id = int(os.environ.get("NETKEEPER_GMAIL_TEST_USER", "1"))
    mailbox_id = int(os.environ.get("NETKEEPER_GMAIL_TEST_MAILBOX", "1"))
    client = service.load_client(user_id)
    token = keychain.get_secret(user_id, service.token_name(mailbox_id))
    if client is None or token is None:
        pytest.fail(
            f"no OAuth client or token in the Keychain for user {user_id}, mailbox"
            f" {mailbox_id}; run `netkeeper gmail login` first"
        )

    def refresh() -> str:
        return gmail_oauth.refresh_access_token(
            client, token, endpoints=gmail_oauth.GOOGLE_ENDPOINTS
        )

    return GmailClient(refresh, mailbox_id=mailbox_id)


def _mail(me: str, subject: str, *, cites: str | None = None) -> EmailMessage:
    message = EmailMessage()
    message["From"] = me
    message["To"] = me
    message["Subject"] = subject
    message["Message-ID"] = make_msgid(domain="netkeeper.invalid")
    if cites:
        message["In-Reply-To"] = cites
        message["References"] = cites
    message.set_content("netkeeper's live Gmail test. Safe to delete.\n")
    return message


def test_the_client_against_a_real_mailbox(live: GmailClient) -> None:
    purpose = "live test"
    profile = live.profile(purpose=purpose)
    label = ensure_label(live, "netkeeper/live-test", purpose=purpose)
    subject = f"netkeeper live test {uuid.uuid4().hex[:8]}"

    first_mail = _mail(profile.email, subject)
    first = live.send(first_mail, purpose=purpose)
    live.modify_labels(first.id, add=[label.id], purpose=purpose)
    follow_up = live.send(
        _mail(profile.email, f"Re: {subject}", cites=first_mail["Message-ID"]),
        thread_id=first.thread_id,
        purpose=purpose,
    )
    draft = live.create_draft(
        _mail(profile.email, f"Re: {subject}", cites=first_mail["Message-ID"]),
        thread_id=first.thread_id,
        purpose=purpose,
    )

    assert follow_up.thread_id == first.thread_id, "Gmail did not thread the follow-up"
    assert draft.message.thread_id == first.thread_id
    assert live.get_draft(draft.id, purpose=purpose) == draft
    thread = live.get_thread(first.thread_id, purpose=purpose)
    assert {first.id, follow_up.id, draft.message.id} <= {m.id for m in thread.messages}
    message = live.get_message(first.id, purpose=purpose)
    assert label.id in message.label_ids and "SENT" in message.label_ids
    assert message.header("Message-ID") == first_mail["Message-ID"]

    msgid = first_mail["Message-ID"].strip("<>")
    deadline = time.monotonic() + 20
    while True:  # search and history can trail a send by a moment
        found = live.search(f"rfc822msgid:{msgid}", purpose=purpose)
        history = live.history(profile.history_id, purpose=purpose)
        added = {ref.id for ref in history.messages_added}
        if (first in found and {first.id, follow_up.id} <= added) or time.monotonic() > deadline:
            break
        time.sleep(2)
    assert first in found
    assert {first.id, follow_up.id} <= added
    assert history.history_id > profile.history_id
