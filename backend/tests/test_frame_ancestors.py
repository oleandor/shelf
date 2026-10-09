"""`SHELF_FRAME_ANCESTORS`: a framing policy on HTML, or nothing at all.

The middleware is tested on a three-route Starlette app of its own. Only
the wiring in `shelf.main` — installed when set, absent when not — needs
the real app, which reads the setting at import time and so has to be
re-imported, with the SPA mounted from a temp dir as in test_spa_fallback.
"""

import importlib
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from shelf.config import Settings
from shelf.frame_ancestors import FrameAncestorsMiddleware, header_value

POLICY = "frame-ancestors 'self' https://viewer.example.com"


# ── The middleware ───────────────────────────────────────────────────────


def _page(_: Request) -> Response:
    return HTMLResponse("<!doctype html><title>Shelf</title>")


def _shouting_page(_: Request) -> Response:
    """Media types are case-insensitive; a proxy or framework that
    capitalises one must not slip a page past the policy."""
    return Response("<!doctype html>", headers={"Content-Type": "Text/HTML"})


def _data(_: Request) -> Response:
    return JSONResponse({"ok": True})


@pytest.fixture
def middleware_client() -> TestClient:
    app = Starlette(
        routes=[
            Route("/page", _page),
            Route("/shouting", _shouting_page),
            Route("/data", _data),
        ]
    )
    app.add_middleware(
        FrameAncestorsMiddleware, sources=["'self'", "https://viewer.example.com"]
    )
    return TestClient(app)


@pytest.mark.parametrize("path", ["/page", "/shouting"])
def test_html_carries_the_policy(middleware_client: TestClient, path: str) -> None:
    r = middleware_client.get(path)
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == POLICY


def test_other_responses_are_left_alone(middleware_client: TestClient) -> None:
    """Only a document can be framed; the header means nothing elsewhere."""
    r = middleware_client.get("/data")
    assert r.status_code == 200
    assert "content-security-policy" not in r.headers


# ── The wiring in shelf.main ─────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _restore_shelf_modules() -> Iterator[None]:
    """Put the original `shelf` modules back afterwards.

    `_fresh_app` re-imports the package to build an app with other
    settings. Left in place, those copies would be what later test files
    monkeypatch — `shelf.services.storage` and the like — while the app
    conftest imported keeps using the originals, and they'd fail far from
    here.
    """
    saved = {k: v for k, v in sys.modules.items() if k.startswith("shelf")}
    yield
    for mod in [m for m in list(sys.modules) if m.startswith("shelf")]:
        sys.modules.pop(mod, None)
    sys.modules.update(saved)


def _fresh_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frame_ancestors: str | None
) -> TestClient:
    (tmp_path / "index.html").write_text("<!doctype html><title>Shelf</title>")
    monkeypatch.setenv("SHELF_FRONTEND_DIR", str(tmp_path))
    if frame_ancestors is None:
        monkeypatch.delenv("SHELF_FRAME_ANCESTORS", raising=False)
    else:
        monkeypatch.setenv("SHELF_FRAME_ANCESTORS", frame_ancestors)
    for mod in [m for m in list(sys.modules) if m.startswith("shelf")]:
        sys.modules.pop(mod, None)
    main = importlib.import_module("shelf.main")
    return TestClient(main.app)


def test_the_app_sends_the_policy_on_its_pages_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _fresh_app(tmp_path, monkeypatch, "'self' https://viewer.example.com")
    assert client.get("/reader/abc?page=3").headers["content-security-policy"] == (
        POLICY
    )
    assert "content-security-policy" not in client.get("/api").headers


@pytest.mark.parametrize("value", [None, ""])
def test_unset_the_app_sends_no_framing_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    """Unset is the behaviour from before the setting existed."""
    r = _fresh_app(tmp_path, monkeypatch, value).get("/")
    assert r.status_code == 200
    assert "content-security-policy" not in r.headers
    assert "x-frame-options" not in r.headers


# ── Parsing and validation ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "'self' https://viewer.example.com",
        '["\'self\'", "https://viewer.example.com"]',
    ],
    ids=["csp-string", "json-list"],
)
def test_both_spellings_parse_to_the_same_list(raw: str) -> None:
    assert Settings(frame_ancestors=raw).frame_ancestors == [
        "'self'",
        "https://viewer.example.com",
    ]


@pytest.mark.parametrize("raw", ["", "  "])
def test_blank_is_unset(raw: str) -> None:
    assert Settings(frame_ancestors=raw).frame_ancestors == []


@pytest.mark.parametrize(
    "value",
    [
        "'self'",
        "'none'",
        "*",
        "https:",
        "https://viewer.example.com",
        "https://*.example.com:8443",
        "http://localhost:5173",
        "viewer.example.com/embed/",
        "'self' https://a.example.com https://b.example.com",
    ],
)
def test_source_expressions_are_accepted(value: str) -> None:
    s = Settings(frame_ancestors=value)
    assert header_value(s.frame_ancestors) == f"frame-ancestors {value}"


@pytest.mark.parametrize(
    "value",
    [
        "self",  # bare: CSP would read it as a host named "self"
        "none",
        "https://a.example.com;script-src *",  # a second directive
        "https://a.example.com,https://b.example.com",  # a second policy
        "'unsafe-inline'",
        ["https://a.example.com\r\nX-Injected: 1"],  # a second header
        ["https://a.example.com "],
        "'none' https://a.example.com",
    ],
)
def test_anything_else_fails_at_startup(value: str | list[str]) -> None:
    with pytest.raises(ValidationError):
        Settings(frame_ancestors=value)
