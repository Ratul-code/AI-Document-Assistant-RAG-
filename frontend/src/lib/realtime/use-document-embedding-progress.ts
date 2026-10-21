"use client";

import { useEffect, useState } from "react";

import { useWSStore, type WSMessage } from "./ws-store";

/**
 * One tick of the `document.embedding_progress` WebSocket event,
 * shaped for the per-document stepper + progress bar.
 *
 * All numeric counters are nullable because the backend sends
 * `null` for counters it doesn't yet know (e.g. `chunks_total` is
 * null until the worker has counted pending chunks). The stepper
 * gates visibility on `chunks_total !== null && chunks_total > 0`
 * so we never render an indeterminate bar.
 */
export type EmbeddingProgress = {
  phase: "parsing" | "chunking" | "embedding" | null;
  pages_total: number | null;
  pages_done: number | null;
  chunks_total: number | null;
  chunks_done: number | null;
  failed_total: number;
  model_name: string | null;
  /** ISO timestamp from the WS envelope; used for staleness guards. */
  updated_at: string;
};

/**
 * Coerce an arbitrary value (the WS payload) into a number, or fall
 * back to a default. `Number(null) === 0` and `Number(undefined) ===
 * NaN`, so we have to handle the null/NaN cases explicitly — we want
 * `null` to stay `null` for the `chunks_total` / `chunks_done`
 * "we don't know yet" signal, and we want bad payloads to be
 * `0` for `failed_total` (a count, never null).
 */
function asIntOrNull(v: unknown): number | null {
  if (v === null || v === undefined) return null;
  const n = Number(v);
  return Number.isFinite(n) ? Math.trunc(n) : null;
}

function asIntOrZero(v: unknown): number {
  if (v === null || v === undefined) return 0;
  const n = Number(v);
  return Number.isFinite(n) ? Math.trunc(n) : 0;
}

function asPhase(v: unknown): EmbeddingProgress["phase"] {
  return v === "parsing" || v === "chunking" || v === "embedding"
    ? v
    : null;
}

/**
 * Subscribe to `document.embedding_progress` events for ONE document.
 *
 * Pattern: per-doc local state + `useWSStore.subscribe` effect, the
 * same shape as `useDocumentLogBridge`. A global store would be
 * overkill — only one doc's progress is ever shown at a time, and
 * the per-doc effect cleans up automatically on unmount.
 *
 * Returns the latest tick for the requested doc, or `null` if no
 * event has been seen yet this lifetime. The stepper checks for
 * `null` to decide whether to show the bar at all.
 *
 * Filtering: the WS envelope includes `user_id` and `data.document_id`;
 * the backend only sends to the doc's owner, so user-scoping is
 * implicit. We only match on `document_id`.
 */
export function useDocumentEmbeddingProgress(
  documentId: string | null,
): EmbeddingProgress | null {
  // The WS handler doesn't know which doc the hook is currently
  // scoped to, so we store BOTH the tick and the id it came from.
  // On render, we project the slice for the requested `documentId`
  // (or `null` if the cached tick belongs to a different doc — the
  // case immediately after the user switches docs in the UI).
  type Tick = { docId: string; progress: EmbeddingProgress };
  const [tick, setTick] = useState<Tick | null>(null);
  const subscribe = useWSStore((s) => s.subscribe);

  useEffect(() => {
    if (!documentId) return;

    return subscribe((msg: WSMessage) => {
      if (msg.type !== "document.embedding_progress") return;
      const data = (msg.data ?? {}) as {
        document_id?: string;
        phase?: string;
        pages_total?: unknown;
        pages_done?: unknown;
        chunks_total?: unknown;
        chunks_done?: unknown;
        failed_total?: unknown;
        model_name?: unknown;
      };
      if (data.document_id !== documentId) return;

      setTick({
        docId: documentId,
        progress: {
          phase: asPhase(data.phase),
          pages_total: asIntOrNull(data.pages_total),
          pages_done: asIntOrNull(data.pages_done),
          chunks_total: asIntOrNull(data.chunks_total),
          chunks_done: asIntOrNull(data.chunks_done),
          failed_total: asIntOrZero(data.failed_total),
          model_name:
            typeof data.model_name === "string" && data.model_name
              ? data.model_name
              : null,
          updated_at: String(msg.ts ?? new Date().toISOString()),
        },
      });
    });
  }, [documentId, subscribe]);

  // Project for the current document. If the cached tick is for a
  // different doc (the moment the user switches), return `null` so
  // the stepper shows the "no tick yet" state until the new doc's
  // first event lands.
  if (!tick || tick.docId !== documentId || !documentId) return null;
  return tick.progress;
}
