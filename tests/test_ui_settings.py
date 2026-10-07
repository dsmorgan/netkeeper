"""Settings-page values over config.toml (#343): hard maximums, precedence, warnings."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import factories
import pytest
from sqlalchemy.orm import Session

from netkeeper.config import Settings
from netkeeper.models import User
from netkeeper.services import ui_settings
from netkeeper.services.settings_kv import get_setting, set_setting

PREFILLS = "linkedin.budget.li_prefills_per_day"
VISITS = "linkedin.budget.profile_visits_per_day"
WEEK = "linkedin.budget.profile_visits_per_week"
WINDOW = "linkedin.active_hours"

# Written out, not read from the modules: each is a hard maximum from spec 9.6 / 11.4.
HARD_MAXIMUMS: dict[str, float] = {
    "linkedin.budget.connection_pages_per_day": 400,
    VISITS: 250,
    WEEK: 1250,
    "linkedin.budget.inbox_polls_per_day": 24,
    PREFILLS: 50,
    "linkedin.budget.li_messages_auto_per_day": 50,
    "linkedin.weekend_multiplier": 1.0,
    "campaigns.mailbox_daily_cap": 400,
}


def _pinned(**keys: Any) -> Settings:
    """Settings as if config.toml at /etc/nk.toml set ``keys`` (dotted, ``__`` for ``.``)."""
    settings = Settings()
    names = set()
    for name, value in keys.items():
        key = name.replace("__", ".")
        settings = ui_settings._with_value(settings, key, value)
        names.add(key)
    return replace(settings, source_path=Path("/etc/nk.toml"), file_keys=frozenset(names))


def _view(views: list[ui_settings.FieldView], key: str) -> ui_settings.FieldView:
    return next(view for view in views if view.spec.key == key)


@pytest.fixture
def user(session: Session) -> User:
    return factories.make_user(session)


# --- hard maximums ----------------------------------------------------------------


def test_the_hard_maximums_are_pinned() -> None:
    for key, maximum in HARD_MAXIMUMS.items():
        assert ui_settings.BY_KEY[key].maximum == maximum, key


def test_the_page_minimums_are_pinned() -> None:
    """Spec 11.4's 90-second floor for both spacing values; a guard of at least a day."""
    minimums = {
        key: ui_settings.BY_KEY[key].minimum
        for key in (
            "campaigns.send_spacing_floor_s",
            "campaigns.send_spacing_median_s",
            "campaigns.contacted_within_days_guard",
        )
    }
    assert minimums == {
        "campaigns.send_spacing_floor_s": 90,
        "campaigns.send_spacing_median_s": 90,
        "campaigns.contacted_within_days_guard": 1,
    }
    assert ui_settings.GUARD_WARN_BELOW_DAYS == 30


@pytest.mark.parametrize(("key", "maximum"), HARD_MAXIMUMS.items())
def test_a_value_at_the_hard_max_is_kept_and_one_above_it_refused(key: str, maximum: float) -> None:
    spec = ui_settings.BY_KEY[key]
    at = maximum if spec.kind == "float" else int(maximum)
    above = maximum + 0.01 if spec.kind == "float" else int(maximum) + 1
    assert ui_settings.parse(spec, at) == at
    with pytest.raises(ValueError, match="hard maximum"):
        ui_settings.parse(spec, above)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        (PREFILLS, 0),
        (PREFILLS, True),
        (PREFILLS, "15"),
        (PREFILLS, 15.5),
        ("linkedin.weekend_multiplier", -0.1),
        ("linkedin.weekend_multiplier", float("nan")),
        ("campaigns.send_spacing_floor_s", 0),
        ("campaigns.reply_poll_minutes", 0),
        ("backup.keep", 0),
        ("llm.enabled", 1),
        (WINDOW, ["09:00", "09:00"]),  # all 24 hours
        (WINDOW, ["05:00", "21:30"]),  # 16.5 hours, past the 16 that is still a window
        (WINDOW, ["9:00", "17:00"]),
        (WINDOW, ["09:00"]),
        ("campaigns.holidays", ["2026-13-01"]),
        ("campaigns.holidays", ["2026-1-01"]),
    ],
)
def test_values_that_cannot_be_used_are_refused(key: str, value: object) -> None:
    with pytest.raises(ValueError):
        ui_settings.parse(ui_settings.BY_KEY[key], value)


def test_a_sixteen_hour_window_and_an_overnight_one_are_kept() -> None:
    spec = ui_settings.BY_KEY[WINDOW]
    assert ui_settings.parse(spec, ["05:30", "21:30"]) == ("05:30", "21:30")
    assert ui_settings.parse(spec, ["22:00", "06:00"]) == ("22:00", "06:00")


def test_holidays_are_sorted_and_deduplicated() -> None:
    spec = ui_settings.BY_KEY["campaigns.holidays"]
    assert ui_settings.parse(spec, ["2026-12-25", "2026-01-01", "2026-12-25"]) == (
        "2026-01-01",
        "2026-12-25",
    )


# --- precedence -------------------------------------------------------------------


def test_the_page_wins_over_the_default_and_the_file_over_the_page() -> None:
    values = {PREFILLS: 30, VISITS: 80}
    settings = ui_settings.apply(_pinned(linkedin__budget__profile_visits_per_day=40), values)
    assert settings.linkedin.budget.li_prefills_per_day == 30  # page over default
    assert settings.linkedin.budget.profile_visits_per_day == 40  # file over page
    assert settings.linkedin.budget.inbox_polls_per_day == 8  # the default
    assert settings.ui_keys == {PREFILLS}


def test_describe_says_where_each_value_comes_from() -> None:
    base = _pinned(linkedin__budget__profile_visits_per_day=40)
    views = ui_settings.describe(base, {PREFILLS: 30, VISITS: 80})
    prefills, visits, polls = (
        _view(views, key) for key in (PREFILLS, VISITS, "linkedin.budget.inbox_polls_per_day")
    )
    assert (prefills.source, prefills.value, prefills.ui_value, prefills.editable) == (
        "ui",
        30,
        30,
        True,
    )
    assert (visits.source, visits.value, visits.file_value, visits.ui_value) == (
        "file",
        40,
        40,
        80,
    )
    assert visits.editable is False
    assert visits.locked_reason is not None and "/etc/nk.toml" in visits.locked_reason
    assert (polls.source, polls.value, polls.default) == ("default", 8, 8)


def test_settings_built_in_code_pin_nothing() -> None:
    """No file_keys (the defaults, or a test's Settings): the page applies everywhere."""
    settings = ui_settings.apply(replace(Settings(), source_path=Path("x.toml")), {PREFILLS: 9})
    assert settings.linkedin.budget.li_prefills_per_day == 9


def test_resolving_again_starts_from_the_file() -> None:
    once = ui_settings.apply(Settings(), {PREFILLS: 30})
    again = ui_settings.apply(once, {})
    assert again.linkedin.budget.li_prefills_per_day == 15
    assert again.ui_keys == frozenset()


def test_a_stored_value_that_fails_validation_is_ignored() -> None:
    settings = ui_settings.apply(Settings(), {PREFILLS: 500, VISITS: "lots"})
    assert settings.linkedin.budget.li_prefills_per_day == 15
    assert settings.linkedin.budget.profile_visits_per_day == 60
    view = _view(ui_settings.describe(Settings(), {PREFILLS: 500}), PREFILLS)
    assert (view.stored_unreadable, view.source, view.ui_value) == (True, "default", None)


def test_auto_send_is_never_taken_from_the_page() -> None:
    key = "campaigns.linkedin_auto_send"
    assert ui_settings.apply(Settings(), {key: True}).campaigns.linkedin_auto_send is False
    view = _view(ui_settings.describe(Settings(), {key: True}), key)
    assert view.editable is False and view.value is False


# --- writing ----------------------------------------------------------------------


def test_write_stores_and_null_resets(session: Session, user: User) -> None:
    ui_settings.write(session, user, Settings(), {PREFILLS: 25, WEEK: 400})
    assert get_setting(session, user, "config." + PREFILLS) == 25
    assert ui_settings.resolve(session, user, Settings()).linkedin.budget.li_prefills_per_day == 25
    ui_settings.write(session, user, Settings(), {WEEK: None})
    resolved = ui_settings.resolve(session, user, Settings())
    assert resolved.linkedin.budget.profile_visits_per_week is None  # automatic again
    assert resolved.linkedin.budget.li_prefills_per_day == 25


@pytest.mark.parametrize(
    "changes",
    [
        {PREFILLS: 51},
        {"campaigns.linkedin_auto_send": True},
        {"web.port": 9000},
        {"linkedin.budget.profile_visits_per_day": 40},  # the file sets it
    ],
)
def test_write_refuses_and_stores_nothing(
    session: Session, user: User, changes: dict[str, Any]
) -> None:
    base = _pinned(linkedin__budget__profile_visits_per_day=40)
    with pytest.raises(ui_settings.SettingsRefused) as refused:
        ui_settings.write(session, user, base, {"backup.keep": 5, **changes})
    assert set(refused.value.problems) == set(changes)
    assert ui_settings.stored(session, user) == {}


def test_values_are_per_user(session: Session, user: User) -> None:
    other = factories.make_user(session)
    ui_settings.write(session, user, Settings(), {PREFILLS: 25})
    assert ui_settings.stored(session, other) == {}
    assert ui_settings.resolve(session, other, Settings()).linkedin.budget.li_prefills_per_day == 15
    set_setting(session, other, "config." + PREFILLS, 5)
    assert ui_settings.stored(session, user) == {PREFILLS: 25}


# --- warnings and notes -----------------------------------------------------------


@pytest.mark.parametrize(("value", "noted"), [(21, True), (20, False)])
def test_a_message_budget_above_twenty_has_the_447_note(value: int, noted: bool) -> None:
    view = _view(ui_settings.describe(Settings(), {PREFILLS: value}), PREFILLS)
    assert bool(view.notes) is noted
    if noted:
        assert view.notes[0].startswith("LinkedIn prefills are set to 21 a day")


@pytest.mark.parametrize(("value", "noted"), [(101, True), (100, False)])
def test_profile_visits_above_100_have_the_318_note(value: int, noted: bool) -> None:
    view = _view(ui_settings.describe(Settings(), {VISITS: value}), VISITS)
    assert bool(view.notes) is noted


def test_a_short_weekly_limit_has_its_note() -> None:
    view = _view(ui_settings.describe(Settings(), {VISITS: 100, WEEK: 300}), WEEK)
    assert view.notes and "automatic in Settings" in view.notes[0]


def test_window_weekend_and_auto_send_warnings_match_posture() -> None:
    views = ui_settings.describe(
        _pinned(campaigns__linkedin_auto_send=True),
        {WINDOW: ["22:00", "06:00"], "linkedin.weekend_multiplier": 1.0},
    )
    assert "overnight" in _view(views, WINDOW).warnings[0]
    assert "not damped" in _view(views, "linkedin.weekend_multiplier").warnings[0]
    assert "ADR 0004" in _view(views, "campaigns.linkedin_auto_send").warnings[0]


def test_a_file_value_above_the_hard_max_is_warned_about() -> None:
    views = ui_settings.describe(_pinned(linkedin__budget__li_prefills_per_day=80), {})
    warning = _view(views, PREFILLS).warnings[0]
    assert "asks for 80 a day, above the hard max of 50" in warning


def test_no_editable_value_needs_a_restart() -> None:
    """#464: serve reads each user's values as it runs, so none waits for a restart."""
    started = ui_settings.apply(Settings(), {})
    views = ui_settings.describe(
        Settings(), {"campaigns.reply_poll_minutes": 5, PREFILLS: 30}, started=started
    )
    assert not any(view.restart_pending for view in views)
    assert not any(view.spec.applies == "restart" and view.spec.editable for view in views)


# --- review follow-ups ------------------------------------------------------------


def test_resolving_again_keeps_what_a_replace_changed_since() -> None:
    """The trap: settings resolved, then changed with ``replace`` (a worker swapping in
    its LinkedIn section), then resolved again must keep that change."""
    once = ui_settings.apply(Settings(), {PREFILLS: 30})
    changed = replace(once, linkedin=replace(once.linkedin, weekend_multiplier=0.25))
    again = ui_settings.apply(changed, {})
    assert again.linkedin.weekend_multiplier == 0.25
    assert again.linkedin.budget.li_prefills_per_day == 15  # the reset still holds
    assert ui_settings.unresolve(once) == Settings()


@pytest.mark.parametrize(
    ("changes", "refused"),
    [
        ({"campaigns.send_spacing_floor_s": 89}, "campaigns.send_spacing_floor_s"),
        ({"campaigns.send_spacing_median_s": 89}, "campaigns.send_spacing_median_s"),
        ({"campaigns.send_spacing_floor_s": 300}, "campaigns.send_spacing_floor_s"),
        (
            {"campaigns.send_spacing_floor_s": 120, "campaigns.send_spacing_median_s": 100},
            "campaigns.send_spacing_median_s",
        ),
        ({"campaigns.contacted_within_days_guard": 0}, "campaigns.contacted_within_days_guard"),
    ],
)
def test_the_page_is_stricter_than_the_file_on_spacing_and_the_guard(
    session: Session, user: User, changes: dict[str, Any], refused: str
) -> None:
    with pytest.raises(ui_settings.SettingsRefused) as error:
        ui_settings.write(session, user, Settings(), changes)
    assert set(error.value.problems) == {refused}


def test_the_lowest_spacing_and_guard_the_page_takes(session: Session, user: User) -> None:
    ui_settings.write(
        session,
        user,
        Settings(),
        {
            "campaigns.send_spacing_floor_s": 90,
            "campaigns.send_spacing_median_s": 90,
            "campaigns.contacted_within_days_guard": 1,
        },
    )
    views = ui_settings.describe(Settings(), ui_settings.stored(session, user))
    guard = _view(views, "campaigns.contacted_within_days_guard")
    assert guard.value == 1 and "last 1 days, below the 30" in guard.warnings[0]


@pytest.mark.parametrize(("days", "warned"), [(29, True), (30, False)])
def test_a_guard_below_30_days_warns(days: int, warned: bool) -> None:
    key = "campaigns.contacted_within_days_guard"
    assert bool(_view(ui_settings.describe(Settings(), {key: days}), key).warnings) is warned


def test_a_file_spacing_below_the_page_minimum_never_blocks_another_write(
    session: Session, user: User
) -> None:
    base = _pinned(campaigns__send_spacing_median_s=30, campaigns__send_spacing_floor_s=20)
    ui_settings.write(session, user, base, {"campaigns.mailbox_daily_cap": 10})
    assert ui_settings.stored(session, user) == {"campaigns.mailbox_daily_cap": 10}
