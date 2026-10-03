"""netkeeper.crm.apply.apply_harvest: a profile visit onto its contact (spec 9.4, 9.8, 10.5).

P2-07's "done when", at the mapping: a fixture harvest updates children
without downgrading known values to null. The attacks are the data-loss
directions: a harvest missing what is stored, a profile that is somebody
else's, a slug another contact holds, a field a person edited, and a
NotFound streak that must not mark anyone gone early.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import factories
import pytest
from profile_fakes import PROFILES, Job, Profile, contact_info_of, details_of
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.crm import apply as mapping
from netkeeper.crm import identity
from netkeeper.crm.apply import HarvestCounts, HarvestResult, apply_harvest
from netkeeper.crm.filters import compile_filter, parse_filter
from netkeeper.crm.identity import IncomingContact, Matched, merge
from netkeeper.crm.provenance import set_manual_field
from netkeeper.db import session_scope
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.enrich import ProfileHarvest
from netkeeper.linkedin.voyager import ContactInfo
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
from netkeeper.services import dashboard

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
        details=details_of(profile),
        contact_info=contact_info_of(profile),
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
    assert snapshot.position_changed  # #286: the dashboard's "changed jobs"
    assert counts.snapshots == 1 and counts.applied == 2


def test_a_new_headline_or_a_first_job_is_not_a_position_change(
    writer: Session, user: User
) -> None:
    """#286: only a title or company replacing a known one is a job change."""
    contact = _stored(writer, user, PRIYA, headline="An old headline", location="Elsewhere")

    apply_harvest(writer, user, _harvest(contact, PRIYA))  # fills title and company

    (snapshot,) = contact.snapshots
    assert (snapshot.headline, snapshot.current_title) == ("An old headline", None)
    assert not snapshot.position_changed


def test_the_dashboard_dates_a_new_job_by_when_the_visit_saw_it(
    writer: Session, user: User
) -> None:
    """#286: the move started months ago by the profile; netkeeper noticed it today."""
    contact = _stored(writer, user, PRIYA)
    apply_harvest(writer, user, _harvest(contact, PRIYA))
    moved = replace(
        PRIYA, jobs=(Job("Principal Engineer", "Madeup Mobility", start=(2025, 11)), *PRIYA.jobs)
    )
    seen = NOW + timedelta(days=60)

    apply_harvest(writer, user, _harvest(contact, moved, at=seen))

    [row], total = dashboard.changed_jobs(writer, user, now=seen + timedelta(days=1), limit=10)
    assert (row.contact.id, row.noticed_at, total) == (contact.id, seen, 1)
    assert row.contact.current_company == "Madeup Mobility"


# --- #323: a job change is a later enrichment against an earlier one ------------------------


def _job_changes(session: Session, user: User, now: datetime) -> list[int]:
    """Contacts the dashboard card lists, after checking the CRM filter agrees (#313)."""
    rows, _ = dashboard.changed_jobs(session, user, now=now, limit=100)
    card = sorted(row.contact.id for row in rows)
    tree = parse_filter({"where": {"op": "changed_jobs_within_days", "days": 30}})
    matched = sorted(
        c.id for c in session.scalars(compile_filter(user, tree, session=session, now=now))
    )
    assert matched == card
    return card


def _imported(
    session: Session, user: User, contact: Contact, source: ContactSource, at: datetime
) -> None:
    """``contact``'s title and company as an archive or CSV import writes them."""
    incoming = IncomingContact(
        source=source,
        observed_at=at,
        li_public_id=contact.li_public_id,
        current_title="Analyst",
        current_company="Imported Co",
    )
    identity.apply(session, user, incoming, Matched(contact.id, "public_id"))


def test_a_first_enrichment_is_never_a_job_change(writer: Session, user: User) -> None:
    """#323: the first visit has no earlier enrichment to compare against."""
    contact = _stored(writer, user, PRIYA)

    apply_harvest(writer, user, _harvest(contact, PRIYA))

    assert contact.snapshots == []
    assert _job_changes(writer, user, NOW) == []


@pytest.mark.parametrize("source", [ContactSource.ARCHIVE, ContactSource.CSV])
def test_a_first_enrichment_over_an_imported_job_is_not_a_job_change(
    writer: Session, user: User, source: ContactSource
) -> None:
    """#323: the archive's or a CSV's position is not the earlier position."""
    contact = _stored(writer, user, PRIYA)
    _imported(writer, user, contact, source, NOW - timedelta(days=90))

    apply_harvest(writer, user, _harvest(contact, PRIYA))

    (snapshot,) = contact.snapshots
    assert (snapshot.current_title, snapshot.current_company) == ("Analyst", "Imported Co")
    assert snapshot.source is ContactSource.SYNC
    assert not snapshot.position_changed
    assert contact.current_company == "Fictional Robotics Co"
    assert _job_changes(writer, user, NOW) == []


def test_a_first_enrichment_after_the_connections_sync_is_not_a_job_change(
    writer: Session, user: User
) -> None:
    """#323: the connections list gives a name and a headline, never a position."""
    contact = _stored(writer, user, PRIYA)
    mapping.apply_page(writer, user, _connections_page([PRIYA], NOW - timedelta(days=1)))
    assert (contact.current_title, contact.current_company) == (None, None)

    apply_harvest(writer, user, _harvest(contact, PRIYA))

    assert not any(snapshot.position_changed for snapshot in contact.snapshots)
    assert _job_changes(writer, user, NOW) == []


def test_a_second_enrichment_with_the_same_position_is_not_a_job_change(
    writer: Session, user: User
) -> None:
    contact = _stored(writer, user, PRIYA)
    _imported(writer, user, contact, ContactSource.ARCHIVE, NOW - timedelta(days=90))
    apply_harvest(writer, user, _harvest(contact, PRIYA))
    reworded = replace(PRIYA, headline="Now writing about data")
    later = NOW + timedelta(days=20)

    apply_harvest(writer, user, _harvest(contact, reworded, at=later))

    assert [s.position_changed for s in contact.snapshots] == [False, False]
    assert _job_changes(writer, user, later) == []


def test_a_second_enrichment_with_a_new_position_is_a_job_change(
    writer: Session, user: User
) -> None:
    """#323: the first enrichment over an archive job is the baseline; the second is a move."""
    contact = _stored(writer, user, PRIYA)
    _imported(writer, user, contact, ContactSource.ARCHIVE, NOW - timedelta(days=90))
    apply_harvest(writer, user, _harvest(contact, PRIYA))
    moved = replace(
        PRIYA, jobs=(Job("Principal Engineer", "Madeup Mobility", start=(2026, 8)), *PRIYA.jobs)
    )
    later = NOW + timedelta(days=20)

    apply_harvest(writer, user, _harvest(contact, moved, at=later))

    first, newest = sorted(contact.snapshots, key=lambda s: s.observed_at)
    assert not first.position_changed
    assert newest.position_changed
    assert (newest.current_title, newest.current_company) == (
        "Staff Data Engineer",
        "Fictional Robotics Co",
    )
    assert _job_changes(writer, user, later) == [contact.id]


def test_a_new_title_counts_even_over_a_hand_edited_one(writer: Session, user: User) -> None:
    """#323: a person's override does not hide a move the next enrichment finds."""
    contact = _stored(writer, user, PRIYA)
    apply_harvest(writer, user, _harvest(contact, PRIYA))
    set_manual_field(contact, "current_title", "Data Lead (my words)")
    writer.flush()
    promoted = replace(
        PRIYA,
        jobs=(
            Job("Principal Data Engineer", "Fictional Robotics Co", start=(2026, 8)),
            *PRIYA.jobs,
        ),
    )
    later = NOW + timedelta(days=20)

    apply_harvest(writer, user, _harvest(contact, promoted, at=later))

    (snapshot,) = contact.snapshots
    assert snapshot.position_changed
    assert snapshot.observed_at == later
    # The job before is what LinkedIn last showed, not the person's words.
    assert (snapshot.current_title, snapshot.current_company) == (
        "Staff Data Engineer",
        "Fictional Robotics Co",
    )
    assert contact.current_title == "Data Lead (my words)"  # still the person's
    assert contact.synced_values["current_title"]["value"] == "Principal Data Engineer"
    assert _job_changes(writer, user, later) == [contact.id]


def test_a_new_company_counts_even_over_a_hand_edited_one(writer: Session, user: User) -> None:
    contact = _stored(writer, user, PRIYA)
    apply_harvest(writer, user, _harvest(contact, PRIYA))
    set_manual_field(contact, "current_company", "Robotics (my words)")
    writer.flush()
    moved = replace(
        PRIYA,
        jobs=(Job("Staff Data Engineer", "Madeup Mobility", start=(2026, 8)), *PRIYA.jobs),
    )
    later = NOW + timedelta(days=20)

    apply_harvest(writer, user, _harvest(contact, moved, at=later))

    (snapshot,) = contact.snapshots
    assert snapshot.position_changed
    assert (snapshot.current_title, snapshot.current_company) == (
        "Staff Data Engineer",
        "Fictional Robotics Co",
    )
    assert contact.current_company == "Robotics (my words)"
    assert _job_changes(writer, user, later) == [contact.id]


def test_the_same_synced_title_under_a_hand_edited_one_is_not_a_job_change(
    writer: Session, user: User
) -> None:
    contact = _stored(writer, user, PRIYA)
    apply_harvest(writer, user, _harvest(contact, PRIYA))
    set_manual_field(contact, "current_title", "Data Lead (my words)")
    writer.flush()
    later = NOW + timedelta(days=20)

    apply_harvest(writer, user, _harvest(contact, PRIYA, at=later))

    assert contact.snapshots == []
    assert contact.current_title == "Data Lead (my words)"
    assert _job_changes(writer, user, later) == []


def test_a_first_enrichment_under_a_hand_edited_title_is_not_a_job_change(
    writer: Session, user: User
) -> None:
    contact = _stored(writer, user, PRIYA)
    _imported(writer, user, contact, ContactSource.ARCHIVE, NOW - timedelta(days=90))
    set_manual_field(contact, "current_title", "Data Lead (my words)")
    writer.flush()

    apply_harvest(writer, user, _harvest(contact, PRIYA))

    assert not any(snapshot.position_changed for snapshot in contact.snapshots)
    assert _job_changes(writer, user, NOW) == []


def test_only_an_enrichment_notices_a_job_change(writer: Session, user: User) -> None:
    """#323: a person's edit over an enriched job writes a snapshot, not a job change."""
    contact = _stored(writer, user, PRIYA)
    apply_harvest(writer, user, _harvest(contact, PRIYA))
    edit = IncomingContact(
        source=ContactSource.MANUAL,
        observed_at=NOW + timedelta(days=1),
        current_company="Typed By Hand",
    )

    identity.apply(writer, user, edit, Matched(contact.id, "urn"))

    (snapshot,) = contact.snapshots
    assert snapshot.current_company == "Fictional Robotics Co"
    assert not snapshot.position_changed


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


# --- #171 review ------------------------------------------------------------------------------


def _unreadable(contact: Contact, at: datetime) -> ProfileHarvest:
    return ProfileHarvest(
        contact_ref=contact.id,
        requested_public_id=contact.li_public_id or "x",
        outcome=Outcome.ROUTE_CHANGED,
        observed_at=at,
    )


def test_every_visit_records_its_attempt_time_even_one_that_wrote_nothing(
    writer: Session, user: User
) -> None:
    """F1: a mismatch, a conflict, an unreadable shape, and NotFound all leave a mark."""
    mismatched = _stored(writer, user, PRIYA)
    conflicted = _stored(writer, user, MATEO)
    holder = _stored(writer, user, TOMASZ)
    unreadable = _stored(writer, user, HANA)
    missing = _stored(writer, user, AIKO)
    other_urn = replace(PRIYA, urn_prefix="ACoAANEW")
    renamed = replace(MATEO, public_id=holder.li_public_id)

    results = [
        apply_harvest(writer, user, _harvest(mismatched, other_urn)),
        apply_harvest(writer, user, _harvest(conflicted, renamed)),
        apply_harvest(writer, user, _unreadable(unreadable, NOW)),
        apply_harvest(writer, user, _not_found(missing, NOW)),
    ]

    assert results == [
        HarvestResult.MISMATCH,
        HarvestResult.CONFLICT,
        HarvestResult.UNREADABLE,
        HarvestResult.NOT_FOUND,
    ]
    for contact in (mismatched, conflicted, unreadable, missing):
        assert contact.li_enrich_attempted_at == NOW
        assert contact.last_enriched_at is None
    assert conflicted.headline is None and unreadable.headline is None
    assert holder.li_enrich_attempted_at is None


def test_an_applied_harvest_records_its_attempt_too(writer: Session, user: User) -> None:
    contact = _stored(writer, user, PRIYA)
    apply_harvest(writer, user, _harvest(contact, PRIYA))
    assert contact.li_enrich_attempted_at == NOW == contact.last_enriched_at


def test_an_unreadable_harvest_counts_and_writes_only_the_attempt(
    writer: Session, user: User
) -> None:
    contact = _stored(writer, user, PRIYA, headline="Keep me")
    counts = HarvestCounts()
    apply_harvest(writer, user, _unreadable(contact, NOW), counts)
    assert counts.unreadable == 1 and contact.headline == "Keep me"
    assert contact.li_not_found_count == 0


def test_a_contact_seen_again_after_being_marked_gone_needs_a_whole_new_streak(
    writer: Session, user: User
) -> None:
    """F4: spec 9.8's three across fourteen days, again, not one NotFound."""
    from voyager_pages import Person

    contact = _stored(writer, user, AIKO)
    for d in (0, 7, 14):
        apply_harvest(writer, user, _not_found(contact, NOW + timedelta(days=d)))
    assert contact.li_disconnected_at is not None
    assert (contact.li_not_found_count, contact.li_not_found_since) == (0, None)

    sighting = Person(AIKO.n, AIKO.first, AIKO.last, None, public_id=AIKO.slug)
    assert sighting.urn == AIKO.urn
    mapping.apply_page(writer, user, _connections_page([sighting], NOW + timedelta(days=20)))
    assert _disconnected(contact) is None

    first = apply_harvest(writer, user, _not_found(contact, NOW + timedelta(days=21)))
    assert first is HarvestResult.NOT_FOUND and _disconnected(contact) is None
    second = apply_harvest(writer, user, _not_found(contact, NOW + timedelta(days=28)))
    assert second is HarvestResult.NOT_FOUND
    third = apply_harvest(writer, user, _not_found(contact, NOW + timedelta(days=35)))
    assert third is HarvestResult.GONE
    assert len(contact.interactions) == 2  # one note per gone-mark, not one per NotFound


def test_a_sighting_restarts_a_streak_that_had_not_reached_gone(
    writer: Session, user: User
) -> None:
    from voyager_pages import Person

    contact = _stored(writer, user, AIKO)
    for d in (0, 7):
        apply_harvest(writer, user, _not_found(contact, NOW + timedelta(days=d)))
    sighting = Person(AIKO.n, AIKO.first, AIKO.last, None, public_id=AIKO.slug)
    mapping.apply_page(writer, user, _connections_page([sighting], NOW + timedelta(days=10)))
    assert (contact.li_not_found_count, contact.li_not_found_since) == (0, None)
    result = apply_harvest(writer, user, _not_found(contact, NOW + timedelta(days=14)))
    assert result is HarvestResult.NOT_FOUND


def _connections_page(people: list[Any], at: datetime) -> Any:
    from netkeeper.linkedin.connections import ConnectionsPage, SyncMode
    from netkeeper.linkedin.voyager import ConnectionSummary

    return ConnectionsPage(
        mode=SyncMode.INCREMENTAL,
        number=0,
        start=0,
        total=len(people),
        connections=tuple(
            ConnectionSummary(
                urn=p.urn,
                public_id=p.slug,
                first_name=p.first,
                last_name=p.last,
                headline=None,
                connected_at=None,
            )
            for p in people
        ),
        observed_at=at,
    )


def test_an_end_month_without_a_year_is_still_an_end(writer: Session, user: User) -> None:
    """M20: a position whose end has a month but no year ended; it is not the current job."""
    contact = _stored(writer, user, PRIYA, current_title="Kept", current_company="Kept Co")
    half_ended = replace(PRIYA, jobs=(Job("Odd Role", "Odd Co", start=(2020, 1), end=None),))
    harvest = _harvest(contact, half_ended)
    assert harvest.details is not None
    (entry,) = harvest.details.positions
    odd = replace(entry, end_month=6)
    harvest = replace(harvest, details=replace(harvest.details, positions=(odd,)))

    apply_harvest(writer, user, harvest)

    (row,) = contact.positions
    assert row.is_current is False
    assert (contact.current_title, contact.current_company) == ("Kept", "Kept Co")


def test_a_host_that_only_ends_in_github_com_is_a_website(writer: Session, user: User) -> None:
    """M21."""
    contact = _stored(writer, user, MATEO)
    lookalike = replace(MATEO, websites=("https://notgithub.com/mateo",))
    apply_harvest(writer, user, _harvest(contact, lookalike))
    assert [(link.url, link.kind) for link in contact.links] == [
        ("https://notgithub.com/mateo", LinkKind.WEBSITE)
    ]


def test_an_applied_harvest_does_not_clear_a_disconnect(writer: Session, user: User) -> None:
    """M27: a profile that can be looked up is not proof of a connection; only a sync is."""
    gone = NOW - timedelta(days=30)
    contact = _stored(writer, user, PRIYA, li_disconnected_at=gone)
    assert apply_harvest(writer, user, _harvest(contact, PRIYA)) is HarvestResult.APPLIED
    assert contact.li_disconnected_at == gone


def _disconnected(contact: Contact) -> datetime | None:
    """Read afresh, so a type checker does not carry an earlier assertion across a write."""
    return contact.li_disconnected_at


# --- #190: a harvest read from the page -----------------------------------------------------


def test_a_mismatch_the_job_did_not_click_on_writes_nothing(writer: Session, user: User) -> None:
    """The job skips the click on a profile under another id; the harvest has no contact
    info, and the core finds the same mismatch."""
    contact = _stored(writer, user, PRIYA)
    stranger = replace(PRIYA, urn_prefix="ACoAANEW", headline="Somebody else")
    harvest = replace(_harvest(contact, stranger), contact_info=None)
    counts = HarvestCounts()
    assert apply_harvest(writer, user, harvest, counts) is HarvestResult.MISMATCH
    assert contact.headline is None and contact.last_enriched_at is None
    assert contact.li_enrich_attempted_at == NOW and counts.mismatch == 1


def test_a_matching_harvest_without_contact_info_writes_nothing(
    writer: Session, user: User
) -> None:
    """Never produced by the job, refused anyway: a visit is written whole or not at all."""
    contact = _stored(writer, user, PRIYA)
    harvest = replace(_harvest(contact, PRIYA), contact_info=None)
    counts = HarvestCounts()
    assert apply_harvest(writer, user, harvest, counts) is HarvestResult.UNREADABLE
    assert contact.headline is None and contact.last_enriched_at is None
    assert contact.emails == [] and counts.unreadable == 1


def test_every_address_the_overlay_shows_is_kept_the_first_as_primary(
    writer: Session, user: User
) -> None:
    contact = _stored(writer, user, PRIYA)
    harvest = _harvest(contact, PRIYA)
    info = replace(
        contact_info_of(PRIYA),
        emails=("priya.first@example.test", "priya.second@example.test"),
    )
    assert apply_harvest(writer, user, replace(harvest, contact_info=info)) is (
        HarvestResult.APPLIED
    )
    assert [(e.email, e.is_primary) for e in contact.emails] == [
        ("priya.first@example.test", True),
        ("priya.second@example.test", False),
    ]


def test_a_website_that_is_not_http_is_never_stored(
    writer: Session, user: User, caplog: pytest.LogCaptureFixture
) -> None:
    """#206 review: the parser keeps only http and https sites; this boundary holds
    even for a source that did not."""
    caplog.set_level(logging.INFO, logger="netkeeper")
    contact = _stored(writer, user, MATEO)
    sites = ("javascript:alert(1)", " Data:text/html,x", "https://mateo.example.test/")
    apply_harvest(writer, user, _harvest(contact, replace(MATEO, websites=sites)))
    assert [link.url for link in contact.links] == ["https://mateo.example.test/"]
    assert caplog.text.count("skipped a website that is not an http or https url") == 2
    assert "javascript" not in caplog.text


# --- #207 review: Contact info from a streamed copy ---------------------------------------


def _from_copy(contact: Contact, profile: Profile, info: ContactInfo) -> ProfileHarvest:
    return replace(_harvest(contact, profile), contact_info=info, contact_info_from_copy=True)


def test_a_thin_copy_is_written_but_the_contact_stays_due(writer: Session, user: User) -> None:
    """A copy with no email and no phone may be one cut short that passed every check:
    what it holds is written, and the contact is visited again next cycle."""
    contact = _stored(writer, user, PRIYA, enrich_priority=5)
    counts = HarvestCounts()
    thin = ContactInfo(websites=("https://priya.example.test/",))
    result = apply_harvest(writer, user, _from_copy(contact, PRIYA, thin), counts)
    assert result is HarvestResult.APPLIED
    assert [link.url for link in contact.links] == ["https://priya.example.test/"]
    assert contact.headline == details_of(PRIYA).headline
    assert (contact.enrich_priority, contact.last_enriched_at) == (5, None)
    assert contact.li_enrich_attempted_at == NOW
    assert (counts.applied, counts.kept_due) == (1, 1)


@pytest.mark.parametrize(
    "info",
    [ContactInfo(emails=("priya@example.test",)), ContactInfo(phones=("+1 555 0100",))],
)
def test_a_copy_with_an_address_or_a_number_is_applied_as_usual(
    writer: Session, user: User, info: ContactInfo
) -> None:
    contact = _stored(writer, user, PRIYA, enrich_priority=5)
    counts = HarvestCounts()
    apply_harvest(writer, user, _from_copy(contact, PRIYA, info), counts)
    assert (contact.enrich_priority, contact.last_enriched_at) == (0, NOW)
    assert counts.kept_due == 0


def test_a_thin_overlay_read_from_its_own_body_is_applied_as_usual(
    writer: Session, user: User
) -> None:
    contact = _stored(writer, user, PRIYA, enrich_priority=5)
    harvest = replace(_harvest(contact, PRIYA), contact_info=ContactInfo())
    apply_harvest(writer, user, harvest)
    assert (contact.enrich_priority, contact.last_enriched_at) == (0, NOW)


def test_a_harvest_without_contact_info_cannot_claim_a_copy() -> None:
    with pytest.raises(ValueError, match="copy"):
        ProfileHarvest(
            contact_ref=1,
            requested_public_id="x",
            outcome=Outcome.OK,
            observed_at=NOW,
            details=details_of(PRIYA),
            contact_info=None,
            contact_info_from_copy=True,
        )
