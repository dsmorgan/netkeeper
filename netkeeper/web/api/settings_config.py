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
- ``POST /settings/restart`` restarts ``netkeeper serve`` (#597), for a field marked
  ``restart_pending`` or a config.toml changed since it started: ``202``, then serve
  stops cleanly and comes back. ``409`` when this process cannot restart itself, when
  config.toml no longer loads (serve would not start again), or while work is running
  (:mod:`netkeeper.services.restart_gate`), unless ``when_idle`` asks to wait for it.
  The UI waits for ``/health`` to answer a new ``boot_id``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from netkeeper.config import ConfigError, Settings, load_settings
from netkeeper.logging_setup import log_file, set_log_rotation
from netkeeper.models import User
from netkeeper.services import restart_gate, ui_settings
from netkeeper.services.restart_gate import RestartHook
from netkeeper.web.api.health import BOOT_ID
from netkeeper.web.deps import CurrentUser, SessionDep, file_settings

router = APIRouter(tags=["settings"])


class ConfigFieldOut(BaseModel):
    key: str
    """The dotted config key, as ``config.toml`` spells it: ``linkedin.active_hours``."""
    group: Literal[
        "linkedin_budgets", "linkedin_hours", "linkedin_runs", "campaigns", "backup", "logs"
    ]
    label: str
    help: str
    kind: Literal["int", "optional_int", "float", "bool", "window", "dates", "text"]
    """``optional_int``: a number, or null for automatic. ``window``: ``[start, end]`` as
    ``HH:MM``. ``dates``: a list of ``YYYY-MM-DD``. ``text``: a string the backend
    checks (a time zone name, a loopback address)."""
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
    log_file: str
    """Where ``serve`` and each command write their log (#602); rotated as ``logs.*`` say."""
    config_changed: bool
    """config.toml on disk now says something else than when serve started (or appeared,
    or went away): a restart reads it again."""
    config_error: str | None
    """config.toml on disk no longer loads, so a restart would fail; fix it first."""
    restart_pending: bool
    """A field is ``restart_pending``, or ``config_changed``: offer **Restart netkeeper**."""
    can_restart: bool
    """This process can restart itself (``netkeeper serve`` without ``--reload``)."""
    busy: list[str]
    """What is running now, which a restart would wait for: refused unless ``when_idle``."""
    restart_waiting: bool
    """A restart was asked for and waits for ``busy`` to clear."""
    fields: list[ConfigFieldOut]


class RestartOut(BaseModel):
    mode: Literal["launchd", "exec"]
    """``launchd``: serve exits and the login item starts it again, within about ten
    seconds. ``exec``: serve starts itself again in place."""
    boot_id: str
    """``/health``'s ``boot_id`` for the process now stopping; the restarted one differs."""
    waiting_on: list[str]
    """What the restart waits for, when it was asked to wait for idle; empty: now."""


class ConfigIn(BaseModel):
    values: dict[str, Any] = Field(min_length=1)
    """Keys to new values; ``null`` goes back to the default (automatic, for the
    weekly profile-visit limit)."""


def _on_disk(base: Settings) -> tuple[Settings | None, str | None]:
    """What a restarted serve would load: the same file, or the same search. Never
    raises: a file that no longer loads comes back as its error."""
    try:
        return load_settings(base.source_path), None
    except ConfigError as exc:
        return None, str(exc)


def _now_busy(request: Request) -> list[str]:
    """:func:`_busy`, from a sync route (a worker thread), only where serve could restart."""
    if _hook(request) is None:
        return []
    return restart_gate.stored_busy(request.app.state.session_factory) + _live_busy(request.app)


def _out(request: Request, session: SessionDep, user: User) -> ConfigOut:
    base = file_settings(request)
    started: Settings | None = getattr(request.app.state, "started_settings", None)
    views = ui_settings.describe(base, ui_settings.stored(session, user), started=started)
    on_disk, error = _on_disk(base)
    changed = error is not None or on_disk != ui_settings.unresolve(base)
    gate: restart_gate.RestartGate | None = getattr(request.app.state, "restart_gate", None)
    return ConfigOut(
        config_path=None if base.source_path is None else str(base.source_path),
        log_file=str(log_file()),
        config_changed=changed,
        config_error=error,
        restart_pending=changed or any(view.restart_pending for view in views),
        can_restart=_hook(request) is not None,
        busy=_now_busy(request),
        restart_waiting=gate is not None and gate.waiting,
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
    base = file_settings(request)
    try:
        ui_settings.write(session, user, base, body.values)
    except ui_settings.SettingsRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # The log file is this process's: new rotation limits apply from the next record.
    logs = ui_settings.resolve(session, user, base).logs
    set_log_rotation(max_mb=logs.max_mb, keep=logs.keep)
    return _out(request, session, user)


class RestartIn(BaseModel):
    when_idle: bool = False
    """Wait for what is running to finish, then restart, instead of refusing (#597)."""


def _hook(request: Request) -> RestartHook | None:
    hook: RestartHook | None = getattr(request.app.state, "restarter", None)
    return hook


def _live_busy(app: FastAPI) -> list[str]:
    state = app.state
    return restart_gate.live_busy(
        getattr(state, "tasks", None),
        getattr(state, "campaign_engine", None),
        getattr(state, "nightly_backups", None),
    )


async def _busy(app: FastAPI) -> list[str]:
    stored = await asyncio.to_thread(restart_gate.stored_busy, app.state.session_factory)
    return stored + _live_busy(app)


async def _quiesce(app: FastAPI) -> None:
    """No new scheduled work: the scheduler and the nightly backup pause, and the campaign
    engine stops after the tick in flight, its send included. The lifespan's teardown
    stops them for good."""
    state = app.state
    scheduler = getattr(state, "scheduler", None)
    if scheduler is not None and scheduler.running:
        scheduler.pause()
    backups = getattr(state, "nightly_backups", None)
    if backups is not None:
        backups.pause()
    engine = getattr(state, "campaign_engine", None)
    if engine is not None:
        await engine.stop()


def _resume(app: FastAPI) -> None:
    """Undo :func:`_quiesce`, when a restart waiting for idle is cancelled."""
    state = app.state
    scheduler = getattr(state, "scheduler", None)
    if scheduler is not None and scheduler.running:
        scheduler.resume()
    backups = getattr(state, "nightly_backups", None)
    if backups is not None:
        backups.resume()
    engine = getattr(state, "campaign_engine", None)
    if engine is not None:
        engine.start()


def _gate(request: Request, hook: RestartHook) -> restart_gate.RestartGate:
    gate: restart_gate.RestartGate | None = getattr(request.app.state, "restart_gate", None)
    if gate is None:
        app: FastAPI = request.app
        gate = restart_gate.RestartGate(
            hook,
            busy=lambda: _busy(app),
            quiesce=lambda: _quiesce(app),
            resume=lambda: _resume(app),
        )
        request.app.state.restart_gate = gate
    return gate


@router.post(
    "/settings/restart",
    operation_id="restart_serve",
    status_code=202,
    responses={
        409: {
            "description": (
                "This process cannot restart, config.toml does not load, or something is"
                " running (a LinkedIn run, the browser, a task, a campaign send)"
            )
        }
    },
)
async def restart_serve(
    request: Request, user: CurrentUser, body: RestartIn | None = None
) -> RestartOut:
    """Restart ``netkeeper serve`` so settings it reads at startup apply, never in the
    middle of work: ``409`` names what is running, unless ``when_idle`` asks to wait for
    it. ``user`` is unused; every route resolves the current user."""
    hook = _hook(request)
    if hook is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "this netkeeper cannot restart itself (netkeeper serve --reload, or not"
                " netkeeper serve); stop it and start it again"
            ),
        )
    _, error = _on_disk(file_settings(request))
    if error is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{error}. Fix config.toml first: netkeeper serve would not start with it",
        )
    gate = _gate(request, hook)
    when_idle = body is not None and body.when_idle
    reasons = await _busy(request.app)
    if reasons and not when_idle and not gate.waiting:
        raise HTTPException(
            status_code=409,
            detail=f"netkeeper is busy: {'; '.join(reasons)}. Restart when it is idle, or wait",
        )
    gate.waiting_on = reasons
    gate.start()
    return RestartOut(mode=hook.mode, boot_id=BOOT_ID, waiting_on=reasons)


class RestartCancelOut(BaseModel):
    cancelled: bool
    """True: the wait stopped, and scheduled runs and sends go on. False: nothing waited."""


@router.delete(
    "/settings/restart",
    operation_id="cancel_restart",
    responses={409: {"description": "The restart has already begun"}},
)
async def cancel_restart(request: Request, user: CurrentUser) -> RestartCancelOut:
    """Cancel a restart that waits for idle (**Cancel** on the banner). ``user`` is
    unused; every route resolves the current user."""
    gate: restart_gate.RestartGate | None = getattr(request.app.state, "restart_gate", None)
    hook = _hook(request)
    if hook is not None and hook.requested:
        raise HTTPException(status_code=409, detail="netkeeper is already restarting")
    return RestartCancelOut(cancelled=gate is not None and await gate.cancel())
