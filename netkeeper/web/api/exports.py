"""``/exports``: CSV, JSON, and vCard downloads of the current filter (spec 10.6).

An export streams from the database as the response sends, so it depends on
:data:`netkeeper.web.deps.StreamingSessionDep` rather than the ordinary
per-request :data:`~netkeeper.web.deps.SessionDep` (see that dependency's
docstring for why: the short version is a leaked connection per request, not a
closed-session error). The whole point of streaming here is to write rows to
the client as the query yields them, without materializing the file or the
result set in memory (:mod:`netkeeper.crm.exports`). It is always a read.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from netkeeper.crm.exports import (
    MEDIA_TYPES,
    ExportFormat,
    ExportPreset,
    export_stream,
    filename_for,
)
from netkeeper.crm.filters import FilterError, FilterTree, SortKey, parse_filter, parse_sort
from netkeeper.web.deps import CurrentUser, StreamingSessionDep

router = APIRouter(tags=["exports"])

FilterParam = Annotated[
    str | None,
    Query(alias="filter", description="A FilterTree (spec 10.4) as JSON. Omitted: every contact."),
]
SortParam = Annotated[
    str | None,
    Query(alias="sort", description="A list of SortKey as JSON. Omitted: id ascending."),
]
FormatParam = Annotated[ExportFormat, Query(alias="format")]


@router.get(
    "/exports",
    operation_id="export_contacts",
    summary="Export contacts as CSV, JSON, or vCard",
    responses={
        200: {
            "description": "The exported file, streamed.",
            "content": {
                "application/json": {"schema": {"type": "array", "items": {"type": "object"}}},
                "text/csv": {"schema": {"type": "string"}},
                "text/vcard": {"schema": {"type": "string"}},
            },
        },
        422: {"description": "An invalid filter or sort"},
    },
)
def export_contacts(
    user: CurrentUser,
    session: StreamingSessionDep,
    preset: ExportPreset = "full",
    output_format: FormatParam = "json",
    headerless: bool = False,
    filter_: FilterParam = None,
    sort: SortParam = None,
) -> StreamingResponse:
    """Stream ``preset`` in ``format`` for the contacts ``filter`` selects, in ``sort`` order.

    ``headerless`` drops the CSV header row (the mailing-tool variant of
    ``nine-column``; harmless, if unusual, on the other presets) and is ignored
    by the other two formats.
    """
    tree = _parse_filter(filter_)
    sort_keys = _parse_sort(sort)
    now = datetime.now(UTC)
    body = export_stream(
        session,
        user,
        preset=preset,
        output_format=output_format,
        headerless=headerless,
        tree=tree,
        sort=sort_keys,
        now=now,
    )
    response = StreamingResponse(body, media_type=MEDIA_TYPES[output_format])
    response.headers["Content-Disposition"] = (
        f'attachment; filename="{filename_for(preset, output_format)}"'
    )
    return response


def _parse_filter(raw: str | None) -> FilterTree:
    if raw is None:
        return FilterTree()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"filter is not valid JSON: {exc}") from None
    try:
        return parse_filter(data)
    except FilterError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _parse_sort(raw: str | None) -> list[SortKey]:
    if raw is None:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"sort is not valid JSON: {exc}") from None
    try:
        return parse_sort(data)
    except FilterError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
