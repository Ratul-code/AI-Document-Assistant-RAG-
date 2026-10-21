/**
 * Documents API calls → FastAPI backend.
 *
 * Cookie strategy:
 * - FastAPI sets access_token + refresh_token as HttpOnly cookies.
 * - apiClient sends `credentials: "include"`, so the browser attaches
 *   them automatically. We never read or write tokens in JS.
 *
 * Endpoints used:
 * - GET    /documents?status=&limit=&offset=        → paginated list
 * - POST   /documents/upload-intent                  → register an
 *                                                       upload; server
 *                                                       returns a
 *                                                       signed PUT URL
 *                                                       for the file.
 * - PUT    {upload_url}                              → upload the
 *                                                       raw file to
 *                                                       object storage
 *                                                       (Supabase signed
 *                                                       URL — NO auth
 *                                                       cookies).
 * - POST   /documents/{id}/confirm                   → tell the server
 *                                                       the upload
 *                                                       finished so it
 *                                                       can start
 *                                                       processing.
 * - POST   /documents/{id}/upload-failed             → tell the server
 *                                                       the PUT to
 *                                                       storage failed,
 *                                                       so it can flip
 *                                                       the doc to
 *                                                       FAILED instead
 *                                                       of leaving it
 *                                                       stuck in
 *                                                       PROCESSING.
 * - DELETE /documents/{id}                           → permanently
 *                                                       delete the
 *                                                       document, all
 *                                                       its pages /
 *                                                       chunks /
 *                                                       embeddings, and
 *                                                       the file in
 *                                                       Supabase
 *                                                       Storage. 204 on
 *                                                       success, 404 if
 *                                                       not found / not
 *                                                       owned.
 * - PATCH  /documents/{id}                           → update editable
 *              (body: { title? })                          attributes
 *                                                       (currently just
 *                                                       the title).
 *                                                       Returns the
 *                                                       updated
 *                                                       `Document`.
 * - GET    /documents/{id}/logs                      → per-document
 *                                                       activity log
 * - GET    /documents/logs                           → global
 *                                                       activity feed
 * - GET    /documents/{id}/download                  → fetch the
 *                                                       original file
 *                                                       as a Blob and
 *                                                       trigger a
 *                                                       browser save.
 *                                                       NOT funnelled
 *                                                       through
 *                                                       `apiClient`
 *                                                       (which assumes
 *                                                       JSON); uses
 *                                                       `rawClient`
 *                                                       instead so the
 *                                                       same
 *                                                       credentials +
 *                                                       auto-refresh-on-401
 *                                                       contract is
 *                                                       preserved.
 * - GET    /documents/{id}/view                      → navigate the
 *                                                       browser to the
 *                                                       original file
 *                                                       in a new tab.
 *                                                       The backend
 *                                                       serves the
 *                                                       bytes with
 *                                                       `Content-Disposition:
 *                                                       inline`, so the
 *                                                       browser renders
 *                                                       the file
 *                                                       directly (PDF,
 *                                                       image, text);
 *                                                       other types
 *                                                       fall back to a
 *                                                       download prompt.
 *                                                       Implemented as a
 *                                                       synchronous
 *                                                       `window.open` in
 *                                                       the click handler
 *                                                       so the
 *                                                       user-gesture
 *                                                       chain isn't
 *                                                       broken.
 */

import { apiClient, rawClient } from "@/lib/api/client";
import { API_BASE_URL, API_PREFIX } from "@/lib/config";

/* ---------- Query keys ---------- */

/**
 * Centralised query-key factory. Using a factory keeps key shape
 * consistent across `useQuery` calls and `invalidateQueries`, so
 * invalidating "all document lists" doesn't accidentally hit the
 * upload-intent mutation cache or vice-versa.
 */
export const documentKeys = {
  all: ["documents"] as const,
  lists: () => [...documentKeys.all, "list"] as const,
  list: (params: ListDocumentsParams) =>
    [...documentKeys.lists(), params] as const,
  /** One document's metadata. Currently unused by any query (no per-doc
   *  GET endpoint), but reserved for the detail page that will land
   *  with the delete affordance, and used today by `useDeleteDocument`
   *  to drop the cache entry on success. */
  details: () => [...documentKeys.all, "detail"] as const,
  detail: (id: string) => [...documentKeys.details(), id] as const,
  /** Aggregate stats for one document — powers the detail dialog. */
  stats: (id: string) => [...documentKeys.details(), id, "stats"] as const,
  logs: () => [...documentKeys.all, "logs"] as const,
  log: (documentId: string, params: ListLogsParams) =>
    [...documentKeys.logs(), documentId, params] as const,
};

/* ---------- Types ---------- */

/** Document status in the backend pipeline. */
export type DocumentStatus =
  | "uploaded"
  | "processing"
  | "ready"
  | "learning"
  | "failed";

/** A document as returned by FastAPI. */
export interface Document {
  id: string;
  title: string;
  filename: string;
  mime_type: string;
  file_size_bytes: number;
  status: DocumentStatus;
  error_message: string | null;
  uploaded_at: string | null; // ISO string
  created_at: string; // ISO string
  updated_at: string; // ISO string
}

/** Paginated list response from FastAPI. */
export interface DocumentListResponse {
  items: Document[];
  total: number;
  limit: number;
  offset: number;
}

/**
 * Per-document aggregate stats as returned by FastAPI's
 * `GET /api/v1/documents/{id}/stats`. The list endpoint stays narrow
 * (no embedded counts); this is the dedicated, on-demand surface for
 * the detail dialog.
 *
 * All numeric fields are zero (not null) when the document hasn't
 * been processed yet — the dialog renders a clear "Not parsed yet"
 * state instead of treating it as an error.
 */
export interface DocumentStatsResponse {
  /** The same `DocumentListItem` the list endpoint returns, so the
   *  dialog can render its header without a second round-trip. */
  document: Document;
  page_count: number;
  chunks_total: number;
  chunks_embedded: number;
  chunks_pending: number;
  chunks_failed: number;
  pages_needing_ocr: number;
  pages_ocr_pending: number;
  pages_ocr_failed: number;
  /** Distinct embedding model names backing this doc, sorted
   *  alphabetically. Most docs have one entry. */
  embedding_models: string[];
  /** Histogram of `document_embeddings.status` values. Keys are
   *  the public status names ("succeeded", "failed", "skipped", ...).
   *  Lets the dialog render any future status without a schema bump. */
  embedding_status_counts: Record<string, number>;
}

/** Body for POST /documents/upload-intent. */
export interface UploadIntentInput {
  title: string;
  filename: string;
  mime_type: string;
  file_size_bytes: number;
}

/**
 * Response from POST /documents/upload-intent.
 *
 * The server returns:
 *   - `document_id`: the new doc's id
 *   - `upload_url`:  signed PUT URL for the raw file (Supabase or
 *                    similar). Browser must use plain fetch here
 *                    WITHOUT auth cookies — the URL's signature is
 *                    the credential.
 *   - `storage_path`: opaque path the server tracks for later cleanup.
 *   - `status`:      the server's current status (usually "processing"
 *                    or "uploading").
 */
export interface UploadIntentResponse {
  document_id: string;
  upload_url: string;
  storage_path: string;
  status: DocumentStatus;
}

/**
 * Response from POST /documents/{id}/confirm.
 *
 * The server flips the document into its post-upload processing
 * state and returns the refreshed `Document` row. We treat the
 * shape as `Document` but stay loose (`Partial<Document>`) so
 * older servers that return a smaller payload still type-check.
 */
export type ConfirmUploadResponse = Partial<Document> & {
  id: string;
};

/**
 * Body for POST /documents/{id}/upload-failed.
 *
 * Mirrors the backend's `UploadFailedRequest` schema. Sent by the
 * client when its direct PUT to the presigned Supabase URL fails
 * — network error, CORS block, 403 from a stale URL, user-cancelled
 * upload, etc. Without this report, the doc would stay stuck in
 * PROCESSING forever ("ghost" row).
 */
export interface UploadFailedInput {
  /** Short human-readable reason for the failure (1..500 chars). */
  error_message: string;
  /**
   * HTTP status the PUT actually returned, if any. `null` when the
   * failure happened before we got a response (network error, abort,
   * CORS block).
   */
  http_status: number | null;
}

/**
 * A single entry in the document activity log, as returned by
 * GET /documents/{id}/logs and GET /documents/logs.
 *
 * The backend emits at most one row per stage
 * (`uploaded → parsing → chunking → embedding`), plus a `*_failed`
 * row when a stage failed. The union below lists the known values
 * for autocomplete; the trailing `(string & {})` fallback keeps the
 * type open for any future event the backend might add without
 * forcing a frontend redeploy.
 */
export type DocumentLogEventType =
  // Success rows (one per stage).
  | "uploaded"
  | "parsing"
  | "chunking"
  | "embedding"
  // Failure rows (one per failed stage).
  | "parsing_failed"
  | "chunking_failed"
  | "embedding_failed"
  | "processing_failed"
  // Operator / out-of-band.
  | "document_deleted"
  // Allow any future event the backend might add without forcing a
  // frontend redeploy. The string-literal union above gives nice
  // autocomplete for known events; this fallback keeps the type
  // open for unknown values.
  | (string & {});

export interface DocumentLogItem {
  id: string;
  document_id: string;
  event_type: DocumentLogEventType;
  message: string | null;
  /** Event-specific extras (file size, chunk count, error trace, ...). */
  metadata: Record<string, unknown> | null;
  /** ISO timestamp from the backend. */
  created_at: string;
  /**
   * Document title as known to the backend at log time.
   *
   * The per-document logs endpoint does NOT denormalize this — only
   * the cross-doc feed (and only in some backends) carries it. We
   * mark it optional so the per-doc `DocumentLogsPanel` can consume
   * the same type without lying. The global feed already null-falls
   * back to "Untitled document".
   */
  document_title?: string | null;
  /**
   * File extension / kind (e.g. "pdf", "docx") for display.
   *
   * Same story as `document_title` — not denormalized onto every
   * log row, optional in the type, and the global feed's `??` guard
   * keeps the UI safe when it's absent.
   */
  file_type?: string | null;
}

export interface DocumentLogListResponse {
  items: DocumentLogItem[];
  total: number;
  limit: number;
  offset: number;
}

/* ---------- Queries ---------- */

export interface ListDocumentsParams {
  status?: DocumentStatus;
  limit?: number;
  offset?: number;
}

/** Pagination params for the activity log endpoints. */
export interface ListLogsParams {
  limit?: number;
  offset?: number;
}

/**
 * GET /documents?status=&limit=&offset=
 * - `status` filters by lifecycle state (omit for "all").
 * - `limit` / `offset` drive pagination on the Library page.
 */
export async function listDocuments(
  params: ListDocumentsParams = {},
): Promise<DocumentListResponse> {
  const search = new URLSearchParams();
  if (params.status) search.set("status", params.status);
  if (params.limit !== undefined) search.set("limit", String(params.limit));
  if (params.offset !== undefined) search.set("offset", String(params.offset));

  const qs = search.toString();
  return apiClient<DocumentListResponse>(
    `/documents${qs ? `?${qs}` : ""}`,
    { method: "GET" },
  );
}

/**
 * GET /documents?status=ready&limit=20
 * Convenience for the sidebar's chat-picker — only docs the AI can
 * ground answers on.
 */
export async function getReadyDocuments(
  limit = 20,
): Promise<DocumentListResponse> {
  return listDocuments({ status: "ready", limit });
}

/**
 * GET /documents/{id}/logs?limit=&offset=
 * Activity log for ONE document, newest first. Powers the "Activity"
 * panel on the document detail page.
 */
export async function getDocumentLogs(
  documentId: string,
  params: ListLogsParams = {},
): Promise<DocumentLogListResponse> {
  const search = new URLSearchParams();
  if (params.limit !== undefined) search.set("limit", String(params.limit));
  if (params.offset !== undefined) search.set("offset", String(params.offset));

  const qs = search.toString();
  return apiClient<DocumentLogListResponse>(
    `/documents/${documentId}/logs${qs ? `?${qs}` : ""}`,
    { method: "GET" },
  );
}

/**
 * GET /documents/logs?limit=&offset=
 * Activity log across ALL of the caller's documents, newest first.
 * Powers the global "Activity" feed.
 */
export async function getMyLogs(
  params: ListLogsParams = {},
): Promise<DocumentLogListResponse> {
  const search = new URLSearchParams();
  if (params.limit !== undefined) search.set("limit", String(params.limit));
  if (params.offset !== undefined) search.set("offset", String(params.offset));

  const qs = search.toString();
  return apiClient<DocumentLogListResponse>(
    `/documents/logs${qs ? `?${qs}` : ""}`,
    { method: "GET" },
  );
}

/**
 * GET /documents/{id}/stats
 *
 * Per-document aggregate counts (page count, chunk breakdown, OCR
 * needs, embedding model names + status histogram). Returns the
 * `Document` row in the same shape as the list endpoint, so the
 * detail dialog can render its header in one round-trip.
 *
 * Owner-scoped server-side: returns 404 for missing or cross-owner
 * documents (no info leak), which the API client surfaces as
 * `ApiError` — the detail dialog renders a friendly error state in
 * that case.
 */
export async function getDocumentStats(
  documentId: string,
): Promise<DocumentStatsResponse> {
  return apiClient<DocumentStatsResponse>(
    `/documents/${documentId}/stats`,
    { method: "GET" },
  );
}

/* ---------- Mutations ---------- */

/**
 * POST /documents/upload-intent
 * Registers the user's intent to upload. The server returns a
 * signed `upload_url` for the actual file PUT.
 */
export async function uploadIntent(
  input: UploadIntentInput,
): Promise<UploadIntentResponse> {
  return apiClient<UploadIntentResponse>("/documents/upload-intent", {
    method: "POST",
    body: input,
  });
}

/**
 * POST /documents/{id}/confirm
 * Tells the server the file PUT succeeded and it can start
 * processing the document.
 */
export async function confirmDocument(
  documentId: string,
): Promise<ConfirmUploadResponse> {
  return apiClient<ConfirmUploadResponse>(
    `/documents/${documentId}/confirm`,
    { method: "POST" },
  );
}

/**
 * POST /documents/{id}/upload-failed
 *
 * Reports that the browser's direct PUT to the signed storage URL
 * didn't complete, so the server can flip the document to FAILED
 * instead of leaving it stuck in PROCESSING. Idempotent on the
 * server side — safe to retry.
 *
 * Called by `putFileToSignedUrl` on the failure path; callers
 * usually don't need to invoke this directly.
 */
export async function reportUploadFailed(
  documentId: string,
  input: UploadFailedInput,
): Promise<ConfirmUploadResponse> {
  return apiClient<ConfirmUploadResponse>(
    `/documents/${documentId}/upload-failed`,
    { method: "POST", body: input },
  );
}

/**
 * DELETE /documents/{id}
 *
 * Permanently deletes a document and all of its derived data — pages,
 * chunks, embeddings — plus the underlying file in Supabase Storage.
 *
 * Returns `void` because the backend responds with 204 No Content.
 * Throws if the request fails (404 for missing/cross-owner, 401 for
 * unauthenticated, network errors). The backend handles the cascade
 * server-side, so callers don't need to do any cleanup of their own.
 *
 * The matching React Query hook (`useDeleteDocument`) wraps this in a
 * mutation that also invalidates the document list cache so any
 * mounted sidebar / page refetches and drops the row.
 */
export async function deleteDocument(documentId: string): Promise<void> {
  await apiClient<void>(`/documents/${documentId}`, { method: "DELETE" });
}

/**
 * Body for `PATCH /documents/{id}`.
 *
 * Mirrors the backend's `DocumentUpdate` schema. All fields are
 * optional — only the ones present are written. Today the only
 * editable user-facing attribute is `title`. The original `filename`
 * is intentionally NOT editable (it's a property of the file on disk,
 * not a label), and the UI doesn't expose it as such.
 */
export interface DocumentUpdateInput {
  title?: string;
}

/**
 * PATCH /documents/{id}
 *
 * Updates editable user-facing attributes on a document. Today the
 * only editable field is `title`. The backend validates the title's
 * length (1..200 chars) and returns 422 for anything outside that
 * range — surfaced here as an `ApiError` with `status === 422`.
 *
 * Returns the updated `Document`. The shape matches the list-item
 * response so the caller's cache can splice the result directly
 * without reshaping.
 */
export async function updateDocument(
  documentId: string,
  input: DocumentUpdateInput,
): Promise<Document> {
  return apiClient<Document>(`/documents/${documentId}`, {
    method: "PATCH",
    body: input,
  });
}

/* ---------- File streaming (download / view) ---------- */

// RFC 6266 `filename="…"` (the shape the backend emits).
const _FILENAME_QUOTED_RE = /filename="([^"]*)"/i;
// Bare `filename=…` token, terminated by `;` or end-of-string — the
// fallback for an upstream proxy that unwrapped the quoted-string.
const _FILENAME_BARE_RE = /filename=([^;]+)/i;

/**
 * GET /documents/{id}/download
 *
 * Streams the original file and triggers a browser save with the
 * filename the server returns in `Content-Disposition`. We use
 * `rawClient` (not `apiClient`) because the JSON parser in
 * `apiClient` would corrupt the binary body; `rawClient` still
 * gives us the same auto-refresh-on-401 + retry-once contract as
 * `apiClient`.
 */
export async function downloadDocument(documentId: string): Promise<void> {
  const res = await rawClient(`/documents/${documentId}/download`, {
    method: "GET",
  });

  if (!res.ok) {
    throw new Error(messageForStatus(res.status, "Couldn't download document"));
  }

  // Fall back to the doc id if the header is missing (defence in
  // depth — the backend always sets it).
  const filename = parseAttachmentFilename(res.headers.get("content-disposition"))
    ?? `document-${documentId}`;

  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  try {
    // Hidden programmatic anchor with `download` is the only
    // cross-browser way to trigger a save without a server-side
    // redirect. Append → click → remove so the click is never
    // visible to the user.
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.rel = "noopener";
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  } finally {
    // Defer revoke until the next tick so the click has a chance
    // to start the download before the URL is freed.
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }
}

/**
 * GET /documents/{id}/view
 *
 * Opens the original file in a new tab. The backend sets
 * `Content-Disposition: inline`; the browser renders the file
 * directly for known MIME types (PDF, image, text) and falls
 * back to a download prompt for the rest (e.g. `.docx`). We proxy
 * through the backend rather than opening a presigned storage
 * URL directly so the auth cookie boundary stays clean — the
 * cookie rides on the request because the new tab is a
 * top-level navigation, and the cookie is set for the backend's
 * origin.
 *
 * Implementation: we open the new tab SYNCHRONOUSLY in the click
 * handler, pointing it directly at the backend's view URL. This
 * sidesteps two failure modes the blob-URL approach hit:
 *   1. `window.open` after an `await` drops the user-gesture
 *      chain and is blocked as a pop-up.
 *   2. Navigating a `noopener` tab to a blob URL created in the
 *      opener fails silently (opaque origin), leaving the tab
 *      blank.
 *
 * Trade-off: if the backend returns 409 (not ready) or 404
 * (missing), the new tab shows the browser's generic error page
 * rather than a friendly toast. The UI prevents this in practice
 * by disabling View + Download while the doc is `processing` or
 * `failed`, so the only realistic 409 is a race where the status
 * flipped between the click and the request.
 */
export function openDocument(documentId: string): void {
  const url = `${API_BASE_URL}${API_PREFIX}/documents/${documentId}/view`;
  // Synchronous, inside the user gesture. Without `noopener` so
  // the new tab can navigate cleanly. If pop-ups are truly
  // blocked, fall back to navigating the current tab in-place —
  // it's the same backend URL, the user can hit Back to return.
  const newWin = window.open(url, "_blank");
  if (!newWin) {
    window.location.assign(url);
  }
}

/**
 * Parse the filename out of a `Content-Disposition` header.
 *
 * The server emits `attachment; filename="<name>"` for the simple
 * case (ASCII, no special chars). We also handle the bare-token
 * form (`filename=<name>`) for resilience against an upstream
 * proxy that unwraps the quoted-string. RFC 5987 / RFC 6266's
 * `filename*=UTF-8''…` form is intentionally NOT supported —
 * modern browsers fall back to the ASCII `filename` when
 * `filename*` is missing, and our backend already sanitises the
 * ASCII value so it never has to round-trip through UTF-8.
 *
 * Returns `null` if the header is missing or malformed; the caller
 * supplies its own fallback (e.g. `document-{id}`).
 */
function parseAttachmentFilename(headerValue: string | null): string | null {
  if (!headerValue) return null;
  const quoted = _FILENAME_QUOTED_RE.exec(headerValue);
  if (quoted && quoted[1]) return quoted[1];
  const bare = _FILENAME_BARE_RE.exec(headerValue);
  if (bare && bare[1]) return bare[1].trim();
  return null;
}

/**
 * Map a non-2xx response status to a short, single-line message
 * suitable for a sonner toast. Falls back to the supplied default
 * for status codes the user can't usefully act on (5xx) — we
 * deliberately don't surface the backend's `detail` field here
 * because the message is meant to be a one-liner, not a full
 * explanation.
 */
function messageForStatus(status: number, fallback: string): string {
  if (status === 401) return "Session expired. Please sign in again.";
  if (status === 404) return "Document not found.";
  if (status === 409) return "Document isn't ready to download yet.";
  return fallback;
}

/**
 * PUT the raw file to a signed storage URL.
 *
 * IMPORTANT: signed URLs (Supabase / S3 / etc.) are pre-authenticated
 * by the URL itself — sending our auth cookies here would be wrong
 * (and potentially leak them to a third-party host). We use plain
 * `fetch` with `credentials: "omit"` and the file's own MIME type.
 *
 * Failure handling:
 *   - If the PUT itself rejects (network error, CORS block, abort)
 *     OR responds with non-2xx, we POST `/documents/{id}/upload-failed`
 *     so the server can flip the doc to FAILED instead of letting it
 *     linger as a ghost in PROCESSING. The report is best-effort:
 *     if it also fails we log and still throw the original error so
 *     the caller can toast it. The server's own row update is the
 *     source of truth.
 *   - We throw a regular `Error` whose message is suitable for
 *     surfacing in a Sonner toast (status + statusText).
 */
export async function putFileToSignedUrl(
  uploadUrl: string,
  file: File,
  documentId: string,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch(uploadUrl, {
      method: "PUT",
      credentials: "omit",
      headers: {
        "Content-Type": file.type || "application/octet-stream",
      },
      body: file,
    });
  } catch (err) {
    // PUT never produced a response (DNS failure, CORS rejection,
    // user aborted, etc.). Tell the server we couldn't store the
    // file — no http_status to report.
    await safeReportFailure(documentId, {
      error_message: humanErrorMessage(err, "Network error during upload"),
      http_status: null,
    });
    throw new Error(
      err instanceof Error ? err.message : "Storage upload failed",
    );
  }

  if (!res.ok) {
    // PUT responded but rejected. http_status goes to the server so
    // 403 vs 500 are distinguishable in the failure logs.
    await safeReportFailure(documentId, {
      error_message: res.statusText || "Storage upload failed",
      http_status: res.status,
    });
    throw new Error(
      `Storage upload failed (${res.status} ${res.statusText})`,
    );
  }
}

/**
 * Best-effort wrapper around `reportUploadFailed`. If the failure
 * report itself fails (server down, network gone, 404 on the doc),
 * we don't want to mask the original PUT error — log it and move
 * on so the caller still throws.
 */
async function safeReportFailure(
  documentId: string,
  input: UploadFailedInput,
): Promise<void> {
  try {
    await reportUploadFailed(documentId, input);
  } catch (err) {
    console.error(
      "[upload] failed to report upload-failed to server:",
      err,
    );
  }
}

/**
 * Coerce arbitrary thrown values into a short, single-line string
 * suitable for sending to the server as `error_message`.
 */
function humanErrorMessage(err: unknown, fallback: string): string {
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}
