"""`SHELF_FRAME_ANCESTORS`: a framing policy on HTML, or nothing at all.

The app is built fresh per case with the SPA mounted from a temp dir, as
in test_spa_fallback, since both the setting and the middleware are read
at import time.
"""

import importlib
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from shelf.frame_ancestors import header_value


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
    (tmp_path / "favicon.svg").write_text("<svg/>")
    monkeypatch.setenv("SHELF_FRONTEND_DIR", str(tmp_path))
    if frame_ancestors is None:
        monkeypatch.delenv("SHELF_FRAME_ANCESTORS", raising=False)
    else:
        monkeypatch.setenv("SHELF_FRAME_ANCESTORS", frame_ancestors)
    for mod in [m for m in list(sys.modules) if m.startswith("shelf")]:
        sys.modules.pop(mod, None)
    main = importlib.import_module("shelf.main")
    return TestClient(main.app)


@pytest.mark.parametrize("path", ["/", "/library", "/reader/abc?page=3"])
def test_html_carries_the_policy_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    client = _fresh_app(
        tmp_path, monkeypatch, "'self' https://viewer.example.com"
    )
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == (
        "frame-ancestors 'self' https://viewer.example.com"
    )


def test_a_json_list_works_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _fresh_app(
        tmp_path, monkeypatch, '["\'self\'", "https://viewer.example.com"]'
    )
    assert client.get("/").headers["content-security-policy"] == (
        "frame-ancestors 'self' https://viewer.example.com"
    )


@pytest.mark.parametrize("path", ["/api", "/health", "/favicon.svg"])
def test_non_html_responses_are_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Only a document can be framed; the header means nothing elsewhere."""
    client = _fresh_app(tmp_path, monkeypatch, "'self'")
    r = client.get(path)
    assert r.status_code == 200
    assert "content-security-policy" not in r.headers


@pytest.mark.parametrize("value", [None, "", "  "])
def test_unset_sends_no_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    """Unset is today's behaviour: no framing header of any kind."""
    client = _fresh_app(tmp_path, monkeypatch, value)
    r = client.get("/")
    assert r.status_code == 200
    assert "content-security-policy" not in r.headers
    assert "x-frame-options" not in r.headers


# ── Validation ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "'self'",
        "'none'",
        "https:",
        "https://viewer.example.com",
        "https://*.example.com:8443",
        "http://localhost:5173",
        "viewer.example.com/embed/",
        "'self' https://a.example.com https://b.example.com",
    ],
)
def test_source_expressions_are_accepted(value: str) -> None:
    from shelf.config import Settings

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
    from shelf.config import Settings

    with pytest.raises(ValidationError):
        Settings(frame_ancestors=value)
