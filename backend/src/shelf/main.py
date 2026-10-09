import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from .api import (
    admin,
    annotations,
    archive_import,
    attachments,
    auth,
    collections,
    export,
    extraction,
    inheritance,
    item_copy,
    items,
    me,
    members,
    notes,
    processing,
    profiles,
    spaces,
    standards,
    tags,
    tokens,
    users,
    v1,
    worker_api,
)
from .config import settings
from .frame_ancestors import FrameAncestorsMiddleware
from .preflight import PreflightError, run_readiness_checks, run_startup_checks

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Verify dependencies before accepting traffic.

    Refusing to start is the point: an instance that cannot reach its
    database, bucket or OIDC provider has no working request path, and a
    container that exits with the reason in its logs is far easier to
    diagnose than one that serves 200s and fails in a browser.
    """
    if settings.preflight_enabled:
        try:
            await run_startup_checks()
        except PreflightError as exc:
            logger.error("preflight failed: %s", exc)
            raise
    yield


app = FastAPI(title="Shelf", version=__version__, lifespan=lifespan)

# Required by authlib's OIDC code-flow client to stash PKCE/state across
# the redirect. Separate from the application's own session JWT cookie —
# this one is short-lived and only active during the OAuth handshake.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret_key,
    https_only=settings.session_cookie_secure,
    same_site="lax",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Only when configured: unset, no framing header is sent and responses are
# exactly as before. See frame_ancestors.py.
if settings.frame_ancestors:
    app.add_middleware(FrameAncestorsMiddleware, sources=settings.frame_ancestors)

app.include_router(auth.router)
app.include_router(me.router)
app.include_router(admin.router)
app.include_router(items.router)
app.include_router(item_copy.router)
app.include_router(spaces.router)
app.include_router(profiles.router)
app.include_router(inheritance.router)
app.include_router(standards.router)
app.include_router(members.router)
app.include_router(users.router)
app.include_router(collections.router)
app.include_router(attachments.router)
app.include_router(annotations.router)
app.include_router(export.router)
app.include_router(archive_import.router)
app.include_router(extraction.router)
app.include_router(notes.router)
app.include_router(processing.router)
app.include_router(tags.router)
app.include_router(tokens.router)
app.include_router(v1.router)
app.include_router(worker_api.router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness only: is the process up. Deliberately checks nothing else,
    so a dependency outage restarts nothing. Use /readyz for traffic."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> JSONResponse:
    """Readiness: can this instance actually serve a request.

    Re-runs the cheap startup checks, so a dependency that disappears
    after boot takes the instance out of rotation instead of leaving it
    advertised as healthy while every request fails.
    """
    try:
        results = await run_readiness_checks()
    except PreflightError as exc:
        return JSONResponse(
            {"status": "unready", "detail": str(exc)}, status_code=503
        )
    return JSONResponse(
        {
            "status": "ok",
            "checks": {r.name: r.detail for r in results},
        }
    )


@app.get("/api")
async def api_root() -> dict[str, str]:
    return {
        "name": "shelf",
        "version": __version__,
        "image_tag": settings.image_tag,
    }


# ── SPA hosting ──────────────────────────────────────────────────────────────
# In production the built frontend lives at /app/frontend (configured via
# SHELF_FRONTEND_DIR in the container). In dev the SPA runs separately on
# vite's :5173 and proxies /api + /auth back here, so we just skip the
# mount when the dist directory is absent.

_frontend_dir = Path(settings.frontend_dir)
if _frontend_dir.is_dir():
    _assets_dir = _frontend_dir / "assets"
    if _assets_dir.is_dir():
        app.mount(
            "/assets", StaticFiles(directory=_assets_dir), name="frontend-assets"
        )

    _index = _frontend_dir / "index.html"

    # response_model=None — FastAPI otherwise tries to build a Pydantic
    # field from the union return type and chokes on FileResponse, which
    # isn't a Pydantic model.
    @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
    async def spa_fallback(
        full_path: str, request: Request
    ) -> FileResponse | JSONResponse:
        # Anything that looks like an API/auth route should 404 cleanly
        # rather than silently fall through to index.html.
        if full_path.startswith(("api/", "auth/", "health", "readyz")):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        # Pass static files (favicon, robots.txt, etc.) through if present.
        candidate = _frontend_dir / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_index)
