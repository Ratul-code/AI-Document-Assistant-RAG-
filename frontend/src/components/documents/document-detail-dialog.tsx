"use client";

/**
 * Per-document detail dialog — opens when a documents-row is clicked.
 *
 * Surfaces the page/chunk/OCR/embedding stats that the list endpoint
 * intentionally omits (broadening the list payload would inflate every
 * page load), a live progress stepper + lifetime Logs panel, and
 * three actions: Edit (header link) / View + Download (footer) /
 * Delete (footer).
 *
 * Visual direction follows `docs/DESIGN.md`:
 *  - bg-card / border-border / text-foreground for every surface
 *  - restrained radii (rounded-md on tiles, rounded-lg on the dialog)
 *  - primary lime reserved for the primary footer action, the
 *    "ready" status chip, and the live progress bar — never on
 *    every status
 *  - badges for embedding-status counts use semantic tokens
 *    (bg-accent for succeeded, bg-destructive/10 for failed, bg-muted
 *    for everything else), per the design system's explicit ban on
 *    painting the brand color on every positive state
 *  - 24px component-section spacing, 16px compact-group spacing
 *
 * Footer shape: Delete (destructive) on the left, View + Download
 * (both outline, both with their real icons) on the right. Edit is
 * NOT in the footer — it's a `variant="link"` text button in the
 * header's metadata row, sitting right next to the status pill. The
 * "Edit" link calls into the same `onEdit` prop the row's dropdown
 * uses; the rest of the dialog (and the parent `DocumentRow`) doesn't
 * need to know whether the user reached it from the row's 3-dot menu
 * or the dialog's header. This keeps the destructive Delete and the
 * file affordances owning the footer — the same pattern Vercel uses
 * for detail cards where the primary action is contextual, not a
 * heavy CTA.
 *
 * Layout: the `DialogContent` is capped at `max-h-[90vh]` with a
 * vertical flex column. The header and footer are pinned (header
 * on top, footer on bottom, both `shrink-0`); everything in between
 * (stat grid + progress + logs + failed-doc error) lives inside a
 * single `flex-1 overflow-y-auto` region. Without this cap the
 * Logs panel used to push the whole dialog off-screen on docs
 * with long histories.
 *
 * State machine: this dialog opens on top of the row. Edit and Delete
 * are still owned by the existing per-row dialogs (`EditDocumentDialog`,
 * `DeleteDocumentDialog`) — clicking either action closes this dialog
 * and asks the parent to open the other. The parent (`DocumentRow`)
 * coordinates so only one of the three is open at a time.
 */

import { useQuery } from "@tanstack/react-query";
import {
  AlertCircle,
  Download,
  Eye,
  FileSpreadsheet,
  FileText,
  FileType,
  Loader2,
  Trash2,
  type LucideIcon,
} from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  documentKeys,
  downloadDocument,
  getDocumentStats,
  openDocument,
  type Document,
  type DocumentStatsResponse,
} from "@/lib/api/documents";
import { cn, formatRelativeTime, humanError } from "@/lib/utils";

import { DocumentLogsPanel } from "./document-logs-panel";
import { DocumentProgressStepper } from "./document-progress-stepper";

type DocumentKind = "pdf" | "docx" | "txt" | "md" | "xlsx" | "csv";

/**
 * Map a filename to an icon-friendly "kind". Mirrors the helper in
 * `document-row.tsx` so the dialog header and the row agree on which
 * icon to show. Kept local rather than imported to avoid widening the
 * row's export surface.
 */
function kindFromFilename(filename: string): DocumentKind {
  const ext = filename.split(".").pop()?.toLowerCase() ?? "";
  switch (ext) {
    case "pdf":
      return "pdf";
    case "docx":
    case "doc":
      return "docx";
    case "txt":
      return "txt";
    case "md":
    case "markdown":
      return "md";
    case "xlsx":
    case "xls":
      return "xlsx";
    case "csv":
      return "csv";
    default:
      return "pdf";
  }
}

const ICON_BY_KIND: Record<DocumentKind, LucideIcon> = {
  pdf: FileText,
  docx: FileType,
  txt: FileType,
  md: FileType,
  xlsx: FileSpreadsheet,
  csv: FileSpreadsheet,
};

/**
 * Format a byte count as a short human-readable string ("1.4 MB",
 * "780 KB"). Mirrors the convention other rows in the codebase use;
 * kept local because the codebase has no shared `formatBytes` yet.
 */
function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * CSS classes for the document status pill in the dialog header.
 * Mirrors the row's existing treatment so the badge looks identical
 * whether the user sees it on the row or in the dialog.
 */
const STATUS_PILL_CLASSES: Record<Document["status"], string> = {
  ready: "bg-accent text-accent-foreground",
  processing: "bg-muted text-foreground",
  uploaded: "bg-secondary text-secondary-foreground",
  learning: "bg-secondary text-secondary-foreground",
  failed: "bg-destructive/10 text-destructive",
};

const STATUS_LABEL: Record<Document["status"], string> = {
  ready: "Ready",
  processing: "Processing",
  uploaded: "Uploaded",
  learning: "Learning",
  failed: "Failed",
};

type Props = {
  /** Document the dialog is for. Pass `null` when the dialog is closed
   *  but mounted (the parent may keep it mounted for animation). The
   *  body is gated on this so we never render stats for a missing doc. */
  doc: Document | null;
  /** Controlled by the row's click handler — see `DocumentRow`. */
  open: boolean;
  /** Called when the dialog should close (ESC, backdrop, X button). */
  onOpenChange: (open: boolean) => void;
  /** Called when the user clicks the Edit action. The parent closes
   *  this dialog and opens `EditDocumentDialog`. */
  onEdit: () => void;
  /** Called when the user clicks the Delete action. The parent closes
   *  this dialog and opens `DeleteDocumentDialog`. */
  onDelete: () => void;
};

export function DocumentDetailDialog({
  doc,
  open,
  onOpenChange,
  onEdit,
  onDelete,
}: Props) {
  // The stats query only runs while the dialog is open AND we have a
  // doc. Keeping the `enabled` gate tight means an idle dialog costs
  // nothing — no refetch loop, no network chatter.
  const statsQuery = useQuery({
    queryKey: doc ? documentKeys.stats(doc.id) : ["documents", "stats", "idle"],
    queryFn: () => getDocumentStats(doc!.id),
    enabled: open && Boolean(doc),
    // Stats change slowly (only on worker events). A short staleTime
    // means reopening a dialog picks up the latest numbers without
    // hammering the API.
    staleTime: 15 * 1000,
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        // Wider than the default (max-w-lg) so the two-column stat
        // grid has room to breathe. 32rem feels right for 4 stat tiles
        // across at typical content widths. Capped at `90vh` with a
        // flex column so the dialog can never exceed the viewport —
        // the body scrolls internally when the content (header +
        // stats + progress + logs + footer + error) outgrows the
        // available space. Without this cap the Logs panel used to
        // push the whole dialog off-screen on docs with long
        // histories.
        className="flex max-h-[90vh] max-w-2xl flex-col"
        // The dialog is mounted INSIDE the row's clickable container
        // (a `role="button"` div in `DocumentRow`). Radix Portal
        // detaches the DOM tree, but React's synthetic event system
        // still bubbles clicks up the React parent chain — so a
        // click on the X (or anywhere inside the dialog) used to
        // also fire the row's `setDetailOpen(true)`, racing against
        // the dialog's own `onOpenChange(false)` and leaving the
        // dialog stuck open. Stopping pointer + click propagation
        // here keeps the row's handler out of the picture for every
        // interaction inside the dialog.
        onPointerDown={(event) => event.stopPropagation()}
        onClick={(event) => event.stopPropagation()}
        // When the user clicks the Edit link in the header, the row
        // closes this dialog AND opens `EditDocumentDialog` in the
        // same render. Without preventing `onCloseAutoFocus`, Radix
        // restores focus to this dialog's trigger (the row) at the
        // same moment the edit dialog tries to focus its input —
        // the row wins, focus leaves the edit dialog, and Radix's
        // outside-focus detection closes the edit dialog. Blocking
        // the close-auto-focus lets the edit dialog keep focus.
        onCloseAutoFocus={(event) => event.preventDefault()}
      // Don't trap focus on a stat tile on first paint — let Radix
      // default to the close button, which is what the user
      // expects in a read-first detail view.
      >
        {doc ? (
          <DetailBody
            doc={doc}
            stats={statsQuery.data}
            statsPending={statsQuery.isPending}
            statsError={statsQuery.error}
            onRetryStats={() => void statsQuery.refetch()}
            onEdit={onEdit}
            onDelete={onDelete}
          />
        ) : (
          // Mounted but no doc yet — render an empty shell so Radix
          // stays happy. The body is gated on `doc` so we never
          // show stats for a missing doc.
          <DialogHeader>
            <DialogTitle>Document</DialogTitle>
            <DialogDescription>Loading…</DialogDescription>
          </DialogHeader>
        )}
      </DialogContent>
    </Dialog>
  );
}

/* ---------- Body ---------- */

function DetailBody({
  doc,
  stats,
  statsPending,
  statsError,
  onRetryStats,
  onEdit,
  onDelete,
}: {
  doc: Document;
  stats: DocumentStatsResponse | undefined;
  statsPending: boolean;
  statsError: unknown;
  onRetryStats: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const Icon = ICON_BY_KIND[kindFromFilename(doc.filename)];
  // Match the row's time exactly — the row shows
  // `uploaded_at ?? created_at`, NOT `updated_at`. Showing the
  // worker's last activity time (which can be much more recent than
  // the upload itself, e.g. "just now" for a doc that just flipped
  // to `ready`) made the dialog and the row disagree on what
  // "uploaded" means.
  const uploadedAt =
    formatRelativeTime(doc.uploaded_at ?? doc.created_at) ?? doc.filename;

  // Live status: prefer the WS-refetched stats payload (it reflects
  // the most recent backend state) and fall back to the doc
  // snapshot. The Progress section is ALWAYS rendered — even on
  // terminal `ready` / `failed` — so the bar stays visible with the
  // final counters after the user opens the dialog for an already-
  // processed doc. The stepper handles the empty-data case by
  // falling back to `stats.chunks_embedded / chunks_total` when no
  // live WS tick is in memory.
  const liveStatus = stats?.document?.status ?? doc.status;

  // Which file-action (if any) is in flight. While one is running,
  // the OTHER action stays clickable so the user can switch
  // between View and Download without waiting, but the active one
  // shows a spinner so the click feels acknowledged. We only guard
  // against double-clicking the same action — that would trigger
  // two downloads / popups for one user intent.
  const [pendingAction, setPendingAction] = useState<"view" | "download" | null>(
    null,
  );

  // File actions. While the doc is in `processing` or `failed`, the
  // server returns 409 ("not ready"). The buttons stay rendered
  // (just disabled) so the affordance doesn't appear and disappear
  // mid-lifecycle; the disabled state + tooltip is the user-facing
  // signal. Errors land in a sonner toast with a short human
  // message — the same shape the upload / edit / delete dialogs
  // use, so the user gets one consistent error treatment.
  const canFetch = doc.status !== "processing" && doc.status !== "failed";

  function handleView() {
    if (pendingAction) return;
    setPendingAction("view");
    try {
      // Synchronous: opens a new tab (or navigates the current
      // tab if pop-ups are blocked). No error path — the
      // browser surfaces the backend's response in the new tab
      // and the UI prevents the only realistic failure (409) by
      // disabling the button while the doc isn't ready.
      openDocument(doc.id);
    } finally {
      // Reset the spinner on the next frame so the click feels
      // instant even if the browser takes a moment to render the
      // new tab's chrome.
      requestAnimationFrame(() => setPendingAction(null));
    }
  }

  async function handleDownload() {
    if (pendingAction) return;
    setPendingAction("download");
    try {
      await downloadDocument(doc.id);
    } catch (err) {
      toast.error("Couldn't download document", {
        description: humanError(err, "Something went wrong while saving the file."),
      });
    } finally {
      setPendingAction(null);
    }
  }

  return (
    <>
      {/* ---- Header ---- */}
      <DialogHeader>
        <div className="flex items-start gap-3 pr-8">
          {/* `pr-8` reserves space for Radix's absolute close (X) so a
              long title can't reach the corner and overlap it. */}
          <div className="grid size-10 shrink-0 place-items-center rounded-md bg-primary/10 text-primary">
            <Icon className="size-4" />
          </div>
          <div className="min-w-0 flex-1">
            <DialogTitle className="truncate">{doc.title}</DialogTitle>
            {/* Status pill moved INTO this row (was top-right) so the
                close button has a clean corner. flex-wrap lets the pill
                drop to a second line on long titles without colliding
                with anything.

                The Edit action used to live in the footer as a primary
                button — it competed with Delete and the file
                affordances for attention in a footer that should be
                about "what can I do with this file?". Moving it here
                as a small `variant="link"` text button keeps the
                rename reachable without giving it a heavy CTA. The
                `size="xs"` keeps it visually flush with the metadata
                row so it doesn't shout. */}
            <DialogDescription className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
              <Button
                type="button"
                variant="link"
                size="xs"
                onClick={onEdit}
                className="h-auto px-0 text-xs font-normal text-muted-foreground hover:text-foreground"
                aria-label={`Edit “${doc.title}”`}
              >
                Edit
              </Button>
              <span aria-hidden className="text-muted-foreground/50">
                ·
              </span>
              <span
                className={cn(
                  "shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium",
                  STATUS_PILL_CLASSES[doc.status],
                )}
              >
                {STATUS_LABEL[doc.status]}
              </span>
              <span aria-hidden className="text-muted-foreground/50">
                ·
              </span>
              <span className="truncate font-mono text-muted-foreground">
                {doc.filename}
              </span>
              <span aria-hidden className="text-muted-foreground/50">
                ·
              </span>
              <span className="text-muted-foreground">
                {formatBytes(doc.file_size_bytes)}
              </span>
              <span aria-hidden className="text-muted-foreground/50">
                ·
              </span>
              <span className="text-muted-foreground">{uploadedAt}</span>
            </DialogDescription>
          </div>
        </div>
      </DialogHeader>

      {/* ---- Scrollable body ---- */}
      {/* Everything between the header and the footer lives inside
          a flex-1, overflow-y-auto container so the dialog itself
          never grows past `max-h-[90vh]`. Long log lists (and the
          page/chunk/OCR/embedding stat grid on big docs) scroll
          inside this region while the header and footer stay
          pinned. The `min-h-0` defeats the default `min-height:
          auto` on flex children, which would otherwise let the
          inner content push past the parent's cap. */}
      <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto pr-1">
      {/* ---- Stat grid ---- */}
      <section aria-labelledby="doc-stats-heading" className="flex flex-col gap-3">
        <h3
          id="doc-stats-heading"
          className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground"
        >
          Status
        </h3>
        {statsPending ? (
          <StatsSkeleton />
        ) : statsError ? (
          <StatsError onRetry={onRetryStats} />
        ) : stats ? (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <StatTile
              label="Pages"
              value={stats.page_count}
              sub={stats.pages_needing_ocr > 0
                ? `${stats.pages_needing_ocr} need OCR`
                : "No OCR needed"}
            />
            <StatTile
              label="Chunks"
              value={stats.chunks_total}
              sub={
                stats.chunks_pending > 0
                  ? `${stats.chunks_pending} pending`
                  : stats.chunks_total === 0
                    ? "Not chunked yet"
                    : "All chunked"
              }
            />
            <StatTile
              label="Embedded"
              value={stats.chunks_embedded}
              sub={
                stats.chunks_failed > 0
                  ? `${stats.chunks_failed} failed`
                  : stats.chunks_total === 0
                    ? "—"
                    : "All embedded"
              }
            />
            <StatTile
              label="Status"
              value={null}
              sub={null}
              // The "Status" tile is special — instead of a number it
              // shows the embedding-status breakdown as small badges,
              // so the user can see the histogram at a glance.
              customBody={
                <EmbeddingStatusBreakdown counts={stats.embedding_status_counts} />
              }
            />
          </div>
        ) : null}

        {/* Embedding models — only render if there's at least one. A
            doc with no embeddings just shows the empty grid above. */}
        {stats && stats.embedding_models.length > 0 ? (
          <div className="flex flex-col gap-2">
            <h4 className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
              Embedding models
            </h4>
            <div className="flex flex-wrap gap-1.5">
              {stats.embedding_models.map((model) => (
                <Badge
                  key={model}
                  variant="muted"
                  className="font-mono text-[11px]"
                  title={model}
                >
                  {model}
                </Badge>
              ))}
            </div>
          </div>
        ) : null}
      </section>

      {/* ---- Live progress (always visible) ---- */}
      {/* The bar is intended to NEVER disappear: it's visible while
          the doc is being processed (driven by `document.embedding_progress`
          WS events) AND after the doc settles to `ready` / `failed`
          (driven by `stats.chunks_embedded / chunks_total`). This
          way, a user who opens the dialog for a doc that was
          processed before they connected still sees a meaningful,
          filled bar — not an empty placeholder. */}
      <section
        aria-labelledby="doc-progress-heading"
        className="flex flex-col gap-2"
      >
        <h3
          id="doc-progress-heading"
          className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground"
        >
          Progress
        </h3>
        <DocumentProgressStepper
          documentId={stats?.document?.id ?? doc.id}
          status={liveStatus}
          stats={stats}
        />
      </section>

      {/* ---- Logs (lifetime event history) ---- */}
      <section
        aria-labelledby="doc-logs-heading"
        className="flex flex-col gap-2"
      >
        <DocumentLogsPanel documentId={stats?.document?.id ?? doc.id} />
      </section>

      {/* Server error message on failed docs — small, restrained, in
          destructive semantic tokens. Lives inside the scrollable
          body so it doesn't compete with the footer for attention. */}
      {doc.status === "failed" && doc.error_message ? (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive"
        >
          <AlertCircle className="mt-0.5 size-3.5 shrink-0" />
          <span className="line-clamp-3">{doc.error_message}</span>
        </div>
      ) : null}
      </div>

      {/* ---- Footer actions ---- */}
      {/* Footer is OUTSIDE the scrollable body and `flex-shrink-0` so
          the action buttons stay anchored at the bottom regardless
          of how long the scroll region gets. Negative margins re-
          connect the footer's background to the dialog's edge. */}
      <div className="-mx-6 -mb-6 flex shrink-0 flex-col-reverse gap-2 border-t border-border bg-background px-6 py-4 sm:flex-row sm:items-center sm:justify-between">
        {/* Delete sits alone on the left — destructive actions get
            visual separation from the other actions so a fat-finger
            can't reach them by accident. The right-hand group now
            owns the file affordances (View + Download). Edit moved to
            the header as a text link, so it doesn't compete here. */}
        <Button
          variant="destructive"
          onClick={onDelete}
          aria-label={`Delete “${doc.title}”`}
          className="sm:w-auto"
        >
          <Trash2 className="size-3.5" />
          Delete
        </Button>
        {/* View + Download on the right. Both call the same helpers
            the row's 3-dot menu uses — `canFetch` greys them out
            while the doc is still processing or in failed state.
            We `flex-col-reverse` the inner group so on mobile the
            file actions sit on top (the thumb-reach zone), matching
            the DialogFooter convention. */}
        <div className="flex flex-col-reverse gap-2 sm:flex-row sm:items-center">
          <Button
            variant="outline"
            disabled={!canFetch || pendingAction !== null}
            onClick={() => void handleView()}
            aria-label={`View “${doc.title}”`}
            title={
              canFetch
                ? "Open the file in a new tab"
                : "This file isn't ready to view yet"
            }
          >
            {pendingAction === "view" ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Eye className="size-3.5" />
            )}
            View
          </Button>
          <Button
            variant="outline"
            disabled={!canFetch || pendingAction !== null}
            onClick={() => void handleDownload()}
            aria-label={`Download “${doc.title}”`}
            title={
              canFetch
                ? "Save the file to your device"
                : "This file isn't ready to download yet"
            }
          >
            {pendingAction === "download" ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Download className="size-3.5" />
            )}
            Download
          </Button>
        </div>
      </div>
    </>
  );
}

/* ---------- Pieces ---------- */

/**
 * A single statistic tile — label + big value + supporting sub-line.
 * Kept intentionally compact (single number, no card chrome) so the
 * four-tile row reads as a strip, not a wall of cards.
 */
function StatTile({
  label,
  value,
  sub,
  customBody,
}: {
  label: string;
  value: number | null;
  sub: string | null;
  customBody?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1 rounded-md border border-border bg-card p-3">
      <span className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
        {label}
      </span>
      {customBody ? (
        customBody
      ) : (
        <>
          <span className="text-xl font-semibold tabular-nums tracking-tight text-foreground">
            {value ?? "—"}
          </span>
          {sub ? (
            <span className="text-[11px] text-muted-foreground">{sub}</span>
          ) : null}
        </>
      )}
    </div>
  );
}

/**
 * Renders the embedding-status histogram inside the "Status" tile.
 * The design system explicitly bans painting the brand lime on every
 * positive state, so we use:
 *  - `bg-accent` (subtle brand tint) + `text-accent-foreground` for
 *    succeeded — the only positive status that earns the brand
 *    treatment, because a successful embedding IS the headline signal
 *  - `bg-destructive/10 text-destructive` for failed
 *  - `bg-muted text-muted-foreground` for everything else (pending,
 *    processing, skipped)
 */
function EmbeddingStatusBreakdown({
  counts,
}: {
  counts: Record<string, number>;
}) {
  const entries = Object.entries(counts).filter(([, n]) => n > 0);
  if (entries.length === 0) {
    return (
      <span className="text-[11px] text-muted-foreground">
        No embeddings yet
      </span>
    );
  }
  return (
    <div className="flex flex-wrap gap-1">
      {entries.map(([status, n]) => {
        const { variant, className } = badgeClassForStatus(status);
        return (
          <Badge
            key={status}
            variant={variant}
            className={cn("text-[10px]", className)}
            title={`${n} ${status}`}
          >
            {n} {status}
          </Badge>
        );
      })}
    </div>
  );
}

/**
 * Pick the Badge variant AND any extra className needed for an
 * embedding-status count. The codebase Badge variants are
 * `default | secondary | outline | muted | success | warning`
 * (no `destructive`), so the "failed" case needs an extra class to
 * pick up the destructive semantic tokens. We compose variant +
 * className in one place so the rest of the component stays clean.
 */
function badgeClassForStatus(status: string): {
  variant: "success" | "muted" | "default" | "secondary" | "outline" | "warning";
  className?: string;
} {
  if (status === "succeeded") {
    // `bg-accent` is the subtle brand-tinted surface in the design
    // system — used here because a successful embedding IS the
    // headline signal of the tile. Other positive states do NOT
    // earn the brand treatment.
    return { variant: "success" };
  }
  if (status === "failed") {
    // No `destructive` Badge variant exists, so we layer the
    // destructive tokens on top of the neutral `muted` variant.
    return {
      variant: "muted",
      className: "bg-destructive/10 text-destructive",
    };
  }
  // pending, processing, skipped, anything else → neutral.
  return { variant: "muted" };
}

/**
 * Skeleton for the four-tile row. Uses the same wrapper classes as
 * the real tiles so the dialog doesn't reflow when the data lands.
 */
function StatsSkeleton() {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      {Array.from({ length: 4 }).map((_, i) => (
        <div
          key={i}
          className="flex flex-col gap-2 rounded-md border border-border bg-card p-3"
        >
          <span className="h-3 w-12 rounded bg-muted" />
          <span className="h-6 w-10 rounded bg-muted" />
          <span className="h-2.5 w-16 rounded bg-muted" />
        </div>
      ))}
    </div>
  );
}

/**
 * Inline error strip for a failed stats fetch. Same visual language
 * as the `LibraryError` and `PanelError` already in the codebase so
 * the user gets one consistent error treatment across the documents
 * page.
 */
function StatsError({ onRetry }: { onRetry: () => void; }) {
  return (
    <div
      role="alert"
      className="flex items-start gap-3 rounded-md border border-destructive/30 bg-destructive/5 px-4 py-3"
    >
      <AlertCircle className="mt-0.5 size-4 shrink-0 text-destructive" />
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium text-foreground">
          Couldn&apos;t load stats
        </p>
        <p className="text-xs text-muted-foreground">
          Try again, or close the dialog and reopen it.
        </p>
      </div>
      <Button size="sm" variant="outline" onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}