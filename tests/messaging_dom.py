"""A LinkedIn profile with a message bubble, without a browser: the prefill's offline fakes.

:class:`MessagingSite` is a :class:`browser_fakes.FakeContext` whose tabs hold a small
DOM parsed from :mod:`messaging_pages`' invented HTML (the captured layout, invented
people). A tab answers the slice of Playwright's locator API that
:meth:`netkeeper.linkedin.browser.BrowserRun.click_message` and
:meth:`~netkeeper.linkedin.browser.BrowserRun.type_into_composer` use: roles and
accessible names, a few CSS selectors, ``filter``, ``and_``, ``first``/``last``/``nth``,
``count``, ``get_attribute``, ``inner_text`` and one ``click``. Locators are lazy, as
Playwright's are, so a test can change the page between two checks.

Clicking a Message link that the test armed makes the page "load" the compose option
(and the thread request) and open the bubble. The keyboard is the safety net: it
**fails the test** on anything but one printable ASCII character other than a space
for ``type``, on a control character for ``insert_text``, and on any key but
``Shift+Enter`` for ``press``, and it has no ``down`` or ``up``. Every key is recorded,
and ``after_key`` lets a test change the page after key *k*.

Nothing here came from a capture, and nothing here makes a request.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from browser_fakes import FakeContext, FakeMouse, FakePage
from flagship_site import FakeRequest, FakeResponse
from messaging_pages import (
    HOST,
    Member,
    compose_option_answer,
    compose_option_url,
    conversation_urn,
    existing_bubble_html,
    messages_sync_url,
    never_messaged_bubble_html,
    profile_message_controls_html,
)

from netkeeper.linkedin.browser import PageLike

VOID = frozenset({"br", "input", "img", "meta", "link", "hr", "svg"})


# --- the DOM ---------------------------------------------------------------------------


@dataclass(eq=False)
class Element:
    tag: str
    attrs: dict[str, str]
    parent: Element | None = None
    children: list[Element | str] = field(default_factory=list)

    def elements(self) -> Iterator[Element]:
        """Every element below this one, in document order (not this one)."""
        for child in self.children:
            if isinstance(child, Element):
                yield child
                yield from child.elements()

    def ancestors(self) -> Iterator[Element]:
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def contains(self, other: Element) -> bool:
        return any(a is self for a in other.ancestors())

    def text_content(self) -> str:
        """The DOM's ``textContent``: every text node, a ``<br>`` adding nothing."""
        return "".join(
            child if isinstance(child, str) else child.text_content() for child in self.children
        )

    def text(self) -> str:
        out: list[str] = []
        for child in self.children:
            if isinstance(child, str):
                out.append(child)
            elif child.tag == "br":
                out.append("\n")
            else:
                out.append(child.text())
        return "".join(out)


class _Parser(HTMLParser):
    def __init__(self, root: Element) -> None:
        super().__init__(convert_charrefs=True)
        self.stack = [root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = Element(tag, {k: v or "" for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(element)
        if tag not in VOID:
            self.stack.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = Element(tag, {k: v or "" for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(element)

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID:
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def parse_into(parent: Element, html: str) -> None:
    _Parser(parent).feed(html)


def role_of(element: Element) -> str | None:
    explicit = element.attrs.get("role")
    if explicit:
        return explicit
    tag = element.tag
    if tag == "a" and "href" in element.attrs:
        return "link"
    if tag == "button":
        return "button"
    if re.fullmatch(r"h[1-6]", tag):
        return "heading"
    if tag == "textarea" or (tag == "input" and element.attrs.get("type", "text") == "text"):
        return "textbox"
    return None


def name_of(element: Element, root: Element, *, include_hidden: bool = False) -> str:
    """The accessible name, as Playwright computes it: with ``include_hidden``, the text of
    ``aria-hidden`` descendants counts too."""
    labelledby = element.attrs.get("aria-labelledby")
    if labelledby:
        for other in root.elements():
            if other.attrs.get("id") == labelledby:
                return " ".join(other.text().split())
    label = element.attrs.get("aria-label")
    if label is not None:
        return " ".join(label.split())
    ident = element.attrs.get("id")
    if ident:
        for other in root.elements():
            if other.tag == "label" and other.attrs.get("for") == ident:
                return " ".join(other.text().split())
    if role_of(element) in {"link", "button", "heading"}:
        text = element.text() if include_hidden else _visible_text(element)
        return " ".join(text.split())
    return ""


def _visible_text(element: Element) -> str:
    out: list[str] = []
    for child in element.children:
        if isinstance(child, str):
            out.append(child)
        elif child.attrs.get("aria-hidden") != "true":
            out.append(_visible_text(child))
    return "".join(out)


def hidden(element: Element) -> bool:
    for node in (element, *element.ancestors()):
        style = node.attrs.get("style", "").replace(" ", "")
        if "hidden" in node.attrs or "display:none" in style:
            return True
    return False


def box_of(element: Element) -> dict[str, float] | None:
    """An element's box, from its invented ``data-box="x,y,width,height"`` (#444)."""
    return _box_attr(element, "data-box")


#: The fake DOM's pseudo-elements (#475): an element's ``data-before-box`` or
#: ``data-after-box`` draws a ``::before`` or ``::after`` there, as CSS ``content`` does.
PSEUDO_BOXES = (("before", "data-before-box"), ("after", "data-after-box"))
#: A pseudo-element's ``backendNodeId``: past every element's, as Chrome gives it its own.
_PSEUDO_ID_BASE = 100_000


def _pseudo_id(index: int, kind: str) -> int:
    return _PSEUDO_ID_BASE + 2 * index + (1 if kind == "after" else 0)


def _box_attr(element: Element, attr: str) -> dict[str, float] | None:
    raw = element.attrs.get(attr)
    if raw is None or hidden(element):
        return None
    x, y, width, height = (float(v) for v in raw.split(","))
    return {"x": x, "y": y, "width": width, "height": height}


class GeometrySession:
    """A CDP session for :meth:`BrowserRun._read_click_geometry`: its read-only methods,
    over the fake DOM. Node ids are positions in document order (from 1), for
    ``nodeId`` and ``backendNodeId`` alike. Boxes are the invented ``data-box`` ones.
    The hit at a point is the last element in document order whose box holds it (a
    fixed overlay comes after what it covers), as ``elementFromPoint`` answers; an
    element's ``data-before-box``/``data-after-box`` pseudo-elements (#475) are hit on
    top of it, with their own ``backendNodeId``, and listed in its ``pseudoElements``;
    ``data-pointer-events="none"`` is skipped. A point outside the viewport, or one over
    no box, raises, as Chrome does. Boxes and quads are viewport coordinates, as Chrome's
    are; ``DOM.getNodeForLocation`` takes a document point and subtracts the site's
    ``scroll_offset``, as Chrome does (#470)."""

    def __init__(self, tab: MessagingTab, viewport: tuple[float, float]) -> None:
        self.tab = tab
        self.viewport = viewport
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.detached = False

    def _all(self) -> list[Element]:
        return list(self.tab.document.elements())

    def _describe(self, element: Element, order: list[Element]) -> dict[str, Any]:
        children = [c for c in element.children if isinstance(c, Element)]
        index = order.index(element)
        described: dict[str, Any] = {
            "nodeId": index + 1,
            "backendNodeId": index + 1,
            "nodeName": element.tag.upper(),
            "attributes": [part for pair in element.attrs.items() for part in pair],
            "children": [self._describe(c, order) for c in children],
        }
        # As Chrome's describeNode does (#475): a node's pseudo-elements beside its children.
        pseudo = [
            {
                "nodeId": _pseudo_id(index, kind),
                "backendNodeId": _pseudo_id(index, kind),
                "nodeName": f"::{kind}",
                "pseudoType": kind,
            }
            for kind, attr in PSEUDO_BOXES
            if attr in element.attrs
        ]
        if pseudo:
            described["pseudoElements"] = pseudo
        return described

    async def send(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        self.sent.append((method, dict(params or {})))
        error = self.tab.site.geometry_error
        if error is not None:
            raise error
        if self.tab.site.geometry_hangs:
            await asyncio.sleep(3600)  # a renderer that never answers
        params = dict(params or {})
        order = self._all()
        scroll_x, scroll_y = self.tab.site.scroll_offset
        if method == "Page.getLayoutMetrics":
            width, height = self.viewport
            ratio = self.tab.site.device_pixel_ratio
            return {
                "cssLayoutViewport": {
                    "clientWidth": width,
                    "clientHeight": height,
                    "pageX": scroll_x,
                    "pageY": scroll_y,
                },
                # Deprecated, in device pixels, as Chrome still sends it (#473).
                "layoutViewport": {"clientWidth": width * ratio, "clientHeight": height * ratio},
                "cssVisualViewport": {"zoom": self.tab.site.zoom, "scale": 1.0},
            }
        if method == "DOM.getDocument":
            if params == {"depth": -1}:
                # The whole tree (#473's detail read): the document node, then every
                # element, with the same ids the other methods use.
                children = [c for c in self.tab.document.children if isinstance(c, Element)]
                return {
                    "root": {
                        "nodeId": 0,
                        "backendNodeId": 0,
                        "nodeName": "#document",
                        "children": [self._describe(c, order) for c in children],
                    }
                }
            assert params == {"depth": 0}
            return {"root": {"nodeId": 0}}
        if method == "DOM.querySelectorAll":
            assert params["nodeId"] == 0
            found = select(params["selector"], self.tab.document, self.tab)
            return {"nodeIds": [order.index(e) + 1 for e in found]}
        if method == "DOM.describeNode":
            assert params["depth"] == -1
            return {"node": self._describe(order[params["nodeId"] - 1], order)}
        if method == "DOM.getBoxModel":
            box = box_of(order[params["nodeId"] - 1])
            if box is None:
                raise RuntimeError("Could not compute box model.")
            x, y, w, h = box["x"], box["y"], box["width"], box["height"]
            return {"model": {"border": [x, y, x + w, y, x + w, y + h, x, y + h]}}
        if method == "DOM.getContentQuads":
            box = box_of(order[params["nodeId"] - 1])
            if box is None:
                raise RuntimeError("Could not compute content quads.")
            x, y, w, h = box["x"], box["y"], box["width"], box["height"]
            return {"quads": [[x, y, x + w, y, x + w, y + h, x, y + h]]}
        if method == "DOM.getNodeForLocation":
            assert params.get("ignorePointerEventsNone") is True
            # As Chrome does (#470): a document point, the scroll offset subtracted.
            x, y = params["x"] - scroll_x, params["y"] - scroll_y
            width, height = self.viewport
            if not (0 <= x < width and 0 <= y < height):
                raise RuntimeError("No node found at given location")
            hit: int | None = None
            for index, element in enumerate(order):
                if element.attrs.get("data-pointer-events") == "none":
                    continue
                # The element, then its ::before, then its ::after: the later one on top.
                layers = [(index + 1, box_of(element))] + [
                    (_pseudo_id(index, kind), _box_attr(element, attr))
                    for kind, attr in PSEUDO_BOXES
                ]
                for node_id, box in layers:
                    if box is None:
                        continue
                    inside_x = box["x"] <= x < box["x"] + box["width"]
                    if inside_x and box["y"] <= y < box["y"] + box["height"]:
                        hit = node_id
            if hit is None:
                raise RuntimeError("No node found at given location")
            return {"backendNodeId": hit, "frameId": "main"}
        raise AssertionError(f"the geometry session never sends {method}")

    def on(self, event: str, handler: Callable[[Any], None]) -> None:
        raise AssertionError("the geometry session never listens")

    async def detach(self) -> None:
        self.detached = True


# --- a tiny selector engine --------------------------------------------------------------


_SIMPLE = re.compile(r'\*|[a-z][a-z0-9]*|\[[a-z-]+="(?:[^"\\]|\\.)*"\]|:focus|:scope|:not\([^)]*\)')


def _matches_simple(part: str, element: Element, page: MessagingTab, scope: Element) -> bool:
    if part == "*":
        return True
    if part == ":focus":
        return page.focused is element
    if part == ":scope":
        return element is scope
    if part.startswith(":not("):
        return not _matches_compound(part[5:-1], element, page, scope)
    if part.startswith("["):
        name, _, value = part[1:-1].partition("=")
        wanted = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        return element.attrs.get(name) == wanted
    return element.tag == part


def _matches_compound(compound: str, element: Element, page: MessagingTab, scope: Element) -> bool:
    parts = _SIMPLE.findall(compound)
    assert "".join(parts) == compound, f"the fake can't read selector {compound!r}"
    return all(_matches_simple(p, element, page, scope) for p in parts)


def select(selector: str, scope: Element, page: MessagingTab) -> list[Element]:
    """``selector`` under ``scope``: ``,`` lists, descendant and ``>`` child combinators."""
    found: list[Element] = []
    for branch in selector.split(","):
        tokens = branch.replace(">", " > ").split()
        for element in scope.elements():
            if _matches_chain(tokens, element, page, scope):
                found.append(element)
    order = {id(e): i for i, e in enumerate(scope.elements())}
    unique = {id(e): e for e in found}
    return sorted(unique.values(), key=lambda e: order[id(e)])


def _matches_chain(tokens: list[str], element: Element, page: MessagingTab, scope: Element) -> bool:
    if not tokens:
        return True
    if not _matches_compound(tokens[-1], element, page, scope):
        return False
    rest = tokens[:-1]
    if not rest:
        return True
    if rest[-1] == ">":
        parent = element.parent
        if parent is None:
            return False
        if rest[:-1] == [":scope"]:
            return parent is scope
        return _matches_chain(rest[:-1], parent, page, scope)
    return any(
        _matches_chain(rest, ancestor, page, scope)
        for ancestor in element.ancestors()
        if ancestor is not scope or rest == [":scope"]
    )


# --- locators ----------------------------------------------------------------------------

_XPATH_NEAREST = re.compile(r"xpath=ancestor::\*\[\.//h2\[normalize-space\(\)='([^']*)'\]\]\[1\]")

Step = Callable[[list[Element]], list[Element]]


class FakeLocator:
    """A lazy locator: a chain of steps from the document, evaluated at each read."""

    def __init__(self, page: MessagingTab, steps: tuple[Step, ...] = (), desc: str = "") -> None:
        self._page = page
        self._steps = steps
        self.desc = desc

    def resolve(self, roots: list[Element] | None = None) -> list[Element]:
        current = [self._page.document] if roots is None else roots
        for step in self._steps:
            current = step(current)
        return current

    def _then(self, step: Step, desc: str = "") -> FakeLocator:
        return FakeLocator(self._page, (*self._steps, step), f"{self.desc}{desc}")

    @property
    def first(self) -> FakeLocator:
        return self._then(lambda found: found[:1])

    @property
    def last(self) -> FakeLocator:
        return self._then(lambda found: found[-1:])

    def nth(self, index: int) -> FakeLocator:
        return self._then(lambda found: found[index : index + 1])

    def and_(self, locator: FakeLocator) -> FakeLocator:
        def both(found: list[Element]) -> list[Element]:
            other = {id(e) for e in locator.resolve()}
            return [e for e in found if id(e) in other]

        return self._then(both, f".and({locator.desc})")

    def filter(self, *, has: FakeLocator | None = None, visible: bool | None = None) -> FakeLocator:
        def keep(found: list[Element]) -> list[Element]:
            out = found
            if has is not None:
                out = [e for e in out if has.resolve([e])]
            if visible is not None:
                out = [e for e in out if (not hidden(e)) == visible]
            return out

        return self._then(keep)

    def locator(self, selector: str) -> FakeLocator:
        self._page.lookups.append(f"locator:{selector}")
        nearest = _XPATH_NEAREST.fullmatch(selector)
        if nearest is not None:
            heading = nearest.group(1)

            def up(found: list[Element]) -> list[Element]:
                out: list[Element] = []
                for element in found:
                    for ancestor in element.ancestors():
                        if any(
                            e.tag == "h2" and " ".join(e.text().split()) == heading
                            for e in ancestor.elements()
                        ):
                            out.append(ancestor)
                            break
                return _unique(out)

            return self._then(up, f".locator({selector})")
        if selector in ("xpath=following::a", "xpath=following::*"):
            # Every <a> (or every element, for #473's check) after the element in document
            # order, outside it (#444's top card).
            wanted = selector.rpartition("::")[2]

            def following(found: list[Element]) -> list[Element]:
                order = list(self._page.document.elements())
                out: list[Element] = []
                for element in found:
                    after = order[order.index(element) + 1 :]
                    out.extend(
                        e for e in after if wanted in ("*", e.tag) and not element.contains(e)
                    )
                return _unique(out)

            return self._then(following, f".locator({selector})")
        if selector == "xpath=ancestor::form[1]":

            def form(found: list[Element]) -> list[Element]:
                out: list[Element] = []
                for element in found:
                    nearest = next((a for a in element.ancestors() if a.tag == "form"), None)
                    if nearest is not None:
                        out.append(nearest)
                return _unique(out)

            return self._then(form, f".locator({selector})")
        assert not selector.startswith("xpath="), f"the fake can't read {selector!r}"

        def under(found: list[Element]) -> list[Element]:
            out: list[Element] = []
            for root in found:
                out.extend(select(selector, root, self._page))
            return _unique(out)

        return self._then(under, f".locator({selector})")

    def get_by_role(
        self,
        role: str,
        *,
        name: str | re.Pattern[str] | None = None,
        exact: bool | None = None,
        include_hidden: bool | None = None,
        level: int | None = None,
    ) -> FakeLocator:
        self._page.lookups.append(f"get_by_role:{role}:{name}")

        def by_role(found: list[Element]) -> list[Element]:
            out: list[Element] = []
            for root in found:
                for element in root.elements():
                    if role_of(element) != role:
                        continue
                    if level is not None and element.tag != f"h{level}":
                        continue
                    if not include_hidden and hidden(element):
                        continue
                    if name is not None and not _name_matches(
                        name,
                        name_of(element, self._page.document, include_hidden=bool(include_hidden)),
                        exact=bool(exact),
                    ):
                        continue
                    out.append(element)
            return _unique(out)

        return self._then(by_role, f".role({role})")

    def _one(self) -> Element:
        found = self.resolve()
        if len(found) != 1:
            raise RuntimeError(f"strict mode violation: {len(found)} elements")
        return found[0]

    async def count(self) -> int:
        self._page.read_log.append(f"count{self.desc}")
        self._page.reads += 1
        self._page.before_read()
        return len(self.resolve())

    async def get_attribute(self, name: str, *, timeout: float | None = None) -> str | None:  # noqa: ASYNC109
        self._page.read_log.append(f"get_attribute{self.desc}")
        self._page.reads += 1
        return self._one().attrs.get(name)

    async def inner_text(self, *, timeout: float | None = None) -> str:  # noqa: ASYNC109
        self._page.read_log.append(f"inner_text{self.desc}")
        self._page.reads += 1
        self._page.before_read()
        return self._one().text()

    async def text_content(self, *, timeout: float | None = None) -> str | None:  # noqa: ASYNC109
        self._page.read_log.append(f"text_content{self.desc}")
        self._page.reads += 1
        self._page.before_read()
        return self._one().text_content()

    async def bounding_box(self, *, timeout: float | None = None) -> Mapping[str, float] | None:  # noqa: ASYNC109
        """The element's ``data-box`` (``x,y,width,height``), or ``None`` without one."""
        self._page.read_log.append(f"bounding_box{self.desc}")
        return box_of(self._one())

    async def click(self, *, delay: float | None = None, timeout: float | None = None) -> None:  # noqa: ASYNC109
        await self._page.clicked(self._one())

    async def is_enabled(self, *, timeout: float | None = None) -> bool:  # noqa: ASYNC109
        self._page.read_log.append(f"is_enabled{self.desc}")
        self._page.reads += 1
        self._page.before_read()
        return "disabled" not in self._one().attrs

    async def focus(self, *, timeout: float | None = None) -> None:  # noqa: ASYNC109
        """``Locator.focus()``: focuses the one element, unless the page ignores it."""
        element = self._one()
        self._page.focus_calls.append(element)
        if self._page.site.focus_error is not None:
            raise self._page.site.focus_error
        if not self._page.site.focus_ignored:
            self._page.focused = element


def _name_matches(name: str | re.Pattern[str], accessible: str, *, exact: bool) -> bool:
    if isinstance(name, re.Pattern):
        return name.search(accessible) is not None
    if exact:
        return accessible == name
    return name.casefold() in accessible.casefold()


def _unique(found: list[Element]) -> list[Element]:
    seen: set[int] = set()
    out: list[Element] = []
    for element in found:
        if id(element) not in seen:
            seen.add(id(element))
            out.append(element)
    return out


# --- the keyboard ------------------------------------------------------------------------


class KeyboardViolation(AssertionError):
    """A key the prefill must never send."""


class FakeKeyboard:
    """Types into the focused composer, and fails the test on any key that could send."""

    def __init__(self, page: MessagingTab) -> None:
        self._page = page

    async def type(self, text: str) -> None:
        self._page.attempt("type", text)
        if len(text) != 1 or not 0x21 <= ord(text) <= 0x7E:
            raise KeyboardViolation(f"keyboard.type with {text!r}")
        await self._page.landed("type", text)

    async def insert_text(self, text: str) -> None:
        self._page.attempt("insert_text", text)
        if not text or any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in text):
            raise KeyboardViolation(f"insert_text with a control character: {text!r}")
        await self._page.landed("insert_text", text)

    async def press(self, key: str) -> None:
        self._page.attempt("press", key)
        if key != "Shift+Enter":
            raise KeyboardViolation(f"pressed {key!r}")
        await self._page.landed("press", key)


# --- the tab and the site ----------------------------------------------------------------


@dataclass
class Bubble:
    """What the Message click opens, and what its compose option says."""

    member: Member
    existing_conversation: int | None = 7
    #: Which bubble HTML; defaults to the layout the compose option names.
    html: str | None = None
    #: Wrap the never-messaged bubble in a ``role="dialog"`` named Messaging.
    dialog_root: bool = False
    #: The compose option answer; ``None`` sends none at all.
    compose: str | None = "default"
    compose_member: Member | None = None
    compose_status: int = 200
    extra_compose: bool = False
    focus_composer: bool = True
    #: How a Shift+Enter shows: ``br`` inside one paragraph, or a new ``p``.
    newline_mode: str = "br"
    #: Whether the page sends the conversation's thread request after the click, and a
    #: thread request for another conversation before it.
    thread: bool = True
    other_thread: int | None = None
    #: Draw the bubble only after this many page reads (a slow paint).
    draw_after_reads: int = 0
    #: Focus the composer only after this many page reads, counted from the drawing.
    focus_after_reads: int = 0
    #: Send a second compose option after this many page reads, counted from the click.
    late_compose_after_reads: int = 0


@dataclass
class Event:
    kind: str
    value: str
    reads_before: int
    #: The page's read log up to this key (what was read, in order).
    log_before: int = 0


class MessagingTab(FakePage):
    """One tab: a DOM, listeners, a keyboard, and a record of everything done to it."""

    def __init__(self, site: MessagingSite) -> None:
        super().__init__(site)
        self.site = site
        self.mouse = FakeMouse()
        self.keyboard = FakeKeyboard(self)
        self.listeners: dict[str, list[Callable[[Any], None]]] = defaultdict(list)
        self.document = Element("#document", {})
        self.focused: Element | None = None
        self.composer: Element | None = None
        self.typed = ""
        self.draft = ""
        self.keys: list[Event] = []
        self.attempts: list[tuple[str, str]] = []
        self.clicks: list[Element] = []
        self.focus_calls: list[Element] = []
        self.lookups: list[str] = []
        self.reads = 0
        #: Each read, described by the locator chain it read through.
        self.read_log: list[str] = []
        self.fronted = 0
        self.goto_after_click: list[str] = []
        self._read_hooks: list[Callable[[MessagingTab], None]] = []

    # PageLike and the listener protocol
    def on(self, event: str, handler: Callable[[Any], None]) -> None:
        self.listeners[event].append(handler)

    def remove_listener(self, event: str, handler: Callable[[Any], None]) -> None:
        self.listeners[event].remove(handler)

    def locator(self, selector: str) -> FakeLocator:  # type: ignore[override]
        return FakeLocator(self).locator(selector)

    def get_by_role(
        self,
        role: str,
        *,
        name: str | re.Pattern[str] | None = None,
        exact: bool | None = None,
        include_hidden: bool | None = None,
        level: int | None = None,
    ) -> FakeLocator:
        return FakeLocator(self).get_by_role(
            role, name=name, exact=exact, include_hidden=include_hidden, level=level
        )

    async def bring_to_front(self) -> None:
        self.fronted += 1

    async def goto(self, url: str) -> object:
        if self.clicks:
            self.goto_after_click.append(url)
        await super().goto(url)
        self.site.navigated(self, url)
        return None

    def emit(self, url: str, body: str, status: int = 200, method: str = "GET") -> None:
        request = FakeRequest(method, "fetch", None, url=url)
        response = FakeResponse(url, status, body.encode(), request)
        for handler in list(self.listeners["response"]):
            handler(response)

    # the page's behavior
    def load(self, html: str) -> None:
        self.document = Element("#document", {})
        parse_into(self.document, html)
        self.focused = None
        self._find_composer()

    def add_html(self, html: str) -> None:
        """Append HTML at the end of the body (a bubble opening at the page's foot)."""
        parse_into(self.document, html)
        self._find_composer()

    def _find_composer(self) -> None:
        for element in self.document.elements():
            if element.attrs.get("role") == "textbox" and element.attrs.get("contenteditable"):
                self.composer = element
                self.draft = element.text().rstrip("\n")
                self.typed = ""

    def composers(self) -> list[Element]:
        return [
            e
            for e in self.document.elements()
            if e.attrs.get("role") == "textbox" and e.attrs.get("contenteditable")
        ]

    def attempt(self, kind: str, value: str) -> None:
        self.attempts.append((kind, value))

    async def landed(self, kind: str, value: str) -> None:
        self.keys.append(Event(kind, value, self.reads, len(self.read_log)))
        target = self.focused
        if target is not None and target is self.composer:
            self.typed += "\n" if kind == "press" else value
            self._render_composer()
        else:
            self.site.stray_keys.append(value)
        hook = self.site.after_key.get(len(self.keys))
        if hook is not None:
            hook(self)

    def _render_composer(self) -> None:
        composer = self.composer
        assert composer is not None
        bubble = self.site.bubble
        text = self.draft + self.typed
        form = next((a for a in composer.ancestors() if a.tag == "form"), None)
        if form is not None and text and not self.site.send_stays_disabled:
            for element in form.elements():
                if element.tag == "button" and element.attrs.get("type") == "submit":
                    element.attrs.pop("disabled", None)
        if text.endswith(" "):
            text = text[:-1] + "\u00a0"  # a contenteditable shows a trailing space this way
        composer.children = []
        mode = bubble.newline_mode if bubble else "br"
        lines = text.split("\n")
        if mode == "p":
            for line in lines:
                p = Element("p", {}, composer)
                if line:
                    p.children.append(line)
                else:
                    p.children.append(Element("br", {}, p))
                composer.children.append(p)
            return
        p = Element("p", {}, composer)
        for index, line in enumerate(lines):
            if index:
                p.children.append(Element("br", {}, p))
            if line:
                p.children.append(line)
        if not text or text.endswith("\n"):
            p.children.append(Element("br", {}, p))  # the browser's placeholder <br>
        composer.children.append(p)

    def before_read(self) -> None:
        for hook in list(self._read_hooks):
            hook(self)

    def on_read(self, hook: Callable[[MessagingTab], None]) -> None:
        self._read_hooks.append(hook)

    async def clicked(self, element: Element) -> None:
        self.clicks.append(element)
        if element.tag == "button" and element.attrs.get("type") == "submit":
            # The Send click (ADR 0008): the page sends what the composer holds, answers
            # with the message it created, and then empties the composer, as LinkedIn does.
            text = self.draft + self.typed
            self.site.sent.append(text)
            if self.site.send_error is not None:
                raise self.site.send_error
            bubble = self.site.bubble
            known = bubble.existing_conversation if bubble is not None else None
            conversation = None if known is None else conversation_urn(known)
            answer = (
                self.site.send_answer(text, conversation)
                if self.site.send_answer is not None
                else None
            )
            if answer is not None:
                status, body = answer
                self.emit(SEND_URL, body, status, method="POST")
            if self.site.send_clears:
                self.draft = ""
                self.typed = ""
                self._render_composer()
            if self.site.after_send is not None:
                self.site.after_send(self)
            return
        if element.tag == "button" and _visible_text(element).strip().startswith("Close "):
            # A bubble's close control: the bubble goes, and its draft with it.
            self.site.closed_bubbles += 1
            if self.site.close_error is not None:
                raise self.site.close_error
            if self.site.close_ignored:
                return
            dialog = next((a for a in element.ancestors() if a.attrs.get("role") == "dialog"), None)
            if dialog is not None and dialog.parent is not None:
                dialog.parent.children.remove(dialog)
            return
        if self.site.click_error is not None:
            raise self.site.click_error
        bubble = self.site.bubble
        if role_of(element) != "link" or bubble is None:
            return
        self.site.open_bubble(self, bubble)


class MessagingSite(FakeContext):
    """The profile pages and the bubble a Message click opens.

    ``profile_html`` is what ``/in/<slug>/`` loads (default: three Message links for
    ``member``); ``before`` is HTML already on the page (an earlier bubble);
    ``land_on`` is a url the tab lands on instead (a wall).
    """

    def __init__(
        self,
        member: Member,
        *,
        bubble: Bubble | None = None,
        profile_html: str | None = None,
        before: str = "",
        land_on: str | None = None,
    ) -> None:
        super().__init__()
        self.member = member
        self.bubble = bubble if bubble is not None else Bubble(member)
        self.profile_html = (
            profile_html if profile_html is not None else profile_message_controls_html(member)
        )
        self.before = before
        self.land_on = land_on
        self.click_error: BaseException | None = None
        #: ``Locator.focus()`` raises this, or is silently ignored by the page.
        self.focus_error: BaseException | None = None
        self.focus_ignored = False
        self.after_key: dict[int, Callable[[MessagingTab], None]] = {}
        #: Auto-send (ADR 0008): what each Send click sent, an error the click raises,
        #: and a Send button that never enables.
        self.sent: list[str] = []
        self.send_error: BaseException | None = None
        self.send_stays_disabled = False
        self.send_clears = True
        #: Changes the page right after a Send click (a test's hook).
        self.after_send: Callable[[MessagingTab], None] | None = None
        #: The page's createMessage answer to a Send of this text: ``(status, body)``, or
        #: ``None`` for no answer at all. The default is LinkedIn's: 200, the same text.
        self.send_answer: Callable[[str, str | None], tuple[int, str] | None] | None = (
            send_answer_ok
        )
        #: The bubble's close control (ADR 0008, D1): clicks counted, an error it raises,
        #: and a page that ignores it.
        self.closed_bubbles = 0
        self.close_error: BaseException | None = None
        self.close_ignored = False
        self.stray_keys: list[str] = []
        self.navigations: list[str] = []
        #: The layout viewport the geometry session reports (#444); ``None`` means the
        #: session can't be opened, as a fake without CDP, so the click is unchecked.
        self.viewport: tuple[float, float] | None = None
        #: How far the page is scrolled, as ``Page.getLayoutMetrics`` reports it (#470).
        self.scroll_offset: tuple[float, float] = (0.0, 0.0)
        #: The page zoom and device pixels per CSS pixel the metrics report (#473).
        self.zoom = 1.0
        self.device_pixel_ratio = 2.0
        self.geometry_error: BaseException | None = None
        self.geometry_hangs = False
        self.geometry_sessions: list[GeometrySession] = []

    async def new_cdp_session(self, page: Any) -> GeometrySession:
        if self.viewport is None:
            raise RuntimeError("this fake has no CDP")
        session = GeometrySession(page, self.viewport)
        self.geometry_sessions.append(session)
        return session

    async def new_page(self) -> PageLike:
        self.new_page_calls += 1
        tab = MessagingTab(self)
        self.pages.append(tab)
        return tab

    @property
    def tab(self) -> MessagingTab:
        [tab] = self.pages
        assert isinstance(tab, MessagingTab)
        return tab

    def navigated(self, tab: MessagingTab, url: str) -> None:
        self.navigations.append(url)
        if self.land_on is not None:
            tab._url = self.land_on
            tab.load("<main><h1>Wall</h1></main>")
            return
        tab.load(self.profile_html + self.before)

    def open_bubble(self, tab: MessagingTab, bubble: Bubble) -> None:
        member = bubble.compose_member or bubble.member
        if bubble.compose is not None:
            body = (
                compose_option_answer(member, existing_conversation=bubble.existing_conversation)
                if bubble.compose == "default"
                else bubble.compose
            )
            tab.emit(compose_option_url(member), body, bubble.compose_status)
            if bubble.extra_compose:
                tab.emit(compose_option_url(member), body)
        if bubble.late_compose_after_reads and bubble.compose is not None:
            url = compose_option_url(member)
            answer = compose_option_answer(
                member, existing_conversation=bubble.existing_conversation
            )
            self._later(tab, bubble.late_compose_after_reads, lambda page: page.emit(url, answer))
        if bubble.other_thread is not None:
            tab.emit(messages_sync_url(bubble.other_thread), "{}")
        if bubble.existing_conversation is not None and bubble.thread:
            tab.emit(messages_sync_url(bubble.existing_conversation), "{}")
        html = bubble.html
        if html is None:
            if bubble.existing_conversation is not None:
                html = existing_bubble_html(bubble.member)
            else:
                html = never_messaged_bubble_html([bubble.member])
                if bubble.dialog_root:
                    html = f'<div role="dialog" aria-label="Messaging">{html}</div>'
        if bubble.draw_after_reads:
            drawn_at = tab.reads + bubble.draw_after_reads
            final = html

            def draw(page: MessagingTab) -> None:
                if page.reads >= drawn_at and draw in page._read_hooks:
                    page._read_hooks.remove(draw)
                    self._draw(page, bubble, final)

            tab.on_read(draw)
            return
        self._draw(tab, bubble, html)

    def _later(self, tab: MessagingTab, reads: int, act: Callable[[MessagingTab], None]) -> None:
        due = tab.reads + reads

        def hook(page: MessagingTab) -> None:
            if page.reads >= due and hook in page._read_hooks:
                page._read_hooks.remove(hook)
                act(page)

        tab.on_read(hook)

    def _draw(self, tab: MessagingTab, bubble: Bubble, html: str) -> None:
        before = set(map(id, tab.composers()))
        tab.add_html(html)
        new = [c for c in tab.composers() if id(c) not in before]
        if new:
            tab.composer = new[-1]
            tab.draft = new[-1].text().rstrip("\n")
            tab.typed = ""
            if bubble.focus_composer and bubble.focus_after_reads:
                composer = new[-1]

                def focus(page: MessagingTab) -> None:
                    page.focused = composer

                self._later(tab, bubble.focus_after_reads, focus)
            elif bubble.focus_composer:
                tab.focused = new[-1]


#: The page's own send request (ADR 0008 reads its answer).
SEND_URL = f"{HOST}/voyager/api/voyagerMessagingDashMessengerMessages?action=createMessage"


def send_answer_ok(text: str, conversation: str | None = None) -> tuple[int, str]:
    """LinkedIn's ``createMessage`` answer: 200, ``value.body.text`` the text sent."""
    value: dict[str, Any] = {"body": {"attributes": [], "text": text}}
    if conversation is not None:
        value["conversationUrn"] = conversation
    return 200, json.dumps({"value": value})


def profile_url(member: Member) -> str:
    return f"{HOST}/in/{member.slug}/"
