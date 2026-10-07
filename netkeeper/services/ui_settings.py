"""Settings you change on the Settings page instead of in ``config.toml`` (#343).

**Where a value comes from.** Each editable setting has three possible sources,
and the first that has a value wins:

1. ``config.toml``, when the file sets that key. The file is optional and holds
   overrides only: a key it sets is pinned, and the Settings page shows the value
   locked, with the file's path, until you remove the key there. The file wins
   because it is the deliberate, out-of-band choice (a file you or a deployment
   wrote), and because an install that already has one keeps behaving exactly as
   it did before the Settings page could change anything.
2. The Settings page: one ``settings_kv`` row per key, ``config.<key>``, per user.
3. The built-in default (:mod:`netkeeper.config`).

**Validation.** A value from the page is checked against the same hard maximums
enforcement uses (:data:`netkeeper.services.budgets.HARD_MAX_PER_DAY` and
``HARD_MAX_PER_WEEK``, :data:`netkeeper.services.mailboxes.MAILBOX_HARD_MAX_PER_DAY`)
and refused above them, where a file value above one is clamped and warned about. A
stored row that no longer passes (edited by hand, or a ceiling that came down) is
ignored, with a log line, so the default applies: it never reaches enforcement.

**Taking effect.** :func:`resolve` is cheap (one scoped query), so every reader
calls it where it uses the value: each request, each campaign tick for each user,
and each LinkedIn run as it starts. A change therefore applies from the next
request, tick or run. The few values a running ``serve`` reads once at startup
(:attr:`FieldSpec.applies` ``"restart"``) say so, and :func:`describe` reports when
the running process still has the old one.

**What is not here.** ``linkedin_auto_send`` is shown, never written: turning it on
from a web page would need a confirm step as deliberate as ``schedule arm``. Secrets
never are settings; they stay in the Keychain. ``web.*``, ``linkedin.cdp_url``,
pacing, heat and the LLM model names stay file-only.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, time
from typing import Any, Final, Literal

from sqlalchemy.orm import Session

from netkeeper.config import Settings
from netkeeper.models import JsonValue, SettingKV, User
from netkeeper.scoping import scoped
from netkeeper.services import budgets
from netkeeper.services.budgets import (
    HARD_MAX_PER_DAY,
    HARD_MAX_PER_WEEK,
    LI_MESSAGE_WARN_ABOVE,
    PROFILE_VISITS_DESIGN_LEVEL,
    ActionClass,
)
from netkeeper.services.mailboxes import MAILBOX_HARD_MAX_PER_DAY
from netkeeper.services.setting_checks import (
    WEEKEND_DAMPING_CEILING,
    active_window_check,
    auto_send_warnings,
    weekend_multiplier_warnings,
)
from netkeeper.services.settings_kv import delete_setting, set_setting

log = logging.getLogger(__name__)

KEY_PREFIX: Final = "config."
"""``settings_kv`` key prefix: ``config.linkedin.budget.li_prefills_per_day``."""

Kind = Literal["int", "optional_int", "float", "bool", "window", "dates"]
Applies = Literal["now", "restart"]
Group = Literal["linkedin_budgets", "linkedin_hours", "campaigns", "llm", "backup"]
Source = Literal["default", "ui", "file"]

MAX_HOLIDAYS: Final = 366
"""The most holidays the page stores: a year of them, every day."""

_CLOCK = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One setting the Settings page shows."""

    key: str
    group: Group
    label: str
    help: str
    kind: Kind
    applies: Applies
    applies_note: str
    minimum: float | None = None
    maximum: float | None = None
    """The hard maximum: a value above it is refused."""
    warn_above: float | None = None
    """Above this, the value is allowed and warned about."""
    editable: bool = True
    locked_reason: str | None = None
    """Why the Settings page never writes this one."""


_BUDGET_NOTE = (
    "Applies from the next LinkedIn run and the next prefill. A run already going keeps"
    " the limit it started with."
)

FIELDS: Final[tuple[FieldSpec, ...]] = (
    FieldSpec(
        key="linkedin.budget.connection_pages_per_day",
        group="linkedin_budgets",
        label="Connection pages a day",
        help="How many pages of your connections list a sync may read in a day.",
        kind="int",
        applies="now",
        applies_note=_BUDGET_NOTE,
        minimum=1,
        maximum=HARD_MAX_PER_DAY[ActionClass.CONNECTION_PAGES],
    ),
    FieldSpec(
        key="linkedin.budget.profile_visits_per_day",
        group="linkedin_budgets",
        label="Profile visits a day",
        help=(
            "How many LinkedIn profiles enrichment and prefills may open in a day. Warm-up"
            " and the weekend multiplier lower it further."
        ),
        kind="int",
        applies="now",
        applies_note=_BUDGET_NOTE,
        minimum=1,
        maximum=HARD_MAX_PER_DAY[ActionClass.PROFILE_VISITS],
        warn_above=PROFILE_VISITS_DESIGN_LEVEL,
    ),
    FieldSpec(
        key="linkedin.budget.profile_visits_per_week",
        group="linkedin_budgets",
        label="Profile visits a week",
        help=(
            f"Automatic is {budgets.PROFILE_VISIT_DAYS_PER_WEEK} times the daily limit in force."
            " Set a number to use it instead."
        ),
        kind="optional_int",
        applies="now",
        applies_note=_BUDGET_NOTE,
        minimum=1,
        maximum=HARD_MAX_PER_WEEK[ActionClass.PROFILE_VISITS],
    ),
    FieldSpec(
        key="linkedin.budget.inbox_polls_per_day",
        group="linkedin_budgets",
        label="Inbox polls a day",
        help="How many times a day netkeeper may read your LinkedIn inbox for replies.",
        kind="int",
        applies="now",
        applies_note=_BUDGET_NOTE,
        minimum=1,
        maximum=HARD_MAX_PER_DAY[ActionClass.INBOX_POLLS],
    ),
    FieldSpec(
        key="linkedin.budget.li_prefills_per_day",
        group="linkedin_budgets",
        label="LinkedIn prefills a day",
        help="How many campaign messages netkeeper may type into LinkedIn for you to send.",
        kind="int",
        applies="now",
        applies_note=_BUDGET_NOTE,
        minimum=1,
        maximum=HARD_MAX_PER_DAY[ActionClass.LI_PREFILLS],
        warn_above=LI_MESSAGE_WARN_ABOVE,
    ),
    FieldSpec(
        key="linkedin.budget.li_messages_auto_per_day",
        group="linkedin_budgets",
        label="Auto-sent LinkedIn messages a day",
        help=(
            "Only used when LinkedIn auto-send is on, which it is not unless config.toml"
            " turns it on."
        ),
        kind="int",
        applies="now",
        applies_note=_BUDGET_NOTE,
        minimum=1,
        maximum=HARD_MAX_PER_DAY[ActionClass.LI_MESSAGES_AUTO],
        warn_above=LI_MESSAGE_WARN_ABOVE,
    ),
    FieldSpec(
        key="linkedin.active_hours",
        group="linkedin_hours",
        label="Active hours",
        help=(
            "When netkeeper may use LinkedIn, in your LinkedIn time zone. Keep it to hours"
            " you are usually online."
        ),
        kind="window",
        applies="restart",
        applies_note=(
            "Runs you start, and each visit of an enrichment run, check the new hours at"
            " once. Scheduled runs keep the old timetable until you restart netkeeper serve."
        ),
    ),
    FieldSpec(
        key="linkedin.weekend_multiplier",
        group="linkedin_hours",
        label="Weekend multiplier",
        help="Saturday and Sunday budgets are multiplied by this. 0.5 halves them.",
        kind="float",
        applies="now",
        applies_note=_BUDGET_NOTE,
        minimum=0,
        maximum=WEEKEND_DAMPING_CEILING,
    ),
    FieldSpec(
        key="campaigns.mailbox_daily_cap",
        group="campaigns",
        label="Emails a day per campaign",
        help=(
            "A campaign's daily cap when it does not set its own, and the cap a newly"
            " connected mailbox starts with."
        ),
        kind="int",
        applies="now",
        applies_note="Applies from the next campaign tick, within a minute.",
        minimum=0,
        maximum=MAILBOX_HARD_MAX_PER_DAY,
    ),
    FieldSpec(
        key="campaigns.send_spacing_median_s",
        group="campaigns",
        label="Typical gap between emails (seconds)",
        help="Gaps vary around this so sends never go out as a burst.",
        kind="int",
        applies="now",
        applies_note="Applies from the next campaign tick, within a minute.",
        minimum=1,
        maximum=3600,
    ),
    FieldSpec(
        key="campaigns.send_spacing_floor_s",
        group="campaigns",
        label="Shortest gap between emails (seconds)",
        help="No two emails from one mailbox go out closer together than this.",
        kind="int",
        applies="now",
        applies_note="Applies from the next campaign tick, within a minute.",
        minimum=1,
        maximum=3600,
    ),
    FieldSpec(
        key="campaigns.contacted_within_days_guard",
        group="campaigns",
        label="Skip people contacted within (days)",
        help="A campaign leaves out anyone you contacted this recently.",
        kind="int",
        applies="now",
        applies_note="New campaigns copy it. A campaign that already exists keeps its own.",
        minimum=0,
        maximum=3650,
    ),
    FieldSpec(
        key="campaigns.reply_poll_minutes",
        group="campaigns",
        label="Check Gmail for replies every (minutes)",
        help="How often netkeeper looks for replies to campaign email.",
        kind="int",
        applies="restart",
        applies_note="Takes effect when you restart netkeeper serve.",
        minimum=1,
        maximum=1440,
    ),
    FieldSpec(
        key="campaigns.holidays",
        group="campaigns",
        label="Holidays",
        help="Days the suggested send slots skip, as YYYY-MM-DD.",
        kind="dates",
        applies="now",
        applies_note="Applies to the next suggestion and the next campaign tick.",
    ),
    FieldSpec(
        key="campaigns.linkedin_auto_send",
        group="campaigns",
        label="LinkedIn auto-send",
        help=(
            "Off: netkeeper types LinkedIn messages for you to send yourself (ADR 0004)."
            " Auto-send isn't built yet."
        ),
        kind="bool",
        applies="restart",
        applies_note="Only config.toml can change it.",
        editable=False,
        locked_reason=(
            "Auto-send is the action LinkedIn restricts hardest, so the Settings page never"
            " turns it on. Only config.toml can change it."
        ),
    ),
    FieldSpec(
        key="llm.enabled",
        group="llm",
        label="Use the LLM",
        help=(
            "Lets netkeeper call the Anthropic API for drafting. No feature calls it yet;"
            " this is saved for when one does. The API key stays in the Keychain."
        ),
        kind="bool",
        applies="now",
        applies_note="Saved now. Nothing reads it yet.",
    ),
    FieldSpec(
        key="backup.nightly",
        group="backup",
        label="Nightly backups",
        help=(
            "Nightly backups are not scheduled yet; this is saved for when they are. Run"
            " netkeeper backup create to make one now."
        ),
        kind="bool",
        applies="now",
        applies_note="Saved now. Nothing reads it yet.",
    ),
    FieldSpec(
        key="backup.keep",
        group="backup",
        label="Backups to keep",
        help="netkeeper backup create deletes older backups past this many.",
        kind="int",
        applies="now",
        applies_note="Applies to the next backup.",
        minimum=1,
        maximum=365,
    ),
)

BY_KEY: Final[dict[str, FieldSpec]] = {spec.key: spec for spec in FIELDS}

_BUDGET_ACTION: Final[dict[str, ActionClass]] = {
    "linkedin.budget.connection_pages_per_day": ActionClass.CONNECTION_PAGES,
    "linkedin.budget.profile_visits_per_day": ActionClass.PROFILE_VISITS,
    "linkedin.budget.inbox_polls_per_day": ActionClass.INBOX_POLLS,
    "linkedin.budget.li_prefills_per_day": ActionClass.LI_PREFILLS,
    "linkedin.budget.li_messages_auto_per_day": ActionClass.LI_MESSAGES_AUTO,
}


class SettingsRefused(ValueError):
    """A write was refused. ``problems`` maps each refused key to why; nothing was stored."""

    def __init__(self, problems: Mapping[str, str]) -> None:
        self.problems = dict(problems)
        super().__init__("; ".join(f"{key}: {why}" for key, why in self.problems.items()))


@dataclass(frozen=True, slots=True)
class FieldView:
    """One setting as the Settings page shows it."""

    spec: FieldSpec
    value: JsonValue
    """The value in force."""
    default: JsonValue
    ui_value: JsonValue
    """What the Settings page stored, or None when it stored nothing usable."""
    file_value: JsonValue
    """What config.toml sets, or None when it does not set this key."""
    source: Source
    editable: bool
    locked_reason: str | None
    restart_pending: bool
    """The running ``serve`` still has another value: restart it to apply this one."""
    stored_unreadable: bool
    """A stored row exists but fails validation, so it is ignored."""
    warnings: tuple[str, ...]
    notes: tuple[str, ...]


# --- reading ----------------------------------------------------------------------


def stored(session: Session, user: User) -> dict[str, JsonValue]:
    """Every ``config.*`` row ``user`` has, keyed without the prefix. Read-only."""
    rows = session.scalars(
        scoped(user, SettingKV).where(SettingKV.key.startswith(KEY_PREFIX, autoescape=True))
    )
    return {row.key[len(KEY_PREFIX) :]: row.value for row in rows}


def resolve(session: Session, user: User, base: Settings) -> Settings:
    """``base`` (the file's settings, or the defaults) with ``user``'s Settings-page values.

    A key the file sets keeps the file's value. ``ui_keys`` on the result names the keys
    whose value came from the page. Read-only.
    """
    return apply(base, stored(session, user))


def apply(base: Settings, values: Mapping[str, JsonValue]) -> Settings:
    """``base`` with ``values`` (``stored``'s shape) laid over it. Pure.

    Settings already resolved are resolved again from what they were resolved from, so
    a value since reset never lingers.
    """
    if base.unresolved is not None:
        base = base.unresolved
    result = base
    used: set[str] = set()
    for spec in FIELDS:
        if spec.key not in values or not spec.editable or file_sets(base, spec.key):
            continue
        try:
            value = parse(spec, values[spec.key])
        except ValueError as exc:
            log.warning("ignoring the stored setting %s: %s", spec.key, exc)
            continue
        result = _with_value(result, spec.key, value)
        used.add(spec.key)
    return replace(result, ui_keys=frozenset(used), unresolved=base)


def file_sets(settings: Settings, key: str) -> bool:
    """True when the config file these settings came from sets ``key``."""
    return settings.file_keys is not None and key in settings.file_keys


def value_at(settings: Settings, key: str) -> Any:
    """The value of a dotted ``key`` in ``settings``."""
    node: Any = settings
    for part in key.split("."):
        node = getattr(node, part)
    return node


def _with_value(settings: Settings, key: str, value: object) -> Settings:
    updated: Settings = _replace_path(settings, key.split("."), value)
    return updated


def _replace_path(node: Any, parts: list[str], value: object) -> Any:
    if not is_dataclass(node) or isinstance(node, type):
        raise TypeError(f"cannot set {'.'.join(parts)} on {type(node).__name__}")
    head, *rest = parts
    if head not in {f.name for f in fields(node)}:
        raise KeyError(head)
    new = value if not rest else _replace_path(getattr(node, head), rest, value)
    return replace(node, **{head: new})


# --- validating -------------------------------------------------------------------


def parse(spec: FieldSpec, raw: object) -> object:
    """``raw`` (JSON) as the value ``Settings`` holds for ``spec``; ValueError if refused."""
    if spec.kind in ("int", "optional_int"):
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ValueError("must be a whole number")
        _check_range(spec, raw)
        return raw
    if spec.kind == "float":
        if isinstance(raw, bool) or not isinstance(raw, int | float) or not math.isfinite(raw):
            raise ValueError("must be a number")
        _check_range(spec, raw)
        return float(raw)
    if spec.kind == "bool":
        if not isinstance(raw, bool):
            raise ValueError("must be true or false")
        return raw
    if spec.kind == "window":
        return _parse_window(raw)
    return _parse_dates(raw)


def _check_range(spec: FieldSpec, value: float) -> None:
    if spec.minimum is not None and value < spec.minimum:
        raise ValueError(f"must be at least {_number(spec.minimum)}")
    if spec.maximum is not None and value > spec.maximum:
        raise ValueError(f"must be at most {_number(spec.maximum)}, its hard maximum")


def _number(value: float) -> str:
    return f"{value:g}" if isinstance(value, float) and not value.is_integer() else f"{value:.0f}"


def _parse_window(raw: object) -> tuple[str, str]:
    if (
        not isinstance(raw, list | tuple)
        or len(raw) != 2
        or not all(isinstance(item, str) and _CLOCK.match(item) for item in raw)
    ):
        raise ValueError("must be two times of day as HH:MM")
    start, end = str(raw[0]), str(raw[1])
    status, warnings = active_window_check(time.fromisoformat(start), time.fromisoformat(end))
    if status == "off":
        raise ValueError(warnings[0])
    return start, end


def _parse_dates(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list | tuple):
        raise ValueError("must be a list of dates")
    if len(raw) > MAX_HOLIDAYS:
        raise ValueError(f"must have at most {MAX_HOLIDAYS} dates")
    days: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not _ISO_DATE.match(item):
            raise ValueError(f"{item!r} is not a date as YYYY-MM-DD")
        try:
            days.add(date.fromisoformat(item).isoformat())
        except ValueError as exc:
            raise ValueError(f"{item!r} is not a date as YYYY-MM-DD") from exc
    return tuple(sorted(days))


def to_json(value: object) -> JsonValue:
    """A ``Settings`` value as JSON: tuples become lists."""
    if isinstance(value, tuple):
        return [to_json(item) for item in value]
    if value is None or isinstance(value, bool | int | float | str):
        return value
    raise TypeError(f"cannot store {type(value).__name__}")


# --- writing ----------------------------------------------------------------------


def write(session: Session, user: User, base: Settings, changes: Mapping[str, JsonValue]) -> None:
    """Store ``changes`` for ``user``: all of them, or none (:class:`SettingsRefused`).

    A ``None`` value removes the stored one, so the default applies (for the weekly
    profile-visit limit, that is automatic). A key the config file sets, a key the page
    never writes, an unknown key, and a value outside its range are each refused.
    Needs a writer session.
    """
    problems: dict[str, str] = {}
    parsed: dict[str, JsonValue] = {}
    for key, raw in changes.items():
        spec = BY_KEY.get(key)
        if spec is None:
            problems[key] = "not a setting the Settings page changes"
            continue
        if not spec.editable:
            problems[key] = spec.locked_reason or "the Settings page does not change it"
            continue
        if file_sets(base, key):
            problems[key] = (
                f"{base.source_path} sets it, and the file wins; remove {key} there to"
                " change it here"
            )
            continue
        if raw is None:
            parsed[key] = None
            continue
        try:
            parsed[key] = to_json(parse(spec, raw))
        except ValueError as exc:
            problems[key] = str(exc)
    if problems:
        raise SettingsRefused(problems)
    for key, value in parsed.items():
        if value is None:
            delete_setting(session, user, KEY_PREFIX + key)
        else:
            set_setting(session, user, KEY_PREFIX + key, value)
        log.info("setting %s changed on the Settings page for user %d", key, user.id)


# --- describing -------------------------------------------------------------------


def describe(
    base: Settings,
    values: Mapping[str, JsonValue],
    *,
    started: Settings | None = None,
) -> list[FieldView]:
    """Every field as the Settings page shows it. Pure.

    ``base`` is the file's settings, ``values`` what :func:`stored` read, and
    ``started`` the settings the running ``serve`` resolved at startup (None outside
    ``serve``): a restart-only field whose value differs from it is ``restart_pending``.
    """
    if base.unresolved is not None:
        base = base.unresolved
    effective = apply(base, values)
    defaults = Settings()
    views: list[FieldView] = []
    for spec in FIELDS:
        pinned = file_sets(base, spec.key)
        ui_value: JsonValue = None
        unreadable = False
        if spec.key in values:
            try:
                ui_value = to_json(parse(spec, values[spec.key]))
            except ValueError:
                unreadable = True
        value = value_at(effective, spec.key)
        source: Source = "file" if pinned else "ui" if spec.key in effective.ui_keys else "default"
        pending = (
            spec.applies == "restart"
            and started is not None
            and value_at(started, spec.key) != value
        )
        warnings, notes = checks(spec.key, effective)
        views.append(
            FieldView(
                spec=spec,
                value=to_json(value),
                default=to_json(value_at(defaults, spec.key)),
                ui_value=ui_value,
                file_value=to_json(value_at(base, spec.key)) if pinned else None,
                source=source,
                editable=spec.editable and not pinned,
                locked_reason=(
                    spec.locked_reason
                    if not spec.editable
                    else f"Set in {base.source_path}, which wins over this page. Remove"
                    f" {spec.key} from the file to change it here."
                    if pinned
                    else None
                ),
                restart_pending=pending,
                stored_unreadable=unreadable,
                warnings=warnings,
                notes=notes,
            )
        )
    return views


def checks(key: str, settings: Settings) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The warnings and notes ``key``'s value in force earns: the same words the posture
    report and ``serve``'s startup log use. Warnings say something is off; notes are the
    person's call and never gate anything."""
    budget = settings.linkedin.budget
    warnings: list[str] = []
    notes: list[str] = []
    action = _BUDGET_ACTION.get(key)
    if action is not None:
        asked = budgets.configured_default(action, budget, "day")
        hard = HARD_MAX_PER_DAY[action]
        if asked is not None and asked > hard:
            warnings.append(
                f"config.toml asks for {asked} a day, above the hard max of {hard}. The"
                f" clamp holds and {hard} is what is enforced."
            )
        if action is ActionClass.PROFILE_VISITS:
            notes.extend(_some(budgets.profile_visit_risk_warning(budget)))
        notes.extend(_some(budgets.li_message_risk_warning(action, budget)))
    elif key == "linkedin.budget.profile_visits_per_week":
        asked_week = budgets.configured_default(ActionClass.PROFILE_VISITS, budget, "week")
        hard_week = HARD_MAX_PER_WEEK[ActionClass.PROFILE_VISITS]
        if asked_week is not None and asked_week > hard_week:
            warnings.append(
                f"config.toml asks for {asked_week} a week, above the hard max of"
                f" {hard_week}. The clamp holds and {hard_week} is what is enforced."
            )
        notes.extend(_some(budgets.profile_visit_week_note(budget)))
    elif key == "linkedin.active_hours":
        warnings.extend(_window_warnings(settings.linkedin.active_hours))
    elif key == "linkedin.weekend_multiplier":
        warnings.extend(weekend_multiplier_warnings(settings.linkedin.weekend_multiplier))
    elif key == "campaigns.linkedin_auto_send":
        warnings.extend(auto_send_warnings(settings.campaigns))
    elif key == "campaigns.mailbox_daily_cap":
        cap = settings.campaigns.mailbox_daily_cap
        if cap > MAILBOX_HARD_MAX_PER_DAY:
            warnings.append(
                f"config.toml asks for {cap} a day, above the hard max of"
                f" {MAILBOX_HARD_MAX_PER_DAY}. The clamp holds and {MAILBOX_HARD_MAX_PER_DAY}"
                " is what is enforced."
            )
    elif key in ("campaigns.send_spacing_median_s", "campaigns.send_spacing_floor_s"):
        if value_at(settings, key) <= 0:
            warnings.append(
                f"{key} is not a positive number of seconds, so campaign sends are held."
            )
    elif key == "campaigns.reply_poll_minutes":
        if settings.campaigns.reply_poll_minutes < 1:
            warnings.append("reply_poll_minutes is below 1, so netkeeper checks every minute.")
    elif key == "backup.keep" and settings.backup.keep < 1:
        warnings.append("backup.keep is below 1, so netkeeper backup create refuses to run.")
    return tuple(warnings), tuple(notes)


def _window_warnings(active_hours: tuple[str, str]) -> tuple[str, ...]:
    try:
        start, end = (time.fromisoformat(value) for value in active_hours)
    except ValueError:
        return (
            "linkedin.active_hours is not two HH:MM times, so no run can start until it is fixed",
        )
    return active_window_check(start, end)[1]


def _some(text: str | None) -> tuple[str, ...]:
    return () if text is None else (text,)
