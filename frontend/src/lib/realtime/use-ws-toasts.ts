"use client";

import { useEffect } from "react";
import { toast } from "sonner";

import { useWSStore, type WSMessage } from "./ws-store";

/**
 * Subscribes to the global WebSocket and routes messages to
 * shadcn/sonner toasts.
 *
 * Wire format (must match `app.schemas.ws_events.WsEvent` server-side):
 *
 *   { type: "document.status",
 *     data: { document_id, title, status, error_message? }, ts }
 *     → render a status toast whose shape depends on `status`:
 *
 *         status === "uploaded"  → info     | title = document title
 *                                          | desc  = "Your document has been uploaded."
 *         status === "learning"  → info     | title = "Learning"
 *                                          | desc  = "AI is reading your document."
 *         status === "ready"     → success  | title = "Document is ready."
 *                                          | desc  = "Document is ready."
 *         status === "failed"    → error    | title = document title (or "Error")
 *                                          | desc  = error_message from server
 *         anything else          → info     | title = document title (or generic)
 *                                          | desc  = "Status: <status>"
 *
 *   { type: "document.log", ... }
 *     → not handled here. `document.log` events are page-scoped
 *       (upload-activity only) and consumed by
 *       `useDocumentLogBridge`. Mounting a toast here would leak
 *       log entries to every page.
 *
 *   anything else
 *     → silently ignored. Feature hooks subscribe directly via
 *       useWSStore.subscribe for cache invalidation etc.
 *
 * Mount once at app root. No work besides subscribing.
 */
export function useWSToasts() {
  const subscribe = useWSStore((s) => s.subscribe);

  useEffect(() => {
    return subscribe((msg: WSMessage) => {
      const t = msg.type;
      const data = (msg.data ?? {}) as Record<string, unknown>;

      if (t === "document.status") {
        const status = (data.status as string | undefined) ?? "";
        const docTitle = (data.title as string | null | undefined) ?? null;
        const errorMessage =
          (data.error_message as string | null | undefined) ?? null;

        // Map status → toast config. See the wire-format block in
        // the file-level JSDoc above for the full spec.
        switch (status) {
          case "uploaded": {
            toast.info(docTitle ?? "Document uploaded", {
              description: "Your document has been uploaded.",
            });
            return;
          }

          case "learning": {
            toast(docTitle ?? "Document uploaded", {
              description: "AI is reading your document.",
            });
            return;
          }

          case "ready": {
            toast.success("Document is ready.", {
              description: "Document is ready.",
            });
            return;
          }

          case "failed": {
            // Sonner has no `danger` variant — `toast.error` renders
            // red and, with the root <Toaster richColors /> mount,
            // matches what the product spec calls a "danger" toast.
            toast.error(docTitle ?? "Error", {
              description: errorMessage ?? "Something went wrong.",
            });
            return;
          }

          default: {
            // Any future / unknown status falls back to a neutral
            // info toast so the user still sees *something* changed.
            toast.info(docTitle ?? "Document updated", {
              description: status ? `Status: ${status}` : "Status changed.",
            });
            return;
          }
        }
      }
    });
  }, [subscribe]);
}