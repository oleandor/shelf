"""File attachment endpoints.

Upload flow (three steps, no proxy through the API):
  1. Client POSTs filename + content_type + size_bytes; backend creates
     the row, computes a storage key, and returns it together with a
     presigned PUT URL. `uploaded_at` is null at this point.
  2. Client uploads the file body straight to object storage with that
     URL.
  3. Client POSTs `/api/attachments/{id}/complete` so the backend
     stamps `uploaded_at`. Without this step the row stays pending
     forever; a periodic HEAD-check pass can prune true orphans.

Inline / proxy uploads (only used by /api/v1/upload) set
`uploaded_at` in the same transaction as the put_async call.
"""

import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import get_current_user
from ..auth.spaces import (
    SPACE_ROLE_EDITOR,
    SPACE_ROLE_VIEWER,
    readable_item_space_ids,
    require_space_role,
)
from ..db import get_session
from ..models import (
    Attachment,
    AttachmentDerivation,
    AttachmentPage,
    AttachmentProcessing,
    Item,
    Space,
    StandardFamily,
    StandardRevision,
    User,
)
from ..services import conversion, extraction, storage
from ..services.audit import AuditAction, record, record_read
from ..services.storage import attachment_storage_key
from .items import user_names

log = logging.getLogger(__name__)

router = APIRouter(tags=["attachments"])


class AttachmentRegister(BaseModel):
    filename: str
    content_type: str
    size_bytes: int | None = None


PdfStatus = Literal["native", "converted", "converting", "failed"]


class AttachmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    item_id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int | None
    uploaded_at: datetime | None = None
    # Who uploaded it, and their display name as it is now.
    created_by: uuid.UUID | None = None
    created_by_name: str | None = None
    # Whether the reader can open it. ``native`` for a PDF upload;
    # ``converted`` once a non-PDF upload has been rendered, with
    # ``converting`` / ``failed`` before that; None for a file shelf
    # doesn't render (it can only be downloaded).
    pdf_status: PdfStatus | None = None
    # Why the last conversion failed, while ``pdf_status`` is ``failed``.
    convert_error: str | None = None


async def attachment_responses(
    db: AsyncSession, atts: list[Attachment]
) -> list[AttachmentResponse]:
    """Serialise attachments with their uploader's name and PDF status —
    one lookup each for the whole list, not one per row."""
    names = await user_names(db, (a.created_by for a in atts))
    convertible = [a.id for a in atts if conversion.is_convertible(a)]
    converted = await conversion.latest_convert_keys(db, convertible)
    convert_state: dict[uuid.UUID, tuple[str, str | None]] = {}
    if convertible:
        convert_state = {
            aid: (st, err)
            for aid, st, err in (
                await db.execute(
                    select(
                        AttachmentProcessing.attachment_id,
                        AttachmentProcessing.convert_status,
                        AttachmentProcessing.convert_error,
                    ).where(AttachmentProcessing.attachment_id.in_(convertible))
                )
            ).tuples()
        }
    out = []
    for a in atts:
        r = AttachmentResponse.model_validate(a)
        if a.created_by is not None:
            r.created_by_name = names.get(a.created_by)
        if conversion.is_pdf(a.content_type, a.filename):
            r.pdf_status = "native"
        elif a.id in converted:
            # An earlier rendering still opens while a re-run is going.
            r.pdf_status = "converted"
        elif a.id in convertible:
            st, err = convert_state.get(a.id, ("untouched", None))
            r.pdf_status = "failed" if st == "failed" else "converting"
            r.convert_error = err if st == "failed" else None
        out.append(r)
    return out


class AttachmentRegisterResponse(BaseModel):
    attachment: AttachmentResponse
    upload_url: str


class AttachmentDownloadResponse(BaseModel):
    url: str


class AttachmentPageDimsRow(BaseModel):
    page: int
    width: float | None
    height: float | None


class AttachmentPageDimsResponse(BaseModel):
    """Per-page dimensions in PDF user-space (1 pt = 1/72 inch) at
    scale=1. The reader uses this to seed its virtualizer height map
    in one HTTP call instead of opening every page client-side at
    load time. Empty list when the extract worker hasn't run yet —
    the reader falls back to a sample-first-page baseline."""

    pages: list[AttachmentPageDimsRow]


class AttachmentProcessingResponse(BaseModel):
    """Quality assessment + downstream-worker state for one
    attachment. Phase A (current) only fills the metric / flag
    columns; the *_status fields stay 'untouched' until the OCR
    and outline workers ship."""

    model_config = ConfigDict(from_attributes=True)

    attachment_id: uuid.UUID
    assessed_at: datetime | None = None
    page_count: int | None = None
    text_chars: int | None = None
    replacement_char_ratio: float | None = None
    alpha_ratio: float | None = None
    chars_per_page: float | None = None
    toc_entry_count: int | None = None
    needs_ocr: bool = False
    needs_outline: bool = False
    ocr_status: str = "untouched"
    ocr_engine: str | None = None
    ocr_completed_at: datetime | None = None
    outline_status: str = "untouched"
    outline_engine: str | None = None
    outline_completed_at: datetime | None = None
    convert_status: str = "untouched"
    convert_engine: str | None = None
    convert_completed_at: datetime | None = None
    convert_error: str | None = None
    original_preserved_at: datetime | None = None
    progress_done: int | None = None
    progress_total: int | None = None


async def _resolve_item(
    db: AsyncSession,
    user: User,
    item_id: uuid.UUID,
    minimum: str = SPACE_ROLE_VIEWER,
) -> Item:
    item = await db.get(Item, item_id)
    if item is None or item.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    space = await db.get(Space, item.space_id)
    await require_space_role(db, space, user.id, minimum, label="Item not found")
    return item


async def _resolve_attachment(
    db: AsyncSession,
    user: User,
    attachment_id: uuid.UUID,
    minimum: str = SPACE_ROLE_VIEWER,
) -> Attachment:
    att, _item = await _resolve_attachment_and_item(db, user, attachment_id, minimum)
    return att


async def _resolve_attachment_and_item(
    db: AsyncSession,
    user: User,
    attachment_id: uuid.UUID,
    minimum: str = SPACE_ROLE_VIEWER,
) -> tuple[Attachment, Item]:
    """`_resolve_attachment`, plus the item — whose space the audit log
    files attachment events under."""
    att = await db.get(Attachment, attachment_id)
    if att is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    # Re-using the item resolver keeps the auth check in one place.
    item = await _resolve_item(db, user, att.item_id, minimum)
    return att, item


def _read_details(item: Item, version: str | None) -> dict[str, str]:
    details = {"item_id": str(item.id)}
    if version is not None:
        details["version"] = version
    return details


@router.get(
    "/api/items/{item_id}/attachments",
    response_model=list[AttachmentResponse],
)
async def list_attachments(
    item_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> list[AttachmentResponse]:
    await _resolve_item(db, user, item_id)
    result = await db.execute(
        select(Attachment)
        .where(Attachment.item_id == item_id)
        .order_by(Attachment.created_at)
    )
    return await attachment_responses(db, list(result.scalars().all()))


@router.post(
    "/api/items/{item_id}/attachments",
    response_model=AttachmentRegisterResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_attachment(
    item_id: uuid.UUID,
    payload: AttachmentRegister,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AttachmentRegisterResponse:
    item = await _resolve_item(db, user, item_id, SPACE_ROLE_EDITOR)
    att_id = uuid.uuid4()
    storage_key = attachment_storage_key(item.space_id, item.id, att_id)
    att = Attachment(
        id=att_id,
        item_id=item.id,
        storage_key=storage_key,
        filename=payload.filename,
        content_type=payload.content_type,
        size_bytes=payload.size_bytes,
        created_by=user.id,
    )
    db.add(att)
    await db.commit()
    await db.refresh(att)
    upload_url = await storage.presign_upload(storage_key)
    return AttachmentRegisterResponse(
        attachment=(await attachment_responses(db, [att]))[0],
        upload_url=upload_url,
    )


class AttachmentDerivationResponse(BaseModel):
    """One derived PDF the reader can switch to. ``parent_storage_key``
    is opaque to the SPA — used only as a stable identifier when the UI
    draws the lineage chain. The derivation's actual content is fetched
    via /download?version=<id>."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    parent_storage_key: str
    engine: str
    created_at: datetime


class AttachmentDerivationListResponse(BaseModel):
    """Returns every derivation plus a synthetic ``original`` row for
    the attachment's untouched bytes, so the UI builds the dropdown
    from a single response. ``current_version`` is the id of the row
    the reader's default-best resolution would pick (or "original" if
    no derivations exist)."""

    derivations: list[AttachmentDerivationResponse]
    current_version: str


async def resolve_version(
    db: AsyncSession,
    attachment: Attachment,
    version: str | None,
) -> tuple[str, str]:
    """Pick the storage key for the requested ``version``.

    ``version`` is one of:
      - None → current best (latest outline > latest ocr > latest
        convert > original)
      - "original" → ``attachments.storage_key``
      - a derivation UUID → that row's storage_key (404 on miss /
        wrong attachment)

    Returns ``(storage_key, version_label)`` where ``version_label`` is
    the same string format the SPA uses to identify the current view.
    """
    if version == "original":
        return attachment.storage_key, "original"
    if version is not None:
        try:
            vid = uuid.UUID(version)
        except ValueError as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "version must be 'original' or a derivation id",
            ) from e
        row = await db.get(AttachmentDerivation, vid)
        if row is None or row.attachment_id != attachment.id:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "Derivation not found"
            )
        return row.storage_key, str(row.id)

    return (await current_versions(db, [attachment]))[attachment.id]


# Which derivation kinds outrank the original, best first. ``convert`` is
# last: an OCR or outline pass over a converted upload starts from its
# rendering, so either one is the better copy.
_CURRENT_KINDS = ("outline", "ocr", conversion.CONVERT_KIND)


async def current_versions(
    db: AsyncSession, attachments: list[Attachment]
) -> dict[uuid.UUID, tuple[str, str]]:
    """``resolve_version(..., None)`` for many attachments in one query:
    latest outline > latest ocr > latest convert > original, as ``(storage_key,
    version_label)`` per attachment id.

    A bulk download of a few hundred PDFs would otherwise spend up to two
    round trips per file deciding which blob to read.
    """
    if not attachments:
        return {}
    rows = await db.execute(
        select(AttachmentDerivation)
        .where(
            AttachmentDerivation.attachment_id.in_([a.id for a in attachments]),
            AttachmentDerivation.kind.in_(_CURRENT_KINDS),
        )
        .order_by(AttachmentDerivation.created_at.desc())
    )
    best: dict[uuid.UUID, AttachmentDerivation] = {}
    for d in rows.scalars().all():
        # Newest first, so the first of each kind is its latest; a
        # better-ranked kind replaces whatever was picked before it.
        cur = best.get(d.attachment_id)
        rank = _CURRENT_KINDS.index
        if cur is None or rank(d.kind) < rank(cur.kind):
            best[d.attachment_id] = d
    return {
        a.id: (best[a.id].storage_key, str(best[a.id].id))
        if a.id in best
        else (a.storage_key, "original")
        for a in attachments
    }


class AttachmentMatch(BaseModel):
    attachment_id: uuid.UUID
    filename: str
    item_id: uuid.UUID
    space_id: uuid.UUID


class AttachmentResolveResponse(BaseModel):
    """Possibly empty: a file the caller can't see is not an error."""

    attachments: list[AttachmentMatch]


# Registered before `/api/attachments/{attachment_id}`, which would
# otherwise claim the path and reject "resolve" as a malformed UUID.
@router.get(
    "/api/attachments/resolve",
    response_model=AttachmentResolveResponse,
    summary="Find the caller's attachments by file hash or standard edition",
)
async def resolve_attachments(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
    sha256: Annotated[str | None, Query(pattern=r"^[0-9a-fA-F]{64}$")] = None,
    body: Annotated[str | None, Query(max_length=120)] = None,
    designation: Annotated[str | None, Query(max_length=200)] = None,
    label: Annotated[str | None, Query(max_length=120)] = None,
    space: Annotated[str | None, Query()] = None,
) -> dict[str, object]:
    """The session twin of `/api/v1/attachments/resolve`, for the SPA.

    It is what makes `/open?sha256=…` work as a link. Item and
    attachment ids differ per instance, so something that holds a PDF
    but knows nothing about this library can still point a reader at
    it: the bytes hash the same everywhere. A standard edition — `body`
    + `designation` + `label` — is the other portable key, for when the
    library's copy is a different download of the same document
    (publishers watermark per copy, which changes the hash).

    One key per request: `sha256`, or all three edition fields. Scoped
    to the spaces whose items the caller can read — the same set the
    item routes allow — so this answers "can *I* open this file", never
    "does anyone have it". An upload that never completed is left out:
    there is nothing to read yet.
    """
    edition = (body, designation, label)
    has_edition = any(v is not None for v in edition)
    if sha256 is not None and has_edition:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Give sha256 or body + designation + label, not both",
        )
    if sha256 is None and not all(v and v.strip() for v in edition):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Give sha256, or all of body, designation and label",
        )

    reachable: Select[tuple[uuid.UUID]] | list[uuid.UUID] = readable_item_space_ids(
        user.id
    )
    if space is not None:
        target = (
            await db.execute(
                select(Space.id).where(Space.slug == space, Space.id.in_(reachable))
            )
        ).scalar_one_or_none()
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Space not found")
        reachable = [target]

    stmt = (
        select(Attachment.id, Attachment.filename, Item.id, Item.space_id)
        .join(Item, Item.id == Attachment.item_id)
        .where(
            Item.deleted_at.is_(None),
            Item.space_id.in_(reachable),
            Attachment.uploaded_at.is_not(None),
        )
    )
    if sha256 is not None:
        stmt = stmt.where(Attachment.sha256 == sha256.lower()).order_by(
            Attachment.created_at
        )
    else:
        assert body is not None and designation is not None and label is not None
        stmt = (
            stmt.join(StandardRevision, StandardRevision.item_id == Item.id)
            .join(StandardFamily, StandardFamily.id == StandardRevision.family_id)
            .where(
                # Body and designation are CITEXT, so this is the same
                # case-insensitive match filing a revision uses; the
                # label is matched as written.
                StandardFamily.body == body.strip(),
                StandardFamily.designation == designation.strip(),
                StandardRevision.label == label.strip(),
            )
            .order_by(Item.created_at, Attachment.created_at)
        )

    rows = (await db.execute(stmt)).all()
    return {
        "attachments": [
            {
                "attachment_id": att_id,
                "filename": filename,
                "item_id": item_id,
                "space_id": space_id,
            }
            for att_id, filename, item_id, space_id in rows
        ]
    }


@router.get("/api/attachments/{attachment_id}", response_model=AttachmentResponse)
async def get_attachment(
    attachment_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AttachmentResponse:
    """One attachment's row. The reader is addressed by attachment id
    alone, and this is how it finds the item the PDF belongs to."""
    att = await _resolve_attachment(db, user, attachment_id)
    return (await attachment_responses(db, [att]))[0]


@router.get(
    "/api/attachments/{attachment_id}/download",
    response_model=AttachmentDownloadResponse,
)
async def download_attachment(
    attachment_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
    version: Annotated[
        str | None,
        Query(
            description=(
                "Which version to fetch. Omit for the current best "
                "(latest outline > latest OCR > original). Pass "
                "'original' for the untouched upload, or a derivation "
                "UUID to fetch a specific run."
            ),
        ),
    ] = None,
) -> AttachmentDownloadResponse:
    """A presigned URL for the reader to load the PDF from.

    Logged as a *view*: the reader is this route's only caller in the
    SPA, and it asks once per document open (or version switch) — pdf.js
    then fetches pages and ranges straight from storage, so nothing here
    repeats per page. The explicit Download button goes through `/file`,
    which is logged as a download.
    """
    att, item = await _resolve_attachment_and_item(db, user, attachment_id)
    storage_key, _ = await resolve_version(db, att, version)
    url = await storage.presign_download(storage_key)
    await record_read(
        db,
        user,
        AuditAction.attachment_view,
        space_id=item.space_id,
        target_type="attachment",
        target_id=att.id,
        label=att.filename,
        details=_read_details(item, version),
    )
    return AttachmentDownloadResponse(url=url)


@router.get("/api/attachments/{attachment_id}/file")
async def stream_attachment(
    attachment_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
    version: Annotated[str | None, Query()] = None,
) -> StreamingResponse:
    """Proxy-stream the attachment with a Content-Disposition header so
    the browser saves it under the user-visible filename rather than the
    opaque storage key. Used by the explicit "Download" action; the
    reader still uses /download (presigned URL) to fetch bytes
    direct-from-storage.

    Signs for the server-side endpoint, not the browser-facing one: this
    process performs the GET itself, so the URL never leaves the server.

    With no ``version``, a converted upload downloads as uploaded — the
    ``.docx`` under its own name and type, not the PDF rendering, which
    ``version=<derivation id>`` still reaches."""
    att, item = await _resolve_attachment_and_item(db, user, attachment_id)
    if version is None and not conversion.is_pdf(att.content_type, att.filename):
        storage_key = att.storage_key
    else:
        storage_key, _ = await resolve_version(db, att, version)
    presigned = await storage.presign_download_internal(storage_key)

    client = httpx.AsyncClient(follow_redirects=True, timeout=60.0)
    req = client.build_request("GET", presigned)
    upstream = await client.send(req, stream=True)
    if upstream.status_code != 200:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Failed to fetch from storage"
        )
    # Logged once storage has answered, so a failed fetch isn't recorded
    # as a file that left.
    try:
        await record_read(
            db,
            user,
            AuditAction.attachment_download,
            space_id=item.space_id,
            target_type="attachment",
            target_id=att.id,
            label=att.filename,
            details=_read_details(item, version),
        )
    except BaseException:
        await upstream.aclose()
        await client.aclose()
        raise

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    # RFC 6266: filename* with UTF-8 percent-encoding covers non-ASCII
    # filenames; filename= gives a safe ASCII fallback for old clients.
    filename, media_type = att.filename, att.content_type
    if storage_key != att.storage_key and not conversion.is_pdf(
        att.content_type, att.filename
    ):
        # A converted upload's rendering: same name, as the PDF it is.
        stem = filename.rsplit(".", 1)[0] if "." in filename else filename
        filename, media_type = f"{stem}.pdf", conversion.PDF_CONTENT_TYPE
    ascii_name = filename.encode("ascii", "replace").decode("ascii")
    disposition = (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(filename)}"
    )
    headers = {"Content-Disposition": disposition}
    content_length = upstream.headers.get("content-length")
    if content_length:
        headers["Content-Length"] = content_length
    return StreamingResponse(
        body(),
        media_type=media_type or "application/octet-stream",
        headers=headers,
    )


@router.get(
    "/api/attachments/{attachment_id}/derivations",
    response_model=AttachmentDerivationListResponse,
)
async def list_derivations(
    attachment_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AttachmentDerivationListResponse:
    """List every derivation for one attachment so the reader can
    show a version dropdown. Ordered newest-first across all kinds —
    the UI groups by kind for display."""
    att = await _resolve_attachment(db, user, attachment_id)
    rows = (
        await db.execute(
            select(AttachmentDerivation)
            .where(AttachmentDerivation.attachment_id == att.id)
            .order_by(AttachmentDerivation.created_at.desc())
        )
    ).scalars().all()
    _, current = await resolve_version(db, att, None)
    return AttachmentDerivationListResponse(
        derivations=[
            AttachmentDerivationResponse.model_validate(r) for r in rows
        ],
        current_version=current,
    )


@router.get(
    "/api/attachments/{attachment_id}/page-dims",
    response_model=AttachmentPageDimsResponse,
)
async def get_page_dims(
    attachment_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AttachmentPageDimsResponse:
    """Per-page width/height for the reader's virtualizer."""
    att = await _resolve_attachment(db, user, attachment_id)
    rows = (
        await db.execute(
            select(
                AttachmentPage.page_number,
                AttachmentPage.width_pts,
                AttachmentPage.height_pts,
            )
            .where(AttachmentPage.attachment_id == att.id)
            .order_by(AttachmentPage.page_number.asc())
        )
    ).all()
    return AttachmentPageDimsResponse(
        pages=[
            AttachmentPageDimsRow(page=r[0], width=r[1], height=r[2]) for r in rows
        ]
    )


@router.get(
    "/api/attachments/{attachment_id}/processing",
    response_model=AttachmentProcessingResponse,
)
async def get_processing(
    attachment_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AttachmentProcessingResponse:
    """Phase A diagnostic: quality metrics + downstream-worker flags
    for one attachment. Returns a 'never assessed' shape (all fields
    null/false/'untouched') if the extract worker hasn't run yet
    against this row — keeps the endpoint friendly to clients
    polling during ingestion."""
    att = await _resolve_attachment(db, user, attachment_id)
    proc = await db.get(AttachmentProcessing, att.id)
    if proc is None:
        return AttachmentProcessingResponse(attachment_id=att.id)
    return AttachmentProcessingResponse.model_validate(proc)


@router.post(
    "/api/attachments/{attachment_id}/complete",
    response_model=AttachmentResponse,
)
async def complete_attachment(
    attachment_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AttachmentResponse:
    """SPA-side parity with /api/v1/uploads/{id}/complete: the browser
    PUTs the body to the presigned URL, then calls this so the row
    stops looking pending."""
    att, item = await _resolve_attachment_and_item(
        db, user, attachment_id, SPACE_ROLE_EDITOR
    )
    if att.uploaded_at is None:
        att.uploaded_at = datetime.now(UTC)
        # The upload is logged here, when it's confirmed — registering a
        # row is only an intent, and a repeat /complete is a no-op.
        record(
            db,
            user,
            AuditAction.attachment_upload,
            space_id=item.space_id,
            target_type="attachment",
            target_id=att.id,
            label=att.filename,
            details={
                "item_id": str(item.id),
                "content_type": att.content_type,
                "size_bytes": att.size_bytes,
            },
        )
        await extraction.mark_and_enqueue(db, att)
        # Eagerly snapshot the just-uploaded blob to its `.original`
        # sibling so any later worker rewrite (OCR, future passes) is
        # reversible. PDF-only — non-PDFs aren't mutated by any worker
        # so the snapshot would be storage cost without payoff. Best-
        # effort: a snapshot failure shouldn't block the upload from
        # being marked complete.
        if att.content_type == "application/pdf":
            try:
                made = await storage.ensure_original(att.storage_key)
            except Exception:
                made = False
                log.exception(
                    "snapshot of original failed for %s; row stays without "
                    "original_preserved_at and Restore stays unavailable",
                    att.id,
                )
            if made:
                now = datetime.now(UTC)
                await db.execute(
                    pg_insert(AttachmentProcessing)
                    .values(
                        attachment_id=att.id,
                        original_preserved_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=[AttachmentProcessing.attachment_id],
                        set_={
                            "original_preserved_at": now,
                            "updated_at": now,
                        },
                    )
                )
        await db.commit()
        await db.refresh(att)
    return (await attachment_responses(db, [att]))[0]


@router.delete(
    "/api/attachments/{attachment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_attachment(
    attachment_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    att, item = await _resolve_attachment_and_item(
        db, user, attachment_id, SPACE_ROLE_EDITOR
    )
    # Best-effort: if an object isn't there, drop the row anyway so
    # the user can recover from a half-done upload. The processing
    # row + derivation rows cascade via FK; we just have to clean up
    # the underlying S3 blobs ourselves.
    derivation_keys = (
        await db.execute(
            select(AttachmentDerivation.storage_key).where(
                AttachmentDerivation.attachment_id == att.id
            )
        )
    ).scalars().all()
    keys_to_delete = [
        att.storage_key,
        storage.original_key(att.storage_key),
        *derivation_keys,
    ]
    for key in keys_to_delete:
        try:
            await storage.delete_object(key)
        except Exception:
            # The row removal is the user-visible action; logging hooks
            # land when we wire structlog in the broader observability
            # pass.
            pass
    record(
        db,
        user,
        AuditAction.attachment_delete,
        space_id=item.space_id,
        target_type="attachment",
        target_id=att.id,
        label=att.filename,
        details={"item_id": str(item.id)},
    )
    await db.delete(att)
    await db.commit()
