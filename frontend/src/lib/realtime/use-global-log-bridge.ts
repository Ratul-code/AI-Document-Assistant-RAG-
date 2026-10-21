"use client";

import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { documentKeys, type DocumentLogItem } from "@/lib/api/documents";

import { useWSStore, type WSMessage } from "./ws-store";

/**
 * Bridges `document.log` WebSocket events to the **cross-doc**
 * activity-feed query cache (the right-side "Activity" panel on
 * `/documents`, or any feed that calls `useMyLogs`).
 *
 * The per-doc panel uses a different key shape
 * (`[...documentKeys.logs(), documentId, params]`) and is patched
 * by `useDocumentLogBridge` instead. The two bridges are
 * independent so each can be mounted where its shape is rendered.
 *
 * Wire format (must match `app.schemas.ws_events.WsEvent`):
 *   { type: "document.log", data: { log: DocumentLogItem }, ts }
 *
 * Implementation note: the global feed's first page is fetched via
 * `useMyLogs` with the key
 *   [...documentKeys.logs(), { limit, offset }]
 * — note there is NO documentId between the "logs" segment and the
 * params. We match this key shape by checking that the second-to-last
 * segment of any matched query key is an object (i.e. `{limit,
 * offset}`), not a string (which would be a documentId from the
 * per-doc key).
 *
 * Behavior on every `document.log` event:
 *   1. Prepend the new row into any matching cache entry so the
 *      global feed updates without a refetch.
 *   2. Bump the cached `total` count by 1 so the page header
 *      ("N / M") stays in sync.
 *
 * Replay-safe: the bridge dedupes by item id, so a duplicate event
 * (e.g. reconnect-with-replay) is a no-op.
 *
 * Mount this hook at the page that renders the global feed
 * (`GlobalActivityFeed`). It does not need to be mounted at app
 * root — pages that don't render the feed have no use for it.
 */
export function useGlobalLogBridge() {
  const subscribe = useWSStore((s) => s.subscribe);
  const queryClient = useQueryClient();

  useEffect(() => {
    return subscribe((msg: WSMessage) => {
      if (msg.type !== "document.log") return;

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
      if (!item.id) {
        // Malformed event; ignore.
        return;
      }

      // ---- Patch the global (cross-doc) feed cache ----
      // We iterate every cached query whose key STARTS with
      // `documentKeys.logs()` and whose second-to-last segment is
      // an object (the `{limit, offset}` params). That excludes
      // per-doc log entries (where the second-to-last segment is
      // a string documentId), which the per-doc bridge owns.
      const queries = queryClient.getQueryCache().findAll({
        queryKey: documentKeys.logs(),
        exact: false,
      });

      for (const q of queries) {
        const key = q.queryKey;
        // Expect: [...documentKeys.logs(), paramsObject]
        if (key.length < 2) continue;
        const last = key[key.length - 1];
        if (typeof last !== "object" || last === null) continue;

        queryClient.setQueryData<{
          items: DocumentLogItem[];
          total: number;
          limit: number;
          offset: number;
        }>(key, (prev) => {
          if (!prev) return prev;
          if (prev.items.some((existing) => existing.id === item.id)) return prev;
          return {
            ...prev,
            items: [item, ...prev.items],
            total: prev.total + 1,
          };
        });
      }
    });
  }, [subscribe, queryClient]);
}
