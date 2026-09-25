"""netkeeper.linkedin.flight: the RSC flight grammar, read defensively (#187).

Every malformed payload is ``RouteChanged`` -- never ``KeyError``, ``ValueError``, or a
``JSONDecodeError`` -- and every error names a shape, never a value. The payloads here
are written by hand; none came from a capture.
"""

from __future__ import annotations

import pytest

from netkeeper.linkedin import flight
from netkeeper.linkedin.flight import element_props, is_element, parse_flight
from netkeeper.linkedin.voyager import RouteChanged

E = "test/flight"


def test_rows_modules_and_hints_are_told_apart() -> None:
    body = (
        b'1:I["fake-chunk",[],"TriggerButton"]\n'
        b':HL["/fake.css","style"]\n'
        b'0:["$","$L1",null,{"children":"$L2"}]\n'
        b'2:{"a":1}\n'
        b"7:null\n"
        b'15:"$Sreact.suspense"\n'
    )
    payload = parse_flight(body, endpoint=E)
    assert set(payload.rows) == {"0", "2", "7", "15"}
    assert payload.rows["7"] is None and payload.rows["15"] == "$Sreact.suspense"
    assert payload.modules == {"1": ["fake-chunk", [], "TriggerButton"]}
    assert payload.resolve("$L2") == {"a": 1}
    assert payload.resolve("$2") == {"a": 1}
    assert payload.resolve("$L1") is None  # a module, not a model row
    assert payload.resolve("$undefined") is None
    assert payload.resolve("$Sreact.suspense") is None


def test_a_text_row_is_read_by_its_byte_length_even_across_newlines() -> None:
    text = "line one\nline two é"  # 21 bytes in UTF-8
    raw = text.encode("utf-8")
    body = b"a:T" + format(len(raw), "x").encode() + b"," + raw + b'0:{"x":"$a"}\n'
    payload = parse_flight(body, endpoint=E)
    assert payload.texts == {"a": text}
    assert payload.rows == {"0": {"x": "$a"}}


def test_a_str_body_is_accepted_like_bytes() -> None:
    assert parse_flight('0:{"x":1}\n', endpoint=E).rows == {"0": {"x": 1}}


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        (b"", "empty flight payload"),
        (b"   \n", "empty flight payload"),
        (b'0{"x":1}\n', "no ':' after the row id"),
        (b"0:{not json}\n", "JSON that does not parse"),
        (b"0:<html>\n", "a model row that is not JSON"),
        (b'0:{"x":1}\n0:{"y":2}\n', "a row id appears twice"),
        (b'0:{"x":1}\n2:E{"message":"boom"}\n', "the server sent an error row"),
        (b"a:T40,short", "a text row runs past the payload"),
        (b'1:I["fake",[],"X"]\n', "a flight payload with no model rows"),
        (b"<!doctype html><html></html>", "no ':' after the row id"),
    ],
)
def test_a_malformed_payload_is_route_changed(body: bytes, detail: str) -> None:
    with pytest.raises(RouteChanged) as caught:
        parse_flight(body, endpoint=E)
    assert detail in caught.value.detail
    assert caught.value.endpoint == E


def test_a_walk_follows_references_once_and_survives_a_cycle() -> None:
    payload = parse_flight(b'0:["$L1","$L1"]\n1:{"next":"$L0","leaf":"x"}\n', endpoint=E)
    nodes = list(payload.walk(payload.rows["0"], follow=True, endpoint=E))
    assert nodes.count("x") == 1  # row 1 entered once though referenced twice
    unfollowed = list(payload.walk(payload.rows["0"], follow=False, endpoint=E))
    assert "x" not in unfollowed


def test_a_pathological_payload_is_refused_not_walked_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(flight, "MAX_NODES", 50)
    payload = parse_flight(b"0:" + str(list(range(100))).encode() + b"\n", endpoint=E)
    with pytest.raises(RouteChanged, match="a walk passed 50 nodes"):
        list(payload.nodes(endpoint=E))


def test_elements_are_four_item_lists_with_dict_props() -> None:
    assert is_element(["$", "div", None, {"a": 1}])
    assert element_props(["$", "$L3", "k", {"componentKey": "x"}]) == {"componentKey": "x"}
    assert not is_element(["$", "div", None, "props"])
    assert not is_element(["x", "div", None, {}])
    assert not is_element(["$", "div", {}])
    assert element_props({"$": 1}) is None


def test_the_max_nodes_bound_is_pinned() -> None:
    assert flight.MAX_NODES == 2_000_000
