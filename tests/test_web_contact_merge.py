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

import logging
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

from netkeeper.crm.identity import merge
from netkeeper.crm.lists import add_members, create_list
from netkeeper.crm.tags import create_tag, tag_contact
from netkeeper.db import session_scope
from netkeeper.models import (
    Contact,
    ContactMet,
    EmailStatus,
    Enrollment,
    EnrollmentStatus,
    HistoryCampaign,
    HistoryRecipient,
    HistoryReplyKind,
    ListKind,
    Message,
    MessageStatus,
    MetSource,
    TemplateChannel,
    User,
    UserKind,
)
from netkeeper.models.base import Base
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


def two_campaigns(session: Session, user: User) -> Pair:
    """#242 twice over: an opt-out on the loser beats the survivor's live enrollment in one
    campaign, and the survivor's further step wins in the other."""
    first = factories.make_campaign(session, user)
    second = factories.make_campaign(
        session, user, channels=(TemplateChannel.EMAIL, TemplateChannel.EMAIL)
    )
    survivor = factories.make_contact(session, user)
    loser = factories.make_contact(session, user, li_urn=None, li_public_id=None)
    live = factories.make_enrollment(session, first, survivor, current_step=1)
    factories.make_message(session, live, status=MessageStatus.DRAFTED, sent_at=None)
    opted_out = factories.make_enrollment(
        session, first, loser, status=EnrollmentStatus.OPTED_OUT, current_step=1
    )
    factories.make_message(session, opted_out)
    ahead = factories.make_enrollment(session, second, survivor, current_step=2)
    factories.make_message(session, ahead, position=2, status=MessageStatus.DRAFTED, sent_at=None)
    behind = factories.make_enrollment(session, second, loser, current_step=1)
    factories.make_message(session, behind)
    return Pair(survivor.id, loser.id)


def merge_chain(session: Session, user: User) -> Pair:
    """A loser that already absorbed another contact: the chain points at the survivor after."""
    survivor = factories.make_contact(session, user, emails=["hal@example.test"])
    loser = factories.make_contact(session, user, li_urn=None, li_public_id=None)
    earlier = factories.make_contact(
        session, user, li_urn=None, li_public_id="hal-old", emails=["hal.old@example.test"]
    )
    merge(session, user, loser.id, earlier.id)
    return Pair(survivor.id, loser.id)


def bounced(session: Session, user: User) -> Pair:
    """A bounced address on the loser, which the merge puts on the do-not-send list (#238)."""
    survivor = factories.make_contact(session, user)
    loser = factories.make_contact(
        session, user, li_urn=None, li_public_id=None, emails=["iris.bounced@example.test"]
    )
    loser.emails[0].status = EmailStatus.BOUNCED
    session.flush()
    return Pair(survivor.id, loser.id)


SCENARIOS: dict[str, Scenario] = {
    "two_campaigns": two_campaigns,
    "merge_chain": merge_chain,
    "bounced": bounced,
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


def _dump(factory: sessionmaker[Session]) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of every table, read on a plain connection: the whole database, as is."""
    engine = factory.kw["bind"]
    with engine.connect() as connection:
        return {
            table.name: sorted((tuple(row) for row in connection.execute(table.select())), key=repr)
            for table in Base.metadata.sorted_tables
        }


# --- the preview -------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_the_preview_is_the_merge_and_keeps_nothing(
    client: httpx.AsyncClient, factory: sessionmaker[Session], owner: User, name: str
) -> None:
    pair = _arrange(factory, owner, SCENARIOS[name])
    before = _rows(factory, owner)
    everything = _dump(factory)
    survivor_before = (await client.get(f"/api/v1/contacts/{pair.survivor}")).json()

    response = await _preview(client, pair.survivor, pair.loser)

    assert response.status_code == 200, response.text
    preview = response.json()
    assert _rows(factory, owner) == before, "the preview wrote something"
    assert _dump(factory) == everything, "the preview left a row behind somewhere"
    assert preview["survivor"] == survivor_before
    assert preview["loser"]["id"] == pair.loser
    assert preview["loser"]["merged_into_id"] is None
    assert preview["undoable"] is False

    merged = await _merge(client, pair.survivor, pair.loser)
    assert merged.status_code == 200, merged.text
    assert _comparable(preview["result"]) == _comparable(merged.json())


async def test_the_preview_counts_two_campaigns_and_a_chain(
    client: httpx.AsyncClient, factory: sessionmaker[Session], owner: User
) -> None:
    pair = _arrange(factory, owner, two_campaigns)
    moves = (await _preview(client, pair.survivor, pair.loser)).json()["moves"]
    assert (moves["enrollments_moved"], moves["enrollments_combined"]) == (0, 2)
    assert moves["messages_moved"] == 2
    # The opt-out wins the first campaign, so the survivor's draft there is discarded; the
    # survivor's further step wins the second, and its draft there stays.
    assert moves["messages_discarded"] == 1

    pair = _arrange(factory, owner, merge_chain)
    moves = (await _preview(client, pair.survivor, pair.loser)).json()["moves"]
    assert moves["emails"] == {"moved": 1, "dropped": 0}


async def test_the_preview_writes_no_merge_log_lines(
    client: httpx.AsyncClient,
    factory: sessionmaker[Session],
    owner: User,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pair = _arrange(factory, owner, bounced)
    caplog.set_level(logging.DEBUG, logger="netkeeper")

    assert (await _preview(client, pair.survivor, pair.loser)).status_code == 200

    messages = [record.getMessage() for record in caplog.records]
    assert not [line for line in messages if "merging contact" in line]
    assert not [line for line in messages if "do-not-send: entry added" in line]
    assert f"merge preview of contact {pair.loser} into {pair.survivor} (rolled back)" in messages

    # The real merge still logs both, so the check above can tell the difference.
    caplog.clear()
    assert (await _merge(client, pair.survivor, pair.loser)).status_code == 200
    messages = [record.getMessage() for record in caplog.records]
    assert [line for line in messages if "merging contact" in line]
    assert [line for line in messages if "do-not-send: entry added" in line]


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


def _differ(found: list[dict[str, Any]]) -> dict[int, bool]:
    return {row["contact_id"]: row["linkedin_ids_differ"] for row in found}


async def test_the_hint_needs_the_names_to_agree_and_ranks_them(
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
        renamed = factories.make_contact(
            session, user, first_name=" gita ", last_name="SAMPLE", li_public_id="gita-sample"
        )
        by_name = factories.make_contact(
            session, user, first_name="Gita", last_name="Sample", li_public_id=None
        )
        by_phone = factories.make_contact(
            session,
            user,
            first_name="Margarita",
            preferred_name="Gita",
            last_name="Sample",
            li_public_id=None,
            phones=["+15550100009"],
        )
        by_email = factories.make_contact(
            session,
            user,
            first_name="Gita",
            last_name="Sample",
            li_public_id=None,
            emails=["gita@example.test"],
        )
        # The same address or number under another name is no match at all.
        email_only = factories.make_contact(session, user, emails=["gita@example.test"])
        phone_only = factories.make_contact(session, user, phones=["+15550100009"])
        other_last = factories.make_contact(session, user, first_name="Gita", last_name="Other")
        archived = factories.make_contact(
            session, user, first_name="Gita", last_name="Sample", archived_at=AT
        )
        ids = card.id, by_email.id, by_phone.id, by_name.id, renamed.id
        excluded = email_only.id, phone_only.id, other_last.id, archived.id

    found = await _duplicates(client, ids[0])

    assert _matches(found) == {
        ids[1]: ["email", "name"],
        ids[2]: ["phone", "name"],
        ids[3]: ["name"],
        ids[4]: ["name"],
    }
    assert not set(excluded) & set(_matches(found))
    assert _differ(found) == {ids[1]: False, ids[2]: False, ids[3]: False, ids[4]: True}
    # An address, then a number, then a name; differing LinkedIn ids rank last.
    assert [row["contact_id"] for row in found] == [ids[1], ids[2], ids[3], ids[4]]


async def test_a_role_address_is_never_a_match(
    client: httpx.AsyncClient, factory: sessionmaker[Session], owner: User
) -> None:
    with session_scope(factory, write=True) as session:
        user = _user(session, owner)
        kai = factories.make_contact(
            session,
            user,
            first_name="Kai",
            last_name="Placeholder",
            li_public_id=None,
            emails=["info@globex.test", "Sales+EU@globex.test"],
        )
        colleague = factories.make_contact(
            session, user, first_name="Lena", last_name="Example", emails=["info@globex.test"]
        )
        namesake = factories.make_contact(
            session,
            user,
            first_name="Kai",
            last_name="Placeholder",
            li_urn=None,
            li_public_id=None,
            emails=["info@globex.test", "sales+eu@globex.test"],
        )
        ids = kai.id, colleague.id, namesake.id

    found = _matches(await _duplicates(client, ids[0]))
    # The colleague behind the same info@ is not named; the namesake is, by name only.
    assert found == {ids[2]: ["name"]}


async def test_a_different_john_smith_at_globex_ranks_last_or_not_at_all(
    client: httpx.AsyncClient, factory: sessionmaker[Session], owner: User
) -> None:
    with session_scope(factory, write=True) as session:
        user = _user(session, owner)
        john = factories.make_contact(
            session,
            user,
            first_name="John",
            last_name="Smith",
            current_company="Globex",
            li_urn=None,
            li_public_id="john-smith-globex",
            needs_review_at=AT,
        )
        other_slug = factories.make_contact(
            session,
            user,
            first_name="John",
            last_name="Smith",
            current_company="Globex",
            li_urn=None,
            li_public_id="john-smith-7f3a",
        )
        no_slug = factories.make_contact(
            session,
            user,
            first_name="John",
            last_name="Smith",
            current_company="Initech",
            li_urn=None,
            li_public_id=None,
        )
        synced = factories.make_contact(
            session, user, first_name="John", last_name="Smith", current_company="Globex"
        )
        other_urn = factories.make_contact(
            session, user, first_name="John", last_name="Smith", current_company="Globex"
        )
        ids = john.id, no_slug.id, other_slug.id, synced.id, other_urn.id

    found = await _duplicates(client, ids[0])
    # Differing slugs count against: still named, labeled, and after the one without.
    assert [row["contact_id"] for row in found][:2] == [ids[1], ids[2]]
    assert _differ(found)[ids[2]] is True
    assert _differ(found)[ids[1]] is False
    # Two contacts with different URNs are two people.
    synced_found = await _duplicates(client, ids[3])
    assert ids[4] not in _matches(synced_found)


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


def test_the_hint_limit_and_role_addresses_are_pinned() -> None:
    from netkeeper.crm.duplicates import DUPLICATE_LIMIT, ROLE_LOCAL_PARTS

    assert DUPLICATE_LIMIT == 5
    assert (
        frozenset(
            {
                "info",
                "hello",
                "sales",
                "office",
                "contact",
                "admin",
                "support",
                "team",
                "hr",
                "jobs",
                "careers",
                "noreply",
                "no-reply",
            }
        )
        == ROLE_LOCAL_PARTS
    )
