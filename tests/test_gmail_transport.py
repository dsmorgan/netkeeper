"""The transport under ``GmailClient`` never sends a write twice (#267).

``httplib2`` sends a request again, whatever its method, when the connection
drops after the request is written and before an answer (``BadStatusLine``,
``RemoteDisconnected``). For ``messages.send`` that is a second email. These
tests drive the real ``httplib2`` over a fake socket that records what is
written and answers from a script, so nothing leaves the process.
"""

import io
import json
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any

import httplib2
import pytest

from netkeeper.campaigns.gmail import (
    RESENDABLE_METHODS,
    GmailClient,
    GmailTransient,
    SentWithoutAnswer,
    WriteOnceHttp,
    WriteOnceMixin,
)

URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
DROP = b""  # the server hangs up without a status line: RemoteDisconnected


def _ok(body: Any) -> bytes:
    raw = json.dumps(body).encode()
    head = (
        f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(raw)}\r\n\r\n"
    )
    return head.encode() + raw


@dataclass
class Wire:
    """Every socket the fake connections open: each gets the next script of answers,
    one per request; a socket out of answers hangs up."""

    scripts: list[list[bytes]]
    connects: int = 0
    written: list[bytes] = field(default_factory=list)

    def requests(self, method: str) -> int:
        return sum(chunk.startswith(f"{method} ".encode()) for chunk in self.written)


class _Socket:
    def __init__(self, wire: Wire, answers: list[bytes]) -> None:
        self._wire = wire
        self._answers = answers

    def sendall(self, data: bytes) -> None:
        self._wire.written.append(bytes(data))

    def makefile(self, mode: str) -> io.BytesIO:
        return io.BytesIO(self._answers.pop(0) if self._answers else DROP)

    def close(self) -> None:
        pass


def _fake_connection(wire: Wire) -> type[Any]:
    """httplib2's HTTPS connection over a :class:`_Socket` in place of TLS."""

    class Fake(httplib2.HTTPSConnectionWithTimeout):  # type: ignore[misc]
        def connect(self) -> None:
            wire.connects += 1
            self.sock = _Socket(wire, wire.scripts.pop(0))

    return Fake


def _write_once(wire: Wire) -> WriteOnceHttp:
    class Fake(WriteOnceMixin, _fake_connection(wire)):  # type: ignore[misc]
        """The production mixin over the fake, in the order ``WriteOnceConnection`` uses."""

    return WriteOnceHttp(timeout=1.0, connection_type=Fake)


def test_plain_httplib2_sends_a_dropped_post_twice() -> None:
    """The bug, pinned: if httplib2 stops resending, this fails and the fix can go."""
    wire = Wire(scripts=[[DROP], [_ok({"id": "m1"})]])
    http = httplib2.Http(timeout=1.0)
    fake = _fake_connection(wire)

    response, _ = http.request(URL, "POST", body="{}", connection_type=fake)

    assert response.status == 200
    assert wire.requests("POST") == 2


def test_a_post_that_drops_after_it_is_written_is_not_sent_again() -> None:
    wire = Wire(scripts=[[DROP], [_ok({"id": "m1"})]])
    with pytest.raises(SentWithoutAnswer):
        _write_once(wire).request(URL, "POST", body="{}")
    assert wire.requests("POST") == 1
    assert wire.connects == 1


def test_a_send_that_drops_is_transient_with_the_outcome_unknown() -> None:
    wire = Wire(scripts=[[DROP], [_ok({"id": "m1", "threadId": "m1"})]])
    client = GmailClient(lambda: "access-1", mailbox_id=3, http=_write_once(wire))
    message = EmailMessage()
    message["To"] = "ada@example.com"
    message["Subject"] = "Catching up"
    message.set_content("Hello Ada.\n")

    with pytest.raises(GmailTransient) as caught:
        client.send(message, purpose="send step 1 for enrollment 7")

    assert (caught.value.code, caught.value.outcome_unknown) == ("unavailable", True)
    assert wire.requests("POST") == 1


def test_a_get_on_a_connection_gone_stale_is_still_sent_again() -> None:
    """Reads keep httplib2's retry: a kept-alive socket the server closed is common."""
    wire = Wire(scripts=[[_ok({"n": 1})], [_ok({"n": 2})]])
    http = _write_once(wire)

    http.request(URL, "GET")
    response, content = http.request(URL, "GET")  # socket 1 is out of answers: it drops

    assert (response.status, json.loads(content)) == (200, {"n": 2})
    assert (wire.requests("GET"), wire.connects) == (3, 2)


def test_a_write_after_a_read_goes_on_a_fresh_connection() -> None:
    """So a kept-alive socket the server closed while idle cannot fail a send."""
    wire = Wire(scripts=[[_ok({}), _ok({"unused": True})], [_ok({"id": "m1"})]])
    http = _write_once(wire)

    http.request(URL, "GET")
    response, content = http.request(URL, "POST", body="{}")

    assert (response.status, json.loads(content)) == (200, {"id": "m1"})
    assert wire.connects == 2


def test_a_write_on_a_new_connection_connects_once() -> None:
    wire = Wire(scripts=[[_ok({"id": "m1"})]])
    response, _ = _write_once(wire).request(URL, "POST", body="{}")
    assert (response.status, wire.connects, wire.requests("POST")) == (200, 1, 1)


def test_only_idempotent_methods_are_resendable() -> None:
    assert frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}) == RESENDABLE_METHODS
    assert "POST" not in RESENDABLE_METHODS and "PATCH" not in RESENDABLE_METHODS


def test_the_client_sends_through_the_write_once_transport_by_default() -> None:
    client = GmailClient(lambda: "access-1", mailbox_id=3, timeout_s=7.0)
    transport = client._service._http.http  # AuthorizedHttp over the transport
    assert isinstance(transport, WriteOnceHttp)
    assert transport.timeout == 7.0
