"""The do-not-send list (#238, Part B): what lands on it, how the guards use it, and that
it outlives the contact that held the address.

The cases are the issue's: a double send, a bounce on contact A that must block contact B,
a merge that must keep a bounce, and ``+tag`` addresses that are separate people.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.crm import contacts as contact_service
from netkeeper.crm import do_not_send
from netkeeper.crm.identity import (
    EMAIL_STATUS_SEVERITY,
    IncomingContact,
    IncomingEmail,
    Matched,
    New,
    resolve,
)
from netkeeper.db import session_scope
from netkeeper.models import (
    DO_NOT_SEND_RANK,
    Campaign,
    ContactSource,
    DoNotSendReason,
    EmailStatus,
    TemplateChannel,
    User,
)
from netkeeper.services.campaign_guards import Reason, check_enrollment, check_step

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
EMAIL = TemplateChannel.EMAIL
LINKEDIN = TemplateChannel.LINKEDIN


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer)


@pytest.fixture
def campaign(writer: Session, user: User) -> Campaign:
    """Email first, then LinkedIn."""
    return factories.make_campaign(writer, user, channels=(EMAIL, LINKEDIN))


def _enroll(
    writer: Session, user: User, campaign: Campaign, *ids: int
) -> dict[int, tuple[Reason, ...]]:
    return {v.contact_id: v.reasons for v in check_enrollment(writer, user, campaign, ids, now=NOW)}


def _listed(writer: Session, user: User) -> dict[str, DoNotSendReason]:
    return {e.email: e.reason for e in do_not_send.entries(writer, user)}


# --- the pinned values ---------------------------------------------------------------------


def test_the_reasons_and_their_ranks_are_pinned() -> None:
    """Safety constants against words and numbers written out here (CLAUDE.md)."""
    assert {r.value: DO_NOT_SEND_RANK[r] for r in DoNotSendReason} == {
        "manual": 0,
        "invalid": 1,
        "bounced": 2,
        "opted_out": 3,
    }
    assert {s.value: r.value for s, r in do_not_send.REASON_FOR_STATUS.items()} == {
        "bounced": "bounced",
        "invalid": "invalid",
    }
    assert {s.value: n for s, n in EMAIL_STATUS_SEVERITY.items()} == {
        "ok": 0,
        "invalid": 1,
        "bounced": 2,
    }


# --- the list itself -----------------------------------------------------------------------


def test_an_address_is_trimmed_and_lowercased_and_nothing_else(writer: Session, user: User) -> None:
    entry = do_not_send.add_by_hand(writer, user, "  Jane.Doe+News@Example.TEST ")
    assert entry.email == "jane.doe+news@example.test"
    assert do_not_send.find(writer, user, "JANE.DOE+NEWS@example.test") is entry
    # Neither the +tag nor the dots are folded (maintainer's decision on #238).
    for other in ("jane.doe@example.test", "janedoe+news@example.test", "jane.doe+x@example.test"):
        assert do_not_send.find(writer, user, other) is None


def test_by_hand_takes_one_bare_address(writer: Session, user: User) -> None:
    for bad in ("", "a@x.test, b@y.test", "Eve <eve@example.test>", "nobody"):
        with pytest.raises(ValueError):
            do_not_send.add_by_hand(writer, user, bad)
    assert do_not_send.entries(writer, user) == []


def test_a_second_reason_keeps_the_stronger(writer: Session, user: User) -> None:
    address = "ada@example.test"
    do_not_send.add_by_hand(writer, user, address)
    do_not_send.add(writer, user, address, DoNotSendReason.BOUNCED)
    do_not_send.add(writer, user, address, DoNotSendReason.INVALID)  # weaker: no change
    assert _listed(writer, user) == {address: DoNotSendReason.BOUNCED}
    do_not_send.add(writer, user, address, DoNotSendReason.OPTED_OUT)
    assert _listed(writer, user) == {address: DoNotSendReason.OPTED_OUT}


def test_changing_the_list_needs_a_writer(
    session_factory: sessionmaker[Session], writer: Session, user: User
) -> None:
    entry_id = do_not_send.add_by_hand(writer, user, "ada@example.test").id
    writer.commit()
    with session_scope(session_factory) as reader:
        with pytest.raises(RuntimeError):
            do_not_send.add_by_hand(reader, user, "bob@example.test")
        with pytest.raises(RuntimeError):
            do_not_send.remove(reader, user, entry_id)


def test_another_users_entry_is_not_found(writer: Session, user: User) -> None:
    stranger = factories.make_user(writer)
    theirs = do_not_send.add_by_hand(writer, stranger, "ada@example.test")
    with pytest.raises(do_not_send.NotFound):
        do_not_send.remove(writer, user, theirs.id)
    assert do_not_send.entries(writer, user) == []
    assert [e.id for e in do_not_send.entries(writer, stranger)] == [theirs.id]


# --- what lands on it ----------------------------------------------------------------------


@pytest.mark.parametrize("status", [EmailStatus.BOUNCED, EmailStatus.INVALID])
def test_marking_an_address_bounced_or_invalid_lists_it(
    writer: Session, user: User, status: EmailStatus
) -> None:
    contact = factories.make_contact(writer, user, emails=["ada@example.test"])
    contact_service.update_email(writer, user, contact.id, contact.emails[0].id, {"status": status})
    added = contact_service.add_email(writer, user, contact.id, "ada2@example.test", status=status)
    assert added.status is status
    entries = {e.email: (e.reason, e.contact_id) for e in do_not_send.entries(writer, user)}
    assert entries == {
        "ada@example.test": (DoNotSendReason(status.value), contact.id),
        "ada2@example.test": (DoNotSendReason(status.value), contact.id),
    }


def test_marking_an_address_ok_again_leaves_the_entry(writer: Session, user: User) -> None:
    """Only a person removing the entry takes it off."""
    contact = factories.make_contact(writer, user, emails=["ada@example.test"])
    email_id = contact.emails[0].id
    contact_service.update_email(writer, user, contact.id, email_id, {"status": "bounced"})
    contact_service.update_email(writer, user, contact.id, email_id, {"status": "ok"})
    assert _listed(writer, user) == {"ada@example.test": DoNotSendReason.BOUNCED}


def test_an_ok_address_is_not_listed(writer: Session, user: User) -> None:
    contact = factories.make_contact(writer, user, emails=["ada@example.test"])
    contact_service.add_email(writer, user, contact.id, "ada2@example.test")
    contact_service.update_email(writer, user, contact.id, contact.emails[0].id, {"kind": "work"})
    assert do_not_send.entries(writer, user) == []


# --- the issue's cases: bounce on A blocks B -----------------------------------------------


def test_a_bounce_on_contact_a_blocks_contact_b_after_a_loses_the_address(
    writer: Session, user: User, campaign: Campaign
) -> None:
    """Part A's guard looks at A's row; once A's address is gone, only the list remembers."""
    a = factories.make_contact(writer, user, emails=["shared@example.test", "a2@example.test"])
    b = factories.make_contact(writer, user, emails=["shared@example.test"])
    contact_service.update_email(writer, user, a.id, a.emails[0].id, {"status": "bounced"})
    assert _enroll(writer, user, campaign, b.id) == {
        b.id: (Reason.DO_NOT_SEND, Reason.ADDRESS_BOUNCED_ELSEWHERE)
    }
    shared = next(e for e in a.emails if e.email == "shared@example.test")
    contact_service.delete_email(writer, user, a.id, shared.id)
    assert _enroll(writer, user, campaign, b.id) == {b.id: (Reason.DO_NOT_SEND,)}


def test_a_bounce_blocks_after_the_contact_that_held_it_is_deleted(
    writer: Session, user: User, campaign: Campaign
) -> None:
    a = factories.make_contact(writer, user, emails=["shared@example.test"])
    contact_service.update_email(writer, user, a.id, a.emails[0].id, {"status": "bounced"})
    writer.delete(a)
    writer.flush()
    writer.expire_all()
    [entry] = do_not_send.entries(writer, user)
    assert (entry.email, entry.contact_id) == ("shared@example.test", None)
    b = factories.make_contact(writer, user, emails=["SHARED@example.test"])
    assert _enroll(writer, user, campaign, b.id) == {b.id: (Reason.DO_NOT_SEND,)}


def test_a_listed_address_is_caught_at_the_step_fire_too(
    writer: Session, user: User, campaign: Campaign
) -> None:
    contact = factories.make_contact(writer, user, emails=["ada@example.test"])
    enrollment = factories.make_enrollment(writer, campaign, contact)
    email_step, linkedin_step = campaign.steps
    assert check_step(writer, user, enrollment, email_step, now=NOW).eligible
    do_not_send.add(writer, user, "ada@example.test", DoNotSendReason.BOUNCED)
    assert check_step(writer, user, enrollment, email_step, now=NOW).reasons == (
        Reason.DO_NOT_SEND,
    )
    # A bounce leaves LinkedIn open (spec 11.5) ...
    assert check_step(writer, user, enrollment, linkedin_step, now=NOW).eligible
    # ... an opt-out does not.
    do_not_send.add(writer, user, "ada@example.test", DoNotSendReason.OPTED_OUT)
    assert check_step(writer, user, enrollment, linkedin_step, now=NOW).reasons == (
        Reason.DO_NOT_SEND,
    )


def test_an_opt_out_on_any_of_the_contacts_addresses_excludes_every_channel(
    writer: Session, user: User
) -> None:
    linkedin_first = factories.make_campaign(writer, user, channels=(LINKEDIN, EMAIL))
    contact = factories.make_contact(
        writer, user, emails=["work@example.test", "home@example.test"]
    )
    do_not_send.add(writer, user, "home@example.test", DoNotSendReason.OPTED_OUT)
    assert _enroll(writer, user, linkedin_first, contact.id) == {contact.id: (Reason.DO_NOT_SEND,)}


def test_a_bounce_on_a_secondary_address_leaves_the_sendable_one(
    writer: Session, user: User, campaign: Campaign
) -> None:
    """Only the address a step would go to is checked for a bounce."""
    contact = factories.make_contact(writer, user, emails=["work@example.test", "old@example.test"])
    do_not_send.add(writer, user, "old@example.test", DoNotSendReason.BOUNCED)
    assert _enroll(writer, user, campaign, contact.id) == {contact.id: ()}


def test_removing_the_entry_lets_the_address_be_sent_to_again(
    writer: Session, user: User, campaign: Campaign
) -> None:
    contact = factories.make_contact(writer, user, emails=["ada@example.test"])
    entry = do_not_send.add_by_hand(writer, user, "ada@example.test")
    assert _enroll(writer, user, campaign, contact.id) == {contact.id: (Reason.DO_NOT_SEND,)}
    do_not_send.remove(writer, user, entry.id)
    assert _enroll(writer, user, campaign, contact.id) == {contact.id: ()}


def test_another_users_list_is_not_this_users(
    writer: Session, user: User, campaign: Campaign
) -> None:
    stranger = factories.make_user(writer)
    do_not_send.add(writer, stranger, "ada@example.test", DoNotSendReason.OPTED_OUT)
    contact = factories.make_contact(writer, user, emails=["ada@example.test"])
    assert _enroll(writer, user, campaign, contact.id) == {contact.id: ()}


# --- the issue's cases: merge keeps the bounce ---------------------------------------------


@pytest.mark.parametrize("bounced_on", ["survivor", "loser"])
@pytest.mark.parametrize("status", [EmailStatus.BOUNCED, EmailStatus.INVALID])
def test_a_merge_keeps_the_worse_status_of_a_shared_address(
    writer: Session, user: User, campaign: Campaign, bounced_on: str, status: EmailStatus
) -> None:
    survivor = factories.make_contact(writer, user, emails=["shared@example.test"])
    loser = factories.make_contact(writer, user, emails=["shared@example.test"])
    held = survivor if bounced_on == "survivor" else loser
    held.emails[0].status = status  # straight on the row: the merge must not need the list
    writer.flush()
    contact_service.merge_contacts(writer, user, survivor.id, loser.id)
    writer.expire_all()
    [row] = survivor.emails
    assert (row.email, row.status) == ("shared@example.test", status)
    assert _listed(writer, user) == {"shared@example.test": DoNotSendReason(status.value)}
    assert _enroll(writer, user, campaign, survivor.id)[survivor.id][0] in (
        Reason.EMAIL_BOUNCED,
        Reason.EMAIL_INVALID,
    )


def test_bounced_beats_invalid_in_a_merge(writer: Session, user: User) -> None:
    survivor = factories.make_contact(writer, user, emails=["shared@example.test"])
    loser = factories.make_contact(writer, user, emails=["shared@example.test"])
    survivor.emails[0].status = EmailStatus.INVALID
    loser.emails[0].status = EmailStatus.BOUNCED
    writer.flush()
    contact_service.merge_contacts(writer, user, survivor.id, loser.id)
    writer.expire_all()
    assert [r.status for r in survivor.emails] == [EmailStatus.BOUNCED]


def test_a_merge_never_takes_an_address_off_the_list(
    writer: Session, user: User, campaign: Campaign
) -> None:
    """The survivor marked the address ok and the loser's bounced copy is dropped: the entry
    the bounce made stays, so the merged person is still not mailed there."""
    survivor = factories.make_contact(writer, user, emails=["shared@example.test"])
    loser = factories.make_contact(writer, user, emails=["shared@example.test"])
    contact_service.update_email(writer, user, loser.id, loser.emails[0].id, {"status": "bounced"})
    contact_service.update_email(writer, user, loser.id, loser.emails[0].id, {"status": "ok"})
    contact_service.merge_contacts(writer, user, survivor.id, loser.id)
    writer.expire_all()
    assert [r.status for r in survivor.emails] == [EmailStatus.OK]
    assert _enroll(writer, user, campaign, survivor.id) == {survivor.id: (Reason.DO_NOT_SEND,)}


# --- the issue's cases: double send, and +tags are separate people -------------------------


def test_two_contacts_with_one_address_are_sent_to_once(
    writer: Session, user: User, campaign: Campaign
) -> None:
    """The double send (Part A's guard), with the address written two ways."""
    first = factories.make_contact(writer, user, emails=["Ada@Example.test"])
    second = factories.make_contact(writer, user, emails=[" ada@example.TEST"])
    assert _enroll(writer, user, campaign, first.id, second.id) == {
        first.id: (),
        second.id: (Reason.DUPLICATE_ADDRESS,),
    }


def test_plus_tag_addresses_are_separate_people_in_one_campaign(
    writer: Session, user: User, campaign: Campaign
) -> None:
    """name+nk1@, name+nk2@ and name@ are three people: all three are enrolled."""
    contacts = [
        factories.make_contact(writer, user, emails=[address])
        for address in ("name+nk1@gmail.com", "name+nk2@gmail.com", "name@gmail.com")
    ]
    verdicts = _enroll(writer, user, campaign, *(c.id for c in contacts))
    assert verdicts == {c.id: () for c in contacts}


def test_a_bounce_or_opt_out_on_one_plus_tag_never_blocks_another(
    writer: Session, user: User, campaign: Campaign
) -> None:
    bounced = factories.make_contact(writer, user, emails=["name+nk1@gmail.com"])
    contact_service.update_email(
        writer, user, bounced.id, bounced.emails[0].id, {"status": "bounced"}
    )
    do_not_send.add(writer, user, "name+nk2@gmail.com", DoNotSendReason.OPTED_OUT)
    tagged = factories.make_contact(writer, user, emails=["name+nk3@gmail.com"])
    bare = factories.make_contact(writer, user, emails=["name@gmail.com"])
    dotted = factories.make_contact(writer, user, emails=["na.me+nk1@gmail.com"])
    assert _enroll(writer, user, campaign, tagged.id, bare.id, dotted.id) == {
        tagged.id: (),
        bare.id: (),
        dotted.id: (),
    }


def test_identity_resolution_never_matches_across_plus_tags(writer: Session, user: User) -> None:
    """An import row at name+nk2@ is not the contact at name+nk1@ (spec 8.2 step 3)."""
    held = factories.make_contact(writer, user, emails=["name+nk1@gmail.com"])

    def by_email(address: str) -> object:
        incoming = IncomingContact(
            source=ContactSource.CSV,
            first_name="Someone",
            last_name="Else",
            emails=(IncomingEmail(address),),
        )
        return resolve(writer, user, incoming)

    assert isinstance(by_email("NAME+nk1@gmail.com"), Matched)
    for other in ("name+nk2@gmail.com", "name@gmail.com", "n.ame+nk1@gmail.com"):
        assert isinstance(by_email(other), New), other
    assert held.id  # the held contact is untouched


def test_a_merge_keeps_plus_tag_addresses_apart(writer: Session, user: User) -> None:
    """Two tags are two addresses: a merge keeps both rows, each with its own status."""
    survivor = factories.make_contact(writer, user, emails=["name+nk1@gmail.com"])
    loser = factories.make_contact(writer, user, emails=["name+nk2@gmail.com"])
    loser.emails[0].status = EmailStatus.BOUNCED
    writer.flush()
    contact_service.merge_contacts(writer, user, survivor.id, loser.id)
    writer.expire_all()
    assert sorted((r.email, r.status) for r in survivor.emails) == [
        ("name+nk1@gmail.com", EmailStatus.OK),
        ("name+nk2@gmail.com", EmailStatus.BOUNCED),
    ]
    assert _listed(writer, user) == {"name+nk2@gmail.com": DoNotSendReason.BOUNCED}


@pytest.mark.parametrize("status", [EmailStatus.BOUNCED, EmailStatus.INVALID])
def test_changing_the_text_of_a_bounced_or_invalid_address_lists_the_new_one(
    writer: Session, user: User, status: EmailStatus
) -> None:
    """The row keeps its status under the new text, so the new address is listed as well."""
    contact = factories.make_contact(writer, user, emails=["old@example.test"])
    email_id = contact.emails[0].id
    contact_service.update_email(writer, user, contact.id, email_id, {"status": status})
    contact_service.update_email(writer, user, contact.id, email_id, {"email": "New@example.test"})
    reason = DoNotSendReason(status.value)
    assert _listed(writer, user) == {"old@example.test": reason, "new@example.test": reason}


def test_changing_the_text_of_an_ok_address_lists_nothing(writer: Session, user: User) -> None:
    contact = factories.make_contact(writer, user, emails=["old@example.test"])
    contact_service.update_email(
        writer, user, contact.id, contact.emails[0].id, {"email": "new@example.test"}
    )
    assert do_not_send.entries(writer, user) == []


def test_a_bounce_under_an_opt_out_is_kept_as_a_flag(writer: Session, user: User) -> None:
    """One reason per entry, the strongest; a bounce is never hidden by it."""
    entry = do_not_send.add(writer, user, "ada@example.test", DoNotSendReason.BOUNCED)
    assert (entry.reason, entry.bounced, do_not_send.also_bounced(entry)) == (
        DoNotSendReason.BOUNCED,
        True,
        False,
    )
    do_not_send.add(writer, user, "ada@example.test", DoNotSendReason.OPTED_OUT)
    assert (entry.reason, entry.bounced, do_not_send.also_bounced(entry)) == (
        DoNotSendReason.OPTED_OUT,
        True,
        True,
    )
    opted = do_not_send.add(writer, user, "bob@example.test", DoNotSendReason.OPTED_OUT)
    do_not_send.add(writer, user, "bob@example.test", DoNotSendReason.INVALID)
    assert (opted.bounced, do_not_send.also_bounced(opted)) == (False, False)


def test_a_merge_lists_again_a_bounce_a_person_removed(
    writer: Session, user: User, campaign: Campaign
) -> None:
    """Removing the entry leaves the contact's own bounced status; a merge lists it again."""
    survivor = factories.make_contact(writer, user, emails=["shared@example.test"])
    loser = factories.make_contact(writer, user, emails=["other@example.test"])
    contact_service.update_email(
        writer, user, survivor.id, survivor.emails[0].id, {"status": "bounced"}
    )
    [entry] = do_not_send.entries(writer, user)
    do_not_send.remove(writer, user, entry.id)
    contact_service.merge_contacts(writer, user, survivor.id, loser.id)
    assert _listed(writer, user) == {"shared@example.test": DoNotSendReason.BOUNCED}
