"""Verifies the SPA fallback route registers cleanly and serves index.html.

The other tests run with SHELF_FRONTEND_DIR pointing at a path that
doesn't exist, so the catch-all route is skipped — meaning a route-time
typing/validation bug can land without any test failing locally. This
test exercises the mounted state by spinning up a fresh app with a
temp dir on disk.
"""

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_with_spa(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    (tmp_path / "index.html").write_text("<!doctype html><title>Shelf</title>")
    (tmp_path / "favicon.svg").write_text("<svg/>")
    monkeypatch.setenv("SHELF_FRONTEND_DIR", str(tmp_path))
    # Force a fresh import so the mount picks up the new env var (settings
    # caches the value at module-load time).
    for mod in [m for m in list(sys.modules) if m.startswith("shelf")]:
        sys.modules.pop(mod, None)
    main = importlib.import_module("shelf.main")
    return main.app


def test_spa_root_serves_index(app_with_spa: object) -> None:
    client = TestClient(app_with_spa)  # type: ignore[arg-type]
    r = client.get("/")
    assert r.status_code == 200
    assert "Shelf" in r.text


def test_spa_passes_through_static_file(app_with_spa: object) -> None:
    client = TestClient(app_with_spa)  # type: ignore[arg-type]
    r = client.get("/favicon.svg")
    assert r.status_code == 200
    assert "<svg" in r.text


@pytest.mark.parametrize(
    "path",
    [
        "/library",
        # Nested client routes too — the settings tabs are route segments,
        # so a reload on one has to reach index.html rather than 404.
        "/settings/account",
        "/settings/admin",
        "/settings/spaces",
        "/reader/00000000-0000-0000-0000-000000000000",
        # An annotation deep link is a reader route with a query string;
        # the fallback must serve it rather than 404 on the whole URL.
        "/reader/00000000-0000-0000-0000-000000000000"
        "?annotation=11111111-1111-1111-1111-111111111111",
        # A document opened by file hash rather than id.
        f"/open?sha256={'ab' * 32}&page=57&find=Table%207-1",
    ],
)
def test_spa_falls_back_to_index_for_client_routes(
    app_with_spa: object, path: str
) -> None:
    client = TestClient(app_with_spa)  # type: ignore[arg-type]
    r = client.get(path)
    assert r.status_code == 200
    assert "Shelf" in r.text


def test_spa_404s_for_unknown_api_path(app_with_spa: object) -> None:
    client = TestClient(app_with_spa)  # type: ignore[arg-type]
    r = client.get("/api/does-not-exist")
    assert r.status_code == 404
