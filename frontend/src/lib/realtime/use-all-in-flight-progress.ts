"use client";

import { useEffect, useState } from "react";

import { useWSStore, type WSMessage } from "./ws-store";

export type InFlightProgress = {
  phase: "parsing" | "chunking" | "embedding" | null;
  pages_total: number | null;
  pages_done: number | null;
  chunks_total: number | null;
  chunks_done: number | null;
  failed_total: number;
  model_name: string | null;
  updated_at: string;
  /** Most-recent log message we observed via `document.log` for
   *  this doc. Updated on every matching log event so the strip
   *  can show "Page 3/12 chunked" etc. without polling. */
  latest_message: string | null;
  /** Most-recent log event_type for the doc. */
  latest_event_type: string | null;
};

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

function asPhase(v: unknown): InFlightProgress["phase"] {
  return v === "parsing" || v === "chunking" || v === "embedding"
    ? v
    : null;
}

/**
 * Page-level aggregator: subscribes to `document.embedding_progress`
 * AND `document.log` events, and returns a `Map<documentId,
 * InFlightProgress>` containing the latest snapshot for every
 * document the user owns.
 *
 * Used by the live progress strip on `/documents` to render one
 * row per in-flight doc, all driven from a single WS subscription
 * (cheaper than N independent `useDocumentEmbeddingProgress`
 * hooks, which each register their own handler).
 *
 * Why a `Map` and not a flat array: the strip is keyed by doc
 * (so the user can correlate the row with the doc list), and
 * updates need to be O(1) per event — `Map.set` is.
 *
 * `Map` is stable across renders when there are no events (the
 * hook returns the same reference until a new event mutates it),
 * so memoized consumers don't churn.
 *
 * Lifetime: ticks live in the hook's local state. On unmount, the
 * WS subscription is cleaned up automatically. The strip should
 * re-mount this hook at the page level.
 */
export function useAllInFlightProgress(): Map<string, InFlightProgress> {
  const [ticks, setTicks] = useState<Map<string, InFlightProgress>>(
    () => new Map(),
  );
  const subscribe = useWSStore((s) => s.subscribe);

  useEffect(() => {
    return subscribe((msg: WSMessage) => {
      // ---- Progress event: counters for one doc ----
      if (msg.type === "document.embedding_progress") {
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
        if (typeof data.document_id !== "string" || !data.document_id) {
          return;
        }
        const docId = data.document_id;

        setTicks((prev) => {
          const existing = prev.get(docId);
          const next = new Map(prev);
          next.set(docId, {
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
            // Preserve the latest log line if we already have one for
            // this doc. The progress event itself carries no message.
            latest_message: existing?.latest_message ?? null,
            latest_event_type: existing?.latest_event_type ?? null,
          });
          return next;
        });
        return;
      }

      // ---- Log event: pick up the latest human-readable line ----
      if (msg.type === "document.log") {
        const data = (msg.data ?? {}) as { log?: { document_id?: string; message?: string | null; event_type?: string } };
        const docId = data.log?.document_id;
        const message = data.log?.message ?? null;
        const eventType = data.log?.event_type ?? null;
        if (typeof docId !== "string" || !docId) return;

        setTicks((prev) => {
          // If we don't already have a progress entry for this doc,
          // ignore — we only show messages for docs that the strip
          // is tracking (i.e. they're in flight). Otherwise every
          // ready-doc's log row would also appear here.
          const existing = prev.get(docId);
          if (!existing) return prev;
          if (
            existing.latest_message === message &&
            existing.latest_event_type === eventType
          ) {
            return prev;
          }
          const next = new Map(prev);
          next.set(docId, {
            ...existing,
            latest_message: message,
            latest_event_type: eventType,
          });
          return next;
        });
        return;
      }

      // ---- Status event: a doc just left (or entered) an in-flight
      // status. We don't remove the entry here — the strip's
      // "in-flight only" filter (status in processing | uploaded |
      // learning) is the source of truth for visibility, and the
      // docs-list refetched by the status bridge will tell us.
      // The tick stays cached for a short while so the row doesn't
      // blink off on the status event before the refetch lands.
    });
  }, [subscribe]);

  return ticks;
}
