"""Merge in the UI (#363): the merge preview and the possible-duplicate hint.

The preview runs the real merge in a rolled-back savepoint, so each scenario
here previews a merge, checks the database kept nothing, then merges for real
and compares the two answers. The scenarios are the merge rules a preview most
needs to show faithfully: the Met rule (#331), the card rules (#186), the review
mark of a reply to an old campaign (#65), and the campaign rows (#242).

The duplicate hint is read-only and scoped: it finds name, email, phone, and
renamed-slug matches, and never another user's contacts.

Every name, address and number here is invented.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import factories
import httpx
import pytest
from fastapi import FastAPI
from isolation.harness import acting_as
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.crm.lists import add_members, create_list
from netkeeper.crm.tags import create_tag, tag_contact
from netkeeper.db import session_scope
from netkeeper.models import (
    Contact,
    ContactMet,
    Enrollment,
    HistoryCampaign,
    HistoryRecipient,
    HistoryReplyKind,
    ListKind,
    Message,
    MessageStatus,
    MetSource,
    User,
    UserKind,
)
from netkeeper.scoping import scoped

CSRF = {"X-Netkeeper-Client": "1"}
AT = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


@pytest.fixture
def factory(running_app: FastAPI) -> sessionmaker[Session]:
    made: sessionmaker[Session] = running_app.state.session_factory
    return made


@pytest.fixture
def owner(factory: sessionmaker[Session]) -> User:
    with session_scope(factory) as session:
        user = session.scalars(
            select(User).where(User.kind == UserKind.LOCAL).order_by(User.id)
        ).first()
        assert user is not None
        return user


def _user(session: Session, owner: User) -> User:
    user = session.get(User, owner.id)
    assert user is not None
    return user


# --- scenarios ---------------------------------------------------------------


@dataclass(frozen=True)
class Pair:
    survivor: int
    loser: int


Scenario = Callable[[Session, User], Pair]


def plain(session: Session, user: User) -> Pair:
    """Children deduplicated by key, a tag and a list carried, notes joined."""
    tag = create_tag(session, user, "Climbing")
    survivor = factories.make_contact(
        session,
        user,
        first_name="Ada",
        last_name="Quill",
        emails=["ada@example.test"],
        phones=["+15550100001"],
        notes="Met at the meetup.",
    )
    loser = factories.make_contact(
        session,
        user,
        first_name="Ada",
        last_name="Quill",
        li_urn=None,
        li_public_id=None,
        emails=["ada@example.test", "ada.q@example.test"],
        phones=["+15550100002"],
        notes="Climbs on Tuesdays.",
    )
    tag_contact(session, user, loser.id, tag.id)
    roster = create_list(session, user, "First 100", ListKind.STATIC)
    add_members(session, user, roster.id, [loser.id])
    return Pair(survivor.id, loser.id)


def met_rule(session: Session, user: User) -> Pair:
    """#331: the person's confirmed answer on the loser beats a batch's on the survivor."""
    survivor = factories.make_contact(
        session, user, met=ContactMet.NOT_MET, met_source=MetSource.AUTOMATIC, triaged_at=AT
    )
    loser = factories.make_contact(
        session,
        user,
        li_urn=None,
        li_public_id=None,
        met=ContactMet.NOT_MET,
        met_source=MetSource.MANUAL,
        triaged_at=AT,
    )
    return Pair(survivor.id, loser.id)


def card_headline(session: Session, user: User) -> Pair:
    """#186: a card contact merged into a confirmed one with no headline leaves none."""
    survivor = factories.make_contact(session, user, headline=None, li_public_id="bo-marsh")
    loser = factories.make_contact(
        session,
        user,
        li_urn=None,
        li_public_id="bo-marsh-2",
        headline="Design Lead at Tinwork",
        field_sources={},
        needs_review_at=AT,
    )
    return Pair(survivor.id, loser.id)


def into_a_card(session: Session, user: User) -> Pair:
    """#184/#186: a confirmed loser's fields win over a card survivor's unrecorded text."""
    survivor = factories.make_contact(
        session,
        user,
        li_urn=None,
        li_public_id="cy-odell-new",
        first_name="Cy",
        last_name="Odell",
        headline="Founder",
        field_sources={},
        needs_review_at=AT,
    )
    loser = factories.make_contact(
        session,
        user,
        first_name="Cyrus",
        last_name="Odell",
        headline="Founder at Roan Labs",
        li_public_id="cy-odell",
    )
    return Pair(survivor.id, loser.id)


def history_reply(session: Session, user: User) -> Pair:
    """#65: a reply to an old campaign moves, and the mark waiting on it stays."""
    survivor = factories.make_contact(session, user)
    loser = factories.make_contact(
        session, user, li_urn=None, li_public_id=None, needs_review_at=AT
    )
    campaign = HistoryCampaign(
        user_id=user.id, name="Spring note", started_on=date(2025, 3, 1), source_sha256="0" * 64
    )
    session.add(campaign)
    session.flush()
    session.add(
        HistoryRecipient(
            user_id=user.id,
            history_campaign_id=campaign.id,
            email="dev@example.test",
            contact_id=loser.id,
            reply_kind=HistoryReplyKind.REPLY,
        )
    )
    session.flush()
    return Pair(survivor.id, loser.id)


def campaign_rows(session: Session, user: User) -> Pair:
    """#242: one enrollment moves, one combines, and the outranked draft is discarded."""
    shared = factories.make_campaign(session, user)
    only_loser = factories.make_campaign(session, user)
    survivor = factories.make_contact(session, user, emails=["esme@example.test"])
    loser = factories.make_contact(session, user, li_urn=None, li_public_id=None)
    factories.make_enrollment(session, shared, survivor, current_step=1)
    outranked = factories.make_enrollment(session, shared, loser)
    factories.make_message(session, outranked, status=MessageStatus.DRAFTED, sent_at=None)
    moved = factories.make_enrollment(session, only_loser, loser, current_step=1)
    factories.make_message(session, moved)
    return Pair(survivor.id, loser.id)


SCENARIOS: dict[str, Scenario] = {
    "plain": plain,
    "met_rule": met_rule,
    "card_headline": card_headline,
    "into_a_card": into_a_card,
    "history_reply": history_reply,
    "campaign_rows": campaign_rows,
}


def _arrange(factory: sessionmaker[Session], owner: User, scenario: Scenario) -> Pair:
    with session_scope(factory, write=True) as session:
        return scenario(session, _user(session, owner))


async def _preview(client: httpx.AsyncClient, survivor: int, loser: int) -> httpx.Response:
    return await client.post(
        f"/api/v1/contacts/{survivor}/merge/preview", json={"loser_id": loser}, headers=CSRF
    )


async def _merge(client: httpx.AsyncClient, survivor: int, loser: int) -> httpx.Response:
    return await client.post(
        f"/api/v1/contacts/{survivor}/merge", json={"loser_id": loser}, headers=CSRF
    )


def _comparable(detail: dict[str, Any]) -> dict[str, Any]:
    """The detail without ``updated_at``, which the real merge stamps again."""
    return {key: value for key, value in detail.items() if key != "updated_at"}


def _rows(factory: sessionmaker[Session], owner: User) -> dict[str, list[tuple[Any, ...]]]:
    """Everything a merge touches, as plain tuples, to show a preview left it alone."""
    with session_scope(factory) as session:
        user = _user(session, owner)
        return {
            "contacts": [
                (c.id, c.merged_into_id, c.met, c.met_source, c.headline, c.needs_review_at)
                for c in session.scalars(scoped(user, Contact).order_by(Contact.id))
            ],
            "enrollments": [
                (e.id, e.contact_id, e.status, e.current_step)
                for e in session.scalars(scoped(user, Enrollment).order_by(Enrollment.id))
            ],
            "messages": [
                (m.id, m.contact_id, m.enrollment_id, m.status)
                for m in session.scalars(scoped(user, Message).order_by(Message.id))
            ],
            "history": [
                (h.id, h.contact_id)
                for h in session.scalars(
                    scoped(user, HistoryRecipient).order_by(HistoryRecipient.id)
                )
            ],
        }


# --- the preview -------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_the_preview_is_the_merge_and_keeps_nothing(
    client: httpx.AsyncClient, factory: sessionmaker[Session], owner: User, name: str
) -> None:
    pair = _arrange(factory, owner, SCENARIOS[name])
    before = _rows(factory, owner)
    survivor_before = (await client.get(f"/api/v1/contacts/{pair.survivor}")).json()

    response = await _preview(client, pair.survivor, pair.loser)

    assert response.status_code == 200, response.text
    preview = response.json()
    assert _rows(factory, owner) == before, "the preview wrote something"
    assert preview["survivor"] == survivor_before
    assert preview["loser"]["id"] == pair.loser
    assert preview["loser"]["merged_into_id"] is None
    assert preview["undoable"] is False

    merged = await _merge(client, pair.survivor, pair.loser)
    assert merged.status_code == 200, merged.text
    assert _comparable(preview["result"]) == _comparable(merged.json())


async def test_the_preview_shows_the_met_rule(
    client: httpx.AsyncClient, factory: sessionmaker[Session], owner: User
) -> None:
    pair = _arrange(factory, owner, met_rule)
    result = (await _preview(client, pair.survivor, pair.loser)).json()["result"]
    assert (result["met"], result["met_source"]) == ("not_met", "manual")


async def test_the_preview_shows_the_card_rules(
    client: httpx.AsyncClient, factory: sessionmaker[Session], owner: User
) -> None:
    pair = _arrange(factory, owner, card_headline)
    result = (await _preview(client, pair.survivor, pair.loser)).json()["result"]
    assert result["headline"] is None
    assert result["needs_review_at"] is None

    pair = _arrange(factory, owner, into_a_card)
    result = (await _preview(client, pair.survivor, pair.loser)).json()["result"]
    assert (result["first_name"], result["headline"]) == ("Cyrus", "Founder at Roan Labs")


async def test_the_preview_keeps_a_reply_s_review_mark(
    client: httpx.AsyncClient, factory: sessionmaker[Session], owner: User
) -> None:
    pair = _arrange(factory, owner, history_reply)
    body = (await _preview(client, pair.survivor, pair.loser)).json()
    assert body["result"]["needs_review_at"] is not None
    assert body["moves"]["history_rows"] == 1


async def test_the_preview_counts_what_moves(
    client: httpx.AsyncClient, factory: sessionmaker[Session], owner: User
) -> None:
    pair = _arrange(factory, owner, plain)
    moves = (await _preview(client, pair.survivor, pair.loser)).json()["moves"]
    assert moves["emails"] == {"moved": 1, "dropped": 1}
    assert moves["phones"] == {"moved": 1, "dropped": 0}
    assert moves["tags_added"] == ["Climbing"]
    assert moves["tags_removed"] == []
    assert moves["lists_added"] == ["First 100"]

    pair = _arrange(factory, owner, campaign_rows)
    moves = (await _preview(client, pair.survivor, pair.loser)).json()["moves"]
    assert (moves["enrollments_moved"], moves["enrollments_combined"]) == (1, 1)
    assert (moves["messages_moved"], moves["messages_discarded"]) == (2, 1)


async def test_the_preview_refuses_what_the_merge_refuses(
    client: httpx.AsyncClient, factory: sessionmaker[Session], owner: User
) -> None:
    pair = _arrange(factory, owner, plain)
    same = await _preview(client, pair.survivor, pair.survivor)
    assert same.status_code == 409
    assert "itself" in same.json()["detail"]
    assert (await _preview(client, pair.survivor, 424242)).status_code == 404
    assert (await _preview(client, 424242, pair.loser)).status_code == 404

    assert (await _merge(client, pair.survivor, pair.loser)).status_code == 200
    gone = await _preview(client, pair.loser, pair.survivor)
    assert gone.status_code == 409
    assert gone.json() == {"detail": "merged", "merged_into_id": pair.survivor}


async def test_the_preview_reaches_no_other_users_contacts(
    client: httpx.AsyncClient,
    running_app: FastAPI,
    factory: sessionmaker[Session],
    owner: User,
) -> None:
    pair = _arrange(factory, owner, plain)
    with session_scope(factory, write=True) as session:
        other = factories.make_user(session)
        stranger, theirs = other.id, factories.make_contact(session, other).id
    with acting_as(running_app, stranger):
        assert (await _preview(client, theirs, pair.loser)).status_code == 404
        assert (await _preview(client, pair.survivor, theirs)).status_code == 404


# --- the duplicate hint ------------------------------------------------------


async def _duplicates(client: httpx.AsyncClient, contact_id: int) -> list[dict[str, Any]]:
    response = await client.get(f"/api/v1/contacts/{contact_id}/duplicates")
    assert response.status_code == 200, response.text
    found: list[dict[str, Any]] = response.json()
    return found


def _matches(found: list[dict[str, Any]]) -> dict[int, list[str]]:
    return {row["contact_id"]: row["matched_by"] for row in found}


async def test_the_hint_finds_name_email_and_phone_matches(
    client: httpx.AsyncClient, factory: sessionmaker[Session], owner: User
) -> None:
    with session_scope(factory, write=True) as session:
        user = _user(session, owner)
        card = factories.make_contact(
            session,
            user,
            first_name="Gita",
            last_name="Sample",
            li_urn=None,
            li_public_id="gita-sample-new",
            emails=["gita@example.test"],
            phones=["+15550100009"],
            needs_review_at=AT,
        )
        by_name = factories.make_contact(
            session, user, first_name=" gita ", last_name="SAMPLE", li_public_id="gita-sample"
        )
        by_preferred = factories.make_contact(
            session,
            user,
            first_name="Margarita",
            preferred_name="Gita",
            last_name="Sample",
            li_public_id=None,
        )
        by_email = factories.make_contact(session, user, emails=["gita@example.test"])
        by_phone = factories.make_contact(session, user, phones=["+15550100009"])
        stranger = factories.make_contact(session, user, first_name="Gita", last_name="Other")
        archived = factories.make_contact(
            session, user, first_name="Gita", last_name="Sample", archived_at=AT
        )
        ids = card.id, by_name.id, by_preferred.id, by_email.id, by_phone.id
        excluded = stranger.id, archived.id

    found = _matches(await _duplicates(client, ids[0]))

    assert found == {
        ids[1]: ["slug", "name"],
        ids[2]: ["name"],
        ids[3]: ["email"],
        ids[4]: ["phone"],
    }
    assert not set(excluded) & set(found)
    # Strongest first: an address, then a number, then a renamed slug, then a name.
    assert list(found) == [ids[3], ids[4], ids[1], ids[2]]


async def test_the_hint_stays_conservative(
    client: httpx.AsyncClient, factory: sessionmaker[Session], owner: User
) -> None:
    with session_scope(factory, write=True) as session:
        user = _user(session, owner)
        one_word = factories.make_contact(session, user, first_name="Hal", last_name="")
        factories.make_contact(session, user, first_name="Hal", last_name="")
        synced = factories.make_contact(session, user, first_name="Iris", last_name="Testerly")
        other_urn = factories.make_contact(
            session,
            user,
            first_name="Iris",
            last_name="Testerly",
            emails=["iris@example.test"],
        )
        merged_away = factories.make_contact(
            session,
            user,
            first_name="Iris",
            last_name="Testerly",
            li_urn=None,
            merged_into_id=other_urn.id,
        )
        ids = one_word.id, synced.id, merged_away.id

    assert await _duplicates(client, ids[0]) == []
    # Two URNs are two people, whatever the names say; a merged-away row is no one.
    assert await _duplicates(client, ids[1]) == []


async def test_the_hint_never_crosses_users(
    client: httpx.AsyncClient,
    running_app: FastAPI,
    factory: sessionmaker[Session],
    owner: User,
) -> None:
    with session_scope(factory, write=True) as session:
        mine = factories.make_contact(
            session,
            _user(session, owner),
            first_name="Jai",
            last_name="Fictional",
            li_urn=None,
            emails=["jai@example.test"],
            phones=["+15550100010"],
        ).id
        stranger = factories.make_user(session)
        theirs = factories.make_contact(
            session,
            stranger,
            first_name="Jai",
            last_name="Fictional",
            li_urn=None,
            emails=["jai@example.test"],
            phones=["+15550100010"],
        ).id
        stranger_id = stranger.id

    assert await _duplicates(client, mine) == []
    with acting_as(running_app, stranger_id):
        assert await _duplicates(client, theirs) == []
        assert (await client.get(f"/api/v1/contacts/{mine}/duplicates")).status_code == 404


async def test_the_hint_writes_nothing(
    client: httpx.AsyncClient, factory: sessionmaker[Session], owner: User
) -> None:
    pair = _arrange(factory, owner, plain)
    before = _rows(factory, owner)
    assert _matches(await _duplicates(client, pair.loser)) == {pair.survivor: ["email", "name"]}
    assert _rows(factory, owner) == before


def test_the_hint_limit_is_pinned() -> None:
    from netkeeper.crm.duplicates import DUPLICATE_LIMIT

    assert DUPLICATE_LIMIT == 5
