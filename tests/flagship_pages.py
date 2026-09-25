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
* :func:`profile_payload`, :func:`experience_payload`, and :func:`contact_info_payload`
  are the profile, a lazy experience card, and the contact-info overlay, which
  :mod:`netkeeper.linkedin.flagship_profile` reads (#190). Where the capture was silent
  (where the member's id sits on a profile, grouped roles, education, the phone,
  Twitter, birthday, and address sections), the shape is marked as invented.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import count
from typing import Any, Final
from urllib.parse import urlencode

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


class _Unset:
    """Sentinel: :func:`screen_payload`'s ``next_start`` was not given at all, distinct
    from an explicit ``None`` (#189 item 2)."""


_UNSET: Final = _Unset()


def screen_payload(
    people: Sequence[Person],
    *,
    total: int | None,
    next_start: int | _Unset | None = _UNSET,
    card_options: dict[int, CardOptions] | None = None,
) -> bytes:
    """The first screen: up to ten cards keyed ``ConnectionCard_0-``, the total, the next request.

    ``next_start`` left unset defaults to the number of cards, as the captured
    first screen's did (``None`` for an empty screen). Pass ``next_start=None``
    explicitly instead for a first screen that asks for *nothing* -- a list
    that ends within its own first screen (#189 item 2): before the ``_Unset``
    sentinel, ``None`` meant "not given" too, so a first screen could never
    "ask for nothing" on purpose.
    """
    resolved = (len(people) if people else None) if isinstance(next_start, _Unset) else next_start
    return _chunk(
        people,
        start=0,
        next_start=resolved,
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


# --- the profile and the contact-info overlay (#190) --------------------------------------


@dataclass(frozen=True, slots=True)
class Role:
    """One experience entry, as the card renders it: text, not structured data.

    As the capture showed it (#203): the title in a plain ``p``, then text runs for
    ``<Company> · <type>`` (or the company alone, or the type alone, or neither), the
    dates, and the location, all inside a link to the company's page.
    """

    title: str
    company: str | None
    employment: str | None  # "Full-time", "Part-time", ...
    dates: str  # "Aug 2021 - Present · 3 yrs 2 mos"
    location: str | None = None


@dataclass(frozen=True, slots=True)
class RoleGroup:
    """Several roles at one company, under a company header. **Invented**: the capture
    described this layout in words only (``<Employment type> · <total duration>`` under
    the company, the roles below), so the nesting here is a guess the reader must fail
    soft on."""

    company: str
    summary: str  # "Full-time · 5 yrs"
    roles: tuple[tuple[str, str], ...]  # (title, dates)


@dataclass(frozen=True, slots=True)
class School:
    """One education entry. **Invented**: education was not in the capture."""

    school: str
    degree: str | None
    years: str | None  # "2012 - 2016"


def _text_el(module: str, text: str, **extra: Any) -> list[Any]:
    return _el(f"$L{module}", {"textProps": {"children": [text], **extra}})


def _company_line(role: Role) -> str | None:
    if role.company and role.employment:
        return f"{role.company} · {role.employment}"
    return role.company or role.employment


def _company_link(module: str, children: list[Any]) -> list[Any]:
    """The link to the company's page an entry renders inside (captured: a trigger
    button whose click navigates to a ``/company/`` url)."""
    click = {
        "$type": "proto.sdui.triggers.Trigger",
        "type": {"$case": "click", "click": {"$type": "proto.sdui.triggers.ClickTrigger"}},
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
                                    "url": "https://www.linkedin.com/company/fake-co/",
                                },
                            },
                        }
                    },
                }
            ],
            "actionLabel": "View Fake Co",
        },
    }
    return _el(
        f"$L{module}", {"triggers": [click], "children": [_el("div", {"children": children})]}
    )


def _title_el(text: str, title: str, *, legacy: bool) -> list[Any]:
    """A role's title: a plain ``p`` as captured, or a text run (``legacy``)."""
    return _text_el(text, title) if legacy else _el("p", {"children": [title]})


def _experience_card(
    rows: _Rows,
    text: str,
    client: str,
    roles: Sequence[Role],
    groups: Sequence[RoleGroup],
    *,
    legacy_titles: bool = False,
) -> str:
    button = rows.module("TriggerButton")
    entries: list[Any] = []
    for role in roles:
        runs = [run for run in (_company_line(role), role.dates, role.location) if run is not None]
        body = [
            _title_el(text, role.title, legacy=legacy_titles),
            *[_text_el(text, run) for run in runs],
        ]
        entries.append(_el("li", {"children": [_company_link(button, body)]}))
    for group in groups:
        inner = [
            _el(
                "li",
                {
                    "children": [
                        _title_el(text, title, legacy=legacy_titles),
                        _text_el(text, dates),
                    ]
                },
            )
            for title, dates in group.roles
        ]
        entries.append(
            _el(
                "li",
                {
                    "children": [
                        _text_el(text, group.company),
                        _text_el(text, group.summary),
                        _el("ul", {"children": inner}),
                    ]
                },
            )
        )
    return rows.model(
        _el(
            f"$L{client}",
            {
                "componentKey": "fake-experience-card",
                "viewTrackingSpecs": {"viewName": "profile-card-experience"},
                "children": [
                    _text_el(text, "Experience", tagName="h2"),
                    _el("ul", {"children": entries}),
                ],
            },
        )
    )


def _identity_button(
    module: str,
    person: Person,
    *,
    profile_id: str,
    slug: str | None,
    key: str = "profileUrn",
    raw_urn: str | None = None,
) -> list[Any]:
    """A button whose action payload names the member (the message button, **invented**
    placement: the capture saw ``firstName``/``lastName`` "sometimes with vanityName or
    profileUrn beside them" in the message and follow buttons' payloads)."""
    payload: dict[str, Any] = {"firstName": person.first, "lastName": person.last}
    if slug is not None:
        payload["vanityName"] = slug
    payload[key] = f"urn:li:fsd_profile:{profile_id}" if key == "profileUrn" else profile_id
    if raw_urn is not None:
        payload[key] = raw_urn
    return _el(
        f"$L{module}",
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
                                    "screenId": "com.linkedin.sdui.flagshipnav.messaging.Fake",
                                    "url": "/messaging/compose/",
                                    "requestedArguments": {
                                        "$type": "proto.sdui.actions.requests.RequestedArguments",
                                        "payload": payload,
                                    },
                                },
                            }
                        },
                    }
                ]
            },
            "children": ["Message"],
        },
    )


def profile_payload(
    person: Person,
    *,
    location: str | None,
    roles: Sequence[Role] = (),
    degree: str = "1st",
    identity: str = "message",
    profile_id_override: str | None = None,
    also_viewed: Sequence[Person] = (),
    experience_inline: bool = True,
    groups: Sequence[RoleGroup] = (),
    schools: Sequence[School] = (),
    contact_links: int = 1,
    contact_slug: str | None = None,
    top_cards: int = 1,
    extra_top_runs: Sequence[str] = (),
    mutuals: Sequence[Person] = (),
    contact_url: str | None = None,
    raw_urn: str | None = None,
    degree_runs: int = 2,
    legacy_titles: bool = False,
) -> bytes:
    """A profile screen (``POST /flagship-web/in/<slug>/``), trimmed to the anchors.

    The top card (``viewName: profile-top-card``) renders the degree, the headline, the
    location, and the **Contact info** link, whose action is a Navigate to
    ``ProfileContactDetailsOverlay`` carrying ``{vanityName, givenName, familyName}``.
    The experience card (``viewName: profile-card-experience``) renders each role as
    text runs under the ``Experience`` heading. Seeds of ``profile_name_loading_state``
    and ``profile_headline_loading_state`` appear on the screen as on a card's click.

    ``identity`` is how the page names the member's id: ``"message"`` (a Message button
    in the top card whose payload carries ``profileUrn`` beside ``vanityName``),
    ``"bare"`` (the same button without ``vanityName``), ``"viewee"`` (a
    ``vieweeProfileId`` beside ``vanityName``), or ``"none"``. Where the id sits is
    **invented** (see :func:`_identity_button`). ``profile_id_override`` puts another
    id there. ``also_viewed`` adds a "People also viewed" rail of other people's cards,
    each with their own id, slug, and seeds. ``mutuals`` puts shared connections *inside*
    the top card, each a link whose payload names that person's ``profileUrn`` beside
    their own ``vanityName`` (**invented** shape: the capture saw the shared-connections
    line in the top card, not its payloads). The other options spoil the page for the
    parser's refusals. ``schools`` adds an education card (**invented** shape).
    ``degree_runs`` is how many times the degree renders: twice, one run after the
    other, as captured (#203). ``legacy_titles`` renders each role's title as a text run
    rather than the captured ``p``.
    """
    rows = _Rows()
    text = rows.module("default")
    link = rows.module("default")
    client = rows.module("ClientComponent")
    shown_slug = person.slug if contact_slug is None else contact_slug

    def contact_link() -> list[Any]:
        return _el(
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
                                        "url": contact_url
                                        or f"/in/{shown_slug}/overlay/contact-info/",
                                        "presentation": {
                                            "$case": "modal",
                                            "modal": {"$type": _MODAL},
                                        },
                                        "requestedArguments": {
                                            "$type": (
                                                "proto.sdui.actions.requests.RequestedArguments"
                                            ),
                                            "payload": {
                                                "vanityName": shown_slug,
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

    pid = profile_id_override or profile_id(person)
    top_texts: list[Any] = [
        *[_text_el(text, f"· {degree}") for _ in range(degree_runs)],
        _text_el(text, person.headline or ""),
        *[_text_el(text, run) for run in extra_top_runs],
    ]
    if location is not None:
        top_texts.append(_text_el(text, location))
    top_texts.append(_text_el(text, "·"))
    top_texts.extend(
        _el(f"$L{text}", {"textProps": {"children": [contact_link()]}})
        for _ in range(contact_links)
    )
    top_texts.append(_text_el(text, "500+ connections"))
    buttons: list[Any] = []
    if identity == "message":
        buttons.append(
            _identity_button(link, person, profile_id=pid, slug=person.slug, raw_urn=raw_urn)
        )
    elif identity == "bare":
        buttons.append(_identity_button(link, person, profile_id=pid, slug=None))
    elif identity == "viewee":
        buttons.append(
            _identity_button(link, person, profile_id=pid, slug=person.slug, key="vieweeProfileId")
        )
    buttons.extend(
        _identity_button(link, other, profile_id=profile_id(other), slug=other.slug)
        for other in mutuals
    )
    tops = [
        rows.model(
            _el(
                f"$L{client}",
                {
                    "componentKey": f"fake-top-card-{n}",
                    "viewTrackingSpecs": {"viewName": "profile-top-card"},
                    "children": [_el("section", {"children": top_texts}), *buttons],
                },
            )
        )
        for n in range(top_cards)
    ]
    main: list[str] = [f"$L{top}" for top in tops]
    if experience_inline and (roles or groups):
        card = _experience_card(rows, text, client, roles, groups, legacy_titles=legacy_titles)
        main.append(f"$L{card}")
    if schools:
        entries = [
            _el(
                "li",
                {
                    "children": [
                        _text_el(text, run)
                        for run in (school.school, school.degree, school.years)
                        if run is not None
                    ]
                },
            )
            for school in schools
        ]
        education = rows.model(
            _el(
                f"$L{client}",
                {
                    "componentKey": "fake-education-card",
                    "viewTrackingSpecs": {"viewName": "profile-card-education"},
                    "children": [
                        _text_el(text, "Education", tagName="h2"),
                        _el("ul", {"children": entries}),
                    ],
                },
            )
        )
        main.append(f"$L{education}")
    if also_viewed:
        rail = rows.model(
            _el(
                "aside",
                {
                    "children": [
                        _el(
                            f"$L{link}",
                            {
                                "action": _profile_click(other)["action"],
                                "children": [display_name(other)],
                            },
                        )
                        for other in also_viewed
                    ]
                },
            )
        )
        main.append(f"$L{rail}")
    seeds = _click(
        [
            _set_state(NAME_STATE_ID, "stringValue", display_name(person)),
            _set_state(HEADLINE_STATE_ID, "stringValue", person.headline or ""),
        ]
    )
    rows.model([_el("main", {"children": main}), {"triggers": [seeds]}], row="0")
    return rows.payload()


def experience_payload(
    roles: Sequence[Role], groups: Sequence[RoleGroup] = (), *, legacy_titles: bool = False
) -> bytes:
    """A lazy experience card (``actions/component?componentId=...profileCardsExperienceOnly``).

    The capture saw experience load this way on one profile but did not keep the answer,
    so this reuses the inline card's shape.
    """
    rows = _Rows()
    text = rows.module("default")
    client = rows.module("ClientComponent")
    card = _experience_card(rows, text, client, roles, groups, legacy_titles=legacy_titles)
    rows.model([f"$L{card}"], row="0")
    return rows.payload()


def _wrapped(site: str) -> str:
    """A site behind LinkedIn's wrapper, as the capture's overlay linked it: ``/safety/go``
    with the site in ``url`` and ``isSdui``, ``mt``, and ``urlhash`` beside it."""
    query = urlencode({"url": site, "urlhash": "FAKE", "isSdui": "true", "mt": "fake"})
    return f"https://www.linkedin.com/safety/go/?{query}"


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
    twitter: Sequence[str] = (),
    birthday: str | None = None,
    address: str | None = None,
    profile_slug: str | None = None,
    profile_section: bool = True,
    email_urls: Sequence[str] | None = None,
    website_urls: Sequence[str] | None = None,
    extra_sections: Sequence[str] = (),
) -> bytes:
    """The contact-info overlay's answer (``POST .../actions/navigation``), trimmed.

    One section per kind, each an element with ``viewTrackingSpecs.viewName`` of
    ``contact-your-profile``, ``contact-website``, ``contact-email`` (captured) or
    ``contact-phone``, ``contact-twitter``, ``contact-birthday``, ``contact-address``
    (**invented**: the capture's profile shared none of them, so the names are guesses
    by analogy), a ``p`` heading, and one link per value whose action is a
    ``NavigateToUrl`` (or, for the birthday and the address, a text). Websites point
    through a ``linkedin.com`` wrapper (``/safety/go``) in the capture, and so do these. Use
    example.test addresses only. ``profile_slug`` makes the profile link name another
    slug; ``email_urls``/``website_urls`` replace the links' urls to spoil the shape.
    """
    rows = _Rows()
    item = rows.module("default")
    link = rows.module("default")

    def section(
        view: str,
        control: str,
        heading: str,
        links: Sequence[tuple[str, str]],
        texts: Sequence[str] = (),
    ) -> Any:
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
                            *[_el("span", {"children": [value]}) for value in texts],
                        ]
                    },
                ),
            },
        )

    slug = person.slug if profile_slug is None else profile_slug
    sections = []
    if profile_section:
        sections.append(
            section(
                "contact-your-profile",
                "contact_share_profile",
                "Your Profile",
                [(f"https://www.linkedin.com/in/{slug}", f"linkedin.com/in/{slug}")],
            )
        )
    if websites or website_urls:
        urls = (
            list(website_urls)
            if website_urls is not None
            else [_wrapped(site.url) for site in websites]
        )
        shown = [f"{site.url} {site.label}" if site.label else site.url for site in websites]
        shown += [""] * (len(urls) - len(shown))
        sections.append(
            section(
                "contact-website",
                "contact_website",
                "Website",
                list(zip(urls, shown, strict=False)),
            )
        )
    if phones:
        sections.append(
            section("contact-phone", "contact_phone", "Phone", [(f"tel:{p}", p) for p in phones])
        )
    if twitter:
        sections.append(
            section(
                "contact-twitter",
                "contact_twitter",
                "Twitter",
                [(f"https://twitter.com/{handle}", f"@{handle}") for handle in twitter],
            )
        )
    if emails or email_urls:
        urls = list(email_urls) if email_urls is not None else [f"mailto:{e}" for e in emails]
        shown = list(emails) + [""] * (len(urls) - len(emails))
        sections.append(
            section("contact-email", "contact_email", "Email", list(zip(urls, shown, strict=False)))
        )
    if birthday is not None:
        sections.append(section("contact-birthday", "contact_birthday", "Birthday", [], [birthday]))
    if address is not None:
        sections.append(section("contact-address", "contact_address", "Address", [], [address]))
    for view in extra_sections:
        sections.append(section(view, view, "Something new", [], ["a value"]))
    children: list[Any] = [*sections]
    if connected_since is not None:
        children.append(_el("p", {"children": ["Connected since"]}))
        children.append(_el("span", {"children": [connected_since]}))
    body = rows.model(_el("div", {"data-testid": "fake-overlay", "children": children}))
    rows.model([f"$L{body}"], row="0")
    return rows.payload()
