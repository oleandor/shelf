"""`GET /api/attachments/resolve` — the session twin of the v1 lookup.

The SPA's `/open?sha256=…` route stands on this, so what matters here is
visibility: a signed-in user finds a file in any space whose items they
can read, and nothing else — another user's private copy must not
resolve, however exact the hash.
"""

import hashlib
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from obstore.store import MemoryStore

from shelf.config import settings
from shelf.services import storage

from .helpers import login

PRIVATE_PDF = b"%PDF-1.7\nalice's own copy\n"
PRIVATE_SHA = hashlib.sha256(PRIVATE_PDF).hexdigest()
SHARED_PDF = b"%PDF-1.7\na shared standard\n"
SHARED_SHA = hashlib.sha256(SHARED_PDF).hexdigest()

Library = dict[str, Any]

EDITION = {"body": "NX Standards", "designation": "NX-ACME 1234", "label": "2020"}


@pytest.fixture(autouse=True)
def memory_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """In-memory bucket; presigning stubbed since a MemoryStore can't."""
    storage._store = MemoryStore()

    async def fake_presign_upload(key: str, *_a: object, **_kw: object) -> str:
        return f"http://memory.invalid/{key}"

    monkeypatch.setattr(storage, "presign_upload", fake_presign_upload)
    yield
    storage.reset_store()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _upload(
    client: AsyncClient, token: str, data: bytes, space_slug: str | None = None
) -> dict[str, dict[str, str]]:
    form = {"title": "A document"}
    if space_slug is not None:
        form["space_slug"] = space_slug
    r = await client.post(
        "/api/v1/upload",
        headers=_auth(token),
        files={"file": ("doc.pdf", data, "application/pdf")},
        data=form,
    )
    assert r.status_code == 201, r.text
    return dict(r.json())


@pytest.fixture
async def library(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> Library:
    """Alice holds one PDF in her personal space and another in a space
    Bob can view. Leaves the client signed in as Alice."""
    await login(client, "bob@example.com")

    monkeypatch.setattr(settings, "admin_emails", ["alice@example.com"])
    await login(client, "alice@example.com")
    token = str(
        (
            await client.post(
                "/api/me/tokens",
                json={"name": "importer", "scopes": ["upload", "search"]},
            )
        ).json()["plaintext"]
    )
    r = await client.post("/api/spaces", json={"name": "Shared", "slug": "shared"})
    assert r.status_code == 201, r.text
    r = await client.post(
        "/api/spaces/shared/members",
        json={"email": "bob@example.com", "role": "viewer"},
    )
    assert r.status_code == 201, r.text

    private = await _upload(client, token, PRIVATE_PDF)
    shared = await _upload(client, token, SHARED_PDF, space_slug="shared")
    return {"token": token, "private": private, "shared": shared}


async def _resolve(client: AsyncClient, **params: str) -> list[dict[str, str]]:
    r = await client.get("/api/attachments/resolve", params=params)
    assert r.status_code == 200, r.text
    return list(r.json()["attachments"])


# ── By hash ──────────────────────────────────────────────────────────────


async def test_owner_finds_their_file_by_hash(
    client: AsyncClient, library: Library
) -> None:
    found = await _resolve(client, sha256=PRIVATE_SHA)
    assert found == [
        {
            "attachment_id": library["private"]["attachment"]["id"],
            "filename": "doc.pdf",
            "item_id": library["private"]["item"]["id"],
            "space_id": library["private"]["item"]["space_id"],
        }
    ]


async def test_another_users_private_file_does_not_resolve(
    client: AsyncClient, library: Library
) -> None:
    await login(client, "bob@example.com")
    assert await _resolve(client, sha256=PRIVATE_SHA) == []


async def test_a_file_in_a_space_the_user_can_view_resolves(
    client: AsyncClient, library: Library
) -> None:
    await login(client, "bob@example.com")
    found = await _resolve(client, sha256=SHARED_SHA)
    assert [a["attachment_id"] for a in found] == [
        library["shared"]["attachment"]["id"]
    ]


async def test_an_uppercase_digest_resolves(
    client: AsyncClient, library: Library
) -> None:
    assert len(await _resolve(client, sha256=PRIVATE_SHA.upper())) == 1


async def test_an_unknown_file_is_empty_not_404(
    client: AsyncClient, library: Library
) -> None:
    assert await _resolve(client, sha256="0" * 64) == []


async def test_a_deleted_item_does_not_resolve(
    client: AsyncClient, library: Library
) -> None:
    r = await client.delete(f"/api/items/{library['private']['item']['id']}")
    assert r.status_code == 204, r.text
    assert await _resolve(client, sha256=PRIVATE_SHA) == []


async def test_an_upload_that_never_completed_does_not_resolve(
    client: AsyncClient, library: Library
) -> None:
    """The presigned path records a declared hash before the bytes
    arrive; until they do there is nothing for a reader to open."""
    pending = b"%PDF-1.7\nnot here yet\n"
    r = await client.post(
        "/api/v1/uploads/register",
        headers=_auth(library["token"]),
        json={
            "filename": "later.pdf",
            "content_type": "application/pdf",
            "size_bytes": len(pending),
            "title": "Pending",
            "sha256": hashlib.sha256(pending).hexdigest(),
        },
    )
    assert r.status_code == 201, r.text
    assert await _resolve(client, sha256=hashlib.sha256(pending).hexdigest()) == []


async def test_narrowing_to_a_space(
    client: AsyncClient, library: Library
) -> None:
    assert len(await _resolve(client, sha256=SHARED_SHA, space="shared")) == 1
    assert await _resolve(client, sha256=PRIVATE_SHA, space="shared") == []


async def test_narrowing_to_a_space_the_user_cannot_read_is_404(
    client: AsyncClient, library: Library
) -> None:
    await login(client, "carol@example.com")
    r = await client.get(
        "/api/attachments/resolve", params={"sha256": SHARED_SHA, "space": "shared"}
    )
    assert r.status_code == 404


async def test_signed_out_is_401(
    client: AsyncClient, library: Library
) -> None:
    client.cookies.clear()
    r = await client.get("/api/attachments/resolve", params={"sha256": SHARED_SHA})
    assert r.status_code == 401


async def test_the_route_does_not_shadow_a_single_attachment(
    client: AsyncClient, library: Library
) -> None:
    att_id = library["private"]["attachment"]["id"]
    r = await client.get(f"/api/attachments/{att_id}")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == att_id


# ── By standard edition ──────────────────────────────────────────────────


async def test_an_edition_resolves_to_its_attachment(
    client: AsyncClient, library: Library
) -> None:
    item_id = library["shared"]["item"]["id"]
    r = await client.put(f"/api/items/{item_id}/revision", json=EDITION)
    assert r.status_code == 200, r.text

    await login(client, "bob@example.com")
    found = await _resolve(
        client, body="nx standards", designation="nx-acme 1234", label="2020"
    )
    assert [a["attachment_id"] for a in found] == [
        library["shared"]["attachment"]["id"]
    ]


async def test_another_users_private_edition_does_not_resolve(
    client: AsyncClient, library: Library
) -> None:
    item_id = library["private"]["item"]["id"]
    r = await client.put(f"/api/items/{item_id}/revision", json=EDITION)
    assert r.status_code == 200, r.text

    await login(client, "bob@example.com")
    assert await _resolve(client, **EDITION) == []


async def test_the_label_is_matched_exactly(
    client: AsyncClient, library: Library
) -> None:
    item_id = library["shared"]["item"]["id"]
    await client.put(f"/api/items/{item_id}/revision", json=EDITION)
    assert await _resolve(client, **{**EDITION, "label": "20"}) == []


# ── Malformed requests ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"sha256": "not-a-hash"},
        {"body": "NX Standards", "designation": "NX-ACME 1234"},
        {"body": "NX Standards", "designation": "NX-ACME 1234", "label": " "},
        {"sha256": PRIVATE_SHA, **EDITION},
    ],
    ids=["no-key", "bad-digest", "partial-edition", "blank-label", "both-keys"],
)
async def test_a_request_without_exactly_one_key_is_422(
    client: AsyncClient, library: Library, params: dict[str, str]
) -> None:
    r = await client.get("/api/attachments/resolve", params=params)
    assert r.status_code == 422, r.text
