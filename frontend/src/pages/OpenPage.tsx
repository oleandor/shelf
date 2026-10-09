import type { ReactNode } from "react";
import { Link, Navigate, useSearchParams } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import {
  resolveAttachments,
  type AttachmentKey,
  type AttachmentMatch,
} from "@/api/attachments";

const SHA256 = /^[0-9a-f]{64}$/i;

/** The server's length caps on the edition fields, mirrored so an
 * over-long one is treated as missing instead of earning a 422. */
const MAX_BODY = 120;
const MAX_DESIGNATION = 200;
const MAX_LABEL = 120;

/**
 * The keys a link carries, in the order they're tried: the file's hash
 * first, since it names exactly these bytes, then the standard edition,
 * which still finds the document when the library holds a different
 * download of it. A malformed hash, or an edition field longer than the
 * server accepts, is ignored rather than sent.
 */
export function keysFromSearch(params: URLSearchParams): AttachmentKey[] {
  const keys: AttachmentKey[] = [];
  const sha256 = params.get("sha256")?.trim() ?? "";
  if (SHA256.test(sha256)) keys.push({ sha256 });
  const body = params.get("body")?.trim() ?? "";
  const designation = params.get("designation")?.trim() ?? "";
  const label = params.get("label")?.trim() ?? "";
  if (
    body &&
    designation &&
    label &&
    body.length <= MAX_BODY &&
    designation.length <= MAX_DESIGNATION &&
    label.length <= MAX_LABEL
  ) {
    keys.push({ body, designation, label });
  }
  return keys;
}

/** The reader URL for a match, carrying the link's `page` and `find`. */
export function readerPath(
  attachmentId: string,
  params: URLSearchParams,
): string {
  const out = new URLSearchParams();
  const page = Number(params.get("page"));
  if (Number.isInteger(page) && page > 1) out.set("page", String(page));
  const find = params.get("find");
  if (find) out.set("find", find);
  const qs = out.toString();
  return `/reader/${encodeURIComponent(attachmentId)}${qs ? `?${qs}` : ""}`;
}

async function firstMatch(
  keys: AttachmentKey[],
): Promise<AttachmentMatch | null> {
  for (const key of keys) {
    const found = await resolveAttachments(key);
    if (found.length > 0) return found[0];
  }
  return null;
}

/**
 * `/open?sha256=…&page=…&find=…` — open a document by what it is rather
 * than by this instance's id for it.
 *
 * Item and attachment ids differ per instance, so a tool that holds a
 * PDF (or knows which edition of a standard it cites) can't link into
 * someone's library with them. It can with the file's SHA-256, which is
 * the same everywhere, optionally backed by `body` + `designation` +
 * `label`. The lookup sees only spaces the signed-in user can read; a
 * match replaces this entry in history with the reader, so Back skips
 * the hop.
 *
 * Sits behind ProtectedRoute like the reader, so a signed-out visitor
 * goes through /login and comes back here with the query intact.
 */
export default function OpenPage() {
  const [params] = useSearchParams();
  const keys = keysFromSearch(params);

  const query = useQuery({
    queryKey: ["open", keys],
    queryFn: () => firstMatch(keys),
    enabled: keys.length > 0,
  });

  if (keys.length > 0 && query.isPending) {
    return (
      <Notice>
        <p style={{ color: "var(--color-text-muted)" }}>Finding document…</p>
      </Notice>
    );
  }
  if (query.data) {
    return <Navigate to={readerPath(query.data.attachment_id, params)} replace />;
  }
  // The server says what it was sent isn't a key: the same answer as a
  // link that carries none, rather than an error the reader can't act on.
  const rejected = query.error instanceof ApiError && query.error.status === 422;
  if (query.isError && !rejected) {
    return (
      <Notice title="Couldn't look the document up">
        <p className="text-sm text-red-600" role="alert">
          {query.error instanceof Error ? query.error.message : "Request failed"}
        </p>
      </Notice>
    );
  }
  if (keys.length === 0 || rejected) {
    return (
      <Notice title="Incomplete link">
        <p className="text-sm" style={{ color: "var(--color-text-muted)" }}>
          This link doesn't say which document to open. It needs a{" "}
          <code>sha256</code> of the file, or a standard's <code>body</code>,{" "}
          <code>designation</code> and <code>label</code>.
        </p>
      </Notice>
    );
  }
  return (
    <Notice title="Not in your library">
      <p className="text-sm" style={{ color: "var(--color-text-muted)" }}>
        The document this link points to isn't in any space you can read.
        Someone may hold it in a space that isn't shared with you, or it
        hasn't been added to this library.
      </p>
      <dl className="mt-4 space-y-1 text-xs" style={{ color: "var(--color-text-muted)" }}>
        {keys.map((key) =>
          "sha256" in key ? (
            <div key="sha256">
              <dt className="inline">SHA-256: </dt>
              <dd className="inline break-all font-mono">{key.sha256.toLowerCase()}</dd>
            </div>
          ) : (
            <div key="edition">
              <dt className="inline">Edition: </dt>
              <dd className="inline">
                {key.body} {key.designation}, {key.label}
              </dd>
            </div>
          ),
        )}
      </dl>
    </Notice>
  );
}

function Notice({
  title,
  children,
}: {
  title?: string;
  children: ReactNode;
}) {
  return (
    <div className="flex h-screen items-center justify-center px-4">
      <div
        className="w-full max-w-md rounded-lg border p-8 shadow-sm"
        style={{
          backgroundColor: "var(--color-surface)",
          borderColor: "var(--color-border)",
        }}
      >
        {title && <h1 className="mb-3 text-lg font-medium">{title}</h1>}
        {children}
        {title && (
          <Link
            to="/library"
            className="mt-6 inline-block text-sm underline"
            style={{ color: "var(--color-accent)" }}
          >
            Go to your library
          </Link>
        )}
      </div>
    </div>
  );
}
