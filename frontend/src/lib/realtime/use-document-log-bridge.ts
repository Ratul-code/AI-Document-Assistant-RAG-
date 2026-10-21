"use client";

import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  documentKeys,
  type DocumentLogItem,
} from "@/lib/api/documents";

import { useWSStore, type WSMessage } from "./ws-store";

/**
 * Bridges `document.log` WebSocket events to BOTH:
 *
 *   1. The per-document activity-log query cache (patches the
 *      detail dialog's "Activity" / "Logs" panel).
 *   2. The documents-list query cache — invalidated so any open
 *      doc list refetches the latest rows. The list is cheap
 *      to refetch and we want it to converge immediately when
 *      a log lands (e.g. a "failed" log row that signals a doc
 *      that should now show the Failed pill).
 *
 * The per-doc stats query cache is invalidated by
 * `useDocumentLogStatsInvalidator`, which is mounted globally
 * inside `RealtimeBridge`. Doing the invalidation there (rather
 * than in this page-scoped bridge) means the detail dialog's
 * stat tiles refresh even when the per-doc panel is unmounted
 * (e.g. dialog closed while a worker event is in flight).
 *
 * The global cross-doc activity feed (right-side "Activity" panel
 * on /documents) is NOT patched by this bridge — it has its own
 * shape (`[...documentKeys.logs(), { limit, offset }]` with no
 * documentId in the key) and is patched by `useGlobalLogBridge`.
 * That hook lives in the same file family and runs alongside this
 * one; they are independent so either can be mounted alone.
 *
 * `event_type` follows the lifecycle contract
 * (processing | uploaded | learning | ready | failed | ...) but the
 * bridge treats it as an opaque string so any future value the
 * server starts sending flows through unchanged.
 *
 * **Mounting**: this hook is page-scoped. Mount it on every page
 * that renders the per-document activity section, and also on
 * pages that show the live doc list so the list refreshes when
 * a log event lands. The global cross-doc feed bridge
 * (`useGlobalLogBridge`) is independent and should be mounted
 * where that feed is rendered.
 */
export function useDocumentLogBridge() {
  const subscribe = useWSStore((s) => s.subscribe);
  const queryClient = useQueryClient();

  useEffect(() => {
    return subscribe((msg: WSMessage) => {
      if (msg.type !== "document.log") return;

      // Wire format (must match `app.schemas.ws_events.WsEvent`):
      //   { type: "document.log", data: { log: DocumentLogItem }, ts }
      const data = (msg.data ?? {}) as { log?: Partial<DocumentLogItem> };
      const raw = data.log ?? {};
      const item: DocumentLogItem = {
        id: String(raw.id ?? ""),
        document_id: String(raw.document_id ?? ""),
        event_type: String(raw.event_type ?? "") as DocumentLogItem["event_type"],
        message: (raw.message as string | null) ?? null,
        metadata: (raw.metadata as Record<string, unknown> | null) ?? null,
        created_at: String(raw.created_at ?? ""),
        document_title: (raw.document_title as string | null) ?? null,
        file_type: (raw.file_type as string | null) ?? null,
      };
      if (!item.id || !item.document_id) {
        // Server sent a malformed event; ignore rather than crash.
        return;
      }

      // ---- Patch the per-document activity panel ----
      queryClient.setQueriesData<{
        items: DocumentLogItem[];
        total: number;
        limit: number;
        offset: number;
      }>(
        {
          queryKey: [...documentKeys.logs(), item.document_id],
          exact: false,
        },
        (prev) => {
          if (!prev) return prev;
          // Skip duplicates if the server happens to send the same
          // event twice (e.g. reconnect-with-replay).
          if (prev.items.some((existing) => existing.id === item.id)) return prev;
          return {
            ...prev,
            items: [item, ...prev.items],
            total: prev.total + 1,
          };
        },
      );

      // ---- Refresh derived data from the DB ----
      // The DB is the source of truth for the list — invalidating
      // here means a single `document.log` event causes the doc list
      // to re-fetch from the backend, so the user sees the true
      // counter values without us trying to keep them in sync from
      // the WS payload alone.
      //
      // Stats invalidation lives in `useDocumentLogStatsInvalidator`,
      // mounted globally by `RealtimeBridge` — that way the dialog's
      // stat tiles refresh even when this page-scoped bridge is
      // unmounted (e.g. when the detail dialog is closed but other
      // pages are listening for log events).
      void queryClient.invalidateQueries({ queryKey: documentKeys.lists() });
    });
  }, [subscribe, queryClient]);
}

/**
 * Convenience: refetch the per-doc log list. Call from a page
 * component's mount effect when you want a fresh pull.
 *
 * Exported separately so doc detail pages can opt in without
 * needing to know about query keys.
 */
export async function refetchDocumentLogs(
  queryClient: ReturnType<typeof useQueryClient>,
  documentId: string,
) {
  await queryClient.invalidateQueries({
    queryKey: documentKeys.log(documentId, {}),
  });
}

/** Refetch only the global feed. */
export async function refetchMyLogs(
  queryClient: ReturnType<typeof useQueryClient>,
) {
  await queryClient.invalidateQueries({ queryKey: documentKeys.logs() });
}