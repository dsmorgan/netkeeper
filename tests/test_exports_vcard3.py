"""The ``macos-contacts`` export (P6-02, #249): vCard 3.0 with a group card per tag.

Whether macOS Contacts actually imports the file is the item's "done when",
and only a person with a Mac can check that (the maintainer does, by hand,
after the merge). What these tests pin is the shape Contacts expects: vCard
3.0, a ``UID`` on every card, tags in ``CATEGORIES``, one
``X-ADDRESSBOOKSERVER-KIND:group`` card per tag whose members name those UIDs,
75-octet folding, RFC 2426 escaping, and CRLF line endings. Every name, email,
and phone number below is made up.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import factories
import pytest
from sqlalchemy.orm import Session

from netkeeper.crm.exports import (
    UID_NAMESPACE,
    VCARD_ONLY_PRESETS,
    ExportError,
    _contact_uid,
    _tag_uid,
    export_stream,
    filename_for,
)
from netkeeper.crm.filters import FilterTree, parse_filter
from netkeeper.models import (
    Contact,
    ContactLink,
    ContactTag,
    EmailKind,
    LinkKind,
    PhoneKind,
    Tag,
    TagSource,
    User,
)

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)

Line = tuple[str, list[str], str]
"""One unfolded content line: property name, its parameters, its raw (still escaped) value."""


def _run(session: Session, user: User, tree: FilterTree | None = None) -> str:
    stream: Iterator[str] = export_stream(
        session,
        user,
        preset="macos-contacts",
        output_format="vcard",
        headerless=False,
        tree=tree or FilterTree(),
        sort=[],
        now=NOW,
    )
    return "".join(stream)


def _cards(text: str) -> list[list[Line]]:
    """``text`` split into cards of content lines, after checking the physical format.

    Every physical line ends in CRLF and is at most 75 octets; a continuation
    starts with one space (RFC 2425 §5.8.1). Unfolding removes CRLF-space.
    """
    assert text.endswith("\r\n")
    physical = text.split("\r\n")[:-1]
    for line in physical:
        assert "\n" not in line and "\r" not in line, repr(line)
        assert len(line.encode("utf-8")) <= 75, repr(line)
    unfolded = text.replace("\r\n ", "").split("\r\n")[:-1]
    cards: list[list[Line]] = []
    current: list[Line] | None = None
    for raw in unfolded:
        head, value = raw.split(":", 1)
        name, *params = head.split(";")
        if (name, value) == ("BEGIN", "VCARD"):
            assert current is None
            current = []
        elif (name, value) == ("END", "VCARD"):
            assert current is not None
            cards.append(current)
            current = None
        else:
            assert current is not None, f"{raw!r} is outside a card"
            current.append((name, params, value))
    assert current is None
    return cards


def _values(card: list[Line], name: str) -> list[str]:
    return [value for prop, _params, value in card if prop == name]


def _one(card: list[Line], name: str) -> str:
    (value,) = _values(card, name)
    return value


def _is_group(card: list[Line]) -> bool:
    return _values(card, "X-ADDRESSBOOKSERVER-KIND") == ["group"]


def _tag(session: Session, user: User, name: str, *contacts: Contact) -> Tag:
    tag = Tag(user_id=user.id, name=name)
    session.add(tag)
    session.flush()
    for contact in contacts:
        session.add(
            ContactTag(
                user_id=user.id, contact_id=contact.id, tag_id=tag.id, source=TagSource.MANUAL
            )
        )
    session.flush()
    return tag


# --- the constants that decide the file's shape ---------------------------------


def test_uid_namespace_is_pinned() -> None:
    """Changing it would give everybody a new card on the next import into Contacts."""
    assert uuid.UUID("853e7fc6-408e-4b6c-833f-998c611189f2") == UID_NAMESPACE


def test_macos_contacts_is_the_only_vcard_only_preset() -> None:
    assert frozenset({"macos-contacts"}) == VCARD_ONLY_PRESETS
    assert filename_for("macos-contacts", "vcard") == "contacts-macos-contacts.vcf"


@pytest.mark.parametrize("output_format", ["csv", "json"])
def test_macos_contacts_refuses_csv_and_json_before_rendering(
    session: Session, output_format: str
) -> None:
    user = factories.make_user(session)
    with pytest.raises(ExportError, match="vCard only"):
        export_stream(
            session,
            user,
            preset="macos-contacts",
            output_format=output_format,  # type: ignore[arg-type]
            headerless=False,
            tree=FilterTree(),
            sort=[],
            now=NOW,
        )


# --- one contact card ---------------------------------------------------------------


def test_contact_card_is_vcard_3_with_every_channel(session: Session) -> None:
    user = factories.make_user(session)
    contact = factories.make_contact(
        session,
        user,
        first_name="Robert",
        preferred_name="Bob",
        last_name="Example",
        current_company="Acme Corp",
        current_title="Staff Engineer",
        location="Austin, TX",
        notes="Met at a conference",
        li_url="https://www.linkedin.com/in/bob-example-fake",
        emails=["bob@work.example.test", "bob@home.example.test"],
        phones=["+15550100001", "ask reception", "+15550100002"],
    )
    contact.emails[0].kind = EmailKind.WORK
    contact.emails[1].kind = EmailKind.PERSONAL
    contact.phones[0].kind = PhoneKind.MOBILE
    contact.phones[2].kind = PhoneKind.OTHER
    contact.links.append(
        ContactLink(user_id=user.id, url="https://bob.example.test", kind=LinkKind.WEBSITE)
    )
    session.commit()

    text = _run(session, user)
    assert text.startswith("BEGIN:VCARD\r\nVERSION:3.0\r\n")
    (card,) = _cards(text)
    assert _one(card, "VERSION") == "3.0"
    assert _one(card, "UID") == _contact_uid(user, contact)
    assert _one(card, "N") == "Example;Robert;;;"
    assert _one(card, "FN") == "Bob Example"
    assert _one(card, "NICKNAME") == "Bob"
    assert _one(card, "ORG") == "Acme Corp"
    assert _one(card, "TITLE") == "Staff Engineer"
    assert _one(card, "ADR") == ";;;Austin\\, TX;;;"
    assert _one(card, "NOTE") == "Met at a conference"
    assert _values(card, "URL") == [
        "https://www.linkedin.com/in/bob-example-fake",
        "https://bob.example.test",
    ]
    emails = [(params, value) for name, params, value in card if name == "EMAIL"]
    assert emails == [
        (["TYPE=INTERNET", "TYPE=WORK", "TYPE=PREF"], "bob@work.example.test"),
        (["TYPE=INTERNET", "TYPE=HOME"], "bob@home.example.test"),
    ]
    phones = [(params, value) for name, params, value in card if name == "TEL"]
    # "ask reception" has no digits to dial, so it is not a TEL at all.
    assert phones == [(["TYPE=CELL", "TYPE=PREF"], "+15550100001"), ([], "+15550100002")]
    assert _values(card, "CATEGORIES") == []  # no tags, no empty CATEGORIES line


def test_no_nickname_when_the_preferred_name_is_the_first_name(session: Session) -> None:
    user = factories.make_user(session)
    factories.make_contact(session, user, first_name="Ada", preferred_name="Ada", last_name="L")
    (card,) = _cards(_run(session, user))
    assert _values(card, "NICKNAME") == []
    assert _one(card, "FN") == "Ada L"


def test_non_ascii_names_survive_folding(session: Session) -> None:
    """Multi-byte names and a long note fold at 75 octets without splitting a character."""
    user = factories.make_user(session)
    factories.make_contact(
        session,
        user,
        first_name="Zoë",
        preferred_name="Zoë",
        last_name="Ångström-Łukasiewicz",
        current_company="東京ソフトウェア株式会社 研究開発部門 第三グループ",
        notes="Ça va? " * 20,
    )
    session.commit()
    text = _run(session, user)
    (card,) = _cards(text)  # also checks every physical line is at most 75 octets
    assert _one(card, "FN") == "Zoë Ångström-Łukasiewicz"
    assert _one(card, "N") == "Ångström-Łukasiewicz;Zoë;;;"
    assert _one(card, "ORG") == "東京ソフトウェア株式会社 研究開発部門 第三グループ"
    assert _one(card, "NOTE") == "Ça va? " * 20
    assert "\r\n " in text  # the note is long enough that something did fold


def test_text_values_escape_comma_semicolon_backslash_and_newline(session: Session) -> None:
    user = factories.make_user(session)
    contact = factories.make_contact(
        session, user, current_company="Acme; Sales, West", notes="a\\b\nc"
    )
    _tag(session, user, "clients, 2026; west\\coast", contact)
    session.commit()
    contact_card, group = _cards(_run(session, user))
    assert _one(contact_card, "ORG") == "Acme\\; Sales\\, West"
    assert _one(contact_card, "NOTE") == "a\\\\b\\nc"
    assert _one(contact_card, "CATEGORIES") == "clients\\, 2026\\; west\\\\coast"
    assert _one(group, "FN") == "clients\\, 2026\\; west\\\\coast"


def test_nickname_title_and_the_group_n_are_escaped(session: Session) -> None:
    """NICKNAME, TITLE, and the group card's N go through the same escaping as
    every other TEXT value, so none can end its line or add a component (#254)."""
    user = factories.make_user(session)
    contact = factories.make_contact(
        session,
        user,
        first_name="Robert",
        preferred_name="Bob; the, \\builder",
        current_title="VP, Sales; West\rRegion",
    )
    _tag(session, user, "clients, 2026; west\\coast\nnew", contact)
    session.commit()
    contact_card, group = _cards(_run(session, user))  # no bare CR or LF on any line
    assert _one(contact_card, "NICKNAME") == "Bob\\; the\\, \\\\builder"
    assert _one(contact_card, "TITLE") == "VP\\, Sales\\; West\\nRegion"
    assert _one(group, "N") == "clients\\, 2026\\; west\\\\coast\\nnew;;;;"


# --- tags: CATEGORIES and group cards -------------------------------------------------


def test_every_tag_becomes_a_group_card_after_the_contacts(session: Session) -> None:
    user = factories.make_user(session)
    ada = factories.make_contact(session, user, first_name="Ada", preferred_name="Ada")
    ben = factories.make_contact(session, user, first_name="Ben", preferred_name="Ben")
    cy = factories.make_contact(session, user, first_name="Cy", preferred_name="Cy")
    vip = _tag(session, user, "VIP", ada, ben)
    alumni = _tag(session, user, "alumni", ben)
    session.commit()

    cards = _cards(_run(session, user))
    people, groups = cards[:3], cards[3:]
    assert not any(_is_group(card) for card in people)
    assert all(_is_group(card) for card in groups)

    by_uid = {_one(card, "UID"): card for card in people}
    assert set(by_uid) == {_contact_uid(user, c) for c in (ada, ben, cy)}
    assert _one(by_uid[_contact_uid(user, ben)], "CATEGORIES") == "alumni,VIP"
    assert _one(by_uid[_contact_uid(user, ada)], "CATEGORIES") == "VIP"
    assert _values(by_uid[_contact_uid(user, cy)], "CATEGORIES") == []

    # Groups in tag-name order, case-insensitively, like Contact.tags.
    assert [_one(group, "FN") for group in groups] == ["alumni", "VIP"]
    alumni_card, vip_card = groups
    assert _one(alumni_card, "VERSION") == "3.0"
    assert _one(alumni_card, "UID") == _tag_uid(user, alumni.id)
    assert _one(alumni_card, "N") == "alumni;;;;"
    assert _values(alumni_card, "X-ADDRESSBOOKSERVER-MEMBER") == [
        f"urn:uuid:{_contact_uid(user, ben)}"
    ]
    assert _one(vip_card, "UID") == _tag_uid(user, vip.id)
    assert _values(vip_card, "X-ADDRESSBOOKSERVER-MEMBER") == [
        f"urn:uuid:{_contact_uid(user, ada)}",
        f"urn:uuid:{_contact_uid(user, ben)}",
    ]


def test_a_tag_with_no_exported_member_gets_no_group(session: Session) -> None:
    user = factories.make_user(session)
    ada = factories.make_contact(session, user)
    gone = factories.make_contact(session, user, do_not_contact=True)
    _tag(session, user, "kept", ada)
    _tag(session, user, "only-dnc", gone)
    _tag(session, user, "empty")
    session.commit()
    groups = [card for card in _cards(_run(session, user)) if _is_group(card)]
    assert [_one(group, "FN") for group in groups] == ["kept"]


def test_a_filtered_export_groups_only_the_contacts_it_selects(session: Session) -> None:
    user = factories.make_user(session)
    ada = factories.make_contact(session, user)
    ben = factories.make_contact(session, user)
    _tag(session, user, "vip", ada)
    _tag(session, user, "shared", ada, ben)
    session.commit()
    tree = parse_filter({"where": {"op": "tag_any", "names": ["vip"]}})
    cards = _cards(_run(session, user, tree))
    people = [card for card in cards if not _is_group(card)]
    groups = {_one(g, "FN"): g for g in cards if _is_group(g)}
    assert [_one(card, "UID") for card in people] == [_contact_uid(user, ada)]
    assert set(groups) == {"shared", "vip"}
    assert _values(groups["shared"], "X-ADDRESSBOOKSERVER-MEMBER") == [
        f"urn:uuid:{_contact_uid(user, ada)}"
    ]


# --- who is in the file ----------------------------------------------------------------


def test_leaves_out_do_not_contact_archived_and_merged_contacts(session: Session) -> None:
    user = factories.make_user(session)
    kept = factories.make_contact(session, user, emails=["kept@example.test"])
    factories.make_contact(session, user, emails=["dnc@example.test"], do_not_contact=True)
    archived = factories.make_contact(
        session, user, emails=["archived@example.test"], archived_at=NOW
    )
    factories.make_contact(session, user, emails=["merged@example.test"], merged_into_id=kept.id)
    session.commit()

    text = _run(session, user)
    assert [_one(card, "UID") for card in _cards(text)] == [_contact_uid(user, kept)]

    # Archived people come back when the filter asks for them, as in every preset;
    # a do-not-contact person never does, whatever the filter says.
    with_archived = _run(session, user, FilterTree(include_archived=True))
    uids = [_one(card, "UID") for card in _cards(with_archived)]
    assert uids == [_contact_uid(user, kept), _contact_uid(user, archived)]
    dnc_only = parse_filter({"where": {"op": "eq", "field": "do_not_contact", "value": True}})
    assert _run(session, user, dnc_only) == ""


def test_uids_are_stable_and_distinct(session: Session) -> None:
    """The same rows export the same UIDs every time; no two cards share one."""
    user = factories.make_user(session)
    ada = factories.make_contact(session, user)
    _tag(session, user, "one", ada)
    session.commit()
    first, second = _run(session, user), _run(session, user)
    assert first == second
    uids = [_one(card, "UID") for card in _cards(first)]
    assert len(uids) == len(set(uids)) == 2
    for uid in uids:
        assert uuid.UUID(uid).version == 5


def test_another_users_contacts_and_tags_never_appear(session: Session) -> None:
    user = factories.make_user(session)
    other = factories.make_user(session)
    mine = factories.make_contact(session, user, emails=["mine@example.test"])
    theirs = factories.make_contact(session, other, emails=["theirs@example.test"])
    _tag(session, user, "mine-tag", mine)
    _tag(session, other, "their-tag", theirs)
    session.commit()
    text = _run(session, user)
    assert "theirs@example.test" not in text
    assert "their-tag" not in text
    assert _contact_uid(other, theirs) not in text
