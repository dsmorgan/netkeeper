"""netkeeper.crm.apply.apply_harvest: a profile visit onto its contact (spec 9.4, 9.8, 10.5).

P2-07's "done when", at the mapping: a fixture harvest updates children
without downgrading known values to null. The attacks are the data-loss
directions: a harvest missing what is stored, a profile that is somebody
else's, a slug another contact holds, a field a person edited, and a
NotFound streak that must not mark anyone gone early.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker
from voyager_profiles import PROFILES, Job, Profile, contact_info_body, details_body

from netkeeper.crm import apply as mapping
from netkeeper.crm.apply import HarvestCounts, HarvestResult, apply_harvest
from netkeeper.crm.identity import merge
from netkeeper.crm.provenance import set_manual_field
from netkeeper.db import session_scope
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.enrich import ProfileHarvest
from netkeeper.linkedin.voyager import parse_contact_info, parse_profile_details
from netkeeper.models import (
    Contact,
    ContactAlias,
    ContactEmail,
    ContactPhone,
    ContactSource,
    EmailKind,
    InteractionKind,
    LinkKind,
    User,
)
from netkeeper.scoping import scoped

NOW = datetime(2026, 9, 23, 15, 0, tzinfo=UTC)
PRIYA, MATEO, HANA, TOMASZ, AIKO = PROFILES


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer)


def _stored(session: Session, user: User, profile: Profile, **overrides: Any) -> Contact:
    """``profile`` as a sync left it: URN, slug, name, and whatever ``overrides`` add."""
    values: dict[str, Any] = {
        "li_urn": profile.urn,
        "li_public_id": profile.slug,
        "first_name": profile.first,
        "last_name": profile.last,
        "headline": None,
        "current_title": None,
        "current_company": None,
        "source": ContactSource.SYNC,
    }
    values.update(overrides)
    contact = factories.make_contact(session, user, **values)
    contact.field_sources = {name: "sync" for name in ("li_urn", "li_public_id", "headline")}
    session.flush()
    return contact


def _harvest(
    contact: Contact, profile: Profile, *, at: datetime = NOW, slug: str | None = None
) -> ProfileHarvest:
    return ProfileHarvest(
        contact_ref=contact.id,
        requested_public_id=slug or profile.slug,
        outcome=Outcome.OK,
        observed_at=at,
        details=parse_profile_details(details_body(profile)),
        contact_info=parse_contact_info(contact_info_body(profile)),
    )


def _not_found(contact: Contact, at: datetime) -> ProfileHarvest:
    return ProfileHarvest(
        contact_ref=contact.id,
        requested_public_id=contact.li_public_id or "x",
        outcome=Outcome.NOT_FOUND,
        observed_at=at,
    )


# --- done when: children update and nothing known is downgraded to null -------------------


def test_a_harvest_fills_the_contact_and_its_children(writer: Session, user: User) -> None:
    contact = _stored(writer, user, PRIYA)

    result = apply_harvest(writer, user, _harvest(contact, PRIYA))

    assert result is HarvestResult.APPLIED
    assert contact.headline == PRIYA.headline
    assert contact.location == "Faketown, State of Example"
    assert (contact.current_title, contact.current_company) == (
        "Staff Data Engineer",
        "Fictional Robotics Co",
    )
    assert [e.email for e in contact.emails] == ["priya.fake.okafor@example.test"]
    assert contact.emails[0].is_primary and contact.emails[0].source is ContactSource.SYNC
    assert [(p.raw, p.number_e164) for p in contact.phones] == [("+1-555-0101", "+15550101")]
    assert {(link.url, link.kind) for link in contact.links} == {
        ("https://priya-fake-okafor.example.test", LinkKind.WEBSITE),
        ("https://twitter.com/priyafakeokafor", LinkKind.TWITTER),
    }
    positions = {(p.title, p.started_on, p.ended_on, p.is_current) for p in contact.positions}
    assert positions == {
        ("Staff Data Engineer", date(2023, 4, 1), None, True),
        ("Data Engineer", date(2019, 1, 1), date(2023, 3, 1), False),
    }
    assert contact.last_enriched_at == NOW


def test_a_harvest_missing_fields_leaves_every_known_value(writer: Session, user: User) -> None:
    """The review's attack: a sparse profile must not null out what the contact holds."""
    contact = _stored(
        writer,
        user,
        HANA,
        headline="Counsel at Hypothetical Holdings",
        location="Exampleton",
        current_title="Counsel",
        current_company="Hypothetical Holdings",
    )
    contact.emails.append(
        ContactEmail(user_id=user.id, email="hana.fake@example.test", source=ContactSource.CSV)
    )
    contact.phones.append(ContactPhone(user_id=user.id, raw="555-0103", source=ContactSource.CSV))
    writer.flush()

    result = apply_harvest(writer, user, _harvest(contact, HANA))

    assert result is HarvestResult.APPLIED
    assert contact.headline == "Counsel at Hypothetical Holdings"
    assert contact.location == "Exampleton"
    assert (contact.current_title, contact.current_company) == ("Counsel", "Hypothetical Holdings")
    assert [e.email for e in contact.emails] == ["hana.fake@example.test"]
    assert [p.raw for p in contact.phones] == ["555-0103"]
    assert contact.snapshots == []  # nothing changed, so no snapshot
    assert contact.last_enriched_at == NOW


def test_a_second_harvest_updates_children_and_never_removes_one(
    writer: Session, user: User
) -> None:
    contact = _stored(writer, user, PRIYA)
    apply_harvest(writer, user, _harvest(contact, PRIYA))
    moved_on = replace(
        PRIYA,
        email=None,
        phones=(),
        websites=(),
        twitter=(),
        jobs=(
            Job("Principal Engineer", "Madeup Mobility", start=(2026, 5)),
            Job("Staff Data Engineer", "Fictional Robotics Co", start=(2023, 4), end=(2026, 4)),
            Job("Data Engineer", "Placeholder Partners", start=(2019, 1), end=(2023, 3)),
        ),
    )

    apply_harvest(writer, user, _harvest(contact, moved_on, at=NOW + timedelta(days=200)))

    positions = {(p.title, p.ended_on, p.is_current) for p in contact.positions}
    assert positions == {
        ("Principal Engineer", None, True),
        ("Staff Data Engineer", date(2026, 4, 1), False),
        ("Data Engineer", date(2023, 3, 1), False),
    }
    assert len(contact.positions) == 3  # the same stints matched, not duplicated
    assert [e.email for e in contact.emails] == ["priya.fake.okafor@example.test"]
    assert len(contact.phones) == 1 and len(contact.links) == 2
    assert (contact.current_title, contact.current_company) == (
        "Principal Engineer",
        "Madeup Mobility",
    )


def test_a_position_that_loses_its_dates_keeps_the_ones_on_file(
    writer: Session, user: User
) -> None:
    """An end date is only ever filled in: a later harvest without it does not erase it."""
    contact = _stored(writer, user, PRIYA)
    apply_harvest(writer, user, _harvest(contact, PRIYA))
    undated_end = replace(
        PRIYA,
        jobs=(
            Job("Staff Data Engineer", "Fictional Robotics Co", start=(2023, 4)),
            Job("Data Engineer", "Placeholder Partners", start=(2019, 1)),
        ),
    )

    apply_harvest(writer, user, _harvest(contact, undated_end, at=NOW + timedelta(days=1)))

    old = next(p for p in contact.positions if p.title == "Data Engineer")
    assert old.ended_on == date(2023, 3, 1)


def test_an_existing_primary_email_stays_primary(writer: Session, user: User) -> None:
    contact = _stored(writer, user, PRIYA)
    contact.emails.append(
        ContactEmail(
            user_id=user.id,
            email="priya.work@example.test",
            kind=EmailKind.WORK,
            is_primary=True,
            source=ContactSource.CSV,
        )
    )
    writer.flush()

    apply_harvest(writer, user, _harvest(contact, PRIYA))

    primary = [e.email for e in contact.emails if e.is_primary]
    assert primary == ["priya.work@example.test"]
    assert len(contact.emails) == 2


def test_a_github_site_is_stored_as_github(writer: Session, user: User) -> None:
    contact = _stored(writer, user, MATEO)
    apply_harvest(writer, user, _harvest(contact, MATEO))
    assert [(link.url, link.kind) for link in contact.links] == [
        ("https://github.com/mateo-fake", LinkKind.GITHUB)
    ]


def test_a_year_only_start_is_the_first_of_january(writer: Session, user: User) -> None:
    contact = _stored(writer, user, TOMASZ)
    apply_harvest(writer, user, _harvest(contact, TOMASZ))
    assert [p.started_on for p in contact.positions] == [date(2020, 1, 1)]


# --- snapshots on change ------------------------------------------------------------------


def test_a_new_job_writes_a_snapshot_of_the_old_one(writer: Session, user: User) -> None:
    contact = _stored(writer, user, PRIYA)
    counts = HarvestCounts()
    apply_harvest(writer, user, _harvest(contact, PRIYA), counts)
    assert counts.snapshots == 0  # filling empty fields is not a change
    promoted = replace(
        PRIYA,
        headline="Principal engineer at Madeup Mobility",
        jobs=(Job("Principal Engineer", "Madeup Mobility", start=(2026, 5)), *PRIYA.jobs[1:]),
    )

    apply_harvest(writer, user, _harvest(contact, promoted, at=NOW + timedelta(days=30)), counts)

    (snapshot,) = contact.snapshots
    assert (snapshot.headline, snapshot.current_title, snapshot.current_company) == (
        PRIYA.headline,
        "Staff Data Engineer",
        "Fictional Robotics Co",
    )
    assert snapshot.observed_at == NOW + timedelta(days=30)
    assert counts.snapshots == 1 and counts.applied == 2


def test_the_same_harvest_twice_writes_no_snapshot(writer: Session, user: User) -> None:
    contact = _stored(writer, user, PRIYA)
    apply_harvest(writer, user, _harvest(contact, PRIYA))
    apply_harvest(writer, user, _harvest(contact, PRIYA, at=NOW + timedelta(days=1)))
    assert contact.snapshots == []


# --- whose profile it is --------------------------------------------------------------------


def test_a_profile_under_another_urn_writes_nothing(writer: Session, user: User) -> None:
    """The slug now belongs to somebody else: nothing of theirs lands on this contact."""
    contact = _stored(writer, user, PRIYA, headline="Keep me")
    stranger = replace(PRIYA, urn_prefix="ACoAANEW", headline="Somebody else", email=None)
    stranger = replace(stranger, email="stranger@example.test")

    result = apply_harvest(writer, user, _harvest(contact, stranger))

    assert result is HarvestResult.MISMATCH
    assert contact.headline == "Keep me"
    assert contact.emails == [] and contact.positions == []
    assert contact.last_enriched_at is None


def test_a_contact_without_a_urn_is_never_written(writer: Session, user: User) -> None:
    contact = _stored(writer, user, PRIYA, li_urn=None)
    assert apply_harvest(writer, user, _harvest(contact, PRIYA)) is HarvestResult.MISMATCH
    assert contact.li_urn is None and contact.headline is None


def test_a_vanity_url_change_under_the_same_urn_keeps_the_old_slug_as_an_alias(
    writer: Session, user: User
) -> None:
    contact = _stored(writer, user, PRIYA)
    old = contact.li_public_id
    renamed = replace(PRIYA, public_id="priya-okafor-renamed")

    result = apply_harvest(writer, user, _harvest(contact, renamed, slug=old))

    assert result is HarvestResult.APPLIED
    assert contact.li_public_id == "priya-okafor-renamed"
    aliases = writer.scalars(scoped(user, ContactAlias)).all()
    assert [(a.contact_id, a.li_public_id) for a in aliases] == [(contact.id, old)]


def test_a_slug_another_contact_holds_writes_nothing(writer: Session, user: User) -> None:
    contact = _stored(writer, user, PRIYA)
    holder = _stored(writer, user, MATEO)
    renamed = replace(PRIYA, public_id=holder.li_public_id)

    result = apply_harvest(writer, user, _harvest(contact, renamed))

    assert result is HarvestResult.CONFLICT
    assert contact.li_public_id == PRIYA.slug and contact.headline is None
    assert contact.last_enriched_at is None


def test_a_field_a_person_edited_stays_theirs(writer: Session, user: User) -> None:
    contact = _stored(writer, user, PRIYA)
    set_manual_field(contact, "headline", "My own words")
    writer.flush()

    apply_harvest(writer, user, _harvest(contact, PRIYA))

    assert contact.headline == "My own words"
    assert contact.synced_values["headline"]["value"] == PRIYA.headline  # revertable


def test_a_merged_contact_is_enriched_through_its_survivor(writer: Session, user: User) -> None:
    survivor = _stored(writer, user, PRIYA)
    loser = _stored(writer, user, PRIYA, li_urn=None, li_public_id="priya-duplicate")
    merge(writer, user, survivor.id, loser.id)

    harvest = replace(_harvest(survivor, PRIYA), contact_ref=loser.id)
    assert apply_harvest(writer, user, harvest) is HarvestResult.APPLIED
    assert survivor.headline == PRIYA.headline and loser.headline is None


def test_another_users_contact_is_missing(writer: Session, user: User) -> None:
    other = factories.make_user(writer)
    theirs = _stored(writer, other, PRIYA)
    counts = HarvestCounts()

    assert apply_harvest(writer, user, _harvest(theirs, PRIYA), counts) is HarvestResult.MISSING
    assert theirs.headline is None and counts.missing == 1


def test_only_an_applied_harvest_clears_the_enrich_request(writer: Session, user: User) -> None:
    asked = _stored(writer, user, PRIYA, enrich_priority=5)
    stranger = replace(PRIYA, urn_prefix="ACoAANEW")
    apply_harvest(writer, user, _harvest(asked, stranger))
    assert asked.enrich_priority == 5
    apply_harvest(writer, user, _harvest(asked, PRIYA))
    assert asked.enrich_priority == 0


# --- spec 9.8's NotFound streak -------------------------------------------------------------


def test_the_not_found_constants_are_the_specs() -> None:
    assert mapping.NOT_FOUND_GONE_AFTER == 3
    assert timedelta(days=14) == mapping.NOT_FOUND_GONE_SPAN


def test_three_not_founds_across_fourteen_days_mark_the_profile_gone(
    writer: Session, user: User
) -> None:
    contact = _stored(writer, user, AIKO)
    days = [0, 7, 14]
    results = [
        apply_harvest(writer, user, _not_found(contact, NOW + timedelta(days=d))) for d in days
    ]

    assert results == [HarvestResult.NOT_FOUND, HarvestResult.NOT_FOUND, HarvestResult.GONE]
    assert contact.li_disconnected_at == NOW + timedelta(days=14)
    (note,) = contact.interactions
    assert note.kind is InteractionKind.NOTE and "not found on 3 visits" in (note.summary or "")
    assert contact.notes is None  # the person's own notes are theirs
    assert contact.last_enriched_at is None


def test_three_not_founds_inside_fourteen_days_do_not(writer: Session, user: User) -> None:
    """Three in a row on consecutive days is a profile that is briefly away, not gone."""
    contact = _stored(writer, user, AIKO)
    for d in (0, 1, 13):
        apply_harvest(writer, user, _not_found(contact, NOW + timedelta(days=d)))
    assert contact.li_not_found_count == 3 and contact.li_disconnected_at is None

    assert apply_harvest(writer, user, _not_found(contact, NOW + timedelta(days=14))) is (
        HarvestResult.GONE
    )


def test_two_not_founds_across_a_month_do_not(writer: Session, user: User) -> None:
    contact = _stored(writer, user, AIKO)
    for d in (0, 30):
        apply_harvest(writer, user, _not_found(contact, NOW + timedelta(days=d)))
    assert contact.li_disconnected_at is None


def test_a_profile_found_again_ends_the_streak(writer: Session, user: User) -> None:
    contact = _stored(writer, user, AIKO)
    for d in (0, 7):
        apply_harvest(writer, user, _not_found(contact, NOW + timedelta(days=d)))
    apply_harvest(writer, user, _harvest(contact, AIKO, at=NOW + timedelta(days=8)))
    assert (contact.li_not_found_count, contact.li_not_found_since) == (0, None)

    apply_harvest(writer, user, _not_found(contact, NOW + timedelta(days=15)))

    assert contact.li_not_found_count == 1
    assert contact.li_not_found_since == NOW + timedelta(days=15)
    assert contact.li_disconnected_at is None


def test_a_disconnect_already_on_file_keeps_its_time(writer: Session, user: User) -> None:
    earlier = NOW - timedelta(days=60)
    contact = _stored(writer, user, AIKO, li_disconnected_at=earlier)
    for d in (0, 7, 14):
        apply_harvest(writer, user, _not_found(contact, NOW + timedelta(days=d)))
    assert contact.li_disconnected_at == earlier
    assert contact.interactions == []


def test_counts_add_up_per_result(writer: Session, user: User) -> None:
    counts = HarvestCounts()
    found = _stored(writer, user, PRIYA)
    missing = _stored(writer, user, MATEO)
    apply_harvest(writer, user, _harvest(found, PRIYA), counts)
    apply_harvest(writer, user, _not_found(missing, NOW), counts)
    apply_harvest(writer, user, _harvest(found, replace(PRIYA, urn_prefix="ACoAANEW")), counts)
    assert (counts.applied, counts.not_found, counts.mismatch) == (1, 1, 1)


def test_mapping_a_harvest_needs_a_writer(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        contact = _stored(session, user, PRIYA)
        harvest = _harvest(contact, PRIYA)
    with session_scope(session_factory) as reader, pytest.raises(RuntimeError, match="writer"):
        apply_harvest(reader, reader.merge(user), harvest)


def test_the_current_job_is_the_open_one_wherever_it_is_listed(writer: Session, user: User) -> None:
    contact = _stored(writer, user, PRIYA)
    past_first = replace(PRIYA, jobs=(PRIYA.jobs[1], PRIYA.jobs[0]))
    apply_harvest(writer, user, _harvest(contact, past_first))
    assert (contact.current_title, contact.current_company) == (
        "Staff Data Engineer",
        "Fictional Robotics Co",
    )


def test_a_profile_with_no_open_job_leaves_the_stored_one(writer: Session, user: User) -> None:
    """Between jobs, or a profile that hides the current one: no job is not "no job"."""
    contact = _stored(writer, user, PRIYA, current_title="Counsel", current_company="Old Firm")
    all_ended = replace(PRIYA, jobs=(PRIYA.jobs[1],))
    apply_harvest(writer, user, _harvest(contact, all_ended))
    assert (contact.current_title, contact.current_company) == ("Counsel", "Old Firm")
