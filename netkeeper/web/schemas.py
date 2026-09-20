"""Pydantic response models for the API. The TypeScript client is generated from these."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from netkeeper.models import UserKind
from netkeeper.services.tasks import TaskStatus


class HealthOut(BaseModel):
    status: Literal["ok"]
    version: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: UserKind
    display_name: str | None
    email: str | None
    timezone: str


class TaskAccepted(BaseModel):
    """The ``202`` body of every route that enqueues work and returns."""

    task_id: str


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    status: TaskStatus
    error: str | None
    created_at: datetime
    finished_at: datetime | None
