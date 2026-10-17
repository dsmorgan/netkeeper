"""A time-zone change never opens extra budget (#597 review, blocker 1).

At 11:00 UTC three local dates exist: Etc/GMT+12 is on the day before, UTC on today,
Pacific/Kiritimati on the day after. Moving the user's zone between them used to give a
fresh daily counter per date (48 auto-sends against a limit of 15).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import BudgetSettings, Settings
from netkeeper.db import session_scope
from netkeeper.models import User
from netkeeper.services import budgets, ui_settings, zone_change
from netkeeper.services.budgets import ActionClass, BudgetExceeded
from netkeeper.services.linkedin_accounts import ensure_account

NOW = datetime(2026, 10, 14, 11, 0, tzinfo=UTC)  # a Wednesday: one ISO week everywhere
ZONES = ("Etc/GMT+12", "UTC", "Pacific/Kiritimati")
SETTINGS = BudgetSettings()


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    made = factories.make_user(writer, timezone="UTC")
    ensure_account(writer, made)
    return made


def _spend_all(session: Session, user: User, action: ActionClass, now: datetime) -> int:
    account = ensure_account(session, user).id
    spent = 0
    while True:
        try:
            budgets.consume(session, user, account, action, now=now, settings=SETTINGS)
        except BudgetExceeded:
            return spent
        spent += 1
        assert spent < 1000


def test_the_three_zones_are_on_three_dates_at_that_instant() -> None:
    dates = {NOW.astimezone(ZoneInfo(zone)).date() for zone in ZONES}
    assert len(dates) == 3


@pytest.mark.parametrize("action", list(ActionClass))
def test_flipping_across_three_zones_spends_no_more_than_one_day(
    writer: Session, user: User, action: ActionClass
) -> None:
    """Every budget kind: the whole spend across every flip stays within one day's
    limit, plus the one unit of overshoot consume always allows."""
    limit = budgets._limits_for(action, SETTINGS).day
    total = 0
    for zone in (*ZONES, *ZONES, "UTC"):
        zone_change.change_zone(writer, user, zone, now=NOW)
        total += _spend_all(writer, user, action, NOW)
    assert total <= limit + 1, f"{action}: {total} units spent against a limit of {limit}"


def test_the_week_counter_carries_too(writer: Session, user: User) -> None:
    """Profile visits are held back by the week too: the week key moves with the zone
    on a Sunday night, and the count goes with it."""
    sunday_late = datetime(2026, 10, 18, 13, 0, tzinfo=UTC)  # Monday already in Kiritimati
    account = ensure_account(writer, user).id
    for _ in range(5):
        budgets.consume(
            writer, user, account, ActionClass.PROFILE_VISITS, now=sunday_late, settings=SETTINGS
        )
    zone_change.change_zone(writer, user, "Pacific/Kiritimati", now=sunday_late)
    week = budgets.status(
        writer, user, account, ActionClass.PROFILE_VISITS, now=sunday_late, settings=SETTINGS
    ).week
    assert week is not None and week.count == 5


def test_a_carry_keeps_the_larger_count(writer: Session, user: User) -> None:
    account = ensure_account(writer, user).id
    zone_change.change_zone(writer, user, "Pacific/Kiritimati", now=NOW)
    for _ in range(3):
        budgets.consume(writer, user, account, ActionClass.LI_PREFILLS, now=NOW, settings=SETTINGS)
    zone_change.change_zone(writer, user, "UTC", now=NOW)
    budgets.consume(writer, user, account, ActionClass.LI_PREFILLS, now=NOW, settings=SETTINGS)
    zone_change.change_zone(writer, user, "Pacific/Kiritimati", now=NOW)
    day = budgets.status(
        writer, user, account, ActionClass.LI_PREFILLS, now=NOW, settings=SETTINGS
    ).day
    assert day.count == 4  # max(3, 4), not 3 and not 7


def test_the_email_caps_keep_counting_from_the_old_zones_day(writer: Session, user: User) -> None:
    """The campaign and mailbox caps count sends inside the local day's bounds. A move
    onto a zone whose day began an hour ago must not forget this morning's sends."""
    utc_start = datetime(2026, 10, 14, tzinfo=UTC)
    kiritimati_start = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)  # 00:00 on the 15th there
    assert zone_change.count_start(writer, user, utc_start, NOW) == utc_start
    zone_change.change_zone(writer, user, "Pacific/Kiritimati", now=NOW)
    assert zone_change.count_start(writer, user, kiritimati_start, NOW) == utc_start
    # Moving on keeps the earliest start for the rest of the day.
    zone_change.change_zone(writer, user, "Etc/GMT+12", now=NOW)
    gmt12_start = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    assert zone_change.count_start(writer, user, gmt12_start, NOW) == gmt12_start
    zone_change.change_zone(writer, user, "Pacific/Kiritimati", now=NOW)
    assert zone_change.count_start(writer, user, kiritimati_start, NOW) == gmt12_start
    # Once the new day is over, the floor no longer applies.
    later = NOW + timedelta(days=2)
    assert zone_change.count_start(writer, user, later, later) == later


def test_each_change_is_logged_and_recorded(
    writer: Session, user: User, caplog: pytest.LogCaptureFixture
) -> None:
    assert zone_change.change_zone(writer, user, "Europe/Berlin", now=NOW)
    assert not zone_change.change_zone(writer, user, "Europe/Berlin", now=NOW)
    assert "time zone UTC -> Europe/Berlin" in caplog.text
    assert zone_change.last_changed(writer, user) == NOW


def test_the_settings_page_changes_the_zone_once_an_hour(
    writer: Session, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [NOW]
    monkeypatch.setattr(ui_settings, "utcnow", lambda: clock[0])
    ui_settings.write(writer, user, Settings(), {"linkedin.timezone": "Europe/Berlin"})
    clock[0] = NOW + timedelta(minutes=59)
    with pytest.raises(ui_settings.SettingsRefused, match="once an hour"):
        ui_settings.write(writer, user, Settings(), {"linkedin.timezone": "Asia/Tokyo"})
    assert user.timezone == "Europe/Berlin"
    # Another setting saved meanwhile is not held up by the zone's limit.
    ui_settings.write(writer, user, Settings(), {"backup.keep": 5})
    clock[0] = NOW + timedelta(minutes=60)
    ui_settings.write(writer, user, Settings(), {"linkedin.timezone": "Asia/Tokyo"})
    assert user.timezone == "Asia/Tokyo"


def test_the_page_flipping_hourly_still_spends_at_most_a_day_and_one_more(
    writer: Session, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the page, one change an hour across the three dates: within the two
    hours that three dates exist (10:00 to 12:00 UTC, before any of the three zones
    reaches its own midnight), the spend stays within one day's limit plus one."""
    clock = [NOW - timedelta(hours=1)]
    monkeypatch.setattr(ui_settings, "utcnow", lambda: clock[0])
    total = _spend_all(writer, user, ActionClass.LI_MESSAGES_AUTO, clock[0])  # in UTC
    for zone in ("Pacific/Kiritimati", "Etc/GMT+12"):
        ui_settings.write(writer, user, Settings(), {"linkedin.timezone": zone})
        total += _spend_all(writer, user, ActionClass.LI_MESSAGES_AUTO, clock[0])
        clock[0] += timedelta(minutes=60)
    clock[0] = NOW + timedelta(minutes=59)  # 11:59, still the same three dates
    total += _spend_all(writer, user, ActionClass.LI_MESSAGES_AUTO, clock[0])
    assert total <= SETTINGS.li_messages_auto_per_day + 1


def test_moving_back_onto_a_full_day_never_lowers_its_count(writer: Session, user: User) -> None:
    """The max-merge's other side (#597 re-review): the new zone's key already holds the
    larger count. At 11:00 UTC Etc/GMT+12 is still on the 13th; its full key must stay
    full when a user on a fresh UTC day moves there, or that day could be spent twice."""
    account = ensure_account(writer, user).id
    gmt12 = ZoneInfo("Etc/GMT+12")
    zone_change.change_zone(writer, user, "Etc/GMT+12", now=NOW)
    limit = SETTINGS.li_messages_auto_per_day
    assert _spend_all(writer, user, ActionClass.LI_MESSAGES_AUTO, NOW) == limit + 1
    # Its day key is now the larger one; a day later in UTC, nothing is spent yet.
    yesterday = NOW.astimezone(gmt12).date()
    tomorrow = NOW + timedelta(days=1)
    zone_change.change_zone(writer, user, "UTC", now=tomorrow)
    budgets.consume(
        writer, user, account, ActionClass.LI_MESSAGES_AUTO, now=tomorrow, settings=SETTINGS
    )
    # Back onto the 13th's key at the original instant: the UTC day holds 1, the 13th 16.
    zone_change.change_zone(writer, user, "Etc/GMT+12", now=NOW)
    key = budgets._day_key(account, ActionClass.LI_MESSAGES_AUTO, yesterday)
    assert budgets._read_count(writer, user, key) == limit + 1
    assert _spend_all(writer, user, ActionClass.LI_MESSAGES_AUTO, NOW) == 0


def test_the_floor_keeps_the_earliest_start_across_two_moves(writer: Session, user: User) -> None:
    """UTC -> Kiritimati -> Etc/GMT+5 at 11:00 UTC (#597 re-review): Kiritimati's day
    began at 10:00 UTC and GMT+5's at 05:00 UTC, but sends made under UTC from 00:00
    still count for the rest of the day."""
    zone_change.change_zone(writer, user, "Pacific/Kiritimati", now=NOW)
    zone_change.change_zone(writer, user, "Etc/GMT+5", now=NOW)
    gmt5_start = datetime(2026, 10, 14, 5, 0, tzinfo=UTC)
    utc_start = datetime(2026, 10, 14, tzinfo=UTC)
    assert zone_change.count_start(writer, user, gmt5_start, NOW) == utc_start
