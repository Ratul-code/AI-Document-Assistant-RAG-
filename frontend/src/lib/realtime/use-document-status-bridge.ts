"use client";

import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { documentKeys } from "@/lib/api/documents";
import { useWSStore, type WSMessage } from "./ws-store";

/**
 * Bridges `document.status` WebSocket events to the documents query
 * cache.
 *
 * Wire format (must match `app.schemas.ws_events.WsEvent` server-side):
 *   { type: "document.status",
 *     data: { document_id, title, status, error_message? }, ts }
 *
 * On every status event we invalidate four keys so the UI stays
 * in sync without the user refreshing:
 *
 *   - `documentKeys.lists()` — refetches the doc list (status pill
 *     on each row updates). Cheap and the common case.
 *   - `documentKeys.stats(id)` — refetches the per-doc stats. The
 *     detail dialog reads its `status` from the stats payload (so
 *     the "Progress" section can hide the moment the doc lands on
 *     `ready` or `failed`), and the four stat tiles in the dialog
 *     (Pages / Chunks / Embedded / Status) need to refetch anyway
 *     when the doc transitions.
 *   - `documentKeys.detail(id)` — reserved for any future per-doc
 *     detail query. Cheap to invalidate even if no consumer exists
 *     today.
 *   - `documentKeys.logs()` — refetches BOTH the per-doc activity
 *     log AND the global / cross-doc activity feed. The status
 *     event we push on delete fires with `status="failed"` and a
 *     `error_message="Document deleted by user."`; without this
 *     invalidation, any log rows the server just cascaded away
 *     would still be in the cached feed until the user refreshed
 *     the page.
 *
 * Mount once at app root alongside `useWSToasts` (toast side). The
 * per-document log listener (`useDocumentLogBridge`) is intentionally
 * page-scoped — do NOT add it here.
 */
export function useDocumentStatusBridge() {
  const subscribe = useWSStore((s) => s.subscribe);
  const queryClient = useQueryClient();

  useEffect(() => {
    return subscribe((msg: WSMessage) => {
      if (msg.type !== "document.status") return;

      const data = (msg.data ?? {}) as { document_id?: string };
      const docId = typeof data.document_id === "string" ? data.document_id : null;

      void queryClient.invalidateQueries({ queryKey: documentKeys.lists() });
      // Invalidate the entire logs namespace — covers both the
      // per-doc activity panel (`documentKeys.log(documentId, ...)`)
      // and the cross-doc global feed (`documentKeys.logs()`). The
      // per-doc entries won't 404 (the doc is gone so the next
      // fetch is empty) and the global feed will drop rows for the
      // deleted doc. Cheap relative to a refetch on every render.
      void queryClient.invalidateQueries({ queryKey: documentKeys.logs() });
      if (docId) {
        void queryClient.invalidateQueries({
          queryKey: documentKeys.stats(docId),
        });
        void queryClient.invalidateQueries({
          queryKey: documentKeys.detail(docId),
        });
      }
    });
  }, [subscribe, queryClient]);
}
