"""TOML settings as frozen dataclasses mirroring ``config.example.toml``.

Resolution order (spec section 15): ``--config``, ``$NETKEEPER_CONFIG``,
``./config.toml``, ``<data_dir>/config.toml``, then the built-in defaults below.
Unknown sections and keys are logged and ignored, except under ``[me]``, where
unknown keys are kept as extra merge fields on purpose; wrong types raise
:class:`ConfigError`. A key netkeeper no longer reads (:data:`DEPRECATED_KEYS`) is
ignored too, with one deprecation warning per file that names each such key.
"""

from __future__ import annotations

import json
import logging
import tomllib
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Union, get_args, get_origin, get_type_hints

from netkeeper.paths import config_candidates

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

log = logging.getLogger(__name__)

# Field metadata roles the loader and renderer understand.
_ROLE = "netkeeper.config"
_SKIP = {_ROLE: "skip"}  # not a config key (for example Settings.source_path)
_EXTRA = {_ROLE: "extra"}  # collects the table's unknown string keys


class ConfigError(ValueError):
    """A config file could not be parsed, or a value has the wrong type or shape."""


DEPRECATED_KEYS: dict[str, str] = {
    "campaigns.send_window_days": "the send window is gone (#338)",
    "campaigns.send_window_hours": "the send window is gone (#338)",
}
"""Keys an older config may still hold, and why netkeeper ignores them. A file that
has any of them still loads: they are left out, with one warning (see
:func:`_load_file`)."""

DEPRECATED_ADVICE = (
    "netkeeper no longer limits when campaigns send; each campaign has a scheduled start,"
    " chosen when you activate it. Remove these keys from the file"
)


@dataclass(frozen=True, slots=True)
class WebSettings:
    host: str = "127.0.0.1"
    port: int = 8000


@dataclass(frozen=True, slots=True)
class MeSettings:
    """Merge fields describing the operator. Extra string keys land in ``extra``."""

    name: str = ""
    website: str = ""
    scheduling_link: str = ""
    signature: str = ""
    city: str = ""
    extra: dict[str, str] = field(default_factory=dict, metadata=_EXTRA)


@dataclass(frozen=True, slots=True)
class BudgetSettings:
    connection_pages_per_day: int = 150
    profile_visits_per_day: int = 60
    # None: 5 x the daily limit in force (``budgets.PROFILE_VISIT_DAYS_PER_WEEK``, #318).
    # TOML has no null, so leaving the key out is how a config file asks for that.
    profile_visits_per_week: int | None = None
    inbox_polls_per_day: int = 8
    li_messages_auto_per_day: int = 15
    warmup_start: int = 20
    warmup_step: int = 10


@dataclass(frozen=True, slots=True)
class PacingSettings:
    profile_delay_median_s: int = 25
    profile_delay_sigma: float = 0.6
    distraction_p: float = 0.08
    distraction_range_s: tuple[int, int] = (120, 480)
    burst_size: tuple[int, int] = (8, 15)
    burst_break_s: tuple[int, int] = (300, 1200)


@dataclass(frozen=True, slots=True)
class HeatSettings:
    per_block: float = 1.0
    half_life_hours: int = 6
    skip_threshold: float = 2.5


@dataclass(frozen=True, slots=True)
class LinkedInSettings:
    cdp_url: str = "http://127.0.0.1:9222"
    timezone: str = "America/New_York"
    active_hours: tuple[str, str] = ("08:30", "21:30")
    weekend_multiplier: float = 0.5
    enrich_stale_days: int = 180
    disconnect_after_misses: int = 2
    budget: BudgetSettings = field(default_factory=BudgetSettings)
    pacing: PacingSettings = field(default_factory=PacingSettings)
    heat: HeatSettings = field(default_factory=HeatSettings)


@dataclass(frozen=True, slots=True)
class CampaignSettings:
    """``[campaigns]``. There is no send window (#338): each campaign has a scheduled
    start, and ``holidays`` only steer the suggested send slots
    (:mod:`netkeeper.campaigns.schedule`)."""

    mailbox_daily_cap: int = 80
    send_spacing_median_s: int = 240
    send_spacing_floor_s: int = 90
    contacted_within_days_guard: int = 30
    linkedin_auto_send: bool = False
    reply_poll_minutes: int = 10
    holidays: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LlmSettings:
    enabled: bool = False
    model: str = "claude-sonnet-5"
    bulk_model: str = "claude-haiku-4-5-20251001"
    daily_call_cap: int = 200


@dataclass(frozen=True, slots=True)
class BackupSettings:
    nightly: bool = True
    keep: int = 14


@dataclass(frozen=True, slots=True)
class Settings:
    """Every section of ``config.toml``. ``source_path`` is None when defaults were used."""

    web: WebSettings = field(default_factory=WebSettings)
    me: MeSettings = field(default_factory=MeSettings)
    linkedin: LinkedInSettings = field(default_factory=LinkedInSettings)
    campaigns: CampaignSettings = field(default_factory=CampaignSettings)
    llm: LlmSettings = field(default_factory=LlmSettings)
    backup: BackupSettings = field(default_factory=BackupSettings)
    source_path: Path | None = field(default=None, metadata=_SKIP)


def load_settings(explicit: Path | None = None) -> Settings:
    """Load the first config file in the search order, or the defaults if none exists.

    ``explicit`` is the ``--config`` path. Because the user asked for that file by
    name, a missing one is an error rather than a silent fall-through.
    """
    if explicit is not None:
        explicit = explicit.expanduser()
        if not explicit.is_file():
            raise ConfigError(f"{explicit}: config file not found")
    for candidate in config_candidates(explicit):
        if candidate.is_file():
            return _load_file(candidate)
        log.debug("%s: no config file there", candidate)
    log.debug("no config file found; using built-in defaults")
    return Settings()


def _load_file(path: Path) -> Settings:
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    raw = _drop_deprecated(raw, source=path)
    settings = _from_table(Settings, raw, prefix="", source=path)
    log.debug("loaded settings from %s", path)
    return replace(settings, source_path=path)


def _drop_deprecated(raw: dict[str, Any], *, source: Path) -> dict[str, Any]:
    """``raw`` without :data:`DEPRECATED_KEYS`, and one warning naming those it held."""
    found: list[str] = []
    cleaned: dict[str, Any] = dict(raw)
    for key in DEPRECATED_KEYS:
        section, name = key.split(".", 1)
        table = cleaned.get(section)
        if isinstance(table, Mapping) and name in table:
            cleaned[section] = {k: v for k, v in table.items() if k != name}
            found.append(key)
    if found:
        reasons = sorted({DEPRECATED_KEYS[key] for key in found})
        log.warning(
            "%s: ignoring deprecated %s %s (%s). %s",
            source,
            "key" if len(found) == 1 else "keys",
            ", ".join(found),
            "; ".join(reasons),
            DEPRECATED_ADVICE,
        )
    return cleaned


def _from_table[T: DataclassInstance](
    cls: type[T], raw: Mapping[str, object], *, prefix: str, source: Path
) -> T:
    """Build ``cls`` from a TOML table, defaulting absent keys and validating present ones."""
    hints = get_type_hints(cls)
    kwargs: dict[str, object] = {}
    known: set[str] = set()
    extra_field: str | None = None
    for f in fields(cls):
        role = f.metadata.get(_ROLE)
        if role == "skip":
            continue
        if role == "extra":
            extra_field = f.name
            continue
        known.add(f.name)
        if f.name in raw:
            key = _join(prefix, f.name)
            kwargs[f.name] = _convert(raw[f.name], hints[f.name], key=key, source=source)
    unknown = [name for name in raw if name not in known]
    if extra_field is not None:
        kwargs[extra_field] = _extra_strings(raw, unknown, prefix=prefix, source=source)
    else:
        for name in unknown:
            key = _join(prefix, name)
            if isinstance(raw[name], Mapping):
                log.warning("%s: ignoring unknown section [%s]", source, key)
            else:
                log.warning("%s: ignoring unknown key %s", source, key)
    build: Callable[..., T] = cls
    return build(**kwargs)


def is_me_key(key: str) -> bool:
    """True for a ``[me]`` key a template can name as ``me.<key>``: an identifier that does
    not start with ``_``. Template lint refuses any other (#344)."""
    return key.isidentifier() and not key.startswith("_")


def _extra_strings(
    raw: Mapping[str, object], names: list[str], *, prefix: str, source: Path
) -> dict[str, str]:
    extra: dict[str, str] = {}
    for name in names:
        value = raw[name]
        if not isinstance(value, str):
            raise ConfigError(
                f"{source}: {_join(prefix, name)} must be a string"
                f" (extra merge fields are strings), got {_kind(value)}"
            )
        if not is_me_key(name):
            # Kept, as every extra key is, but no template can name it.
            log.warning(
                "%s: %s cannot be used as a merge field: a key under [me] must be letters, "
                "digits and _, must not start with a digit or _",
                source,
                _join(prefix, name),
            )
        extra[name] = value
    return extra


def _convert(value: object, hint: Any, *, key: str, source: Path) -> object:
    """Check ``value`` against the dataclass field type ``hint`` and return it.

    ``X | None`` checks ``value`` as ``X``: TOML has no null, so a present key
    always has a value, and an absent one keeps the field's default.
    """
    if get_origin(hint) in (Union, types.UnionType):
        options = [arg for arg in get_args(hint) if arg is not type(None)]
        if len(options) != 1:
            raise TypeError(f"unsupported settings field type for {key}: {hint!r}")
        return _convert(value, options[0], key=key, source=source)
    if is_dataclass(hint) and isinstance(hint, type):
        if not isinstance(value, Mapping):
            raise ConfigError(f"{source}: {key} must be a table, got {_kind(value)}")
        return _from_table(hint, value, prefix=key, source=source)
    if hint is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{source}: {key} must be true or false, got {_kind(value)}")
        return value
    if hint is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{source}: {key} must be an integer, got {_kind(value)}")
        return value
    if hint is float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"{source}: {key} must be a number, got {_kind(value)}")
        return float(value)
    if hint is str:
        if not isinstance(value, str):
            raise ConfigError(f"{source}: {key} must be a string, got {_kind(value)}")
        return value
    if get_origin(hint) is tuple:
        return _convert_list(value, get_args(hint), key=key, source=source)
    raise TypeError(f"unsupported settings field type for {key}: {hint!r}")


def _convert_list(
    value: object, args: tuple[Any, ...], *, key: str, source: Path
) -> tuple[object, ...]:
    """Validate a TOML array against ``tuple[X, ...]`` (any length) or ``tuple[X, Y]`` (fixed)."""
    variable = len(args) == 2 and args[1] is Ellipsis
    if not isinstance(value, list):
        raise ConfigError(f"{source}: {key} must be a list, got {_kind(value)}")
    if variable:
        item_hints = [args[0]] * len(value)
    elif len(value) == len(args):
        item_hints = list(args)
    else:
        raise ConfigError(f"{source}: {key} must have exactly {len(args)} items, got {len(value)}")
    return tuple(
        _convert(item, item_hint, key=f"{key}[{index}]", source=source)
        for index, (item, item_hint) in enumerate(zip(value, item_hints, strict=True))
    )


_TOML_KINDS: dict[type[object], str] = {
    bool: "boolean",
    int: "integer",
    float: "float",
    str: "string",
    list: "list",
    dict: "table",
}


def _kind(value: object) -> str:
    return _TOML_KINDS.get(type(value), type(value).__name__)


def _join(prefix: str, name: str) -> str:
    return f"{prefix}.{name}" if prefix else name


def render_toml(obj: DataclassInstance, *, table: str = "") -> str:
    """Render a settings dataclass as TOML text that :func:`load_settings` reads back.

    ``table`` names the top-level table to emit the object's scalars under; leave it
    empty for :class:`Settings`, whose fields are all sub-tables.
    """
    lines: list[str] = []
    _render_table(obj, prefix=table, lines=lines)
    return "\n".join(lines) + "\n"


def _render_table(obj: DataclassInstance, *, prefix: str, lines: list[str]) -> None:
    scalars: list[str] = []
    nested: list[tuple[str, DataclassInstance]] = []
    for f in fields(obj):
        role = f.metadata.get(_ROLE)
        if role == "skip":
            continue
        value = getattr(obj, f.name)
        if value is None:
            continue  # TOML has no null; an absent key reads back as the None default
        if role == "extra":
            scalars.extend(f"{name} = {_toml_value(item)}" for name, item in value.items())
        elif is_dataclass(value) and not isinstance(value, type):
            nested.append((f.name, value))
        else:
            scalars.append(f"{f.name} = {_toml_value(value)}")
    if prefix:
        if lines:
            lines.append("")
        lines.append(f"[{prefix}]")
    lines.extend(scalars)
    for name, sub in nested:
        _render_table(sub, prefix=_join(prefix, name), lines=lines)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        # JSON string escapes are a subset of TOML basic-string escapes.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, tuple | list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"cannot render {type(value).__name__} as TOML")
