"""netkeeper.linkedin.flight: the RSC flight grammar, read defensively (#187).

Every malformed payload is ``RouteChanged`` -- never ``KeyError``, ``ValueError``, or a
``JSONDecodeError`` -- and every error names a shape, never a value. The payloads here
are written by hand; none came from a capture.
"""

from __future__ import annotations

import pytest

from netkeeper.linkedin import flight
from netkeeper.linkedin.flight import element_props, is_element, is_whole, parse_flight
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


# --- whole or cut short (#203, #207 review) ------------------------------------------------


def _whole(body: bytes) -> bool:
    return is_whole(parse_flight(body, endpoint="t"), endpoint="t")


#: A small answer written child first, as React's ``outlineModel`` orders rows and the
#: fixtures write them: every row the root reaches arrives before the root.
CHILD_FIRST = (
    b'1:I["chunk",[],"TriggerButton"]\n'
    b"2:T5,hello\n"
    b'a:["$","span",null,{"children":["$2"]}]\n'
    b'b:{"then":"$@c"}\n'
    b'c:["$","p",null,{"children":["$La"]}]\n'
    b'3:["$","div",null,{"children":["$Lc","$b:then"]}]\n'
    b'0:["$","$L1",null,{"children":"$L3","x":"$undefined","y":"$Sreact.suspense","z":"$-0"}]\n'
)


def test_a_whole_answer_is_whole() -> None:
    assert _whole(CHILD_FIRST)


def test_a_root_first_answer_is_whole_too() -> None:
    lines = CHILD_FIRST.splitlines(keepends=True)
    assert _whole(lines[-1] + b"".join(lines[:-1]))


def _prefixes(body: bytes) -> list[bytes]:
    lines = body.splitlines(keepends=True)
    return [b"".join(lines[:cut]) for cut in range(1, len(lines))]


@pytest.mark.parametrize("root_first", [False, True])
def test_every_cut_at_a_row_boundary_is_not_whole(root_first: bool) -> None:
    """Child first, a cut has no root; root first, a cut names a row it never got."""
    lines = CHILD_FIRST.splitlines(keepends=True)
    body = lines[-1] + b"".join(lines[:-1]) if root_first else CHILD_FIRST
    judged = []
    for prefix in _prefixes(body):
        try:
            judged.append(_whole(prefix))
        except RouteChanged:
            judged.append(False)  # no model row at all
    assert judged and not any(judged)


def test_no_root_is_not_whole() -> None:
    assert not _whole(b'1:["$","div",null,{}]\n')


@pytest.mark.parametrize(
    "marker",
    [
        "$L9",
        "$9",
        "$@9",
        "$Q9",
        "$W9",
        "$K9",
        "$B9",
        "$F9",
        "$R9",
        "$r9",
        "$X9",
        "$x9",
        "$h9",
        "$9:props:children",
    ],
)
def test_every_id_bearing_marker_to_a_missing_row_is_not_whole(marker: str) -> None:
    assert not _whole(b'0:["$","div",null,{"children":"' + marker.encode() + b'"}]\n')
    assert _whole(b'9:{"x":1}\n0:["$","div",null,{"children":"' + marker.encode() + b'"}]\n')


@pytest.mark.parametrize("start", [b"R", b"X"])
def test_a_stream_cut_after_its_first_chunk_is_not_whole(start: bytes) -> None:
    """#196 item 10: an ``$R`` stream (or ``$X`` async iterable) cut after its first
    chunk still has its root, every row the root reaches, and no orphan. Only the
    missing ``C`` row says it was cut."""
    root = b'0:["$","div",null,{"children":"$' + start + b'9"}]\n'
    started = b"9:" + start + b"\n"
    chunk = b'9:{"chunk":1}\n'
    assert not _whole(started + chunk + root)
    assert _whole(started + chunk + b"9:C\n" + root)
    assert _whole(started + chunk + b'9:C{"last":true}\n' + root)


def test_a_close_for_another_stream_does_not_close_this_one() -> None:
    root = b'0:["$","div",null,{"children":"$R9"}]\n'
    assert not _whole(b'9:R\n9:{"chunk":1}\n8:C\n' + root)


def test_an_orphan_row_is_not_whole() -> None:
    """A row nothing reaches from the root: a whole answer sends none."""
    assert not _whole(b'5:{"stray":true}\n0:["$","div",null,{}]\n')


def test_the_root_row_and_the_markers_are_pinned() -> None:
    assert flight.ROOT_ROW == "0"
    for text in ("$undefined", "$Sreact.suspense", "$-0", "$NaN", "$Infinity", "$D2026", "$n12"):
        assert flight._ROW_MARKER.fullmatch(text) is None
