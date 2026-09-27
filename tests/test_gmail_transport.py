"""The transport under ``GmailClient`` never sends a write twice (#267).

``httplib2`` sends a request again, whatever its method, when the connection
drops after the request is written and before an answer (``BadStatusLine``,
``RemoteDisconnected``). For ``messages.send`` that is a second email. These
tests drive the real ``httplib2`` over a fake socket that records what is
written and answers from a script, so nothing leaves the process.
"""

import errno
import io
import json
import socket
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any

import httplib2
import pytest

from netkeeper.campaigns.gmail import (
    RESENDABLE_METHODS,
    GmailClient,
    GmailRejected,
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


@dataclass(frozen=True)
class Refuse:
    """A script entry: connecting raises ``error``, so nothing is sent."""

    error: OSError


@dataclass(frozen=True)
class FailWrite:
    """A script entry: the socket connects, and writing to it raises ``error``.

    With ``after_bytes``, the first that many bytes of the write go out before
    it fails: a connection reset part way through the body."""

    error: OSError
    after_bytes: int | None = None


def _answer(status: str, headers: str = "", body: bytes = b"", length: int | None = None) -> bytes:
    size = len(body) if length is None else length
    return f"HTTP/1.1 {status}\r\n{headers}Content-Length: {size}\r\n\r\n".encode() + body


@dataclass
class Wire:
    """Every socket the fake connections open: each gets the next script of answers,
    one per request; a socket out of answers hangs up."""

    scripts: list[list[bytes] | Refuse | FailWrite]
    connects: int = 0
    written: list[bytes] = field(default_factory=list)

    def requests(self, method: str) -> int:
        return sum(chunk.startswith(f"{method} ".encode()) for chunk in self.written)


class _Socket:
    def __init__(
        self,
        wire: Wire,
        answers: list[bytes],
        write_error: OSError | None,
        after_bytes: int | None = None,
    ) -> None:
        self._wire = wire
        self._answers = answers
        self._write_error = write_error
        self._after_bytes = after_bytes

    def sendall(self, data: bytes) -> None:
        chunk = bytes(data) if self._after_bytes is None else bytes(data)[: self._after_bytes]
        self._wire.written.append(chunk)  # counted as an attempt, then it fails
        if self._write_error is not None:
            raise self._write_error

    def makefile(self, mode: str) -> io.BytesIO:
        return io.BytesIO(self._answers.pop(0) if self._answers else DROP)

    def close(self) -> None:
        pass


def _fake_connection(wire: Wire) -> type[Any]:
    """httplib2's HTTPS connection over a :class:`_Socket` in place of TLS."""

    class Fake(httplib2.HTTPSConnectionWithTimeout):  # type: ignore[misc]
        def connect(self) -> None:
            wire.connects += 1
            script = wire.scripts.pop(0)
            if isinstance(script, Refuse):
                raise script.error
            if isinstance(script, FailWrite):
                self.sock = _Socket(wire, [], script.error, script.after_bytes)
            else:
                self.sock = _Socket(wire, script, None)

    return Fake


def _write_once(wire: Wire) -> WriteOnceHttp:
    class Fake(WriteOnceMixin, _fake_connection(wire)):  # type: ignore[misc]
        """The production mixin over the fake, in the order ``WriteOnceConnection`` uses."""

    return WriteOnceHttp(timeout=1.0, connection_type=Fake)


def _client(wire: Wire) -> GmailClient:
    return GmailClient(lambda: "access-1", mailbox_id=3, http=_write_once(wire))


def _message() -> EmailMessage:
    message = EmailMessage()
    message["To"] = "ada@example.com"
    message["Subject"] = "Catching up"
    message.set_content("Hello Ada.\n")
    return message


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
    with pytest.raises(GmailTransient) as caught:
        _client(wire).send(_message(), purpose="send step 1 for enrollment 7")

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


# --- review of #270 ------------------------------------------------------------------


@pytest.mark.parametrize("code", [errno.ENETUNREACH, errno.EADDRNOTAVAIL, errno.ECONNRESET])
def test_a_post_whose_write_fails_is_attempted_once(code: int) -> None:
    """httplib2 answers ENETUNREACH and EADDRNOTAVAIL mid-write with ``continue``;
    without the conversion to SentWithoutAnswer the mixin would reconnect and
    write the request again on the second connection."""
    wire = Wire(scripts=[FailWrite(OSError(code, "down")), [_ok({"id": "m1"})]])
    with pytest.raises(SentWithoutAnswer):
        _write_once(wire).request(URL, "POST", body="{}")
    assert wire.requests("POST") == 1


def test_a_send_whose_write_fails_is_transient_with_the_outcome_unknown() -> None:
    wire = Wire(scripts=[FailWrite(OSError(errno.ENETUNREACH, "down")), [_ok({"id": "m1"})]])
    with pytest.raises(GmailTransient) as caught:
        _client(wire).send(_message(), purpose="send step 1 for enrollment 7")
    assert (caught.value.code, caught.value.outcome_unknown) == ("unavailable", True)
    assert wire.requests("POST") == 1


def test_a_send_whose_answer_is_cut_short_is_transient_with_the_outcome_unknown() -> None:
    """A 200 that promises more body than arrives: IncompleteRead, never a raw exception."""
    cut = _answer("200 OK", "Content-Type: application/json\r\n", b'{"id": "m', length=40)
    wire = Wire(scripts=[[cut], [_ok({"id": "m1", "threadId": "m1"})]])
    with pytest.raises(GmailTransient) as caught:
        _client(wire).send(_message(), purpose="send step 1 for enrollment 7")
    assert (caught.value.code, caught.value.outcome_unknown) == ("unavailable", True)
    assert wire.requests("POST") == 1


def test_a_read_whose_answer_is_cut_short_is_transient() -> None:
    cut = _answer("200 OK", "Content-Type: application/json\r\n", b'{"id": "m', length=40)
    wire = Wire(scripts=[[cut]])
    with pytest.raises(GmailTransient) as caught:
        _client(wire).get_message("m1", purpose="read message 1")
    assert (caught.value.code, caught.value.outcome_unknown) == ("unavailable", False)


@pytest.mark.parametrize("status", ["308 Permanent Redirect", "307 Temporary Redirect"])
def test_a_redirected_post_is_not_followed(status: str) -> None:
    moved = _answer(status, f"Location: {URL}?again=1\r\n")
    wire = Wire(scripts=[[moved], [_ok({"id": "m1"})]])
    response, _ = _write_once(wire).request(URL, "POST", body="{}")
    assert response.status == int(status.split()[0])
    assert (wire.requests("POST"), wire.connects) == (1, 1)


def test_a_redirect_to_plain_http_is_not_followed() -> None:
    """Following it would leave WriteOnceHttp's connections for plain httplib2's."""
    moved = _answer("308 Permanent Redirect", "Location: http://gmail.example.com/send\r\n")
    wire = Wire(scripts=[[moved]])
    response, _ = _write_once(wire).request(URL, "POST", body="{}")
    assert response.status == 308
    assert wire.requests("POST") == 1


def test_a_redirected_send_is_a_refusal_sent_once() -> None:
    moved = _answer("308 Permanent Redirect", f"Location: {URL}?again=1\r\n")
    wire = Wire(scripts=[[moved], [_ok({"id": "m1", "threadId": "m1"})]])
    with pytest.raises(GmailRejected) as caught:
        _client(wire).send(_message(), purpose="send step 1 for enrollment 7")
    assert caught.value.code == "http_308"
    assert wire.requests("POST") == 1


def test_a_refused_connection_sent_nothing() -> None:
    wire = Wire(scripts=[Refuse(ConnectionRefusedError(errno.ECONNREFUSED, "refused"))])
    with pytest.raises(GmailTransient) as caught:
        _client(wire).send(_message(), purpose="send step 1 for enrollment 7")
    assert (caught.value.code, caught.value.outcome_unknown) == ("unavailable", False)
    assert wire.requests("POST") == 0


def test_a_name_that_does_not_resolve_sent_nothing() -> None:
    wire = Wire(scripts=[Refuse(socket.gaierror(socket.EAI_NONAME, "unknown"))])
    with pytest.raises(GmailTransient) as caught:
        _client(wire).send(_message(), purpose="send step 1 for enrollment 7")
    assert caught.value.outcome_unknown is False
    assert wire.requests("POST") == 0


# --- verification of #270, required in P3-07 (#269) ----------------------------------


def test_a_send_answered_with_a_garbage_status_line_is_sent_once() -> None:
    """``BadStatusLine`` is an ``HTTPException``, not an ``OSError``: a mixin whose
    ``getresponse`` caught only ``OSError`` would let httplib2 send it again."""
    wire = Wire(scripts=[[b"GARBAGE\r\n\r\n"], [_ok({"id": "m1", "threadId": "m1"})]])
    with pytest.raises(GmailTransient) as caught:
        _client(wire).send(_message(), purpose="send step 1 for enrollment 7")
    assert (caught.value.code, caught.value.outcome_unknown) == ("unavailable", True)
    assert wire.requests("POST") == 1
    assert wire.connects == 1


def test_a_send_reset_part_way_through_its_body_is_sent_once() -> None:
    reset = FailWrite(OSError(errno.ECONNRESET, "reset by peer"), after_bytes=200)
    wire = Wire(scripts=[reset, [_ok({"id": "m1", "threadId": "m1"})]])
    with pytest.raises(GmailTransient) as caught:
        _client(wire).send(_message(), purpose="send step 1 for enrollment 7")
    assert (caught.value.code, caught.value.outcome_unknown) == ("unavailable", True)
    assert wire.requests("POST") == 1
    assert len(wire.written[0]) == 200  # the body was cut off, not sent whole


def test_a_send_whose_answer_does_not_decompress_has_the_outcome_unknown() -> None:
    """Gmail answered, so the message went; the answer is unreadable. Never a raw
    ``FailedToDecompressContent``, and never a known outcome."""
    broken = _answer(
        "200 OK", "Content-Type: application/json\r\nContent-Encoding: gzip\r\n", b"not gzip"
    )
    wire = Wire(scripts=[[broken], [_ok({"id": "m1", "threadId": "m1"})]])
    with pytest.raises(GmailTransient) as caught:
        _client(wire).send(_message(), purpose="send step 1 for enrollment 7")
    assert (caught.value.code, caught.value.outcome_unknown) == ("unavailable", True)
    assert wire.requests("POST") == 1
