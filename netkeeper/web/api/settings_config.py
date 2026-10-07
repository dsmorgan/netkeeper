"""``/settings/config``: the settings you change on the Settings page (#343).

Per user, in ``settings_kv`` (:mod:`netkeeper.services.ui_settings`). ``config.toml``
is optional and wins for any key it sets; each field says where its value comes from.

- ``GET /settings/config`` answers every field: the value in force, its default, what
  the page and the file hold, which one wins (``source``), whether you can change it
  here, when a change applies, and the warnings and notes the value in force earns.
- ``PUT /settings/config`` stores ``values`` (a key to a value, or ``null`` to go back
  to the default): every one, or none. ``422`` names each value refused: above its hard
  maximum, below its minimum, the wrong type, a key the file sets, a key the page never
  changes (``campaigns.linkedin_auto_send``), or an unknown key.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from netkeeper.config import Settings
from netkeeper.models import User
from netkeeper.services import ui_settings
from netkeeper.web.deps import CurrentUser, SessionDep, file_settings

router = APIRouter(tags=["settings"])


class ConfigFieldOut(BaseModel):
    key: str
    """The dotted config key, as ``config.toml`` spells it: ``linkedin.active_hours``."""
    group: Literal["linkedin_budgets", "linkedin_hours", "campaigns", "llm", "backup"]
    label: str
    help: str
    kind: Literal["int", "optional_int", "float", "bool", "window", "dates"]
    """``optional_int``: a number, or null for automatic. ``window``: ``[start, end]`` as
    ``HH:MM``. ``dates``: a list of ``YYYY-MM-DD``."""
    applies: Literal["now", "restart"]
    """``restart``: a running ``serve`` reads it once, at startup."""
    applies_note: str
    minimum: float | None
    maximum: float | None
    """The hard maximum; the page refuses anything above it."""
    warn_above: float | None
    """Above this the value is allowed, with a warning."""
    value: Any
    """The value in force."""
    default: Any
    ui_value: Any
    """What the Settings page stored, or null."""
    file_value: Any
    """What config.toml sets, or null when it does not set this key."""
    source: Literal["default", "ui", "file"]
    """Where ``value`` comes from. The file wins over the page, the page over the default."""
    editable: bool
    locked_reason: str | None
    restart_pending: bool
    """The running ``serve`` still uses another value; restart it to apply this one."""
    stored_unreadable: bool
    """A stored value fails validation, so it is ignored and the default applies."""
    warnings: list[str]
    """Something is off with the value in force: the posture report says so too."""
    notes: list[str]
    """The value in force is your call, and this is what it means. Never gates anything."""


class ConfigOut(BaseModel):
    config_path: str | None
    """The config file in use, or null when there is none."""
    fields: list[ConfigFieldOut]


class ConfigIn(BaseModel):
    values: dict[str, Any] = Field(min_length=1)
    """Keys to new values; ``null`` goes back to the default (automatic, for the
    weekly profile-visit limit)."""


def _started(request: Request, user: User) -> Settings | None:
    """What the running ``serve`` resolved for ``user`` at startup, or None."""
    if getattr(request.app.state, "started_user_id", None) != user.id:
        return None
    started: Settings | None = getattr(request.app.state, "started_settings", None)
    return started


def _out(request: Request, session: SessionDep, user: User) -> ConfigOut:
    base = file_settings(request)
    views = ui_settings.describe(
        base, ui_settings.stored(session, user), started=_started(request, user)
    )
    return ConfigOut(
        config_path=None if base.source_path is None else str(base.source_path),
        fields=[
            ConfigFieldOut(
                key=view.spec.key,
                group=view.spec.group,
                label=view.spec.label,
                help=view.spec.help,
                kind=view.spec.kind,
                applies=view.spec.applies,
                applies_note=view.spec.applies_note,
                minimum=view.spec.minimum,
                maximum=view.spec.maximum,
                warn_above=view.spec.warn_above,
                value=view.value,
                default=view.default,
                ui_value=view.ui_value,
                file_value=view.file_value,
                source=view.source,
                editable=view.editable,
                locked_reason=view.locked_reason,
                restart_pending=view.restart_pending,
                stored_unreadable=view.stored_unreadable,
                warnings=list(view.warnings),
                notes=list(view.notes),
            )
            for view in views
        ],
    )


@router.get("/settings/config", operation_id="get_config_settings")
def get_config_settings(request: Request, session: SessionDep, user: CurrentUser) -> ConfigOut:
    """Every setting the Settings page shows, where its value comes from, and its warnings."""
    return _out(request, session, user)


@router.put(
    "/settings/config",
    operation_id="set_config_settings",
    responses={422: {"description": "A value refused: nothing was stored"}},
)
def set_config_settings(
    body: ConfigIn, request: Request, session: SessionDep, user: CurrentUser
) -> ConfigOut:
    """Store new values, all or none. Each applies as its ``applies_note`` says."""
    try:
        ui_settings.write(session, user, file_settings(request), body.values)
    except ui_settings.SettingsRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _out(request, session, user)
