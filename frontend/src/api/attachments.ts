import { apiFetch } from "./client";

export interface Attachment {
  id: string;
  item_id: string;
  filename: string;
  content_type: string;
  size_bytes: number | null;
  uploaded_at: string | null;
  /** Id of the user who uploaded the file, and their current display name. */
  created_by?: string | null;
  created_by_name?: string | null;
  /**
   * Whether the reader can open it: `native` for a PDF upload, `converted`
   * once the worker has rendered a Word/PowerPoint/spreadsheet/image upload
   * to PDF, `converting` / `failed` before that. Null for files shelf
   * doesn't render — those can only be downloaded.
   */
  pdf_status?: "native" | "converted" | "converting" | "failed" | null;
  /** Why the last conversion failed, while `pdf_status` is `failed`. */
  convert_error?: string | null;
}

/**
 * Whether the reader can open this attachment — a PDF upload, or one the
 * worker has rendered to PDF. Falls back to the content type for a server
 * that predates `pdf_status`.
 */
export function opensInReader(att: Attachment): boolean {
  if (att.pdf_status !== undefined) {
    return att.pdf_status === "native" || att.pdf_status === "converted";
  }
  return (
    att.content_type === "application/pdf" ||
    att.filename.toLowerCase().endsWith(".pdf")
  );
}

/**
 * What the upload pickers accept: PDFs, plus everything the worker
 * renders to PDF. Mirrors `shelf.services.conversion`.
 */
export const UPLOAD_ACCEPT = [
  "application/pdf",
  ".pdf",
  ".docx",
  ".doc",
  ".odt",
  ".rtf",
  ".pptx",
  ".ppt",
  ".ppsx",
  ".pps",
  ".odp",
  ".xlsx",
  ".xls",
  ".ods",
  ".png",
  ".jpg",
  ".jpeg",
  ".tif",
  ".tiff",
  ".bmp",
  ".gif",
].join(",");

/** Matches the extension of a file `UPLOAD_ACCEPT` lets through. */
export const UPLOAD_EXTENSION =
  /\.(pdf|docx?|odt|rtf|pptx?|ppsx?|odp|xlsx?|ods|png|jpe?g|tiff?|bmp|gif)$/i;

interface RegisterResponse {
  attachment: Attachment;
  upload_url: string;
}

export function getAttachment(id: string): Promise<Attachment> {
  return apiFetch<Attachment>(`/api/attachments/${encodeURIComponent(id)}`);
}

export interface AttachmentMatch {
  attachment_id: string;
  filename: string;
  item_id: string;
  space_id: string;
}

/**
 * A portable key for a document: its file's SHA-256, or the edition of a
 * standard it is (`body` + `designation` + `label`). Neither depends on
 * this instance's ids, which is what lets a link be built without one.
 */
export type AttachmentKey =
  | { sha256: string }
  | { body: string; designation: string; label: string };

/**
 * The caller's attachments matching `key`, oldest first; empty when the
 * file is not in any space they can read.
 */
export async function resolveAttachments(
  key: AttachmentKey,
): Promise<AttachmentMatch[]> {
  const qs = new URLSearchParams(key).toString();
  const r = await apiFetch<{ attachments: AttachmentMatch[] }>(
    `/api/attachments/resolve?${qs}`,
  );
  return r.attachments;
}

export function listAttachments(itemId: string): Promise<Attachment[]> {
  return apiFetch<Attachment[]>(
    `/api/items/${encodeURIComponent(itemId)}/attachments`,
  );
}

export function registerAttachment(
  itemId: string,
  payload: {
    filename: string;
    content_type: string;
    size_bytes: number | null;
  },
): Promise<RegisterResponse> {
  return apiFetch<RegisterResponse>(
    `/api/items/${encodeURIComponent(itemId)}/attachments`,
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export function deleteAttachment(id: string): Promise<void> {
  return apiFetch<void>(`/api/attachments/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export function completeAttachment(id: string): Promise<Attachment> {
  return apiFetch<Attachment>(
    `/api/attachments/${encodeURIComponent(id)}/complete`,
    { method: "POST" },
  );
}

export function cleanupOrphanAttachments(
  olderThanMinutes = 60,
): Promise<{ deleted: number }> {
  return apiFetch<{ deleted: number }>(
    `/api/me/attachments/cleanup-orphans?older_than_minutes=${olderThanMinutes}`,
    { method: "POST" },
  );
}

/**
 * Fetch a presigned URL for the PDF.
 *
 * `version` selects which derived (or original) blob to download:
 *   - undefined → server's "current best" (latest outline > latest OCR >
 *     latest conversion > original)
 *   - "original" → the untouched upload
 *   - a derivation UUID → that specific run, for compare-versions UX
 */
export async function getDownloadUrl(
  id: string,
  version?: string,
): Promise<string> {
  const qs = version ? `?version=${encodeURIComponent(version)}` : "";
  const r = await apiFetch<{ url: string }>(
    `/api/attachments/${encodeURIComponent(id)}/download${qs}`,
  );
  return r.url;
}

/**
 * Backend-proxied stream URL with Content-Disposition set to the
 * stored filename. Use this for explicit "Download" actions where the
 * browser should save the file under its real name rather than the
 * opaque storage key the presigned URL exposes.
 */
export function getStreamUrl(id: string, version?: string): string {
  const qs = version ? `?version=${encodeURIComponent(version)}` : "";
  return `/api/attachments/${encodeURIComponent(id)}/file${qs}`;
}

export interface AttachmentDerivation {
  id: string;
  kind: string;
  parent_storage_key: string;
  engine: string;
  created_at: string;
}

export interface AttachmentDerivationList {
  derivations: AttachmentDerivation[];
  /** id of the version `getDownloadUrl(id)` returns by default — either a
   * derivation UUID or the literal string "original". */
  current_version: string;
}

export function listDerivations(
  attachmentId: string,
): Promise<AttachmentDerivationList> {
  return apiFetch<AttachmentDerivationList>(
    `/api/attachments/${encodeURIComponent(attachmentId)}/derivations`,
  );
}

export interface AttachmentPageDim {
  page: number;
  width: number | null;
  height: number | null;
}

export interface AttachmentPageDimsResponse {
  pages: AttachmentPageDim[];
}

/**
 * Per-page width/height in PDF user-space (1 pt = 1/72 in) at scale=1.
 * Returned in page-number order. Empty array means the extract worker
 * hasn't run yet — caller should fall back to opening page 1
 * client-side and using its dimensions as a baseline.
 */
export function getPageDims(
  attachmentId: string,
): Promise<AttachmentPageDimsResponse> {
  return apiFetch<AttachmentPageDimsResponse>(
    `/api/attachments/${encodeURIComponent(attachmentId)}/page-dims`,
  );
}

/**
 * Optional parts of an archive, all off unless asked for. Notes and
 * annotations are only ever the downloading user's own; private ones
 * stay marked private in the archive.
 */
export interface ArchiveOptions {
  notes?: boolean;
  annotations?: boolean;
  revisions?: boolean;
}

function archiveOptionsQuery(o: ArchiveOptions): string {
  return (
    (o.notes ? "&include_notes=true" : "") +
    (o.annotations ? "&include_annotations=true" : "") +
    (o.revisions ? "&include_revisions=true" : "")
  );
}

/**
 * Download the PDFs of several items as one flat ZIP.
 *
 * Resolves to the number of PDFs the archive will hold; 0 means there
 * was nothing to download and no download was started.
 */
export async function downloadItemPdfsZip(
  slug: string,
  itemIds: string[],
  options: ArchiveOptions = {},
): Promise<{ files: number }> {
  return downloadPdfsZip(
    slug,
    itemIds.map((id) => `item=${encodeURIComponent(id)}`).join("&"),
    options,
  );
}

/**
 * The PDFs of every item filed under one collection or any of its
 * subcollections, each subcollection a folder in the ZIP.
 *
 * The collection id goes to the server rather than being expanded into
 * an id per item here: a folder of a few hundred documents would make a
 * query string long enough to be refused.
 */
export async function downloadCollectionPdfsZip(
  slug: string,
  collectionId: string,
  options: ArchiveOptions = {},
): Promise<{ files: number }> {
  return downloadPdfsZip(
    slug,
    `collection=${encodeURIComponent(collectionId)}`,
    options,
  );
}

/**
 * Every document of a space: its collections as folders, unfiled
 * documents at the root.
 */
export async function downloadSpacePdfsZip(
  slug: string,
  options: ArchiveOptions = {},
): Promise<{ files: number }> {
  return downloadPdfsZip(slug, "whole_space=true", options);
}

/**
 * The server streams the archive (`/api/spaces/{slug}/attachments-zip`)
 * as it reads each PDF from storage, so it is handed to the browser as
 * an ordinary download: it goes straight to disk with the browser's own
 * progress and cancel, however many GB it comes to. Fetching it into a
 * blob instead would hold the whole archive in the tab's memory and
 * show nothing until the last byte arrived.
 *
 * A navigation can't report an error readably, so the `summary`
 * preflight checks the selection first: a bad request throws with the
 * server's message, and an empty one resolves to 0 without downloading.
 * A PDF missing from storage doesn't fail the download; the archive
 * lists it in _MISSING_FILES.txt.
 */
async function downloadPdfsZip(
  slug: string,
  query: string,
  options: ArchiveOptions,
): Promise<{ files: number }> {
  const base = `/api/spaces/${encodeURIComponent(slug)}/attachments-zip`;
  const res = await fetch(`${base}/summary?${query}`, {
    credentials: "include",
  });
  if (!res.ok) {
    let message = res.statusText;
    try {
      const body = await res.json();
      if (body && typeof body.detail === "string") message = body.detail;
    } catch {
      // non-JSON error body — keep the status text
    }
    throw new Error(message);
  }
  const { files } = (await res.json()) as { items: number; files: number };
  if (files === 0) return { files };
  const a = document.createElement("a");
  a.href = `${base}?${query}${archiveOptionsQuery(options)}`;
  // The server's Content-Disposition names the file; `download` just
  // keeps a failed response from replacing the app in this tab.
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
  return { files };
}

/**
 * End-to-end upload helper. Three steps:
 *  1. register the attachment (backend mints a presigned PUT URL,
 *     row's uploaded_at is null)
 *  2. PUT the body straight to object storage (no Shelf credentials —
 *     the URL signature carries auth)
 *  3. tell the backend the upload is done so uploaded_at gets stamped
 *
 * On PUT failure the just-registered row is cleaned up so the user
 * doesn't see a phantom attachment.
 */
export async function uploadAttachment(
  itemId: string,
  file: File,
): Promise<Attachment> {
  const { attachment, upload_url } = await registerAttachment(itemId, {
    filename: file.name,
    content_type: file.type || "application/octet-stream",
    size_bytes: file.size,
  });

  // The PUT can fail two ways: a non-2xx Response, or `fetch` itself
  // rejecting (CORS preflight denied, network drop). Both must clean up
  // the just-registered row — otherwise it lingers as a phantom
  // "(pending)" attachment whose object was never stored (→ 404 on view).
  // A single try/catch covers the rejection path the old `if (!res.ok)`
  // check silently skipped.
  try {
    const res = await fetch(upload_url, {
      method: "PUT",
      body: file,
      headers: file.type ? { "Content-Type": file.type } : {},
    });
    if (!res.ok) {
      throw new Error(`Upload failed: ${res.status} ${res.statusText}`);
    }
  } catch (e) {
    try {
      await deleteAttachment(attachment.id);
    } catch {
      // swallow — surfacing the upload failure is more important
    }
    throw e instanceof Error ? e : new Error("Upload failed");
  }
  // Step 3 is best-effort: a missed completion call leaves the row
  // looking pending until the next sweep, but the body is already in
  // the bucket. Don't blow up the visible upload over it.
  try {
    return await completeAttachment(attachment.id);
  } catch {
    return attachment;
  }
}
