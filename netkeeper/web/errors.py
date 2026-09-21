"""Error responses whose body carries more than ``{"detail": ...}``.

FastAPI's ``HTTPException`` always answers ``{"detail": <whatever was given>}``,
so a route that needs to say *why* with structured fields (the survivor of a
merged-away contact, the count a bulk action actually found) raises
:class:`ApiError` with the body it wants. :func:`install_error_handlers` wires
the handler on the app; the body is sent as given, so every module that raises
one documents its shape with a response model in ``responses``.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse


class ApiError(Exception):
    """An HTTP error with a JSON body of the caller's choosing.

    ``body`` should carry a ``detail`` string like every other error, plus the
    fields the client needs to act on the error.
    """

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(body.get("detail", f"HTTP {status_code}"))


async def _handle_api_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, ApiError)
    return JSONResponse(status_code=exc.status_code, content=exc.body)


def install_error_handlers(app: FastAPI) -> None:
    """Register the handler for :class:`ApiError` on ``app``."""
    app.add_exception_handler(ApiError, _handle_api_error)
