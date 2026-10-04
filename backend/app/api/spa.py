"""Serve the built frontend from the API process (same origin: no CORS, one port)."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

_API_PREFIXES = ("api/", "metrics")


class SPAStaticFiles(StaticFiles):
    """Static files with client-side-routing fallback (deep links return index.html)."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code == 404 and not path.replace("\\", "/").startswith(_API_PREFIXES):
                return await super().get_response("index.html", scope)
            raise


def default_dist_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "frontend" / "dist"


def mount_frontend(app: FastAPI, dist: Path | None = None) -> bool:
    """Mount the built UI at ``/`` if it exists. Returns whether it was mounted."""
    directory = dist or default_dist_dir()
    if not (directory / "index.html").is_file():
        return False
    app.mount("/", SPAStaticFiles(directory=directory, html=True), name="frontend")
    return True
