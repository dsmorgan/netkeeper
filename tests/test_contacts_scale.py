"""The contacts list at ten thousand rows (item P1-05's "done when").

The bar is 300 ms for one page of the Contacts table over a ten-thousand-contact
address book, measured through the ASGI stack, not against the service: the page
has to pay for the count, the sort, the page, and the children of the rows on it.

A page is a fixed cost plus the count, so the ways this regresses are few and
known: an index the filter or the sort stops using, and a child relationship
loaded per row instead of per page (:func:`netkeeper.crm.contacts.query` uses
``selectinload``, two queries for the whole page). ``test_a_page_is_a_fixed
_number_of_queries`` fails on the second one directly, so a regression names
itself rather than showing up as a slow clock on a busy machine.

The timing runs after a warm-up and takes the best of a few runs, because the
number that matters is how long the work takes, not how long a cold cache and a
loaded CI box take.
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, event, insert, select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.crm.filters import parse_filter
from netkeeper.db import session_scope
from netkeeper.models import (
    Contact,
    ContactEmail,
    ContactList,
    ContactPhone,
    ContactSource,
    ListKind,
    ListMember,
    User,
    UserKind,
)
from netkeeper.scoping import scoped

CSRF = {"X-Netkeeper-Client": "1"}
CONTACTS = 10_000
BUDGET_MS = 300.0
RUNS = 5

# Invented people: a name built from the row number, and companies and places
# that repeat, so a filter matches a realistic slice instead of one row.
COMPANIES = ("Blueleaf", "Tinwork", "Roan Labs", "Harborline", "Pellmont")
PLACES = ("Portland", "Denver", "Austin", "Ithaca", "Tacoma")
TITLES = ("Staff Engineer", "Design Lead", "Founder", "Recruiter", "Analyst")


@pytest.fixture
def crowd(running_app: FastAPI) -> int:
    """Ten thousand contacts of the local user, a tenth of them with an email and a phone."""
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = session.scalars(
            select(User).where(User.kind == UserKind.LOCAL).order_by(User.id)
        ).first()
        assert user is not None
        _insert_crowd(session, user)
        return user.id


def _insert_crowd(session: Session, user: User) -> None:
    """Core inserts with ``user_id`` in every row: the shape ADR 0005 asks for in bulk."""
    start = date(2019, 1, 1)
    rows: list[dict[str, Any]] = [
        {
            "user_id": user.id,
            "li_urn": f"urn:li:fsd_profile/SEED{n:06d}",
            "li_public_id": f"person-{n:05d}",
            "li_url": f"https://www.linkedin.com/in/person-{n:05d}/",
            "first_name": f"Given{n:05d}",
            "last_name": f"Family{n % 997:03d}",
            "headline": f"{TITLES[n % len(TITLES)]} at {COMPANIES[n % len(COMPANIES)]}",
            "current_title": TITLES[n % len(TITLES)],
            "current_company": COMPANIES[n % len(COMPANIES)],
            "location": PLACES[n % len(PLACES)],
            "connected_on": start + timedelta(days=n % 2000),
            "degree": 1,
            "source": ContactSource.ARCHIVE,
            "archived_at": None,
        }
        for n in range(CONTACTS)
    ]
    session.execute(insert(Contact), rows)
    session.flush()
    ids = list(session.scalars(scoped(user, Contact).with_only_columns(Contact.id)))
    assert len(ids) == CONTACTS
    session.execute(
        insert(ContactEmail),
        [
            {
                "user_id": user.id,
                "contact_id": contact_id,
                "email": f"person{index:05d}@example.test",
                "is_primary": True,
                "source": ContactSource.ARCHIVE,
                "observed_at": datetime(2026, 1, 1, tzinfo=UTC),
            }
            for index, contact_id in enumerate(ids)
            if index % 10 == 0
        ],
    )
    session.execute(
        insert(ContactPhone),
        [
            {
                "user_id": user.id,
                "contact_id": contact_id,
                "raw": f"+1555{index:07d}",
                "number_e164": f"+1555{index:07d}",
                "is_primary": True,
                "source": ContactSource.ARCHIVE,
                "observed_at": datetime(2026, 1, 1, tzinfo=UTC),
            }
            for index, contact_id in enumerate(ids)
            if index % 10 == 0
        ],
    )


PAGE: dict[str, Any] = {
    "filter": {
        "where": {
            "op": "and",
            "children": [
                {"op": "eq", "field": "location", "value": "Portland"},
                {"op": "contains", "field": "current_company", "value": "lea"},
            ],
        }
    },
    "sort": [{"field": "last_name"}, {"field": "first_name"}],
    "limit": 50,
    "offset": 0,
}


def _ms(timings: list[float]) -> str:
    """The run times, rounded, for a failure message or a ``-s`` run."""
    return ", ".join(f"{value:.1f}" for value in timings)


async def _timed(client: httpx.AsyncClient, body: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    started = time.perf_counter()
    response = await client.post("/api/v1/contacts/query", json=body, headers=CSRF)
    elapsed = (time.perf_counter() - started) * 1000
    assert response.status_code == 200, response.text
    page: dict[str, Any] = response.json()
    return elapsed, page


async def test_ten_thousand_contacts_list_under_the_budget(
    client: httpx.AsyncClient, crowd: int
) -> None:
    await _timed(client, PAGE)  # warm up: connection, compiled statements, page cache
    timings = []
    for _ in range(RUNS):
        elapsed, page = await _timed(client, PAGE)
        timings.append(elapsed)
        assert len(page["items"]) == 50
        assert page["total"] > 100, "the filter should select a realistic slice, not a handful"
    best = min(timings)
    print(f"MEASURED filtered page: best {best:.1f} ms of {_ms(timings)}")
    assert best < BUDGET_MS, (
        f"a page of {CONTACTS} contacts took {best:.0f} ms, over the {BUDGET_MS:.0f} ms budget "
        f"(runs: {_ms(timings)}). Look for an index the filter or the sort "
        "stopped using, or a child relationship loading per row."
    )


async def test_an_unfiltered_page_is_under_the_budget_too(
    client: httpx.AsyncClient, crowd: int
) -> None:
    """The worst case for the count: every row matches, so nothing narrows it."""
    body: dict[str, Any] = {"sort": [{"field": "last_name"}], "limit": 50}
    await _timed(client, body)
    timings = []
    for _ in range(RUNS):
        elapsed, page = await _timed(client, body)
        timings.append(elapsed)
        assert page["total"] == CONTACTS
    best = min(timings)
    print(f"MEASURED unfiltered page: best {best:.1f} ms of {_ms(timings)}")
    assert best < BUDGET_MS, f"an unfiltered page of {CONTACTS} took {best:.0f} ms"


async def test_a_page_is_a_fixed_number_of_queries(
    client: httpx.AsyncClient, crowd: int, bare_engine: Engine
) -> None:
    """No N+1: the count, the page, and one query per preloaded child collection."""
    await _timed(client, PAGE)  # the local user lookup and the schema are warm
    statements: list[str] = []

    @event.listens_for(bare_engine, "before_cursor_execute")
    def record(*args: Any) -> None:
        statement: str = args[2]
        if not statement.startswith(("BEGIN", "COMMIT", "ROLLBACK", "PRAGMA")):
            statements.append(statement)

    try:
        _, page = await _timed(client, PAGE)
    finally:
        event.remove(bare_engine, "before_cursor_execute", record)

    assert len(page["items"]) == 50
    selects = [statement for statement in statements if statement.lstrip().startswith("SELECT")]
    assert len(selects) <= 5, (
        f"{len(selects)} selects for one page of 50: the current user, the count, the page, "
        f"and one each for emails and phones is the budget. {selects}"
    )


async def test_the_worst_page_is_still_under_the_budget(
    client: httpx.AsyncClient, crowd: int
) -> None:
    """The deepest page, ordered by a column no index covers: the slowest shape the UI can ask."""
    body: dict[str, Any] = {
        "sort": [{"field": "headline", "direction": "desc"}],
        "limit": 50,
        "offset": CONTACTS - 50,
    }
    await _timed(client, body)
    timings = []
    for _ in range(RUNS):
        elapsed, page = await _timed(client, body)
        timings.append(elapsed)
        assert len(page["items"]) == 50
    best = min(timings)
    print(f"MEASURED worst page: best {best:.1f} ms of {_ms(timings)}")
    assert best < BUDGET_MS, f"the last page of {CONTACTS} took {best:.0f} ms"


# --- list_member at scale (P1-27) -------------------------------------------

LIST_MEMBERS = 2_000


@pytest.fixture
def crowd_lists(running_app: FastAPI, crowd: int) -> tuple[int, int]:
    """A static list holding a fifth of the crowd, and a smart list defined over it.

    Returns ``(static_id, outer_id)``. The outer list is the nested case: a
    smart list whose filter is "met, and in the static list", so compiling it
    inlines one tree and one ``list_members`` subquery.
    """
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = session.get_one(User, crowd)
        static = ContactList(user_id=user.id, name="First 2000", kind=ListKind.STATIC)
        session.add(static)
        session.flush()
        ids = list(session.scalars(scoped(user, Contact).with_only_columns(Contact.id)))[
            :LIST_MEMBERS
        ]
        session.execute(
            insert(ListMember),
            [
                {
                    "user_id": user.id,
                    "list_id": static.id,
                    "contact_id": contact_id,
                    "added_at": datetime(2026, 1, 1, tzinfo=UTC),
                }
                for contact_id in ids
            ],
        )
        outer = ContactList(
            user_id=user.id,
            name="Portland in the first 2000",
            kind=ListKind.SMART,
            filter_json=parse_filter(
                {
                    "where": {
                        "op": "and",
                        "children": [
                            {"op": "eq", "field": "location", "value": "Portland"},
                            {"op": "list_member", "list_id": static.id},
                        ],
                    }
                }
            ).model_dump(mode="json"),
        )
        session.add(outer)
        session.flush()
        return static.id, outer.id


def _list_page(list_id: int) -> dict[str, Any]:
    return {
        "filter": {"where": {"op": "list_member", "list_id": list_id}},
        "sort": [{"field": "last_name"}],
        "limit": 50,
        "offset": 0,
    }


async def test_a_static_list_of_two_thousand_pages_under_the_budget(
    client: httpx.AsyncClient, crowd_lists: tuple[int, int]
) -> None:
    """The EXISTS is an index lookup per candidate row: ``(user_id, list_id, contact_id)``."""
    static_id, _ = crowd_lists
    body = _list_page(static_id)
    await _timed(client, body)
    timings = []
    for _ in range(RUNS):
        elapsed, page = await _timed(client, body)
        timings.append(elapsed)
        assert page["total"] == LIST_MEMBERS
        assert len(page["items"]) == 50
    best = min(timings)
    print(f"MEASURED static list page: best {best:.1f} ms of {_ms(timings)}")
    assert best < BUDGET_MS, (
        f"a page of a {LIST_MEMBERS}-member static list took {best:.0f} ms, over the "
        f"{BUDGET_MS:.0f} ms budget (runs: {_ms(timings)}). Look for the unique index on "
        "list_members going unused."
    )


async def test_a_nested_list_filter_stays_under_the_budget_and_a_fixed_query_count(
    client: httpx.AsyncClient, crowd_lists: tuple[int, int], bare_engine: Engine
) -> None:
    """The nested case: a smart list over a static one, both resolved once per request.

    Resolving lists is one indexed lookup per distinct list id in the tree, at
    compile time — never per contact. A page compiles the tree twice, once for
    the count and once for the rows, and the cache lives on the compile, so the
    budget is the page's usual queries plus twice the number of lists named.
    What matters is that none of those numbers move with the size of the crowd
    or of the list.
    """
    _, outer_id = crowd_lists
    body = _list_page(outer_id)
    await _timed(client, body)
    timings = []
    for _ in range(RUNS):
        elapsed, page = await _timed(client, body)
        timings.append(elapsed)
        assert 0 < page["total"] < LIST_MEMBERS, "the nested filter should select a real slice"
    best = min(timings)
    print(f"MEASURED nested list page: best {best:.1f} ms of {_ms(timings)}")
    assert best < BUDGET_MS, f"a nested list page took {best:.0f} ms (runs: {_ms(timings)})"

    statements: list[str] = []

    @event.listens_for(bare_engine, "before_cursor_execute")
    def record(*args: Any) -> None:
        statement: str = args[2]
        if not statement.startswith(("BEGIN", "COMMIT", "ROLLBACK", "PRAGMA")):
            statements.append(statement)

    try:
        await _timed(client, body)
    finally:
        event.remove(bare_engine, "before_cursor_execute", record)

    selects = [statement for statement in statements if statement.lstrip().startswith("SELECT")]
    lookups = [statement for statement in selects if "FROM lists" in statement]
    assert len(lookups) == 4, (
        f"two lists, compiled twice (count and page), one cached lookup each: {lookups}"
    )
    assert len(selects) <= 9, (
        f"{len(selects)} selects for one page: the current user, the count, the page, emails, "
        f"phones, and one lookup per list per compile is the budget. {selects}"
    )
