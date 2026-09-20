"""Serving the built frontend (spec section 5: the SPA is served as a static build).

The build is ``frontend/dist`` at the repository root, or ``$NETKEEPER_FRONTEND_DIST``.
When it exists, ``/assets`` is served as static files and every other GET that is
not under ``/api/`` and names no file gets ``index.html`` (the SPA routes client
side). When it does not, ``/`` explains how to build it.
"""

from __future__ import annotations

import html
import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from starlette.responses import FileResponse, HTMLResponse, Response
from starlette.staticfiles import StaticFiles

log = logging.getLogger(__name__)

FRONTEND_DIST_ENV = "NETKEEPER_FRONTEND_DIST"
DEFAULT_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
API_ROOT = "api"

_NOT_BUILT = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>netkeeper</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 40rem; margin: 4rem auto;
             line-height: 1.5">
<h1>netkeeper</h1>
<p>The frontend is not built: <code>{dist}</code> has no <code>index.html</code>.</p>
<p>Build it with <code>make build-ui</code> at the repository root, then reload this page.
For development run <code>pnpm dev</code> in <code>frontend/</code> and open
<a href="http://localhost:5173">http://localhost:5173</a> instead.</p>
<p>The API is up: <a href="/api/v1/health">/api/v1/health</a>, documented at
<a href="/docs">/docs</a>.</p>
</body>
</html>
"""


def frontend_dist() -> Path:
    """Where the frontend build is expected: ``$NETKEEPER_FRONTEND_DIST`` or ``frontend/dist``."""
    override = os.environ.get(FRONTEND_DIST_ENV)
    return Path(override).expanduser() if override else DEFAULT_DIST


def mount_frontend(app: FastAPI, dist: Path) -> None:
    """Add the static mount and the SPA fallback, or the not-built page. Call last."""
    index = dist / "index.html"
    if not index.is_file():
        log.info("frontend build not found at %s; serving the not-built page", dist)
        _mount_placeholder(app, dist)
        return
    log.info("serving the frontend build from %s", dist)
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> Response:
        _refuse_api(path)
        if Path(path).suffix:
            target = _inside(dist, path)
            if target is None or not target.is_file():
                raise HTTPException(status_code=404)
            return FileResponse(target)
        return FileResponse(index)


def _mount_placeholder(app: FastAPI, dist: Path) -> None:
    page = _NOT_BUILT.format(dist=html.escape(str(dist)))

    @app.get("/{path:path}", include_in_schema=False)
    def not_built(path: str) -> Response:
        _refuse_api(path)
        if Path(path).suffix:
            raise HTTPException(status_code=404)
        return HTMLResponse(page)


def _refuse_api(path: str) -> None:
    """An unknown ``/api/...`` path is a 404, never the SPA page."""
    if path == API_ROOT or path.startswith(API_ROOT + "/"):
        raise HTTPException(status_code=404)


def _inside(root: Path, relative: str) -> Path | None:
    """``root / relative`` if it stays inside ``root`` after resolving, else None."""
    base = root.resolve()
    candidate = (base / relative).resolve()
    return candidate if candidate.is_relative_to(base) else None
