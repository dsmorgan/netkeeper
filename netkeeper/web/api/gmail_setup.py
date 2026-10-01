"""``/gmail-setup``: the guided Gmail setup's progress (#302).

The Settings → Gmail wizard keeps the project ID its console links name, the
address the user sends from, and which console steps the user marked done. See
:mod:`netkeeper.services.gmail_setup` for what is and isn't stored, and why
netkeeper never runs ``gcloud`` itself.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from netkeeper.services import gmail_setup as service
from netkeeper.web.deps import CurrentUser, SessionDep

router = APIRouter(tags=["gmail-setup"])

INVALID: dict[int | str, dict[str, Any]] = {
    422: {"description": "A project ID, address or step the wizard can't use"}
}


class GmailSetupIn(BaseModel):
    """The whole of the wizard's progress; a ``PUT`` replaces what was stored."""

    project_id: str | None = Field(default=None, max_length=100)
    sender_email: str | None = Field(default=None, max_length=320)
    done: list[str] = Field(default_factory=list, max_length=len(service.MANUAL_STEPS))


class GmailSetupOut(BaseModel):
    project_id: str | None
    """Your Google Cloud project's ID, which the wizard's console links name."""
    sender_email: str | None
    """The Gmail address you send from: the support email and the one test user."""
    done: list[str]
    """The console steps you marked done, in the wizard's order."""
    steps: list[str]
    """Every step that can be marked done, in order."""


def _out(setup: service.GmailSetup) -> GmailSetupOut:
    return GmailSetupOut(
        project_id=setup.project_id,
        sender_email=setup.sender_email,
        done=list(setup.done),
        steps=list(service.MANUAL_STEPS),
    )


@router.get("/gmail-setup")
def get_gmail_setup(session: SessionDep, user: CurrentUser) -> GmailSetupOut:
    """Where the wizard is: the project, the sending address, the steps marked done."""
    return _out(service.load(session, user))


@router.put("/gmail-setup", responses=INVALID)
def put_gmail_setup(body: GmailSetupIn, session: SessionDep, user: CurrentUser) -> GmailSetupOut:
    """Replace the wizard's progress; answers it as stored (normalized, in order)."""
    try:
        setup = service.validate(body.project_id, body.sender_email, body.done)
    except service.InvalidSetup as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _out(service.save(session, user, setup))
