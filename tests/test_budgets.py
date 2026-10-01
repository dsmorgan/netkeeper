"""netkeeper.services.budgets: per-day/per-week action counters (spec 9.6, 9.10).

The three things #101 calls out as easy to get wrong:

* the local-day boundary (``test_*local_midnight*`` / ``test_*utc_midnight*``
  below) -- counters are keyed by the account owner's local day and week even
  though datetimes are stored naive UTC and returned aware;
* enforcement between units of work only, so a run can end at most one unit
  over its limit, never two (``test_overshoot_is_capped_at_one_unit`` and
  friends);
* the hard-max ceiling from spec 9.6's table clamping a user-configured
  default, never the other way around.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import BudgetSettings
from netkeeper.db import session_scope
from netkeeper.models import User
from netkeeper.services.budgets import (
    HARD_MAX_PER_DAY,
    HARD_MAX_PER_WEEK,
    ActionClass,
    BudgetExceeded,
    LocalPeriod,
    PeriodBudget,
    configured_default,
    consume,
    profile_visit_risk_warning,
    profile_visit_week_note,
    status,
)

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
ACCOUNT = 1
DEFAULT_SETTINGS = BudgetSettings()


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer)


def _settings(**overrides: Any) -> BudgetSettings:
    return replace(DEFAULT_SETTINGS, **overrides)


# --- PeriodBudget (pure, no database) -----------------------------------------


def test_period_budget_remaining_under_the_limit() -> None:
    assert PeriodBudget(count=3, limit=10).remaining == 7


def test_period_budget_remaining_at_the_limit_is_zero() -> None:
    assert PeriodBudget(count=10, limit=10).remaining == 0


def test_period_budget_remaining_never_goes_negative_once_over() -> None:
    assert PeriodBudget(count=11, limit=10).remaining == 0
    assert PeriodBudget(count=11, limit=10).over


# --- the local-day and local-week boundary ------------------------------------


def test_local_period_uses_the_users_timezone_not_utc() -> None:
    # 09:00 UTC is 23:00 the same day at UTC+14, and 01:00 the *next* day at UTC+14
    # two hours later -- the pair below straddles LOCAL midnight while UTC's date
    # (the 20th) never changes.
    ahead = User(timezone="Pacific/Kiritimati")
    early = LocalPeriod.at(ahead, datetime(2026, 9, 20, 9, 0, tzinfo=UTC))
    late = LocalPeriod.at(ahead, datetime(2026, 9, 20, 11, 0, tzinfo=UTC))
    assert early.day.isoformat() == "2026-09-20"
    assert late.day.isoformat() == "2026-09-21"


def test_a_run_straddling_local_midnight_resets_the_day_counter(writer: Session) -> None:
    """Same UTC calendar day throughout; the local day rolls over partway through."""
    ahead = factories.make_user(writer, timezone="Pacific/Kiritimati")
    before_local_midnight = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)  # local 2026-09-20 23:00
    after_local_midnight = datetime(2026, 9, 20, 11, 0, tzinfo=UTC)  # local 2026-09-21 01:00
    assert before_local_midnight.date() == after_local_midnight.date()  # same UTC day throughout

    consume(
        writer,
        ahead,
        ACCOUNT,
        ActionClass.CONNECTION_PAGES,
        now=before_local_midnight,
        settings=DEFAULT_SETTINGS,
    )
    consume(
        writer,
        ahead,
        ACCOUNT,
        ActionClass.CONNECTION_PAGES,
        now=before_local_midnight,
        settings=DEFAULT_SETTINGS,
    )
    after = consume(
        writer,
        ahead,
        ACCOUNT,
        ActionClass.CONNECTION_PAGES,
        now=after_local_midnight,
        settings=DEFAULT_SETTINGS,
    )

    # A fresh local day: 1, not 3. Keying on the UTC day (both calls share one)
    # would instead carry the first two calls' count forward and give 3.
    assert after.day.count == 1


def test_a_run_straddling_utc_midnight_but_not_local_midnight_keeps_one_counter(
    writer: Session,
) -> None:
    """UTC's calendar date changes; the account owner's local day does not."""
    behind = factories.make_user(writer, timezone="Pacific/Honolulu")  # UTC-10, no DST
    before_utc_midnight = datetime(2026, 9, 20, 23, 0, tzinfo=UTC)  # local 2026-09-20 13:00
    after_utc_midnight = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)  # local 2026-09-20 15:00
    assert before_utc_midnight.date() != after_utc_midnight.date()  # UTC day did change

    consume(
        writer,
        behind,
        ACCOUNT,
        ActionClass.CONNECTION_PAGES,
        now=before_utc_midnight,
        settings=DEFAULT_SETTINGS,
    )
    after = consume(
        writer,
        behind,
        ACCOUNT,
        ActionClass.CONNECTION_PAGES,
        now=after_utc_midnight,
        settings=DEFAULT_SETTINGS,
    )

    # Still the same local day: the second call adds to the first call's count (2),
    # not a fresh counter (which would read 1).
    assert after.day.count == 2


def test_local_midnight_and_utc_midnight_straddles_behave_differently(writer: Session) -> None:
    """The two tests above are not the same test in disguise: run each zone
    through the window built for it and confirm the two outcomes differ."""
    action = ActionClass.CONNECTION_PAGES

    # Same UTC calendar day throughout; local day rolls over partway through.
    ahead = factories.make_user(writer, timezone="Pacific/Kiritimati")
    consume(
        writer,
        ahead,
        ACCOUNT,
        action,
        now=datetime(2026, 9, 20, 9, 0, tzinfo=UTC),
        settings=DEFAULT_SETTINGS,
    )
    ahead_after = consume(
        writer,
        ahead,
        ACCOUNT,
        action,
        now=datetime(2026, 9, 20, 11, 0, tzinfo=UTC),
        settings=DEFAULT_SETTINGS,
    )

    # UTC's calendar date changes; the account owner's local day does not.
    behind = factories.make_user(writer, timezone="Pacific/Honolulu")
    consume(
        writer,
        behind,
        ACCOUNT,
        action,
        now=datetime(2026, 9, 20, 23, 0, tzinfo=UTC),
        settings=DEFAULT_SETTINGS,
    )
    behind_after = consume(
        writer,
        behind,
        ACCOUNT,
        action,
        now=datetime(2026, 9, 21, 1, 0, tzinfo=UTC),
        settings=DEFAULT_SETTINGS,
    )

    assert ahead_after.day.count == 1  # local day rolled over between the two calls
    assert behind_after.day.count == 2  # local day did not
    assert ahead_after.day.count != behind_after.day.count


def test_an_unknown_timezone_falls_back_to_utc_with_a_warning(
    writer: Session, caplog: pytest.LogCaptureFixture
) -> None:
    user = factories.make_user(writer, timezone="Mars/Olympus")
    with caplog.at_level("WARNING", logger="netkeeper.services.budgets"):
        result = status(
            writer, user, ACCOUNT, ActionClass.CONNECTION_PAGES, now=NOW, settings=DEFAULT_SETTINGS
        )
    assert result.day.count == 0
    assert "Mars/Olympus" in caplog.text


def test_now_must_be_aware(writer: Session, user: User) -> None:
    naive = datetime(2026, 9, 20, 12, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        consume(
            writer,
            user,
            ACCOUNT,
            ActionClass.CONNECTION_PAGES,
            now=naive,
            settings=DEFAULT_SETTINGS,
        )


# --- enforcement is between units of work: overshoot by one unit is the maximum ------


def test_overshoot_is_capped_at_one_unit(writer: Session, user: User) -> None:
    settings = _settings(connection_pages_per_day=2)
    action = ActionClass.CONNECTION_PAGES

    consume(writer, user, ACCOUNT, action, now=NOW, settings=settings)  # count -> 1
    at_limit = consume(
        writer, user, ACCOUNT, action, now=NOW, settings=settings
    )  # count -> 2 (== limit)
    assert at_limit.day.count == 2
    assert not at_limit.day.over

    # The unit that finds the count sitting exactly at the limit is still let
    # through -- its own increment is what pushes the count one over.
    one_over = consume(writer, user, ACCOUNT, action, now=NOW, settings=settings)
    assert one_over.day.count == 3
    assert one_over.day.over

    # The very next unit sees a count already past the limit and is refused --
    # "the second unit is refused".
    with pytest.raises(BudgetExceeded) as excinfo:
        consume(writer, user, ACCOUNT, action, now=NOW, settings=settings)
    assert excinfo.value.action is action
    assert excinfo.value.period == "day"
    assert excinfo.value.count == 3
    assert excinfo.value.limit == 2

    # The count never reaches two over; the refused call did not record itself.
    final = status(writer, user, ACCOUNT, action, now=NOW, settings=settings)
    assert final.day.count == 3


def test_consume_under_the_limit_never_overshoots(writer: Session, user: User) -> None:
    settings = _settings(connection_pages_per_day=5)
    result = consume(
        writer, user, ACCOUNT, ActionClass.CONNECTION_PAGES, now=NOW, settings=settings
    )
    assert result.day.count == 1
    assert not result.day.over


def test_week_budget_is_also_capped_at_one_unit_over_and_checked_independently_of_day(
    writer: Session, user: User
) -> None:
    settings = _settings(profile_visits_per_day=100, profile_visits_per_week=2)
    action = ActionClass.PROFILE_VISITS

    consume(writer, user, ACCOUNT, action, now=NOW, settings=settings)  # week -> 1
    consume(writer, user, ACCOUNT, action, now=NOW, settings=settings)  # week -> 2 (== limit)
    over = consume(
        writer, user, ACCOUNT, action, now=NOW, settings=settings
    )  # week -> 3 (one over)
    assert over.week is not None
    assert over.week.count == 3
    assert over.day.count == 3  # day limit (100) is nowhere close; day never refuses here

    with pytest.raises(BudgetExceeded) as excinfo:
        consume(writer, user, ACCOUNT, action, now=NOW, settings=settings)
    assert excinfo.value.period == "week"

    final = status(writer, user, ACCOUNT, action, now=NOW, settings=settings)
    assert final.week is not None
    assert final.week.count == 3


# --- hard max clamps the configured default -----------------------------------


def test_hard_max_per_day_matches_spec_9_6() -> None:
    """Pinned against the literal numbers in spec 9.6's table, not against the
    module's own constant: a self-referential ``== HARD_MAX_PER_DAY[...]``
    assertion would pass no matter what the dict said, including a bad merge
    that raised ``profile_visits`` to 999. Profile visits went from 100 to 250
    in #318, by the maintainer's decision."""
    assert HARD_MAX_PER_DAY == {
        ActionClass.CONNECTION_PAGES: 400,
        ActionClass.PROFILE_VISITS: 250,
        ActionClass.INBOX_POLLS: 24,
        ActionClass.LI_MESSAGES_AUTO: 30,
    }


def test_hard_max_per_week_matches_spec_9_6() -> None:
    assert HARD_MAX_PER_WEEK == {ActionClass.PROFILE_VISITS: 1250}


def test_a_configured_default_above_the_hard_max_is_clamped(writer: Session, user: User) -> None:
    settings = _settings(connection_pages_per_day=10_000)
    result = status(writer, user, ACCOUNT, ActionClass.CONNECTION_PAGES, now=NOW, settings=settings)
    assert result.day.limit == 400
    assert result.day.limit < 10_000


def test_a_configured_default_under_the_hard_max_is_left_alone(writer: Session, user: User) -> None:
    settings = _settings(connection_pages_per_day=7)
    result = status(writer, user, ACCOUNT, ActionClass.CONNECTION_PAGES, now=NOW, settings=settings)
    assert result.day.limit == 7


def test_a_configured_weekly_default_above_the_hard_max_is_clamped(
    writer: Session, user: User
) -> None:
    """The week limit's own clamp, mirroring the day one above: dropping
    ``min(..., week_hard)`` from ``_limits_for`` leaves every other test green
    (nothing else pushes a configured weekly default past 1,250), so it needs
    its own case."""
    settings = _settings(profile_visits_per_week=10_000)
    result = status(writer, user, ACCOUNT, ActionClass.PROFILE_VISITS, now=NOW, settings=settings)
    assert result.week is not None
    assert result.week.limit == 1250
    assert result.week.limit < 10_000


# --- #318: profile visits up to 250 a day, the week following the day ---------


@pytest.mark.parametrize(("asked", "enforced"), [(250, 250), (251, 250), (10_000, 250)])
def test_profile_visits_a_day_clamp_at_250(
    writer: Session, user: User, asked: int, enforced: int
) -> None:
    settings = _settings(profile_visits_per_day=asked)
    result = status(writer, user, ACCOUNT, ActionClass.PROFILE_VISITS, now=NOW, settings=settings)
    assert result.day.limit == enforced


@pytest.mark.parametrize(("day", "week"), [(60, 300), (100, 500), (250, 1250), (10_000, 1250)])
def test_an_unset_weekly_limit_is_five_times_the_daily_one(
    writer: Session, user: User, day: int, week: int
) -> None:
    """60 gives 300 and 100 gives 500, today's numbers; 250 gives the weekly
    hard max. A daily value past its ceiling derives from the clamped 250, so
    it does not also read as a weekly value past 1,250."""
    settings = _settings(profile_visits_per_day=day, profile_visits_per_week=None)
    result = status(writer, user, ACCOUNT, ActionClass.PROFILE_VISITS, now=NOW, settings=settings)
    assert result.week is not None
    assert result.week.limit == week
    assert configured_default(ActionClass.PROFILE_VISITS, settings, "week") == week


def test_the_default_weekly_limit_is_derived_from_the_default_daily_one() -> None:
    assert DEFAULT_SETTINGS.profile_visits_per_week is None
    assert configured_default(ActionClass.PROFILE_VISITS, DEFAULT_SETTINGS, "week") == 300


@pytest.mark.parametrize(("asked", "enforced"), [(42, 42), (1250, 1250), (1251, 1250)])
def test_an_explicit_weekly_limit_applies_clamped_to_1250(
    writer: Session, user: User, asked: int, enforced: int
) -> None:
    settings = _settings(profile_visits_per_day=250, profile_visits_per_week=asked)
    result = status(writer, user, ACCOUNT, ActionClass.PROFILE_VISITS, now=NOW, settings=settings)
    assert result.week is not None
    assert result.week.limit == enforced


def test_no_risk_warning_at_or_below_100_a_day() -> None:
    assert profile_visit_risk_warning(_settings(profile_visits_per_day=60)) is None
    assert profile_visit_risk_warning(_settings(profile_visits_per_day=100)) is None


def test_a_risk_warning_above_100_a_day_names_the_number_in_force() -> None:
    assert profile_visit_risk_warning(_settings(profile_visits_per_day=101)) == (
        "Profile visits are set to 101 a day, above the 100 a day netkeeper was designed"
        " around. More visits a day make it more likely that LinkedIn restricts your account"
        " or asks you to verify it. Heat still slows runs down after LinkedIn throttles a visit."
    )
    clamped = profile_visit_risk_warning(_settings(profile_visits_per_day=10_000))
    assert clamped is not None and clamped.startswith("Profile visits are set to 250 a day")


# --- week only applies where spec 9.6 says it does ----------------------------


def test_only_profile_visits_carries_a_week_budget(writer: Session, user: User) -> None:
    pages = status(
        writer, user, ACCOUNT, ActionClass.CONNECTION_PAGES, now=NOW, settings=DEFAULT_SETTINGS
    )
    visits = status(
        writer, user, ACCOUNT, ActionClass.PROFILE_VISITS, now=NOW, settings=DEFAULT_SETTINGS
    )
    assert pages.week is None
    assert visits.week is not None


def test_week_counter_is_keyed_by_iso_week_not_by_calendar_month(
    writer: Session, user: User
) -> None:
    settings = _settings(profile_visits_per_week=10)
    action = ActionClass.PROFILE_VISITS
    # 2026-09-28 (Mon) and 2026-10-02 (Fri) are the same ISO week (40), crossing
    # a month boundary; 2026-10-05 (Mon) is the next ISO week.
    same_week_a = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    same_week_b = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    next_week = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

    consume(writer, user, ACCOUNT, action, now=same_week_a, settings=settings)
    still_same_week = consume(writer, user, ACCOUNT, action, now=same_week_b, settings=settings)
    assert still_same_week.week is not None
    assert still_same_week.week.count == 2

    fresh_week = consume(writer, user, ACCOUNT, action, now=next_week, settings=settings)
    assert fresh_week.week is not None
    assert fresh_week.week.count == 1


def test_week_counter_is_keyed_by_iso_year_not_by_calendar_year(
    writer: Session, user: User
) -> None:
    """2026-12-31 and 2027-01-01 are both ISO week 2026-W53: the year the ISO
    week belongs to, not the calendar year of the date, has to be part of the
    key. Using ``local.year`` in place of the ``iso_year`` that
    ``isocalendar()`` returns would key these as ``2026-W53`` and
    ``2027-W53`` -- two different weeks -- and silently double the weekly
    limit across every New Year's Eve."""
    settings = _settings(profile_visits_per_week=10)
    action = ActionClass.PROFILE_VISITS
    new_years_eve = datetime(2026, 12, 31, 12, 0, tzinfo=UTC)
    new_years_day = datetime(2027, 1, 1, 12, 0, tzinfo=UTC)

    consume(writer, user, ACCOUNT, action, now=new_years_eve, settings=settings)
    after = consume(writer, user, ACCOUNT, action, now=new_years_day, settings=settings)

    assert after.week is not None
    assert after.week.count == 2


def test_week_counter_uses_the_local_iso_week_not_the_stored_utc_one(
    writer: Session, user: User
) -> None:
    """The day key and the week key must both cross the local/UTC seam through
    the same conversion. A ``Pacific/Kiritimati`` (UTC+14) account's Monday
    01:00 is UTC Sunday 11:00 the day before -- still the *previous* ISO week
    if ``isocalendar()`` is read off the stored UTC instant instead of the
    local one. The default fixture user's timezone is UTC (factories.py),
    where local and UTC never disagree, so this needs its own non-UTC user."""
    ahead = factories.make_user(writer, timezone="Pacific/Kiritimati")
    settings = _settings(profile_visits_per_week=10)
    action = ActionClass.PROFILE_VISITS
    # UTC 2026-09-20 11:00 is local 2026-09-21 01:00 (Monday, ISO week 39).
    monday_local = datetime(2026, 9, 20, 11, 0, tzinfo=UTC)
    # UTC 2026-09-22 11:00 is local 2026-09-23 01:00 (Wednesday, still week 39).
    wednesday_local = datetime(2026, 9, 22, 11, 0, tzinfo=UTC)

    first = consume(writer, ahead, ACCOUNT, action, now=monday_local, settings=settings)
    assert first.week is not None
    assert first.week.count == 1

    second = consume(writer, ahead, ACCOUNT, action, now=wednesday_local, settings=settings)
    assert second.week is not None
    # Both calls land in the account's local ISO week 39: one counter, count 2.
    # Keying off the raw UTC instant's own isocalendar() would put the first
    # call in week 38 instead, leaving this at 1.
    assert second.week.count == 2


# --- isolation -----------------------------------------------------------


def test_budgets_are_isolated_per_account(writer: Session, user: User) -> None:
    action = ActionClass.CONNECTION_PAGES
    consume(writer, user, 1, action, now=NOW, settings=DEFAULT_SETTINGS)
    consume(writer, user, 1, action, now=NOW, settings=DEFAULT_SETTINGS)
    one = status(writer, user, 1, action, now=NOW, settings=DEFAULT_SETTINGS)
    two = status(writer, user, 2, action, now=NOW, settings=DEFAULT_SETTINGS)
    assert one.day.count == 2
    assert two.day.count == 0


def test_budgets_are_isolated_per_action_class(writer: Session, user: User) -> None:
    consume(writer, user, ACCOUNT, ActionClass.CONNECTION_PAGES, now=NOW, settings=DEFAULT_SETTINGS)
    pages = status(
        writer, user, ACCOUNT, ActionClass.CONNECTION_PAGES, now=NOW, settings=DEFAULT_SETTINGS
    )
    visits = status(
        writer, user, ACCOUNT, ActionClass.PROFILE_VISITS, now=NOW, settings=DEFAULT_SETTINGS
    )
    assert pages.day.count == 1
    assert visits.day.count == 0


def test_budgets_are_isolated_per_user(writer: Session) -> None:
    owner = factories.make_user(writer)
    other = factories.make_user(writer)
    action = ActionClass.CONNECTION_PAGES
    consume(writer, owner, ACCOUNT, action, now=NOW, settings=DEFAULT_SETTINGS)
    assert status(writer, other, ACCOUNT, action, now=NOW, settings=DEFAULT_SETTINGS).day.count == 0


# --- writer session guard ------------------------------------------------


def test_consume_requires_a_writer_session(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    with session_scope(session_factory, write=True) as setup:
        user_id = factories.make_user(setup).id
    owner = session.get(User, user_id)
    assert owner is not None
    with pytest.raises(RuntimeError, match="writer session"):
        consume(
            session,
            owner,
            ACCOUNT,
            ActionClass.CONNECTION_PAGES,
            now=NOW,
            settings=DEFAULT_SETTINGS,
        )


def test_status_does_not_need_a_writer_session(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    with session_scope(session_factory, write=True) as setup:
        user_id = factories.make_user(setup).id
    owner = session.get(User, user_id)
    assert owner is not None
    # Must not raise: a read has no business demanding the write lock.
    result = status(
        session, owner, ACCOUNT, ActionClass.CONNECTION_PAGES, now=NOW, settings=DEFAULT_SETTINGS
    )
    assert result.day.count == 0


@pytest.mark.parametrize(
    ("day", "week", "expected"),
    [
        (60, None, None),  # derived: nothing to note
        (60, 300, None),  # exactly 5 x daily
        (100, 600, None),  # above 5 x daily
        (1000, 1250, None),  # day clamps to 250; 1,250 is 5 x that
        (1000, 2000, None),  # week clamps to 1,250, still 5 x the clamped day
        (
            1000,
            300,
            "The weekly limit (300) is below 5 times your daily limit (250); remove"
            " profile_visits_per_week from config.toml to use 5 times daily (1,250).",
        ),
        (
            101,
            500,
            "The weekly limit (500) is below 5 times your daily limit (101); remove"
            " profile_visits_per_week from config.toml to use 5 times daily (505).",
        ),
    ],
)
def test_an_explicit_week_below_five_times_the_daily_limit_gets_a_note(
    day: int, week: int | None, expected: str | None
) -> None:
    """#319 review S3: compared against the limits in force, after both clamps."""
    settings = _settings(profile_visits_per_day=day, profile_visits_per_week=week)
    assert profile_visit_week_note(settings) == expected
