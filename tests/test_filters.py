"""netkeeper.crm.filters (spec 10.4): parsing, compiling, sorting, paging, describing.

The JSON schema of the tree is snapshotted in ``tests/snapshots/filter_schema.json``
because the frontend builder (P1-15) is written against it. When a change to the
tree is intended, regenerate the snapshot and commit it:

    UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest tests/test_filters.py
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, get_args

import factories
import pytest
from sqlalchemy import Boolean, Date, Enum, Integer, String
from sqlalchemy.orm import Session

from netkeeper.crm.filters import (
    FIELDS,
    NODE_TYPES,
    OPS,
    PLACEHOLDERS,
    AnyField,
    EmptyableField,
    FilterError,
    FilterTree,
    OrderedField,
    SortKey,
    StringField,
    UnsupportedPredicate,
    apply_sort,
    compile_count,
    compile_filter,
    compile_update,
    compile_where,
    describe,
    paginate,
    parse_filter,
    parse_sort,
)
from netkeeper.models import (
    Contact,
    ContactMet,
    ContactSnapshot,
    ContactSource,
    EmailStatus,
    User,
    UserKind,
    UTCDateTime,
)
from netkeeper.scoping import SCOPE_OPTION, scoped

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
TODAY = NOW.date()
SNAPSHOT = Path(__file__).parent / "snapshots" / "filter_schema.json"

# Representative trees with their readings. Every op appears at least once
# (a test checks), so these double as the round-trip and describe corpus and as
# the "ten representative filters" CP1 asks to see compiled.
EXAMPLES: list[tuple[dict[str, Any], str]] = [
    ({"where": None}, "all contacts"),
    (
        {
            "where": {
                "op": "and",
                "children": [
                    {"op": "eq", "field": "met", "value": "met"},
                    {"op": "has_email"},
                    {"op": "connected_within_days", "days": 365},
                ],
            }
        },
        "met, has email, connected in the last 365 days",
    ),
    (
        {
            "where": {
                "op": "or",
                "children": [
                    {"op": "last_contacted", "never": True},
                    {"op": "last_contacted", "older_than_days": 90},
                ],
            }
        },
        "never contacted or last contacted more than 90 days ago",
    ),
    (
        {
            "where": {
                "op": "and",
                "children": [
                    {"op": "eq", "field": "do_not_contact", "value": False},
                    {"op": "has_email", "status": "ok"},
                    {"op": "contains", "field": "headline", "value": "vp"},
                ],
            }
        },
        'ok to contact, has a working email, headline contains "vp"',
    ),
    (
        {
            "where": {
                "op": "and",
                "children": [
                    {"op": "changed_jobs_within_days", "days": 30},
                    {
                        "op": "not",
                        "child": {
                            "op": "or",
                            "children": [
                                {"op": "has_phone"},
                                {"op": "email_contains", "value": "@acme.example"},
                            ],
                        },
                    },
                ],
            }
        },
        'changed jobs in the last 30 days, not (has phone or email contains "@acme.example")',
    ),
    (
        {
            "where": {
                "op": "between",
                "field": "connected_on",
                "low": "2020-01-01",
                "high": "2020-12-31",
            }
        },
        "connected on between 2020-01-01 and 2020-12-31",
    ),
    (
        {
            "where": {
                "op": "and",
                "children": [
                    {"op": "starts_with", "field": "last_name", "value": "a"},
                    {"op": "is_empty", "field": "location"},
                    {"op": "gte", "field": "degree", "value": 1},
                    {"op": "lte", "field": "degree", "value": 2},
                ],
            }
        },
        'last name starts with "a", location is empty, degree at least 1, degree at most 2',
    ),
    (
        {
            "where": {
                "op": "and",
                "children": [
                    {"op": "lt", "field": "created_at", "value": "2026-01-01T00:00:00Z"},
                    {"op": "neq", "field": "source", "value": "csv"},
                    {"op": "gt", "field": "last_enriched_at", "value": "2026-06-01T00:00:00+02:00"},
                ],
            }
        },
        "created before 2026-01-01T00:00:00Z, not from csv, "
        "last enriched after 2026-06-01T00:00:00+02:00",
    ),
    (
        {
            "where": {
                "op": "and",
                "children": [
                    {"op": "tag_any", "names": ["vp", "founder"]},
                    {"op": "tag_none", "names": ["recruiter"]},
                    {"op": "list_member", "list_id": 3},
                ],
            }
        },
        "tagged any of vp, founder, not tagged recruiter, in list #3",
    ),
    (
        {
            "where": {
                "op": "and",
                "children": [
                    {"op": "enrolled_in", "campaign_id": 2},
                    {"op": "not", "child": {"op": "replied_in", "campaign_id": 2}},
                ],
            }
        },
        "enrolled in campaign #2, not replied in campaign #2",
    ),
    (
        {
            "where": {"op": "not", "child": {"op": "is_empty", "field": "archived_at"}},
            "include_archived": True,
        },
        "not archived is empty, including archived",
    ),
    (
        {
            "where": {
                "op": "and",
                "children": [
                    {"op": "has_li_url"},
                    {"op": "has_position"},
                    {"op": "tag_all", "names": ["a", "b"]},
                    {"op": "eq", "field": "met", "value": "unknown"},
                    {"op": "neq", "field": "met", "value": "skip"},
                ],
            }
        },
        "has LinkedIn URL, has a position, tagged all of a, b, untriaged, met is not skip",
    ),
    (
        {
            "where": {
                "op": "or",
                "children": [
                    {
                        "op": "and",
                        "children": [
                            {"op": "eq", "field": "first_name", "value": "Bob"},
                            {"op": "eq", "field": "last_name", "value": "Smith"},
                        ],
                    },
                    {"op": "eq", "field": "preferred_name", "value": "Bobby"},
                ],
            }
        },
        '(first name is "Bob" and last name is "Smith") or preferred name is "Bobby"',
    ),
    (
        {"where": {"op": "last_contacted", "within_days": 7}},
        "contacted in the last 7 days",
    ),
    (
        {
            "where": {
                "op": "or",
                "children": [
                    {"op": "has_email", "status": "bounced"},
                    {"op": "has_email", "status": "invalid"},
                ],
            }
        },
        "has a bounced email or has an invalid email",
    ),
]


@pytest.fixture
def user(session: Session) -> User:
    return factories.make_user(session)


@pytest.fixture
def other(session: Session) -> User:
    return factories.make_user(session, kind=UserKind.HOSTED)


def matching(
    session: Session,
    user: User,
    where: dict[str, Any] | None,
    *,
    include_archived: bool = False,
    now: datetime = NOW,
) -> list[int]:
    """Ids the compiled filter returns, after checking the count query agrees."""
    tree = parse_filter({"where": where, "include_archived": include_archived})
    ids = sorted(c.id for c in session.scalars(compile_filter(user, tree, now=now)))
    assert session.scalar(compile_count(user, tree, now=now)) == len(ids)
    return ids


# --- the allowlist ----------------------------------------------------------


def test_field_sets_agree_with_fields_and_with_the_contact_columns() -> None:
    kinds = {name: spec.kind for name, spec in FIELDS.items()}
    assert set(get_args(AnyField)) == set(FIELDS)
    assert set(get_args(StringField)) == {n for n, k in kinds.items() if k == "string"}
    assert set(get_args(OrderedField)) == {
        n for n, k in kinds.items() if k in ("int", "date", "datetime")
    }
    assert set(get_args(EmptyableField)) == {
        n for n, k in kinds.items() if k in ("string", "date", "datetime")
    }
    columns = Contact.__table__.c
    for name, spec in FIELDS.items():
        column = columns[name]
        match spec.kind:
            case "string":
                assert type(column.type) is String, name
            case "enum":
                assert isinstance(column.type, Enum), name
                assert spec.values == tuple(column.type.enums), name
            case "int":
                assert isinstance(column.type, Integer), name
            case "date":
                assert isinstance(column.type, Date), name
            case "datetime":
                assert isinstance(column.type, UTCDateTime), name
            case "bool":
                assert isinstance(column.type, Boolean), name
        assert spec.label
        assert "eq" in spec.ops and "neq" in spec.ops


def test_every_op_has_one_node_type_and_placeholders_are_ops() -> None:
    assert len(OPS) == len(set(OPS)) == len(NODE_TYPES) == 27
    assert set(PLACEHOLDERS) < set(OPS)
    assert set(PLACEHOLDERS.values()) == {"P1-07", "P1-08", "P3-04"}


def test_examples_cover_every_op() -> None:
    def ops_in(node: Any) -> set[str]:
        if not isinstance(node, dict):
            return set()
        found = {node["op"]} if "op" in node else set()
        for value in node.values():
            if isinstance(value, dict):
                found |= ops_in(value)
            elif isinstance(value, list):
                for item in value:
                    found |= ops_in(item)
        return found

    used: set[str] = set()
    for tree, _ in EXAMPLES:
        used |= ops_in(tree)
    assert used == set(OPS)


# --- comparisons ------------------------------------------------------------


def test_eq_on_a_string_is_case_insensitive_and_neq_is_its_complement(
    session: Session, user: User
) -> None:
    acme = factories.make_contact(session, user, current_company="Acme")
    globex = factories.make_contact(session, user, current_company="Globex")
    nobody = factories.make_contact(session, user, current_company=None)
    assert matching(session, user, {"op": "eq", "field": "current_company", "value": "acme"}) == [
        acme.id
    ]
    assert matching(session, user, {"op": "neq", "field": "current_company", "value": "ACME"}) == [
        globex.id,
        nobody.id,
    ]


def test_eq_on_enum_int_and_bool_fields(session: Session, user: User) -> None:
    met = factories.make_contact(session, user, met=ContactMet.MET, degree=2, do_not_contact=True)
    not_met = factories.make_contact(session, user, met=ContactMet.NOT_MET)
    assert matching(session, user, {"op": "eq", "field": "met", "value": "met"}) == [met.id]
    assert matching(session, user, {"op": "neq", "field": "met", "value": "met"}) == [not_met.id]
    assert matching(session, user, {"op": "eq", "field": "degree", "value": 2}) == [met.id]
    assert matching(session, user, {"op": "eq", "field": "do_not_contact", "value": True}) == [
        met.id
    ]
    assert matching(session, user, {"op": "neq", "field": "do_not_contact", "value": True}) == [
        not_met.id
    ]
    assert matching(session, user, {"op": "eq", "field": "source", "value": "manual"}) == [
        met.id,
        not_met.id,
    ]


def test_eq_on_a_date_and_on_a_datetime_with_an_offset(session: Session, user: User) -> None:
    hit = factories.make_contact(
        session, user, connected_on=date(2020, 5, 17), last_contacted_at=NOW
    )
    factories.make_contact(
        session, user, connected_on=date(2020, 5, 18), last_contacted_at=NOW + timedelta(hours=1)
    )
    assert matching(
        session, user, {"op": "eq", "field": "connected_on", "value": "2020-05-17"}
    ) == [hit.id]
    # 14:00 at +02:00 is NOW.
    tree = {"op": "eq", "field": "last_contacted_at", "value": "2026-09-20T14:00:00+02:00"}
    assert matching(session, user, tree) == [hit.id]


def test_contains_is_case_insensitive_and_matches_wildcards_literally(
    session: Session, user: User
) -> None:
    vp = factories.make_contact(session, user, headline="VP of Sales")
    factories.make_contact(session, user, headline="Engineer")
    factories.make_contact(session, user, headline=None)
    percent = factories.make_contact(session, user, headline="50% growth")
    factories.make_contact(session, user, headline="50 growth")
    underscore = factories.make_contact(session, user, headline="a_b")
    factories.make_contact(session, user, headline="axb")
    assert matching(session, user, {"op": "contains", "field": "headline", "value": "vp"}) == [
        vp.id
    ]
    assert matching(session, user, {"op": "contains", "field": "headline", "value": "50%"}) == [
        percent.id
    ]
    assert matching(session, user, {"op": "contains", "field": "headline", "value": "a_b"}) == [
        underscore.id
    ]


def test_starts_with_anchors_at_the_start(session: Session, user: User) -> None:
    anna = factories.make_contact(session, user, last_name="Anderson")
    factories.make_contact(session, user, last_name="Hannah")
    assert matching(session, user, {"op": "starts_with", "field": "last_name", "value": "an"}) == [
        anna.id
    ]


def test_is_empty_means_null_or_blank(session: Session, user: User) -> None:
    null = factories.make_contact(session, user, location=None, connected_on=None)
    blank = factories.make_contact(session, user, location="", connected_on=date(2020, 1, 1))
    factories.make_contact(
        session, user, location="Austin", connected_on=date(2020, 1, 1), last_contacted_at=NOW
    )
    assert matching(session, user, {"op": "is_empty", "field": "location"}) == [null.id, blank.id]
    assert matching(session, user, {"op": "is_empty", "field": "connected_on"}) == [null.id]
    assert matching(session, user, {"op": "is_empty", "field": "last_contacted_at"}) == [
        null.id,
        blank.id,
    ]


def test_ordered_comparisons_on_an_int_include_or_exclude_the_boundary(
    session: Session, user: User
) -> None:
    one = factories.make_contact(session, user, degree=1)
    two = factories.make_contact(session, user, degree=2)
    three = factories.make_contact(session, user, degree=3)
    assert matching(session, user, {"op": "gt", "field": "degree", "value": 2}) == [three.id]
    assert matching(session, user, {"op": "gte", "field": "degree", "value": 2}) == [
        two.id,
        three.id,
    ]
    assert matching(session, user, {"op": "lt", "field": "degree", "value": 2}) == [one.id]
    assert matching(session, user, {"op": "lte", "field": "degree", "value": 2}) == [one.id, two.id]
    tree = {"op": "between", "field": "degree", "low": 2, "high": 3}
    assert matching(session, user, tree) == [two.id, three.id]


def test_date_boundaries(session: Session, user: User) -> None:
    before = factories.make_contact(session, user, connected_on=date(2020, 5, 16))
    on = factories.make_contact(session, user, connected_on=date(2020, 5, 17))
    after = factories.make_contact(session, user, connected_on=date(2020, 5, 18))
    factories.make_contact(session, user, connected_on=None)
    assert matching(
        session, user, {"op": "gt", "field": "connected_on", "value": "2020-05-17"}
    ) == [after.id]
    assert matching(
        session, user, {"op": "gte", "field": "connected_on", "value": "2020-05-17"}
    ) == [
        on.id,
        after.id,
    ]
    assert matching(
        session, user, {"op": "lt", "field": "connected_on", "value": "2020-05-17"}
    ) == [before.id]
    assert matching(
        session, user, {"op": "lte", "field": "connected_on", "value": "2020-05-17"}
    ) == [
        before.id,
        on.id,
    ]
    tree = {"op": "between", "field": "connected_on", "low": "2020-05-17", "high": "2020-05-18"}
    assert matching(session, user, tree) == [on.id, after.id]


def test_datetime_boundaries_compare_instants(session: Session, user: User) -> None:
    before = factories.make_contact(session, user, created_at=NOW - timedelta(seconds=1))
    on = factories.make_contact(session, user, created_at=NOW)
    after = factories.make_contact(session, user, created_at=NOW + timedelta(seconds=1))
    at = "2026-09-20T12:00:00Z"
    assert matching(session, user, {"op": "gt", "field": "created_at", "value": at}) == [after.id]
    assert matching(session, user, {"op": "gte", "field": "created_at", "value": at}) == [
        on.id,
        after.id,
    ]
    assert matching(session, user, {"op": "lt", "field": "created_at", "value": at}) == [before.id]
    assert matching(session, user, {"op": "lte", "field": "created_at", "value": at}) == [
        before.id,
        on.id,
    ]
    tree = {"op": "between", "field": "created_at", "low": at, "high": "2026-09-20T13:00:00+01:00"}
    assert matching(session, user, tree) == [on.id]


# --- children ---------------------------------------------------------------


def test_has_email_optionally_by_status(session: Session, user: User) -> None:
    ok = factories.make_contact(session, user, emails=["ok@example.test"])
    bounced = factories.make_contact(session, user, emails=["b@example.test"])
    bounced.emails[0].status = EmailStatus.BOUNCED
    session.flush()
    without = factories.make_contact(session, user)
    assert matching(session, user, {"op": "has_email"}) == [ok.id, bounced.id]
    assert matching(session, user, {"op": "has_email", "status": "bounced"}) == [bounced.id]
    assert matching(session, user, {"op": "has_email", "status": "ok"}) == [ok.id]
    assert matching(session, user, {"op": "not", "child": {"op": "has_email"}}) == [without.id]


def test_has_phone_has_position_and_email_contains(session: Session, user: User) -> None:
    full = factories.make_contact(
        session,
        user,
        emails=["Pat@Acme.example", "pat@home.example"],
        phones=["+15550100"],
        positions=[{"title": "Engineer", "company": "Acme"}],
    )
    bare = factories.make_contact(session, user, emails=["x@home.example"])
    assert matching(session, user, {"op": "has_phone"}) == [full.id]
    assert matching(session, user, {"op": "has_position"}) == [full.id]
    assert matching(session, user, {"op": "email_contains", "value": "@ACME."}) == [full.id]
    assert matching(session, user, {"op": "email_contains", "value": "home"}) == [full.id, bare.id]
    assert matching(session, user, {"op": "email_contains", "value": "50%"}) == []


def test_has_li_url_needs_a_non_blank_url(session: Session, user: User) -> None:
    linked = factories.make_contact(session, user)
    assert linked.li_url
    factories.make_contact(session, user, li_urn=None, li_public_id=None, li_url=None)
    factories.make_contact(session, user, li_urn=None, li_public_id=None, li_url="")
    assert matching(session, user, {"op": "has_li_url"}) == [linked.id]


# --- relative time ----------------------------------------------------------


def test_last_contacted_windows_and_never(session: Session, user: User) -> None:
    cutoff = NOW - timedelta(days=30)
    recent = factories.make_contact(session, user, last_contacted_at=NOW - timedelta(days=1))
    on_cutoff = factories.make_contact(session, user, last_contacted_at=cutoff)
    old = factories.make_contact(session, user, last_contacted_at=cutoff - timedelta(seconds=1))
    never = factories.make_contact(session, user, last_contacted_at=None)
    assert matching(session, user, {"op": "last_contacted", "within_days": 30}) == [
        recent.id,
        on_cutoff.id,
    ]
    assert matching(session, user, {"op": "last_contacted", "older_than_days": 30}) == [old.id]
    assert matching(session, user, {"op": "last_contacted", "never": True}) == [never.id]
    # The complement of "within" is "older or never": no three-valued surprises.
    within = {"op": "last_contacted", "within_days": 30}
    assert matching(session, user, {"op": "not", "child": within}) == [old.id, never.id]


def test_connected_within_days_counts_from_today(session: Session, user: User) -> None:
    edge = factories.make_contact(session, user, connected_on=TODAY - timedelta(days=7))
    factories.make_contact(session, user, connected_on=TODAY - timedelta(days=8))
    today = factories.make_contact(session, user, connected_on=TODAY)
    factories.make_contact(session, user, connected_on=None)
    assert matching(session, user, {"op": "connected_within_days", "days": 7}) == [
        edge.id,
        today.id,
    ]
    assert matching(session, user, {"op": "connected_within_days", "days": 0}) == [today.id]


def test_day_windows_use_today_in_the_users_timezone(session: Session) -> None:
    # 02:00 UTC on the 20th is the 20th at UTC+14 and still the 19th at UTC-10.
    now = datetime(2026, 9, 20, 2, 0, tzinfo=UTC)
    ahead = factories.make_user(session, timezone="Pacific/Kiritimati")
    behind = factories.make_user(session, timezone="Pacific/Honolulu")
    factories.make_contact(session, ahead, connected_on=date(2026, 9, 19))
    yesterday_there = factories.make_contact(session, behind, connected_on=date(2026, 9, 19))
    tree = {"op": "connected_within_days", "days": 0}
    assert matching(session, ahead, tree, now=now) == []
    assert matching(session, behind, tree, now=now) == [yesterday_there.id]


def test_an_unknown_timezone_falls_back_to_utc_with_a_warning(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    now = datetime(2026, 9, 20, 2, 0, tzinfo=UTC)
    user = factories.make_user(session, timezone="Mars/Olympus")
    contact = factories.make_contact(session, user, connected_on=date(2026, 9, 19))
    with caplog.at_level(logging.WARNING, logger="netkeeper.crm.filters"):
        assert matching(session, user, {"op": "connected_within_days", "days": 0}, now=now) == []
        assert matching(session, user, {"op": "connected_within_days", "days": 1}, now=now) == [
            contact.id
        ]
    assert "Mars/Olympus" in caplog.text


def test_now_must_be_aware(user: User) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        compile_filter(user, parse_filter({}), now=datetime(2026, 9, 20, 12, 0))


def test_changed_jobs_within_days_looks_for_a_snapshot_in_the_window(
    session: Session, user: User
) -> None:
    changed = factories.make_contact(session, user)
    changed.snapshots.append(
        ContactSnapshot(user_id=user.id, headline="then", observed_at=NOW - timedelta(days=10))
    )
    long_ago = factories.make_contact(session, user)
    long_ago.snapshots.append(
        ContactSnapshot(user_id=user.id, headline="then", observed_at=NOW - timedelta(days=40))
    )
    factories.make_contact(session, user)
    session.flush()
    assert matching(session, user, {"op": "changed_jobs_within_days", "days": 30}) == [changed.id]
    assert matching(session, user, {"op": "changed_jobs_within_days", "days": 5}) == []
    assert matching(session, user, {"op": "changed_jobs_within_days", "days": 40}) == [
        changed.id,
        long_ago.id,
    ]


# --- logic, archive, merge --------------------------------------------------


def test_logical_nesting(session: Session, user: User) -> None:
    a = factories.make_contact(session, user, met=ContactMet.MET, emails=["a@example.test"])
    b = factories.make_contact(session, user, met=ContactMet.MET)
    c = factories.make_contact(session, user, met=ContactMet.NOT_MET, emails=["c@example.test"])
    d = factories.make_contact(session, user, met=ContactMet.UNKNOWN)
    met = {"op": "eq", "field": "met", "value": "met"}
    has_email = {"op": "has_email"}
    assert matching(session, user, {"op": "and", "children": [met, has_email]}) == [a.id]
    assert matching(session, user, {"op": "or", "children": [met, has_email]}) == [a.id, b.id, c.id]
    tree = {
        "op": "and",
        "children": [
            {"op": "not", "child": {"op": "and", "children": [met, has_email]}},
            {"op": "or", "children": [met, {"op": "eq", "field": "met", "value": "unknown"}]},
        ],
    }
    assert matching(session, user, tree) == [b.id, d.id]
    assert matching(session, user, {"op": "and", "children": [met]}) == [a.id, b.id]


def test_archived_contacts_need_include_archived_and_merged_never_appear(
    session: Session, user: User
) -> None:
    live = factories.make_contact(session, user)
    archived = factories.make_contact(session, user, archived_at=NOW)
    factories.make_contact(session, user, merged_into_id=live.id)
    factories.make_contact(session, user, merged_into_id=live.id, archived_at=NOW)
    assert matching(session, user, None) == [live.id]
    assert matching(session, user, None, include_archived=True) == [live.id, archived.id]
    only_archived = {"op": "not", "child": {"op": "is_empty", "field": "archived_at"}}
    assert matching(session, user, only_archived) == []
    assert matching(session, user, only_archived, include_archived=True) == [archived.id]


# --- placeholders -----------------------------------------------------------

PLACEHOLDER_NODES: dict[str, dict[str, Any]] = {
    "tag_any": {"op": "tag_any", "names": ["vp"]},
    "tag_all": {"op": "tag_all", "names": ["vp", "founder"]},
    "tag_none": {"op": "tag_none", "names": ["recruiter"]},
    "list_member": {"op": "list_member", "list_id": 3},
    "enrolled_in": {"op": "enrolled_in", "campaign_id": 2},
    "replied_in": {"op": "replied_in", "campaign_id": 2},
}


@pytest.mark.parametrize("op", list(PLACEHOLDER_NODES))
def test_placeholders_parse_but_do_not_compile(user: User, op: str) -> None:
    node = PLACEHOLDER_NODES[op]
    tree = parse_filter({"where": {"op": "and", "children": [{"op": "has_email"}, node]}})
    assert tree.model_dump(mode="json")["where"]["children"][1] == node
    with pytest.raises(UnsupportedPredicate) as info:
        compile_where(user, tree)
    assert info.value.op == op
    assert info.value.item == PLACEHOLDERS[op]
    assert isinstance(info.value, FilterError)
    assert [issue.path for issue in info.value.issues] == ["where.children[1]"]
    assert (
        str(info.value)
        == f"where.children[1]: {op} is not available yet; {PLACEHOLDERS[op]} delivers it"
    )


def test_placeholder_set_is_exactly_the_six(user: User) -> None:
    assert set(PLACEHOLDER_NODES) == set(PLACEHOLDERS)


# --- invalid trees ----------------------------------------------------------

INVALID: list[tuple[str, Any, str, str]] = [
    ("unknown op", {"where": {"op": "bogus"}}, "where", "unknown op 'bogus'"),
    ("missing op", {"where": {"field": "met"}}, "where", "missing op"),
    (
        "unknown field",
        {"where": {"op": "eq", "field": "nope", "value": 1}},
        "where.field",
        "Input should be",
    ),
    (
        "op not for kind",
        {"where": {"op": "contains", "field": "degree", "value": "x"}},
        "where.field",
        "Input should be 'first_name'",
    ),
    (
        "enum value",
        {"where": {"op": "eq", "field": "met", "value": "friend"}},
        "where.value",
        "met expects one of: unknown, met, not_met, skip",
    ),
    (
        "int as string",
        {"where": {"op": "eq", "field": "degree", "value": "1"}},
        "where.value",
        "degree expects an integer",
    ),
    (
        "int as bool",
        {"where": {"op": "gt", "field": "degree", "value": True}},
        "where.value",
        "degree expects an integer",
    ),
    (
        "bool as int",
        {"where": {"op": "eq", "field": "do_not_contact", "value": 1}},
        "where.value",
        "expects true or false",
    ),
    (
        "string as int",
        {"where": {"op": "eq", "field": "headline", "value": 1}},
        "where.value",
        "headline expects a string",
    ),
    (
        "bad date",
        {"where": {"op": "eq", "field": "connected_on", "value": "2020-13-01"}},
        "where.value",
        "ISO date",
    ),
    (
        "naive datetime",
        {"where": {"op": "gt", "field": "created_at", "value": "2026-01-01T00:00:00"}},
        "where.value",
        "timezone offset",
    ),
    (
        "float value",
        {"where": {"op": "eq", "field": "headline", "value": 1.5}},
        "where.value",
        "string, integer, or boolean",
    ),
    (
        "null value",
        {"where": {"op": "eq", "field": "headline", "value": None}},
        "where.value",
        "string, integer, or boolean",
    ),
    ("empty group", {"where": {"op": "and", "children": []}}, "where.children", "at least 1 item"),
    (
        "deep path",
        {
            "where": {
                "op": "not",
                "child": {
                    "op": "or",
                    "children": [{"op": "has_email"}, {"op": "eq", "field": "met", "value": 3}],
                },
            }
        },
        "where.child.children[1].value",
        "met expects one of",
    ),
    (
        "email status",
        {"where": {"op": "has_email", "status": "meh"}},
        "where.status",
        "Input should be",
    ),
    (
        "extra key",
        {"where": {"op": "eq", "field": "met", "value": "met", "extra": 1}},
        "where.extra",
        "Extra inputs",
    ),
    (
        "last_contacted none",
        {"where": {"op": "last_contacted"}},
        "where",
        "exactly one of within_days, older_than_days, never",
    ),
    (
        "last_contacted two",
        {"where": {"op": "last_contacted", "within_days": 3, "never": True}},
        "where",
        "exactly one",
    ),
    (
        "negative days",
        {"where": {"op": "last_contacted", "within_days": -1}},
        "where.within_days",
        "greater than or equal to 0",
    ),
    (
        "days as string",
        {"where": {"op": "connected_within_days", "days": "7"}},
        "where.days",
        "valid integer",
    ),
    (
        "between reversed",
        {"where": {"op": "between", "field": "degree", "low": 3, "high": 1}},
        "where",
        "high is below low",
    ),
    (
        "between dates reversed",
        {
            "where": {
                "op": "between",
                "field": "connected_on",
                "low": "2021-01-01",
                "high": "2020-01-01",
            }
        },
        "where",
        "high is below low",
    ),
    (
        "between high bad",
        {"where": {"op": "between", "field": "connected_on", "low": "2020-01-01", "high": "x"}},
        "where.high",
        "ISO date",
    ),
    (
        "empty contains",
        {"where": {"op": "contains", "field": "headline", "value": ""}},
        "where.value",
        "at least 1 character",
    ),
    ("empty names", {"where": {"op": "tag_any", "names": []}}, "where.names", "at least 1 item"),
    (
        "list id type",
        {"where": {"op": "list_member", "list_id": "3"}},
        "where.list_id",
        "valid integer",
    ),
    (
        "include_archived type",
        {"where": {"op": "has_email"}, "include_archived": "yes"},
        "include_archived",
        "valid boolean",
    ),
    ("root extra", {"bogus": 1}, "bogus", "Extra inputs"),
    ("root not an object", [], "", "valid dictionary"),
]


@pytest.mark.parametrize(
    ("data", "path", "message"), [i[1:] for i in INVALID], ids=[i[0] for i in INVALID]
)
def test_invalid_trees_raise_filter_error_with_a_path(data: Any, path: str, message: str) -> None:
    with pytest.raises(FilterError) as info:
        parse_filter(data)
    (issue,) = info.value.issues
    assert issue.path == path
    assert message in issue.message
    assert str(info.value) == (f"{path}: {issue.message}" if path else issue.message)
    assert isinstance(info.value, ValueError)


def test_every_problem_is_reported() -> None:
    data = {
        "where": {
            "op": "and",
            "children": [
                {"op": "eq", "field": "met", "value": "friend"},
                {"op": "has_email"},
                {"op": "eq", "field": "degree", "value": "x"},
            ],
        }
    }
    with pytest.raises(FilterError) as info:
        parse_filter(data)
    assert [issue.path for issue in info.value.issues] == [
        "where.children[0].value",
        "where.children[2].value",
    ]


def test_parse_sort_validates_each_key() -> None:
    assert parse_sort([]) == []
    assert parse_sort([{"field": "last_name"}]) == [SortKey(field="last_name", direction="asc")]
    with pytest.raises(FilterError) as info:
        parse_sort([{"field": "last_name"}, {"field": "nope"}])
    assert [issue.path for issue in info.value.issues] == ["[1].field"]
    with pytest.raises(FilterError) as info:
        parse_sort([{"field": "met", "direction": "up"}])
    assert [issue.path for issue in info.value.issues] == ["[0].direction"]
    with pytest.raises(FilterError):
        parse_sort({"field": "met"})


# --- scoping ----------------------------------------------------------------

BROAD: dict[str, Any] = {
    "op": "or",
    "children": [
        {"op": "has_email"},
        {"op": "has_phone"},
        {"op": "has_position"},
        {"op": "email_contains", "value": "example"},
        {"op": "changed_jobs_within_days", "days": 30},
        {"op": "eq", "field": "met", "value": "met"},
    ],
}


def test_a_compiled_filter_never_returns_another_users_contact(
    session: Session, user: User, other: User
) -> None:
    mine = [
        factories.make_contact(session, user, emails=["a@example.test"]),
        factories.make_contact(session, user, phones=["+15550100"]),
        factories.make_contact(session, user, met=ContactMet.MET),
    ]
    theirs = [
        factories.make_contact(session, other, emails=["b@example.test"]),
        factories.make_contact(session, other, positions=[{"title": "CEO"}]),
        factories.make_contact(session, other, met=ContactMet.MET),
    ]
    theirs[0].snapshots.append(
        ContactSnapshot(user_id=other.id, headline="then", observed_at=NOW - timedelta(days=1))
    )
    session.flush()
    nobody = factories.make_user(session)
    assert matching(session, user, BROAD) == [c.id for c in mine]
    assert matching(session, other, BROAD) == [c.id for c in theirs]
    assert matching(session, nobody, BROAD) == []


def test_compile_update_applies_a_bulk_action_to_the_filter(
    session: Session, user: User, other: User
) -> None:
    mine = factories.make_contact(session, user, emails=["a@example.test"])
    mine_without = factories.make_contact(session, user)
    theirs = factories.make_contact(session, other, emails=["b@example.test"])
    tree = parse_filter({"where": {"op": "has_email"}})
    statement = compile_update(user, tree, now=NOW)
    assert statement.get_execution_options()["synchronize_session"] is False
    session.execute(statement.values(met=ContactMet.MET))
    session.expire_all()
    assert mine.met is ContactMet.MET
    assert mine_without.met is ContactMet.UNKNOWN
    assert theirs.met is ContactMet.UNKNOWN


def test_compiled_statements_are_scoped_and_every_subquery_names_the_user(user: User) -> None:
    tree = parse_filter({"where": BROAD})
    for statement in (compile_filter(user, tree, now=NOW), compile_count(user, tree, now=NOW)):
        assert statement.get_execution_options()[SCOPE_OPTION] == user.id
        sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
        for table in ("contact_emails", "contact_phones", "contact_positions", "contact_snapshots"):
            assert f"{table}.user_id = {user.id}" in sql, table
        assert f"contacts.user_id = {user.id}" in sql


# --- sort and page ----------------------------------------------------------


def test_sort_is_case_insensitive_with_nulls_last_and_id_as_tiebreak(
    session: Session, user: User
) -> None:
    adams = factories.make_contact(session, user, last_name="adams", current_company="Same")
    brown = factories.make_contact(session, user, last_name="Brown", current_company="Same")
    carter = factories.make_contact(session, user, last_name="carter", current_company=None)
    base = compile_filter(user, parse_filter({}), now=NOW)

    def order(sort: list[dict[str, Any]]) -> list[int]:
        return [c.id for c in session.scalars(apply_sort(base, parse_sort(sort)))]

    assert order([{"field": "last_name"}]) == [adams.id, brown.id, carter.id]
    assert order([{"field": "last_name", "direction": "desc"}]) == [carter.id, brown.id, adams.id]
    # Ties on company fall back to id ascending, in either direction; NULL company is last.
    assert order([{"field": "current_company"}]) == [adams.id, brown.id, carter.id]
    assert order([{"field": "current_company", "direction": "desc"}]) == [
        adams.id,
        brown.id,
        carter.id,
    ]
    assert order([]) == [adams.id, brown.id, carter.id]


def test_sort_puts_empty_datetimes_last_in_both_directions(session: Session, user: User) -> None:
    older = factories.make_contact(session, user, last_contacted_at=NOW - timedelta(days=9))
    newer = factories.make_contact(session, user, last_contacted_at=NOW - timedelta(days=1))
    never = factories.make_contact(session, user, last_contacted_at=None)
    base = compile_filter(user, parse_filter({}), now=NOW)
    asc = apply_sort(base, [SortKey(field="last_contacted_at")])
    desc = apply_sort(base, [SortKey(field="last_contacted_at", direction="desc")])
    assert [c.id for c in session.scalars(asc)] == [older.id, newer.id, never.id]
    assert [c.id for c in session.scalars(desc)] == [newer.id, older.id, never.id]


def test_apply_sort_replaces_an_earlier_ordering_and_keys_stack(
    session: Session, user: User
) -> None:
    a = factories.make_contact(session, user, last_name="Lee", degree=2)
    b = factories.make_contact(session, user, last_name="Lee", degree=1)
    c = factories.make_contact(session, user, last_name="Kim", degree=3)
    base = scoped(user, Contact).order_by(Contact.id.desc())
    sort = [SortKey(field="last_name"), SortKey(field="degree", direction="desc")]
    assert [x.id for x in session.scalars(apply_sort(base, sort))] == [c.id, a.id, b.id]


def test_paginate_slices_after_the_sort(session: Session, user: User) -> None:
    ids = [factories.make_contact(session, user).id for _ in range(5)]
    base = apply_sort(compile_filter(user, parse_filter({}), now=NOW), [])
    assert [c.id for c in session.scalars(paginate(base, limit=2, offset=2))] == ids[2:4]
    assert [c.id for c in session.scalars(paginate(base, limit=10))] == ids
    assert [c.id for c in session.scalars(paginate(base, limit=2, offset=10))] == []
    with pytest.raises(ValueError, match="limit"):
        paginate(base, limit=0)
    with pytest.raises(ValueError, match="offset"):
        paginate(base, limit=1, offset=-1)


# --- describe, round trip, schema -------------------------------------------


@pytest.mark.parametrize(("tree", "expected"), EXAMPLES, ids=[e[1][:40] for e in EXAMPLES])
def test_describe(tree: dict[str, Any], expected: str) -> None:
    assert describe(parse_filter(tree)) == expected


def test_describe_of_the_remaining_phrasings() -> None:
    def one(node: dict[str, Any]) -> str:
        return describe(parse_filter({"where": node}))

    assert one({"op": "eq", "field": "met", "value": "not_met"}) == "not met"
    assert one({"op": "eq", "field": "met", "value": "skip"}) == "skipped"
    assert one({"op": "eq", "field": "do_not_contact", "value": True}) == "do not contact"
    assert one({"op": "neq", "field": "do_not_contact", "value": False}) == "do not contact"
    assert one({"op": "eq", "field": "source", "value": "sync"}) == "from sync"
    assert one({"op": "neq", "field": "degree", "value": 1}) == "degree is not 1"
    assert one({"op": "gt", "field": "degree", "value": 1}) == "degree more than 1"
    assert one({"op": "lt", "field": "degree", "value": 3}) == "degree less than 3"
    assert one({"op": "gte", "field": "connected_on", "value": "2020-01-01"}) == (
        "connected on since 2020-01-01"
    )
    assert one({"op": "lte", "field": "triaged_at", "value": "2026-01-01T00:00:00Z"}) == (
        "triaged up to 2026-01-01T00:00:00Z"
    )
    assert one({"op": "eq", "field": "li_public_id", "value": "bob-smith"}) == (
        'LinkedIn id is "bob-smith"'
    )
    assert one({"op": "not", "child": {"op": "has_phone"}}) == "not has phone"


@pytest.mark.parametrize("tree", [e[0] for e in EXAMPLES], ids=[e[1][:40] for e in EXAMPLES])
def test_every_example_round_trips_through_json(tree: dict[str, Any]) -> None:
    parsed = parse_filter(tree)
    dumped = parsed.model_dump(mode="json")
    assert parse_filter(dumped) == parsed
    assert parse_filter(dumped).model_dump(mode="json") == dumped
    assert FilterTree.model_validate_json(parsed.model_dump_json()) == parsed
    assert FilterTree.model_validate(parsed.model_dump()) == parsed
    json.dumps(dumped)  # nothing in the dump needs a custom encoder


@pytest.mark.parametrize("tree", [e[0] for e in EXAMPLES], ids=[e[1][:40] for e in EXAMPLES])
def test_every_example_compiles_or_is_a_placeholder(
    session: Session, user: User, tree: dict[str, Any]
) -> None:
    parsed = parse_filter(tree)
    try:
        session.scalars(compile_filter(user, parsed, now=NOW)).all()
    except UnsupportedPredicate as exc:
        assert exc.op in PLACEHOLDERS


def test_schema_snapshot_is_current() -> None:
    schema = FilterTree.model_json_schema()
    assert set(schema["x-netkeeper-fields"]) == set(FIELDS)
    assert schema["x-netkeeper-fields"]["met"]["values"] == [m.value for m in ContactMet]
    assert schema["x-netkeeper-fields"]["source"]["values"] == [m.value for m in ContactSource]
    assert set(schema["$defs"]["FilterNode"]["discriminator"]["mapping"]) == set(OPS)
    current = json.dumps(schema, indent=2) + "\n"
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(current, encoding="utf-8")
    committed = SNAPSHOT.read_text(encoding="utf-8") if SNAPSHOT.exists() else ""
    assert committed == current, (
        "tests/snapshots/filter_schema.json does not match FilterTree.model_json_schema(). "
        "The schema is the frontend builder's contract (P1-15). If the change is intended, run "
        "`UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest tests/test_filters.py` and commit the file."
    )
