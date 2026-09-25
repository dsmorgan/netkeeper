"""flagship-web answers for tests: hand-built to the captured shape, with invented people.

Nothing here came from a capture. The people are :mod:`voyager_pages`' invented cast
(fake ``ACoAAFAKE`` profile ids, ``-fake-`` slugs, companies that do not exist), and
every payload is assembled by the functions below from the *structure* recorded in
``docs/linkedin-flagship-web-shapes.md``: the row grammar, the element and action
``$type`` names, the anchors a parser reads (``componentKey``, the SetState ids, the
Navigate payload, ``Connected on``), and the nesting around them, trimmed to what a
parser needs. Where the capture could not say (how the last page of the list looks),
the choice is marked as invented.

* :func:`screen_payload`, :func:`pagination_payload`, :func:`document_html`, and
  :func:`pagination_request` are the connections list (#187).
* :func:`profile_payload` and :func:`contact_info_payload` are the profile and the
  contact-info overlay, for the enrichment lane: no parser reads them yet, and
  ``tests/test_linkedin_flagship.py`` only checks that they are the grammar and carry
  the anchors the shape note names.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import count
from typing import Any

from voyager_pages import Person

from netkeeper.linkedin.flagship import (
    CONNECTIONS_PAGER_ID,
    CONTACT_DETAILS_SCREEN_ID,
    HEADLINE_STATE_ID,
    NAME_STATE_ID,
    PAGINATION_REQUEST_TYPE,
    PROFILE_SCREEN_ID,
    REHYDRATION_GLOBAL,
    REHYDRATION_SCRIPT_ID,
    SORT_NEWEST_FIRST,
    SORT_STATE_ID,
    TOTAL_STATE_ID,
)

#: The screen id every connections request names, as captured.
CONNECTIONS_SCREEN_ID = "com.linkedin.sdui.flagshipnav.mynetwork.Connections"
#: The ``/mynetwork`` page's other pager, whose answers share the pagination path.
OTHER_PAGER_ID = "com.linkedin.sdui.pagers.mynetwork.scribeCohortBackfill"
SORT_NAMESPACE = "connectionsListSortOptionMenu"
_REMOVE_REQUEST = "com.linkedin.sdui.mynetwork.RemoveConnectionVanityName"
_MODAL = "proto.sdui.actions.core.presentation.ModalPresentation"
_TO_URL = "proto.sdui.actions.core.NavigateToUrl"

_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def connected_on_text(person: Person) -> str | None:
    """The card's "Connected on <Month d, yyyy>" for ``person``, or ``None`` without a date."""
    if person.created_ms is None:
        return None
    day = datetime.fromtimestamp(person.created_ms / 1000, tz=UTC).date()
    return f"Connected on {_MONTH_NAMES[day.month - 1]} {day.day}, {day.year}"


def profile_id(person: Person) -> str:
    """The id after ``urn:li:fsd_profile:`` -- invented, ``ACoAAFAKE`` and seven digits."""
    return person.urn.removeprefix("urn:li:fsd_profile:")


def display_name(person: Person) -> str:
    return f"{person.first} {person.last}".strip()


# --- the row grammar -----------------------------------------------------------------


class _Rows:
    """Flight rows in the order they are added, with hex ids handed out as the capture's are."""

    def __init__(self) -> None:
        self._ids = count(1)
        self.lines: list[str] = []

    def next_id(self) -> str:
        return format(next(self._ids), "x")

    def module(self, name: str) -> str:
        row = self.next_id()
        self.lines.append(f'{row}:I["fake-chunk-{row}",[],"{name}"]')
        return row

    def model(self, value: object, row: str | None = None) -> str:
        row = self.next_id() if row is None else row
        self.lines.append(f"{row}:{json.dumps(value, separators=(',', ':'))}")
        return row

    def payload(self) -> bytes:
        return ("\n".join(self.lines) + "\n").encode("utf-8")


def _el(kind: str, props: dict[str, Any]) -> list[Any]:
    return ["$", kind, None, props]


def _set_state(state_id: str, case: str, value: object) -> dict[str, Any]:
    return {
        "$type": "proto.sdui.actions.core.SetState",
        "value": {
            "state": {
                "key": {
                    "key": {"value": {"$case": "id", "id": state_id}},
                    "namespace": "MemberProfile",
                },
                "value": {"$case": case, case: value},
            }
        },
    }


def _click(actions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "$type": "proto.sdui.triggers.Trigger",
        "delegateComponentKey": "",
        "type": {"$case": "click", "click": {"$type": "proto.sdui.triggers.ClickTrigger"}},
        "action": {"actions": actions},
    }


def _navigate_to_profile(person: Person) -> dict[str, Any]:
    return {
        "$type": "proto.sdui.actions.core.Navigate",
        "value": {
            "content": {
                "$case": "screen",
                "screen": {
                    "$type": "proto.sdui.actions.core.NavigateToScreen",
                    "screenId": PROFILE_SCREEN_ID,
                    "url": f"/in/{person.slug}/",
                    "requestedArguments": {
                        "$type": "proto.sdui.actions.requests.RequestedArguments",
                        "requestedStateKeys": [],
                        "payload": {
                            "vanityName": person.slug,
                            "isVanityNameResolved": True,
                            "vieweeProfileId": profile_id(person),
                        },
                        "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
                    },
                },
            }
        },
    }


def _profile_click(person: Person) -> dict[str, Any]:
    """The click a card's image and name share: seed the profile screen, then go to it."""
    actions = [_set_state(NAME_STATE_ID, "stringValue", display_name(person))]
    if person.headline is not None:
        actions.append(_set_state(HEADLINE_STATE_ID, "stringValue", person.headline))
    actions.append(_set_state("profile_loading_has_data", "booleanValue", True))
    actions.append(_navigate_to_profile(person))
    return _click(actions)


def _pagination_request_value(start: int, pager: str = CONNECTIONS_PAGER_ID) -> dict[str, Any]:
    return {
        "$type": PAGINATION_REQUEST_TYPE,
        "pagerId": pager,
        "trigger": {
            "$case": "itemDistanceTrigger",
            "itemDistanceTrigger": {
                "$type": "proto.sdui.actions.requests.ItemDistanceTrigger",
                "preloadDistance": 3,
                "preloadLength": 250,
            },
        },
        "retryCount": 2,
        "requestedArguments": {
            "$type": "proto.sdui.actions.requests.RequestedArguments",
            "requestedStateKeys": [
                {
                    "key": {"value": {"$case": "id", "id": SORT_STATE_ID}},
                    "namespace": SORT_NAMESPACE,
                }
            ],
            "payload": {
                "startIndex": start,
                "sortByOptionBinding": {"key": SORT_STATE_ID, "namespace": SORT_NAMESPACE},
            },
            "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
        },
    }


# --- the connections list --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CardOptions:
    """Ways to spoil one card, for the parser's refusals. Default: a faithful card."""

    other_profile_id: str | None = None  # a second identity inside the same card
    key_slug: str | None = None  # a componentKey slug that is not the card's
    key_start: int | None = None  # a componentKey start that is not the page's
    name: str | None = None  # a display name other than the person's
    connected_on: str | None = None  # a "Connected on" text other than the person's
    drop_name: bool = False
    second_name: str | None = None  # the name link seeds a different name than the image
    second_connected_on: str | None = None  # a second "Connected on" text in the card


def _card_rows(
    rows: _Rows, modules: dict[str, str], person: Person, start: int, options: CardOptions
) -> list[Any]:
    """Add one card's own rows and return its element for the page's item list."""
    key_slug = options.key_slug if options.key_slug is not None else person.slug
    key_start = options.key_start if options.key_start is not None else start
    card_key = f"ConnectionCard_{key_start}-{key_slug}"
    click = _profile_click(person)
    if options.drop_name:
        click["action"]["actions"] = [
            action
            for action in click["action"]["actions"]
            if "state" not in action["value"]
            or action["value"]["state"]["key"]["key"]["value"]["id"] != NAME_STATE_ID
        ]
    if options.name is not None:
        click["action"]["actions"][0] = _set_state(NAME_STATE_ID, "stringValue", options.name)
    image = rows.model(
        _el(
            f"$L{modules['scope']}",
            {
                "trackingScope": "$undefined",
                "children": _el(
                    f"$L{modules['client']}",
                    {
                        "componentKey": f"ConnectionCardProfileImage_{key_start}-{key_slug}",
                        "children": _el(f"$L{modules['trigger']}", {"triggers": [click]}),
                    },
                ),
            },
        )
    )
    name_click = json.loads(json.dumps(click))
    if options.second_name is not None:
        name_click["action"]["actions"][0] = _set_state(
            NAME_STATE_ID, "stringValue", options.second_name
        )
    if options.other_profile_id is not None:
        name_click["action"]["actions"][-1]["value"]["content"]["screen"]["requestedArguments"][
            "payload"
        ]["vieweeProfileId"] = options.other_profile_id
    name = rows.model(
        _el(
            f"$L{modules['scope']}",
            {
                "trackingScope": "$undefined",
                "children": _el(
                    f"$L{modules['client']}",
                    {
                        "componentKey": f"fake-name-link-{person.n:04d}",
                        "children": _el(f"$L{modules['trigger']}", {"triggers": [name_click]}),
                    },
                ),
            },
        )
    )
    menu = rows.model(
        _el(
            f"$L{modules['scope']}",
            {
                "children": _el(
                    f"$L{modules['client']}",
                    {
                        "componentKey": f"fake-remove-{person.n:04d}",
                        "children": _el(
                            f"$L{modules['trigger']}",
                            {
                                "triggers": [
                                    _click(
                                        [
                                            {
                                                "$type": "proto.sdui.actions.core.RemoveUi",
                                                "value": {
                                                    "key": card_key,
                                                    "componentKey": {
                                                        "$type": "proto.sdui.Key",
                                                        "value": {"$case": "id", "id": card_key},
                                                    },
                                                },
                                            },
                                            {
                                                "$type": "proto.sdui.actions.core.ServerRequest",
                                                "value": {
                                                    "requestId": _REMOVE_REQUEST,
                                                    "requestedArguments": {
                                                        "payload": {
                                                            "disconnectVanityName": person.slug
                                                        }
                                                    },
                                                },
                                            },
                                        ]
                                    )
                                ]
                            },
                        ),
                    },
                ),
            },
        )
    )
    connected = (
        options.connected_on if options.connected_on is not None else connected_on_text(person)
    )
    text_children: list[Any] = [f"$L{name}"]
    if connected is not None:
        text_children.append(_el(f"$L{modules['text']}", {"textProps": {"children": [connected]}}))
    if options.second_connected_on is not None:
        text_children.append(
            _el(f"$L{modules['text']}", {"textProps": {"children": [options.second_connected_on]}})
        )
    return _el(
        f"$L{modules['client']}",
        {
            "componentKey": card_key,
            "children": _el(
                "div",
                {
                    "componentkey": card_key,
                    "children": _el(
                        f"$L{modules['stack']}",
                        {
                            "direction": "horizontal",
                            "children": [
                                f"$L{image}",
                                _el("div", {"children": [_el("div", {"children": text_children})]}),
                                _el("div", {"children": [f"$L{menu}"]}),
                            ],
                        },
                    ),
                },
            ),
        },
    )


def _chunk(
    people: Sequence[Person],
    *,
    start: int,
    next_start: int | None,
    total: int | None,
    card_options: dict[int, CardOptions] | None = None,
) -> bytes:
    rows = _Rows()
    modules = {
        "provider": rows.module("default"),
        "stack": rows.module("VisibleItemsProvider"),
        "client": rows.module("ClientComponent"),
        "text": rows.module("default"),
        "scope": rows.module("SduiTrackingScopeWrapper"),
        "trigger": rows.module("TriggerButton"),
    }
    items: list[Any] = []
    for index, person in enumerate(people):
        options = (card_options or {}).get(index, CardOptions())
        items.append([f"fake-divider-{start + index}", _el("div", {"role": "separator"})])
        items.append(
            [f"fake-item-{start + index}", _card_rows(rows, modules, person, start, options)]
        )
    model_states: list[Any] = []
    if total is not None:
        model_states.append(
            {
                "key": {"key": {"value": {"$case": "id", "id": TOTAL_STATE_ID}}},
                "value": {"$case": "intValue", "intValue": total},
            }
        )
    # Invented: the capture never reached the end of the list, so what an answer with no
    # next page carries in that slot is not known. "$undefined" is React's own marker.
    next_request = (
        json.dumps(_pagination_request_value(next_start))
        if next_start is not None
        else "$undefined"
    )
    root = [
        [
            _el(f"$L{modules['provider']}", {"modelStates": model_states, "isPartialPage": True}),
            "$undefined",
        ],
        next_request,
        items,
        "horizontal",
    ]
    rows.model(root, row="0")
    return rows.payload()


def screen_payload(
    people: Sequence[Person],
    *,
    total: int | None,
    next_start: int | None = None,
    card_options: dict[int, CardOptions] | None = None,
) -> bytes:
    """The first screen: up to ten cards keyed ``ConnectionCard_0-``, the total, the next request.

    ``next_start`` defaults to the number of cards, as the captured first screen's did.
    """
    return _chunk(
        people,
        start=0,
        next_start=len(people) if next_start is None and people else next_start,
        total=total,
        card_options=card_options,
    )


def pagination_payload(
    people: Sequence[Person],
    *,
    start: int,
    next_start: int | None,
    card_options: dict[int, CardOptions] | None = None,
) -> bytes:
    """One pagination answer: cards keyed ``ConnectionCard_<start>-``, no total."""
    return _chunk(people, start=start, next_start=next_start, total=None, card_options=card_options)


def pagination_request(
    start: int, *, sort: str | None = SORT_NEWEST_FIRST, pager: str = CONNECTIONS_PAGER_ID
) -> str:
    """The JSON body the page sends to the pagination endpoint, as captured in shape.

    ``sort=None`` leaves the sort state out, as a page that never set one might.
    """
    arguments: dict[str, object] = {
        "$type": "proto.sdui.actions.requests.RequestedArguments",
        "requestedStateKeys": [
            {"key": {"value": {"$case": "id", "id": SORT_STATE_ID}}, "namespace": SORT_NAMESPACE}
        ],
        "payload": {
            "startIndex": start,
            "sortByOptionBinding": {"key": SORT_STATE_ID, "namespace": SORT_NAMESPACE},
        },
        "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
        "states": [
            {
                "key": SORT_STATE_ID,
                "namespace": SORT_NAMESPACE,
                "value": sort,
                "originalProtoCase": "stringValue",
            }
        ],
        "screenId": CONNECTIONS_SCREEN_ID,
        "knownTemplateIds": [],
    }
    if sort is None:
        arguments["states"] = []
    if pager != CONNECTIONS_PAGER_ID:
        arguments = {"payload": {"pageSize": 6, "pageToken": "fake-token"}}
    return json.dumps(
        {
            "pagerId": pager,
            "clientArguments": arguments,
            "paginationRequest": _pagination_request_value(start, pager),
        }
    )


def document_html(payload: bytes, *, chunks: int = 3) -> str:
    """A full page load's HTML: the first screen split across ``rehydrate-data``'s strings."""
    text = payload.decode("utf-8")
    size = max(1, len(text) // chunks + 1)
    pieces = [text[i : i + size] for i in range(0, len(text), size)]
    array = json.dumps(pieces).replace("</", "<\\/")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        "<title>netkeeper replica: connections</title></head><body>"
        '<div id="root"><a href="/login">Sign in</a></div>'
        f'<script nonce="fake" type="text/javascript" id="{REHYDRATION_SCRIPT_ID}">'
        f"window.{REHYDRATION_GLOBAL} = {array};</script>"
        "</body></html>"
    )


def pages_of(
    people: Sequence[Person], *, first: int = 10, size: int = 10
) -> Iterator[tuple[int, Sequence[Person]]]:
    """``(start, people)`` for every pagination answer after a first screen of ``first``."""
    start = first
    while start < len(people):
        yield start, people[start : start + size]
        start += size


# --- the profile and the contact-info overlay (enrichment's, next) ----------------------


@dataclass(frozen=True, slots=True)
class Role:
    """One experience entry, as the card renders it: text, not structured data."""

    title: str
    company: str
    employment: str | None  # "Full-time", "Part-time", ...
    dates: str  # "Aug 2021 - Present · 3 yrs 2 mos"
    location: str | None = None


def profile_payload(
    person: Person, *, location: str, roles: Sequence[Role], degree: str = "1st"
) -> bytes:
    """A profile screen (``POST /flagship-web/in/<slug>/``), trimmed to the anchors.

    The top card (``viewName: profile-top-card``) renders the degree, the headline, the
    location, and the **Contact info** link, whose action is a Navigate to
    ``ProfileContactDetailsOverlay`` carrying ``{vanityName, givenName, familyName}``.
    The experience card (``viewName: profile-card-experience``) renders each role as
    text runs under the ``Experience`` heading. Seeds of ``profile_name_loading_state``
    and ``profile_headline_loading_state`` appear on the screen as on a card's click.
    """
    rows = _Rows()
    text = rows.module("default")
    link = rows.module("default")
    client = rows.module("ClientComponent")
    contact_link = _el(
        f"$L{link}",
        {
            "action": {
                "actions": [
                    {
                        "$type": "proto.sdui.actions.core.Navigate",
                        "value": {
                            "content": {
                                "$case": "screen",
                                "screen": {
                                    "$type": "proto.sdui.actions.core.NavigateToScreen",
                                    "screenId": CONTACT_DETAILS_SCREEN_ID,
                                    "url": f"/in/{person.slug}/overlay/contact-info/",
                                    "presentation": {
                                        "$case": "modal",
                                        "modal": {"$type": _MODAL},
                                    },
                                    "requestedArguments": {
                                        "$type": "proto.sdui.actions.requests.RequestedArguments",
                                        "payload": {
                                            "vanityName": person.slug,
                                            "givenName": person.first,
                                            "familyName": person.last,
                                            "isVanityNameResolved": True,
                                        },
                                    },
                                },
                            }
                        },
                    }
                ]
            },
            "linkStyle": "inherit",
            "viewTrackingSpecs": "$undefined",
            "children": ["Contact info"],
        },
    )
    top_texts: list[Any] = [
        _el(f"$L{text}", {"textProps": {"children": [f"· {degree}"]}}),
        _el(f"$L{text}", {"textProps": {"children": [person.headline or ""]}}),
        _el(f"$L{text}", {"textProps": {"children": [location]}}),
        _el(f"$L{text}", {"textProps": {"children": ["·"]}}),
        _el(f"$L{text}", {"textProps": {"children": [contact_link]}}),
        _el(f"$L{text}", {"textProps": {"children": ["500+ connections"]}}),
    ]
    top = rows.model(
        _el(
            f"$L{client}",
            {
                "componentKey": "fake-top-card",
                "viewTrackingSpecs": {"viewName": "profile-top-card"},
                "children": _el("section", {"children": top_texts}),
            },
        )
    )
    entries: list[Any] = []
    for role in roles:
        runs = [
            role.title,
            f"{role.company} · {role.employment}" if role.employment else role.company,
        ]
        runs.append(role.dates)
        if role.location:
            runs.append(role.location)
        entries.append(
            _el(
                "li",
                {
                    "children": [
                        _el(f"$L{text}", {"textProps": {"children": [run]}}) for run in runs
                    ]
                },
            )
        )
    experience = rows.model(
        _el(
            f"$L{client}",
            {
                "componentKey": "fake-experience-card",
                "viewTrackingSpecs": {"viewName": "profile-card-experience"},
                "children": [
                    _el(f"$L{text}", {"textProps": {"tagName": "h2", "children": ["Experience"]}}),
                    _el("ul", {"children": entries}),
                ],
            },
        )
    )
    seeds = _click(
        [
            _set_state(NAME_STATE_ID, "stringValue", display_name(person)),
            _set_state(HEADLINE_STATE_ID, "stringValue", person.headline or ""),
        ]
    )
    rows.model(
        [
            _el("main", {"children": [f"$L{top}", f"$L{experience}"]}),
            {"triggers": [seeds]},
        ],
        row="0",
    )
    return rows.payload()


@dataclass(frozen=True, slots=True)
class Website:
    url: str
    label: str | None = None  # "(Personal)", "(Company)", ...


def contact_info_payload(
    person: Person,
    *,
    emails: Sequence[str] = (),
    websites: Sequence[Website] = (),
    phones: Sequence[str] = (),
    connected_since: str | None = None,
) -> bytes:
    """The contact-info overlay's answer (``POST .../actions/navigation``), trimmed.

    One section per kind, each an element with ``viewTrackingSpecs.viewName`` of
    ``contact-your-profile``, ``contact-website``, ``contact-email`` (captured) or
    ``contact-phone`` (invented: the capture's profile shared no phone, so the name
    is a guess by analogy), a ``p`` heading, and one link per value whose action is a
    ``NavigateToUrl``. Websites point through a ``linkedin.com`` redirect wrapper in
    the capture, and so do these. Use example.test addresses only.
    """
    rows = _Rows()
    item = rows.module("default")
    link = rows.module("default")

    def section(view: str, control: str, heading: str, links: list[tuple[str, str]]) -> Any:
        return _el(
            f"$L{item}",
            {
                "componentKey": f"fake-{view}",
                "viewTrackingSpecs": {"viewName": view, "legacyControlName": control},
                "children": _el(
                    "div",
                    {
                        "children": [
                            _el("p", {"children": [heading]}),
                            *[
                                _el(
                                    f"$L{link}",
                                    {
                                        "action": {
                                            "actions": [
                                                {
                                                    "$type": "proto.sdui.actions.core.Navigate",
                                                    "value": {
                                                        "content": {
                                                            "$case": "url",
                                                            "url": {
                                                                "$type": _TO_URL,
                                                                "urlValue": {
                                                                    "$case": "url",
                                                                    "url": url,
                                                                },
                                                            },
                                                        }
                                                    },
                                                }
                                            ]
                                        },
                                        "children": [shown],
                                    },
                                )
                                for url, shown in links
                            ],
                        ]
                    },
                ),
            },
        )

    sections = [
        section(
            "contact-your-profile",
            "contact_share_profile",
            "Your Profile",
            [(f"https://www.linkedin.com/in/{person.slug}", f"linkedin.com/in/{person.slug}")],
        )
    ]
    if websites:
        sections.append(
            section(
                "contact-website",
                "contact_website",
                "Website",
                [
                    (
                        f"https://www.linkedin.com/redir/redirect?url={site.url}",
                        f"{site.url} {site.label}" if site.label else site.url,
                    )
                    for site in websites
                ],
            )
        )
    if phones:
        sections.append(
            section("contact-phone", "contact_phone", "Phone", [(f"tel:{p}", p) for p in phones])
        )
    if emails:
        sections.append(
            section("contact-email", "contact_email", "Email", [(f"mailto:{e}", e) for e in emails])
        )
    children: list[Any] = [*sections]
    if connected_since is not None:
        children.append(_el("p", {"children": ["Connected since"]}))
        children.append(_el("span", {"children": [connected_since]}))
    body = rows.model(_el("div", {"data-testid": "fake-overlay", "children": children}))
    rows.model([f"$L{body}"], row="0")
    return rows.payload()
