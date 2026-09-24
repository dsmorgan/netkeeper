"""netkeeper.linkedin.contact_info: the contact-info source seam, with no database (spec 9.3, 9.10).

``ApiContactInfoSource`` is a thin wrapper around
:func:`netkeeper.linkedin.voyager.parse_contact_info` and
:func:`~netkeeper.linkedin.classify.classify`, exercised here the same way
``tests/test_linkedin_connections.py`` exercises ``VoyagerConnections``: an
in-memory fake ``VoyagerFetch``, no socket, nothing that reaches
linkedin.com. ``FallbackContactInfoSource`` is tested with plain scripted
fakes, mirroring ``test_linkedin_connections.py``'s ``FallbackConnectionsSource``
tests -- the two composites share the same switching logic and the same tests
for "cannot loop" and "cannot double-spend a fetch".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.contact_info import (
    ApiContactInfoSource,
    ContactInfoResult,
    FallbackContactInfoSource,
)
from netkeeper.linkedin.voyager import (
    CONTACT_INFO_ENDPOINT,
    ContactInfo,
    VoyagerRequest,
    VoyagerResponse,
    contact_info_path,
)

CONTACT_INFO_URL = "https://www.linkedin.com/voyager/api/identity/profiles/jamie/profileContactInfo"


@dataclass(slots=True)
class FakeFetch:
    """A ``VoyagerFetch`` over a scripted answer per call."""

    answers: list[VoyagerResponse]
    requests: list[VoyagerRequest] = field(default_factory=list)

    async def __call__(self, request: VoyagerRequest) -> VoyagerResponse:
        self.requests.append(request)
        return self.answers[len(self.requests) - 1]


def _body(**fields: object) -> str:
    return json.dumps(fields)


# --- ApiContactInfoSource ------------------------------------------------------------


async def test_an_ok_response_parses_into_contact_info() -> None:
    body = _body(
        emailAddress="jamie@example.test",
        phoneNumbers=[{"number": "+1-555-0100"}],
        websites=[{"url": "https://jamie.example.test"}],
        twitterHandles=[{"name": "jamiefake"}],
    )
    fetch = FakeFetch([VoyagerResponse(200, body, CONTACT_INFO_URL)])
    source = ApiContactInfoSource(fetch)

    result = await source.fetch_contact_info("jamie")

    assert source.endpoint == CONTACT_INFO_ENDPOINT
    assert result.outcome is Outcome.OK
    assert result.info == ContactInfo(
        email="jamie@example.test",
        phones=("+1-555-0100",),
        websites=("https://jamie.example.test",),
        twitter_handles=("jamiefake",),
    )
    assert fetch.requests[0].path == contact_info_path("jamie")


async def test_a_checkpoint_is_classified_and_never_parsed() -> None:
    fetch = FakeFetch(
        [
            VoyagerResponse(
                200,
                "<html>checkpoint</html>",
                "https://www.linkedin.com/checkpoint/challenge/?ctx=x",
            )
        ]
    )
    result = await ApiContactInfoSource(fetch).fetch_contact_info("jamie")
    assert result.outcome is Outcome.CHECKPOINT
    assert result.info is None


async def test_a_body_with_no_fields_shared_is_still_ok_and_all_empty() -> None:
    """Every field in ContactInfo is optional at the value level (a person can share
    none of them); an object missing every key is a legitimate 'shared nothing', not
    a shape change."""
    fetch = FakeFetch([VoyagerResponse(200, json.dumps({"somethingElse": True}), CONTACT_INFO_URL)])
    result = await ApiContactInfoSource(fetch).fetch_contact_info("jamie")
    assert result.outcome is Outcome.OK
    assert result.info == ContactInfo(email=None, phones=(), websites=(), twitter_handles=())


async def test_a_top_level_shape_that_is_not_an_object_is_route_changed() -> None:
    fetch = FakeFetch([VoyagerResponse(200, json.dumps([1, 2, 3]), CONTACT_INFO_URL)])
    result = await ApiContactInfoSource(fetch).fetch_contact_info("jamie")
    assert result.outcome is Outcome.ROUTE_CHANGED
    assert result.info is None


# --- FallbackContactInfoSource: automatic selection, no loop, no double spend -------


@dataclass(slots=True)
class ScriptedSource:
    """A ``ContactInfoSource`` that answers ``answers[i]`` on its ``i``-th call."""

    name: str
    answers: list[ContactInfoResult]
    calls: list[str] = field(default_factory=list)

    @property
    def endpoint(self) -> str:
        return self.name

    async def fetch_contact_info(self, public_id: str) -> ContactInfoResult:
        self.calls.append(public_id)
        return self.answers[len(self.calls) - 1]


_OK_INFO = ContactInfo(email="jamie@example.test", phones=(), websites=(), twitter_handles=())
_ROUTE_CHANGED = ContactInfoResult(outcome=Outcome.ROUTE_CHANGED, final_url=CONTACT_INFO_URL)


def _ok(info: ContactInfo = _OK_INFO) -> ContactInfoResult:
    return ContactInfoResult(outcome=Outcome.OK, final_url=CONTACT_INFO_URL, info=info)


async def test_fallback_reads_only_the_primary_until_a_route_change() -> None:
    primary = ScriptedSource("primary", [_ok()])
    fallback = ScriptedSource("fallback", [])
    source = FallbackContactInfoSource(primary, fallback)

    result = await source.fetch_contact_info("jamie")

    assert result.outcome is Outcome.OK
    assert source.endpoint == "primary"
    assert fallback.calls == []


async def test_fallback_switches_once_and_never_switches_back() -> None:
    primary = ScriptedSource(
        "primary",
        [_ok(), _ROUTE_CHANGED, _ok()],  # a 3rd answer that must never be read
    )
    fallback = ScriptedSource("fallback", [_ok(), _ok()])
    source = FallbackContactInfoSource(primary, fallback)

    first = await source.fetch_contact_info("alex")
    assert first.outcome is Outcome.OK
    assert source.endpoint == "primary"

    second = await source.fetch_contact_info("blair")  # primary's RouteChanged profile
    assert second.outcome is Outcome.OK
    assert source.endpoint == "fallback"

    third = await source.fetch_contact_info("casey")
    assert third.outcome is Outcome.OK
    assert source.endpoint == "fallback"

    # Exactly two primary calls ever: the switch is one-way.
    assert primary.calls == ["alex", "blair"]
    assert fallback.calls == ["blair", "casey"]


async def test_a_route_changed_profile_costs_exactly_one_primary_and_one_fallback_call() -> None:
    """The switch happens inside the one call the caller made -- a caller fetching
    one profile's contact info never has to know two sources were tried."""
    primary = ScriptedSource("primary", [_ROUTE_CHANGED])
    fallback = ScriptedSource("fallback", [_ok()])
    source = FallbackContactInfoSource(primary, fallback)

    result = await source.fetch_contact_info("jamie")

    assert result.outcome is Outcome.OK
    assert primary.calls == ["jamie"]
    assert fallback.calls == ["jamie"]


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(Outcome.THROTTLED, id="throttled"),
        pytest.param(Outcome.CHECKPOINT, id="checkpoint"),
        pytest.param(Outcome.LOGGED_OUT, id="logged-out"),
        pytest.param(Outcome.NOT_FOUND, id="not-found"),
    ],
)
async def test_fallback_never_switches_on_anything_but_route_changed(outcome: Outcome) -> None:
    """#173 review, R6 (mutation survived): a checkpoint, a throttle, a login wall,
    or a 404 from the primary must answer as itself -- never trigger a switch to
    DOM, which is only for "this endpoint's shape changed"."""
    primary = ScriptedSource(
        "primary", [ContactInfoResult(outcome=outcome, final_url=CONTACT_INFO_URL)]
    )
    fallback = ScriptedSource("fallback", [])
    source = FallbackContactInfoSource(primary, fallback)

    result = await source.fetch_contact_info("jamie")

    assert result.outcome is outcome
    assert not source.switched
    assert source.endpoint == "primary"
    assert fallback.calls == []
