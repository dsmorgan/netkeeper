"""React Server Components "flight" payloads, read defensively (ADR 0006, spec 9.3).

LinkedIn's ``flagship-web`` client renders pages with React Server Components. What
the page fetches -- a screen, a page of the connections list, a profile, the contact
info overlay -- comes back as a *flight* payload: rows of ``<id>:<value>``, one per
line, where ``<id>`` is a hex number and ``<value>`` is JSON, or a tag and its data:

* ``1:I["<chunk>",[],"TriggerButton"]`` -- ``I``: a client component the page imports.
  Only its id matters here, as a name other rows refer to.
* ``0:[...]``, ``4:{...}``, ``7:null``, ``15:"$Sreact.suspense"`` -- a JSON model row.
* ``1a:T3f,<63 bytes of text>`` -- ``T``: a text row, length-prefixed in hex bytes,
  which may itself contain newlines.
* ``2:E{...}`` -- ``E``: the server's own error for that row.
* ``5:R`` / ``5:X`` -- the start of a stream (``R``) or an async iterable (``X``)
  whose chunks follow as rows of the same id, and ``5:C`` -- its close (#196 item 10).
* ``:HL[...]`` and other upper-case tags -- hints about fonts, preloads, and the like.

Rendered elements are four-item lists, ``["$", type, key, props]``, and a string of
the form ``$<id>`` or ``$L<id>`` refers to row ``<id>``: the lazy reference
(``$L``) is how a page factors each card of a list out into rows of its own. Other
``$``-strings (``$undefined``, ``$Sreact.suspense``, ``$@1``) are React's own markers
and are left as they are.

This module only knows that grammar. What a connections card or a profile looks like
lives in :mod:`netkeeper.linkedin.flagship`. Everything here raises
:class:`~netkeeper.linkedin.voyager.RouteChanged` for a payload it cannot read --
never ``KeyError``, ``ValueError``, or ``json.JSONDecodeError`` -- and describes the
problem by shape, never by value: a payload is a person's data. Pure: no browser, no
database (spec 9.10).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Final

from netkeeper.linkedin.voyager import RouteChanged

#: A reference to another row: ``$1a`` or ``$L1a``. Deliberately exact, so React's
#: own markers (``$undefined``, ``$Sreact.suspense``, ``$@1``) never read as one.
_REF: Final = re.compile(r"\$L?([0-9a-f]+)")

#: The root row of every answer: the model the page renders from.
ROOT_ROW: Final = "0"

#: A string that names another row by id, as React's flight client reads one: ``$<id>``,
#: or ``$`` and one marker letter and the id -- ``L`` (lazy), ``@`` (a promise), ``Q``
#: (a map), ``W`` (a set), ``K`` (form data), ``B`` (a blob), ``F`` (a server
#: reference), ``R``/``r`` (a stream), ``X``/``x`` (an async iterable), ``h`` (a hint)
#: -- optionally followed by ``:`` and a path into that row (#207 review).
_ROW_MARKER: Final = re.compile(r"\$[@QWKBFRrXxhL]?([0-9a-f]+)(?::.*)?", re.DOTALL)

#: A row id. LinkedIn's are lower-case hex; a hint row has none.
_ROW_ID: Final = re.compile(rb"[0-9a-f]*")

#: Upper-case tags. ``I``, ``T``, and ``E`` mean something here; the rest are hints.
_TAG: Final = re.compile(rb"[A-Z]+")

#: The characters a JSON value can start with.
_JSON_START: Final = frozenset(b'[{"-0123456789tfn')

#: The most nodes one walk visits before the payload is refused as unreadable. A real
#: page of the connections list is a few tens of thousands; this bounds a pathological
#: payload rather than a real one.
MAX_NODES: Final = 2_000_000


@dataclass(frozen=True, slots=True)
class FlightPayload:
    """A parsed flight payload: the JSON rows, the component imports, and the text rows.

    ``rows`` holds every JSON model row by id. ``modules`` holds each ``I`` row's
    import (its data, unparsed beyond JSON). ``texts`` holds each ``T`` row's text.
    ``streams`` holds the id of each ``R`` or ``X`` row (a stream started) and
    ``closed`` the id of each ``C`` row (a stream closed).
    """

    rows: Mapping[str, object]
    modules: Mapping[str, object]
    texts: Mapping[str, str]
    streams: frozenset[str] = frozenset()
    closed: frozenset[str] = frozenset()

    def resolve(self, value: object) -> object | None:
        """The JSON row ``value`` refers to, or ``None`` when it is not a reference to one."""
        if not isinstance(value, str):
            return None
        match = _REF.fullmatch(value)
        if match is None:
            return None
        return self.rows.get(match.group(1))

    def walk(self, root: object, *, follow: bool, endpoint: str) -> Iterator[object]:
        """Every node under ``root``, depth first; with ``follow``, through row references too.

        Each row is entered at most once per walk, so a reference cycle ends. A walk
        past :data:`MAX_NODES` nodes raises :class:`RouteChanged`.
        """
        stack: list[object] = [root]
        entered: set[str] = set()
        visited = 0
        while stack:
            node = stack.pop()
            visited += 1
            if visited > MAX_NODES:
                raise RouteChanged(endpoint, f"a walk passed {MAX_NODES} nodes")
            yield node
            if isinstance(node, dict):
                stack.extend(reversed(list(node.values())))
            elif isinstance(node, list):
                stack.extend(reversed(node))
            elif follow and isinstance(node, str):
                match = _REF.fullmatch(node)
                if match is not None and match.group(1) not in entered:
                    target = match.group(1)
                    if target in self.rows:
                        entered.add(target)
                        stack.append(self.rows[target])

    def nodes(self, *, endpoint: str) -> Iterator[object]:
        """Every node of every JSON row, without following references (each row once)."""
        for row in self.rows.values():
            yield from self.walk(row, follow=False, endpoint=endpoint)


def is_whole(payload: FlightPayload, *, endpoint: str) -> bool:
    """Whether ``payload`` holds a whole answer: its root and every row the root reaches.

    A copy of an answer can be cut short at a row boundary and still parse (#203). The
    rows arrive in no order a reader can rely on -- a child before the row that refers
    to it, as React's ``outlineModel`` writes them, or after -- so a cut copy is told
    apart by what it lacks: row ``0``, the root, is required; every id-bearing
    reference reachable from it (:data:`_ROW_MARKER`) must name a model row, an ``I``
    import, or a ``T`` text row the copy holds; and every model row must be reachable
    from the root, since a whole answer sends none that nothing uses (#207 review).
    A stream (``R``) or async iterable (``X``) that started and never closed (no
    ``C`` row of its id) is a copy cut mid-stream, however whole its rows look
    (#196 item 10).
    """
    if ROOT_ROW not in payload.rows:
        return False
    if not payload.streams <= payload.closed:
        return False
    reached = {ROOT_ROW}
    stack = [ROOT_ROW]
    while stack:
        for node in payload.walk(payload.rows[stack.pop()], follow=False, endpoint=endpoint):
            if not isinstance(node, str):
                continue
            match = _ROW_MARKER.fullmatch(node)
            if match is None:
                continue
            row = match.group(1)
            if row in payload.rows:
                if row not in reached:
                    reached.add(row)
                    stack.append(row)
            elif row not in payload.modules and row not in payload.texts:
                return False
    return reached == set(payload.rows)


def is_element(node: object) -> bool:
    """Whether ``node`` is a rendered element: ``["$", type, key, props]`` with dict props."""
    return (
        isinstance(node, list)
        and len(node) == 4
        and node[0] == "$"
        and isinstance(node[1], str)
        and isinstance(node[3], dict)
    )


def element_props(node: object) -> dict[str, object] | None:
    """An element's props, or ``None`` when ``node`` is not an element."""
    if not is_element(node):
        return None
    assert isinstance(node, list)
    props = node[3]
    assert isinstance(props, dict)
    return props


def parse_flight(body: bytes | str, *, endpoint: str) -> FlightPayload:
    """Parse a flight payload, or raise :class:`RouteChanged` describing where it broke.

    ``endpoint`` names what was being read, for the error. An empty body, a row with
    no ``:``, a JSON row that does not parse, a text row whose length runs past the
    body, an ``E`` row, and a row id seen twice are all :class:`RouteChanged`.
    """
    data = body.encode("utf-8") if isinstance(body, str) else body
    if not data.strip():
        raise RouteChanged(endpoint, "empty flight payload")
    rows: dict[str, object] = {}
    modules: dict[str, object] = {}
    texts: dict[str, str] = {}
    streams: set[str] = set()
    closed: set[str] = set()
    seen: set[str] = set()
    position = 0
    line = 0
    end = len(data)
    while position < end:
        if data[position] in b"\r\n":
            position += 1
            continue
        line += 1
        id_match = _ROW_ID.match(data, position)
        assert id_match is not None  # [0-9a-f]* always matches, if only the empty string
        row_id = id_match.group().decode("ascii")
        colon = id_match.end()
        if colon >= end or data[colon] != ord(":"):
            raise RouteChanged(endpoint, f"row {line}: no ':' after the row id")
        cursor = colon + 1
        tag_match = _TAG.match(data, cursor)
        tag = tag_match.group().decode("ascii") if tag_match else ""
        after_tag = tag_match.end() if tag_match else cursor
        if tag == "T":
            text_start, text_end = _text_bounds(data, after_tag)
            if text_end > end:
                raise RouteChanged(endpoint, f"row {line}: a text row runs past the payload")
            texts[row_id] = data[text_start:text_end].decode("utf-8", errors="replace")
            position = text_end
            _mark(seen, row_id, endpoint, line)
            continue
        newline = data.find(b"\n", cursor)
        stop = end if newline == -1 else newline
        value = data[after_tag:stop]
        position = stop + 1
        if tag == "":
            if not value or value[0] not in _JSON_START:
                raise RouteChanged(endpoint, f"row {line}: a model row that is not JSON")
            _mark(seen, row_id, endpoint, line)
            rows[row_id] = _json(value, endpoint, line)
        elif tag == "I":
            _mark(seen, row_id, endpoint, line)
            modules[row_id] = _json(value, endpoint, line)
        elif tag == "E":
            raise RouteChanged(endpoint, f"row {line}: the server sent an error row")
        elif tag in ("R", "X"):
            streams.add(row_id)
        elif tag == "C":
            closed.add(row_id)
        # Any other tag is a hint (fonts, preloads, debug info): nothing to read.
    if not rows:
        raise RouteChanged(endpoint, "a flight payload with no model rows")
    return FlightPayload(
        rows=rows,
        modules=modules,
        texts=texts,
        streams=frozenset(streams),
        closed=frozenset(closed),
    )


def _text_bounds(data: bytes, cursor: int) -> tuple[int, int]:
    """Where a ``T`` row's text starts and ends: ``<hex length>,<text>``."""
    comma = data.find(b",", cursor)
    if comma == -1 or comma - cursor > 16:
        return cursor, len(data) + 1
    try:
        length = int(data[cursor:comma].decode("ascii"), 16)
    except ValueError:
        return cursor, len(data) + 1
    return comma + 1, comma + 1 + length


def _mark(seen: set[str], row_id: str, endpoint: str, line: int) -> None:
    if row_id in seen:
        raise RouteChanged(endpoint, f"row {line}: a row id appears twice")
    seen.add(row_id)


def _json(value: bytes, endpoint: str, line: int) -> object:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raise RouteChanged(endpoint, f"row {line}: JSON that does not parse") from None
