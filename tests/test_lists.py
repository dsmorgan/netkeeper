"""netkeeper.crm.lists (spec 10.1, 10.4; item P1-08): lists, membership, saved views."""

from __future__ import annotations

from collections.abc import Iterator

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.crm import identity
from netkeeper.crm.filters import FilterError, SortKey, compile_filter, parse_filter
from netkeeper.crm.lists import (
    UNSET,
    VALIDATED_FILTER,
    VALIDATED_LIST_NAME,
    VALIDATED_SEEDED_KEY,
    ContactNotFound,
    DuplicateListName,
    DuplicateViewName,
    InvalidListValue,
    InvalidViewValue,
    ListNotFound,
    ViewNotFound,
    WrongListKind,
    add_members,
    create_list,
    create_view,
    delete_list,
    delete_view,
    ensure_validated_list,
    find_list,
    get_list,
    get_view,
    list_lists,
    list_members,
    list_views,
    member_count,
    member_counts,
    remove_member,
    update_list,
    update_view,
)
from netkeeper.db import session_scope
from netkeeper.models import ContactMet, ListKind, User, UserKind
from netkeeper.services.settings_kv import get_setting

# --- fixtures -----------------------------------------------------------


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A writer session, as every caller of the service must use: it reads, then writes."""
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer)


@pytest.fixture
def other(writer: Session) -> User:
    return factories.make_user(writer, kind=UserKind.HOSTED)


# --- lists CRUD -----------------------------------------------------------


def test_create_list_static_and_smart(writer: Session, user: User) -> None:
    static = create_list(writer, user, "First 100", ListKind.STATIC)
    assert static.kind is ListKind.STATIC and static.filter_json is None

    tree = parse_filter({"where": {"op": "has_email"}})
    smart = create_list(writer, user, "Has email", ListKind.SMART, filter=tree)
    assert smart.kind is ListKind.SMART
    assert smart.filter_json == tree.model_dump(mode="json")


def test_a_static_list_takes_no_filter_and_a_smart_list_needs_one(
    writer: Session, user: User
) -> None:
    tree = parse_filter({"where": {"op": "has_email"}})
    with pytest.raises(InvalidListValue):
        create_list(writer, user, "bad", ListKind.STATIC, filter=tree)
    with pytest.raises(InvalidListValue):
        create_list(writer, user, "bad", ListKind.SMART)


def test_a_broken_or_unsupported_filter_is_refused_at_write_time(
    writer: Session, user: User
) -> None:
    ok_placeholder = parse_filter({"where": {"op": "list_member", "list_id": 1}})
    with pytest.raises(FilterError):  # UnsupportedPredicate: list_member does not compile yet
        create_list(writer, user, "bad filter", ListKind.SMART, filter=ok_placeholder)


def test_duplicate_list_name_is_refused(writer: Session, user: User) -> None:
    create_list(writer, user, "Mine", ListKind.STATIC)
    with pytest.raises(DuplicateListName):
        create_list(writer, user, "Mine", ListKind.STATIC)


def test_list_name_is_cleaned_and_empty_is_refused(writer: Session, user: User) -> None:
    row = create_list(writer, user, "  Spacey  ", ListKind.STATIC)
    assert row.name == "Spacey"
    with pytest.raises(InvalidListValue):
        create_list(writer, user, "   ", ListKind.STATIC)


def test_rename_list_and_replace_a_smart_lists_filter(writer: Session, user: User) -> None:
    tree = parse_filter({"where": {"op": "has_email"}})
    smart = create_list(writer, user, "Has email", ListKind.SMART, filter=tree)
    renamed = update_list(writer, user, smart.id, name="Has an email")
    assert renamed.name == "Has an email"
    assert renamed.filter_json == tree.model_dump(mode="json")

    other_tree = parse_filter({"where": {"op": "has_phone"}})
    replaced = update_list(writer, user, smart.id, filter=other_tree)
    assert replaced.filter_json == other_tree.model_dump(mode="json")


def test_update_list_leaves_filter_alone_when_not_given(writer: Session, user: User) -> None:
    tree = parse_filter({"where": {"op": "has_email"}})
    smart = create_list(writer, user, "Has email", ListKind.SMART, filter=tree)
    renamed = update_list(writer, user, smart.id, name="Still has email", filter=UNSET)
    assert renamed.filter_json == tree.model_dump(mode="json")


def test_a_static_lists_filter_cannot_be_set(writer: Session, user: User) -> None:
    static = create_list(writer, user, "Static", ListKind.STATIC)
    tree = parse_filter({"where": {"op": "has_email"}})
    with pytest.raises(WrongListKind):
        update_list(writer, user, static.id, filter=tree)


def test_rename_to_an_existing_name_is_refused(writer: Session, user: User) -> None:
    create_list(writer, user, "A", ListKind.STATIC)
    b = create_list(writer, user, "B", ListKind.STATIC)
    with pytest.raises(DuplicateListName):
        update_list(writer, user, b.id, name="A")
    # renaming to its own name is fine
    same = update_list(writer, user, b.id, name="B")
    assert same.name == "B"


def test_get_and_delete_list(writer: Session, user: User) -> None:
    row = create_list(writer, user, "Mine", ListKind.STATIC)
    assert get_list(writer, user, row.id).id == row.id
    with pytest.raises(ListNotFound):
        get_list(writer, user, row.id + 1000)
    delete_list(writer, user, row.id)
    with pytest.raises(ListNotFound):
        get_list(writer, user, row.id)
    assert find_list(writer, user, "Mine") is None


def test_list_lists_orders_by_name(writer: Session, user: User) -> None:
    create_list(writer, user, "Zebra", ListKind.STATIC)
    create_list(writer, user, "Apple", ListKind.STATIC)
    assert [row.name for row in list_lists(writer, user)] == ["Apple", "Zebra"]


# --- static membership ------------------------------------------------------


def test_add_and_remove_static_members(writer: Session, user: User) -> None:
    static = create_list(writer, user, "First 100", ListKind.STATIC)
    c1 = factories.make_contact(writer, user)
    c2 = factories.make_contact(writer, user)

    added = add_members(writer, user, static.id, [c1.id, c2.id, c1.id])  # duplicate id in input
    assert added == 2

    added_again = add_members(writer, user, static.id, [c1.id])  # already a member
    assert added_again == 0

    members, total = list_members(writer, user, static.id, limit=50)
    assert total == 2
    assert {c.id for c in members} == {c1.id, c2.id}
    assert member_count(writer, user, static.id) == 2

    assert remove_member(writer, user, static.id, c1.id) is True
    assert remove_member(writer, user, static.id, c1.id) is False
    assert member_count(writer, user, static.id) == 1


def test_adding_a_contact_that_is_not_the_users_fails(
    writer: Session, user: User, other: User
) -> None:
    static = create_list(writer, user, "Mine", ListKind.STATIC)
    theirs = factories.make_contact(writer, other)
    with pytest.raises(ContactNotFound):
        add_members(writer, user, static.id, [theirs.id])


def test_membership_operations_refuse_a_smart_list(writer: Session, user: User) -> None:
    tree = parse_filter({"where": {"op": "has_email"}})
    smart = create_list(writer, user, "Has email", ListKind.SMART, filter=tree)
    contact = factories.make_contact(writer, user)
    with pytest.raises(WrongListKind):
        add_members(writer, user, smart.id, [contact.id])
    with pytest.raises(WrongListKind):
        remove_member(writer, user, smart.id, contact.id)


def test_member_counts_batches_several_lists(writer: Session, user: User) -> None:
    a = create_list(writer, user, "A", ListKind.STATIC)
    b = create_list(writer, user, "B", ListKind.STATIC)
    c1 = factories.make_contact(writer, user)
    add_members(writer, user, a.id, [c1.id])
    counts = member_counts(writer, user, [a, b])
    assert counts == {a.id: 1, b.id: 0}


# --- smart list membership: the "done when" for P1-08 -----------------------


def test_a_smart_lists_members_equal_the_filters_result(writer: Session, user: User) -> None:
    """A smart list's members must equal a direct run of its filter (P1-08 "done when")."""
    met = factories.make_contact(writer, user, met=ContactMet.MET)
    factories.make_contact(writer, user, met=ContactMet.MET)
    factories.make_contact(writer, user, met=ContactMet.NOT_MET)
    factories.make_contact(writer, user, met=ContactMet.UNKNOWN)

    tree = parse_filter({"where": {"op": "eq", "field": "met", "value": "met"}})
    smart = create_list(writer, user, "Met", ListKind.SMART, filter=tree)

    direct_ids = sorted(c.id for c in writer.scalars(compile_filter(user, tree)).all())
    members, total = list_members(writer, user, smart.id, limit=50)
    assert sorted(c.id for c in members) == direct_ids
    assert total == len(direct_ids) == 2
    assert met.id in direct_ids
    assert member_count(writer, user, smart.id) == len(direct_ids)


def test_a_smart_lists_members_track_the_live_data(writer: Session, user: User) -> None:
    """Nothing is materialized: changing a contact's field changes the smart list at once."""
    contact = factories.make_contact(writer, user, met=ContactMet.UNKNOWN)
    tree = parse_filter({"where": {"op": "eq", "field": "met", "value": "met"}})
    smart = create_list(writer, user, "Met", ListKind.SMART, filter=tree)
    assert member_count(writer, user, smart.id) == 0

    contact.met = ContactMet.MET
    writer.flush()
    assert member_count(writer, user, smart.id) == 1
    members, _total = list_members(writer, user, smart.id, limit=50)
    assert [c.id for c in members] == [contact.id]

    # A contact that did not exist when the list was made still belongs to it. Without
    # this, an implementation that froze membership at creation time passes every other
    # test in this file, because they all build their contacts before the list.
    later = factories.make_contact(writer, user, met=ContactMet.MET)
    assert member_count(writer, user, smart.id) == 2
    members, _total = list_members(writer, user, smart.id, limit=50)
    assert sorted(c.id for c in members) == sorted([contact.id, later.id])


def test_a_static_list_drops_a_member_that_was_merged_away(writer: Session, user: User) -> None:
    """A merged-away contact is a tombstone: `merge` moves its emails, phones and
    LinkedIn identity to the survivor, so leaving it in a static list would hand a
    campaign a row with no way to reach anyone. Spec 8.2 and `filters.py`'s rule that
    merged-away contacts never appear; every smart list already honors it."""
    keep = factories.make_contact(writer, user)
    loser = factories.make_contact(writer, user)
    survivor = factories.make_contact(writer, user)
    row = create_list(writer, user, "First 100", ListKind.STATIC)
    add_members(writer, user, row.id, [keep.id, loser.id])
    assert member_count(writer, user, row.id) == 2

    identity.merge(writer, user, survivor.id, loser.id)

    members, total = list_members(writer, user, row.id, limit=50)
    assert [c.id for c in members] == [keep.id]
    assert total == 1
    assert member_count(writer, user, row.id) == 1


def test_smart_list_members_pagination(writer: Session, user: User) -> None:
    ids = [factories.make_contact(writer, user, met=ContactMet.MET).id for _ in range(5)]
    tree = parse_filter({"where": {"op": "eq", "field": "met", "value": "met"}})
    smart = create_list(writer, user, "Met", ListKind.SMART, filter=tree)
    page1, total = list_members(writer, user, smart.id, limit=2, offset=0)
    page2, _total = list_members(writer, user, smart.id, limit=2, offset=2)
    assert total == 5
    assert [c.id for c in page1] == sorted(ids)[:2]
    assert [c.id for c in page2] == sorted(ids)[2:4]


# --- the built-in "Validated" list -------------------------------------


def test_validated_list_is_seeded_once_and_matches_met_contacts(
    writer: Session, user: User
) -> None:
    met = factories.make_contact(writer, user, met=ContactMet.MET)
    factories.make_contact(writer, user, met=ContactMet.NOT_MET)

    row = ensure_validated_list(writer, user)
    assert row is not None and row.name == VALIDATED_LIST_NAME and row.kind is ListKind.SMART
    assert get_setting(writer, user, VALIDATED_SEEDED_KEY) is True

    tree = parse_filter(VALIDATED_FILTER)
    direct_ids = [c.id for c in writer.scalars(compile_filter(user, tree)).all()]
    members, _total = list_members(writer, user, row.id, limit=50)
    assert [c.id for c in members] == direct_ids == [met.id]

    # idempotent: a second call seeds nothing
    assert ensure_validated_list(writer, user) is None


def test_a_deleted_validated_list_never_comes_back(writer: Session, user: User) -> None:
    row = ensure_validated_list(writer, user)
    assert row is not None
    delete_list(writer, user, row.id)
    assert ensure_validated_list(writer, user) is None
    assert find_list(writer, user, VALIDATED_LIST_NAME) is None


def test_validated_list_reuses_an_existing_list_of_that_name(writer: Session, user: User) -> None:
    """Like ensure_default_rules reusing an existing tag: a list the user already made by
    that exact name is adopted, not duplicated."""
    manual = create_list(writer, user, VALIDATED_LIST_NAME, ListKind.STATIC)
    row = ensure_validated_list(writer, user)
    assert row is not None and row.id == manual.id
    assert row.kind is ListKind.STATIC  # untouched: not converted to smart


# --- saved views ------------------------------------------------------------


def test_create_and_read_back_a_saved_view(writer: Session, user: User) -> None:
    tree = parse_filter({"where": {"op": "has_email"}})
    view = create_view(
        writer,
        user,
        "Default",
        ["first_name", "last_name", "headline"],
        sort=[SortKey(field="last_name")],
        filter=tree,
    )
    assert view.columns == ["first_name", "last_name", "headline"]
    assert view.sort == [{"field": "last_name", "direction": "asc"}]
    assert view.filter_json == tree.model_dump(mode="json")
    assert get_view(writer, user, view.id).id == view.id


def test_a_view_needs_at_least_one_column(writer: Session, user: User) -> None:
    with pytest.raises(InvalidViewValue):
        create_view(writer, user, "Empty", [])


def test_duplicate_view_name_is_refused(writer: Session, user: User) -> None:
    create_view(writer, user, "Mine", ["first_name"])
    with pytest.raises(DuplicateViewName):
        create_view(writer, user, "Mine", ["first_name"])


def test_update_view_replaces_columns_and_sort_but_leaves_them_alone_when_omitted(
    writer: Session, user: User
) -> None:
    view = create_view(writer, user, "V", ["first_name"])
    updated = update_view(writer, user, view.id, columns=["last_name"])
    assert updated.columns == ["last_name"]
    unchanged = update_view(writer, user, view.id, name="V2")
    assert unchanged.columns == ["last_name"]
    assert unchanged.name == "V2"


def test_update_view_filter_is_tri_state(writer: Session, user: User) -> None:
    tree = parse_filter({"where": {"op": "has_email"}})
    view = create_view(writer, user, "V", ["first_name"])
    assert view.filter_json is None

    with_filter = update_view(writer, user, view.id, filter=tree)
    assert with_filter.filter_json == tree.model_dump(mode="json")

    left_alone = update_view(writer, user, view.id, filter=UNSET)
    assert left_alone.filter_json == tree.model_dump(mode="json")

    cleared = update_view(writer, user, view.id, filter=None)
    assert cleared.filter_json is None


def test_delete_view(writer: Session, user: User) -> None:
    view = create_view(writer, user, "V", ["first_name"])
    delete_view(writer, user, view.id)
    with pytest.raises(ViewNotFound):
        get_view(writer, user, view.id)


def test_list_views_orders_by_name(writer: Session, user: User) -> None:
    create_view(writer, user, "Zebra", ["first_name"])
    create_view(writer, user, "Apple", ["first_name"])
    assert [row.name for row in list_views(writer, user)] == ["Apple", "Zebra"]


# --- cross-user isolation (service level) -----------------------------------


def test_user_b_cannot_read_modify_or_add_members_to_user_as_list(
    writer: Session, user: User, other: User
) -> None:
    mine = create_list(writer, user, "Mine", ListKind.STATIC)
    contact = factories.make_contact(writer, user)

    with pytest.raises(ListNotFound):
        get_list(writer, other, mine.id)
    with pytest.raises(ListNotFound):
        update_list(writer, other, mine.id, name="Stolen")
    with pytest.raises(ListNotFound):
        delete_list(writer, other, mine.id)
    with pytest.raises(ListNotFound):
        add_members(writer, other, mine.id, [contact.id])
    with pytest.raises(ListNotFound):
        remove_member(writer, other, mine.id, contact.id)
    with pytest.raises(ListNotFound):
        list_members(writer, other, mine.id, limit=50)


def test_a_smart_list_owned_by_one_user_never_returns_another_users_contacts(
    writer: Session, user: User, other: User
) -> None:
    factories.make_contact(writer, other, met=ContactMet.MET)  # belongs to `other`, not `user`
    mine = factories.make_contact(writer, user, met=ContactMet.MET)

    tree = parse_filter({"where": {"op": "eq", "field": "met", "value": "met"}})
    smart = create_list(writer, user, "Met", ListKind.SMART, filter=tree)

    members, total = list_members(writer, user, smart.id, limit=50)
    assert total == 1
    assert [c.id for c in members] == [mine.id]

    # the same filter, run as `other`, must never see `user`'s contact
    other_ids = [c.id for c in writer.scalars(compile_filter(other, tree)).all()]
    assert mine.id not in other_ids


def test_user_b_cannot_read_or_modify_user_as_saved_view(
    writer: Session, user: User, other: User
) -> None:
    mine = create_view(writer, user, "Mine", ["first_name"])
    with pytest.raises(ViewNotFound):
        get_view(writer, other, mine.id)
    with pytest.raises(ViewNotFound):
        update_view(writer, other, mine.id, name="Stolen")
    with pytest.raises(ViewNotFound):
        delete_view(writer, other, mine.id)


# --- writer session requirement ---------------------------------------------


def test_writes_need_a_writer_session(session: Session, user: User) -> None:
    with pytest.raises(RuntimeError):
        create_list(session, user, "x", ListKind.STATIC)
