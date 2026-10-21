"use client";

import { CheckCircle2, Loader2, XCircle } from "lucide-react";

import { Progress } from "@/components/ui/progress";
import type {
  DocumentStatsResponse,
  DocumentStatus,
} from "@/lib/api/documents";
import { useDocumentEmbeddingProgress } from "@/lib/realtime/use-document-embedding-progress";
import { cn } from "@/lib/utils";

/**
 * Live progress section for a document's processing pipeline.
 *
 * Renders ABOVE the Logs section in the detail dialog. The section
 * is ALWAYS visible — even after a doc settles to `ready` or
 * `failed` — so the bar remains in the dialog as a permanent record
 * of how the doc was processed.
 *
 * Data source priority (high → low):
 *
 *  1. Live `document.embedding_progress` WS tick from
 *     `useDocumentEmbeddingProgress`. This is the source of truth
 *     while the doc is being processed: it carries the active
 *     phase, the running `chunks_done` numerator, and the
 *     `chunks_total` denominator.
 *  2. `stats` payload (passed in by the parent, fetched via
 *     `getDocumentStats`). Used as a fallback when the WS hasn't
 *     delivered a tick yet — most importantly, when the user opens
 *     the dialog for a doc that was processed before the page
 *     connected. In that case we render the bar with
 *     `chunks_embedded / chunks_total` and label the phase
 *     "Embedding" (the final phase).
 *  3. `null` progress + `null` stats — render a "Starting…"
 *     placeholder so the section never reads as empty.
 *
 * Layout:
 *  - Header row: status icon (spinner / check / x) + phase label on
 *    the left, live counter on the right.
 *  - Full-width `Progress` bar.
 *  - Subtitle row: model name + failed count (in destructive tokens
 *    when present) on the left, percent on the right.
 *
 * No new dependencies — same shadcn `Progress` primitive. Visual
 * direction follows `docs/DESIGN.md`: monochrome chrome, `bg-primary`
 * for the active progress fill, destructive tokens only for the
 * failed-count text.
 */

type Props = {
  documentId: string;
  status: DocumentStatus;
  /**
   * The same stats payload the dialog fetches for the stat grid.
   * Used as a fallback for the bar's numerator/denominator when no
   * WS progress event is in memory (e.g. the user opened the dialog
   * after the doc was already processed). Optional because the
   * stats query can be pending; the component handles all three
   * states (live / stats / neither) below.
   */
  stats?: DocumentStatsResponse;
};

type WsPhase = "parsing" | "chunking" | "embedding" | null;

const PHASE_LABEL: Record<NonNullable<WsPhase>, string> = {
  parsing: "Parsing",
  chunking: "Chunking",
  embedding: "Embedding",
};

/**
 * Pick the header label. Prefers the WS phase when present;
 * otherwise falls back to a sensible label from the coarse doc
 * status, so the header is never blank.
 */
function phaseLabelFor(
  status: DocumentStatus,
  phase: WsPhase,
): string {
  if (phase) return PHASE_LABEL[phase];
  if (status === "uploaded") return "Preparing…";
  if (status === "processing" || status === "learning") {
    return "Working on it…";
  }
  // Terminal — the icon next to the label already conveys the
  // outcome, so keep the label short and consistent.
  if (status === "ready") return "Embedding";
  if (status === "failed") return "Embedding";
  return "Processing";
}

export function DocumentProgressStepper({ documentId, status, stats }: Props) {
  const live = useDocumentEmbeddingProgress(documentId);

  // Build a unified "snapshot" the bar will render against. Live WS
  // wins; stats fill in when WS is silent.
  const chunksTotal = live?.chunks_total ?? (stats?.chunks_total ?? null);
  const chunksDone = live?.chunks_done ?? (stats?.chunks_embedded ?? null);
  const failedTotal = live?.failed_total ?? (stats?.chunks_failed ?? 0);
  const phase: WsPhase = live?.phase ?? (status === "ready" || status === "failed" ? "embedding" : null);
  const modelName = live?.model_name ?? (stats?.embedding_models?.[0] ?? null);

  // ---- No denominator yet (no live WS, no stats) ----
  // For an in-flight doc this is the brief moment between "claim
  // for processing" and "first progress event" — show a spinner
  // placeholder so the bar slot doesn't read as empty.
  // For a terminal doc with no chunks at all (parse failure on an
  // empty file, etc.) show a settled "Failed / Ready · no chunks"
  // line so the section stays anchored.
  if (chunksTotal === null || chunksTotal <= 0) {
    if (status === "ready" || status === "failed") {
      return <TerminalSettledLine status={status} />;
    }
    return <StartingState phaseLabel={phaseLabelFor(status, null)} />;
  }

  const safeDone = chunksDone ?? (status === "ready" ? chunksTotal : 0);
  const percent = Math.min(
    100,
    Math.max(0, Math.round((safeDone / chunksTotal) * 100)),
  );
  const isDone = safeDone >= chunksTotal;
  const isFailed = status === "failed";
  const label = phaseLabelFor(status, phase);
  const hasFailed = failedTotal > 0;

  return (
    <div
      className={cn(
        "flex flex-col gap-2 rounded-md border p-3",
        isFailed
          ? "border-destructive/30 bg-destructive/5"
          : "border-border/60 bg-muted/30",
      )}
      aria-label="Document processing progress"
    >
      {/* Header row: status icon + phase label on the left, live
          counter on the right. tabular-nums keeps the number
          column from jumping as digits change. */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <StatusIcon status={status} isDone={isDone} />
          <span className="truncate text-xs font-medium text-foreground">
            {label}
          </span>
        </div>
        <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
          <span className="text-foreground">
            {safeDone.toLocaleString()}
          </span>
          {" / "}
          {chunksTotal.toLocaleString()}
        </span>
      </div>

      {/* Single full-width progress bar. Value is clamped 0..100
          above; the bar's own transition handles the fill motion. */}
      <Progress
        value={percent}
        aria-label={`${safeDone} of ${chunksTotal} chunks embedded`}
      />

      {/* Subtitle: model name + failed count when relevant. Surgical
          use of destructive tokens — only the failed-count text is
          red, per `docs/DESIGN.md`. */}
      <div className="flex items-center justify-between gap-2 text-[11px] tabular-nums text-muted-foreground">
        <span className="truncate">
          {modelName ? <span className="font-mono">{modelName}</span> : null}
          {hasFailed ? (
            <span
              className={cn(
                modelName ? "ml-2" : "",
                "text-destructive",
              )}
            >
              {failedTotal.toLocaleString()} failed
            </span>
          ) : null}
        </span>
        <span>{percent}%</span>
      </div>
    </div>
  );
}

/* ---------- Pieces ---------- */

/**
 * Icon shown next to the phase label. Spinner for in-flight,
 * check for ready, x for failed. Kept tiny so the row reads as
 * a status indicator, not a button.
 */
function StatusIcon({
  status,
  isDone,
}: {
  status: DocumentStatus;
  isDone: boolean;
}) {
  if (status === "failed") {
    return <XCircle className="size-3.5 shrink-0 text-destructive" />;
  }
  if (status === "ready" || isDone) {
    return <CheckCircle2 className="size-3.5 shrink-0 text-accent-foreground" />;
  }
  return (
    <Loader2 className="size-3.5 shrink-0 animate-spin text-muted-foreground" />
  );
}

/**
 * Settled line for terminal docs that have zero chunks (e.g. an
 * empty page result, or a doc processed before any chunks landed).
 * Keeps the section anchored instead of returning `null` so the
 * dialog never shows a gap above the logs panel.
 */
function TerminalSettledLine({ status }: { status: DocumentStatus }) {
  const Icon = status === "failed" ? XCircle : CheckCircle2;
  const tone =
    status === "failed"
      ? "border-destructive/30 bg-destructive/5 text-destructive"
      : "border-border/60 bg-muted/30 text-foreground";
  const label = status === "failed" ? "Failed" : "Ready";
  return (
    <div
      className={cn(
        "flex items-center gap-2 rounded-md border px-3 py-2",
        tone,
      )}
      role="status"
      aria-live="polite"
      aria-label={label}
    >
      <Icon className="size-3.5 shrink-0" />
      <span className="text-xs font-medium">{label}</span>
      <span className="text-[11px] text-muted-foreground">· no chunks</span>
    </div>
  );
}

/**
 * Placeholder rendered while a doc is in flight but the first
 * `document.embedding_progress` WS tick hasn't arrived yet AND the
 * stats query is still pending. Returning `null` would leave the
 * section visually empty. The chrome is intentionally similar to
 * the real bar so the layout doesn't jump when the first numbers
 * land.
 */
function StartingState({ phaseLabel }: { phaseLabel: string }) {
  return (
    <div
      className="flex items-center gap-2 rounded-md border border-border/60 bg-muted/30 px-3 py-2"
      role="status"
      aria-live="polite"
      aria-label={`${phaseLabel} — awaiting first progress event`}
    >
      <Loader2 className="size-3.5 animate-spin text-muted-foreground" />
      <span className="text-xs text-muted-foreground">
        {phaseLabel}…
      </span>
    </div>
  );
}
