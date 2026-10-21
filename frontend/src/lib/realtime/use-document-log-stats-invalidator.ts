"use client";

import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { documentKeys } from "@/lib/api/documents";

import { useWSStore, type WSMessage } from "./ws-store";

/**
 * Global invalidator: every `document.log` WS event invalidates the
 * per-doc stats query so the detail dialog's stat tiles refetch on
 * the next paint.
 *
 * Why is this a SEPARATE global hook and not folded into
 * `useDocumentLogBridge`?
 *
 * `useDocumentLogBridge` is page-scoped: it only runs while a page
 * that renders the per-doc activity panel is mounted (e.g. the
 * detail dialog or the page-level `DocumentActivityPanel`). When the
 * user closes the detail dialog, that bridge unmounts, and any
 * `document.log` event that arrives while the dialog is closed is
 * ignored. The next time the user opens the dialog, the stats query
 * is still considered fresh (`staleTime: 15_000` on the dialog's
 * `useQuery`) and doesn't refetch — so the stat tiles show stale
 * numbers.
 *
 * Mounting the invalidator at app root (inside `RealtimeBridge`)
 * makes the invalidation fire regardless of which page is mounted.
 * The DB is the source of truth for stats, so we re-read rather
 * than try to derive counters from the WS payload (which can drift
 * if an event is missed).
 *
 * Wire format (must match `app.schemas.ws_events.WsEvent`):
 *   { type: "document.log", data: { log: DocumentLogItem }, ts }
 */
export function useDocumentLogStatsInvalidator() {
  const subscribe = useWSStore((s) => s.subscribe);
  const queryClient = useQueryClient();

  useEffect(() => {
    return subscribe((msg: WSMessage) => {
      if (msg.type !== "document.log") return;
      const data = (msg.data ?? {}) as { log?: { document_id?: string } };
      const docId = data.log?.document_id;
      if (!docId) return;
      void queryClient.invalidateQueries({
        queryKey: documentKeys.stats(docId),
      });
    });
  }, [subscribe, queryClient]);
}
