"use client";

import { useAssistantMessageBridge } from "@/lib/realtime/use-assistant-message-bridge";
import { useDocumentLogStatsInvalidator } from "@/lib/realtime/use-document-log-stats-invalidator";
import { useDocumentStatusBridge } from "@/lib/realtime/use-document-status-bridge";
import { useGlobalLogBridge } from "@/lib/realtime/use-global-log-bridge";
import { useUserMessageBridge } from "@/lib/realtime/use-user-message-bridge";
import { useWSStatusToasts } from "@/lib/realtime/use-ws-status-toasts";
import { useWSToasts } from "@/lib/realtime/use-ws-toasts";

/**
 * Renders nothing — mounts side-effecting hooks that wire the
 * WebSocket store to UI:
 *
 *   - useWSToasts               → shows toasts for `document.status`
 *                                  events (any page; always on) and
 *                                  for `document.log` info toasts.
 *   - useDocumentStatusBridge   → invalidates the documents list
 *                                  cache when document status changes
 *   - useUserMessageBridge      → optimistically prepends a just-
 *                                  persisted user message into the
 *                                  chat-history cache, so the chat UI
 *                                  shows the user bubble + typing
 *                                  indicator immediately on submit
 *                                  instead of waiting for the
 *                                  assistant to finish.
 *   - useAssistantMessageBridge → optimistically splices the
 *                                  just-finished assistant (or error)
 *                                  row into the chat-history cache
 *                                  on `analysis.done` / `analysis.error`.
 *                                  Mirrors `useUserMessageBridge` for
 *                                  the terminal event so the messages
 *                                  cache stays in sync regardless of
 *                                  which route is mounted.
 *   - useGlobalLogBridge        → prepends `document.log` events into
 *                                  the cross-doc activity feed cache
 *                                  so the global feed (when mounted)
 *                                  updates without a refetch.
 *   - useDocumentLogStatsInvalidator → invalidates the per-doc stats
 *                                  query on every `document.log` event
 *                                  so the detail dialog's stat tiles
 *                                  refetch on next paint. Mounted at
 *                                  app root so the invalidation runs
 *                                  even when the per-doc activity
 *                                  panel is unmounted.
 *   - useWSStatusToasts         → silently pushes the user to
 *                                  /login when the WS reconnect path
 *                                  couldn't refresh the session
 *                                  (auth-expired). No toast — per
 *                                  product spec the user only sees
 *                                  sonners from explicit feature
 *                                  code (doc status, etc.).
 *
 * `useDocumentLogBridge` (per-doc log prepend + stats + list
 * invalidation) is mounted by the page-level component that
 * renders the per-doc activity section (e.g. the detail dialog
 * or the page-level per-doc Activity panel). It's NOT mounted
 * here because the per-doc log cache is page-shaped and only
 * pages that render the panel need to patch it.
 *
 * Place once at app root, INSIDE <WSProvider>. The `Toaster` from
 * shadcn/ui must also be mounted in the tree (separately) for
 * `toast(...)` calls to actually render.
 */
export function RealtimeBridge() {
  useWSToasts();
  useDocumentStatusBridge();
  useUserMessageBridge();
  useAssistantMessageBridge();
  useGlobalLogBridge();
  useDocumentLogStatsInvalidator();
  useWSStatusToasts();
  return null;
}