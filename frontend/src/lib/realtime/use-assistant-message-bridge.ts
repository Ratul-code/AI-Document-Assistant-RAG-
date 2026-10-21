"use client";

import { useQueryClient, type InfiniteData, type QueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import {
  conversationKeys,
  type ConversationCitation,
  type ConversationMessage,
  type ConversationMessageListResponse,
} from "@/lib/api/conversations";
import { useWSStore, type WSMessage } from "./ws-store";

/**
 * Bridges terminal `analysis.done` / `analysis.error` WebSocket events
 * to the chat-history cache.
 *
 * Wire format (must match `app.schemas.ws_events.WsEvent` server-side):
 *   { type: "analysis.done",
 *     data: { stream_id, conversation_id, answer, citations }, ts }
 *   { type: "analysis.error",
 *     data: { stream_id, conversation_id, code, message }, ts }
 *
 * Why this hook exists at the root:
 *   The orchestrator persists the assistant (or error) row to Postgres
 *   before emitting `done`/`error`. The chat UI reads messages via
 *   TanStack Query — a plain HTTP cache. Without a root listener, the
 *   cache would only refresh while `ChatWorkspace` is mounted, because
 *   the reconciliation logic lived inside `useAnalysisStream`'s
 *   `useEffect`-scoped WS subscription. Navigating away mid-stream and
 *   returning once the orchestrator had finished left the user looking
 *   at a missing assistant bubble until they hard-refreshed the page.
 *
 *   Mirrors `useUserMessageBridge`: lives in `RealtimeBridge` so the
 *   messages cache stays in sync regardless of which route is mounted.
 *
 * Strategy:
 *   - Optimistically append the just-finished row (assistant OR error)
 *     into the cached infinite query for this conversation. The row
 *     belongs at the END of the newest page — same place the streaming
 *     bubble was being rendered. Dedupe by id so a concurrent refetch
 *     that already inserted the canonical row is honoured.
 *   - Then invalidate the messages cache so the server-canonical row
 *     (with its real id and timestamps) replaces our optimistic insert
 *     on the next refetch.
 *
 * Mount once at app root alongside `useUserMessageBridge` /
 * `useWSToasts` / `useDocumentStatusBridge`.
 */
export function useAssistantMessageBridge() {
  const subscribe = useWSStore((s) => s.subscribe);
  const queryClient = useQueryClient();

  useEffect(() => {
    return subscribe((msg: WSMessage) => {
      if (msg.type !== "analysis.done" && msg.type !== "analysis.error") {
        return;
      }

      const data = (msg.data ?? {}) as Record<string, unknown>;
      const eventStreamId = data.stream_id as string | undefined;
      const eventConvId = data.conversation_id as string | undefined;
      if (!eventStreamId) return;

      // The chat flow always carries `conversation_id` on terminal
      // events. Drop legacy one-shot events (the old `/analysis/query`
      // endpoint) that don't — they have no chat-history row to
      // reconcile.
      if (!eventConvId) return;
      const targetConvId = eventConvId;

      let row: ConversationMessage;
      if (msg.type === "analysis.done") {
        const answer = (data.answer as string | undefined) ?? "";
        const rawCitations = (data.citations as unknown[]) ?? [];
        const citations = rawCitations.map((c) => normaliseCitation(c));
        row = {
          id: eventStreamId, // best-known id; refetch will replace
          role: "assistant",
          content: answer,
          citations,
          error_code: null,
          created_at: new Date().toISOString(),
        };
      } else {
        const code =
          (data.code as ConversationMessage["error_code"] | undefined) ??
          "INTERNAL";
        const message =
          (data.message as string | undefined) ?? "Something went wrong.";
        row = {
          id: eventStreamId,
          role: "assistant",
          content: message,
          citations: [],
          error_code: code,
          created_at: new Date().toISOString(),
        };
      }

      // Optimistic insert into the chat-history infinite query. The
      // chat workspace fetches pages in DESC order, so the NEWEST items
      // live at the START of the FIRST page (`pages[0].items[0]`). The
      // workspace then reverses each page on the way out before
      // flattening, so the persisted assistant row will render at the
      // END of the visible list — right where the streaming bubble was.
      //
      // We prepend into `pages[0].items` rather than appending to the
      // last page: appending to a "last page" that is already full
      // (e.g. 20 items with `hasNextPage=false`) would push the new
      // row into a logical page-2 that nobody fetches, and bumping
      // `total` would make `getNextPageParam` think there's more data
      // to load without actually placing the row in any rendered page.
      //
      // If the first page is full we still splice in at index 0 — the
      // refetch below will reconcile against the canonical server
      // ordering. This is the same position the server will return
      // after invalidation, so it's a faithful optimistic insert.
      //
      // Dedupe across pages: if a refetch already populated this id
      // somewhere (e.g. a concurrent invalidation resolved), skip.
      const cached = queryClient.getQueryData<
        InfiniteData<ConversationMessageListResponse, unknown>
      >(conversationKeys.messages(targetConvId));
      if (cached && cached.pages.length > 0) {
        const alreadyPresent = cached.pages.some((p) =>
          p.items.some((m) => m.id === row.id),
        );
        if (!alreadyPresent) {
          queryClient.setQueryData<
            InfiniteData<ConversationMessageListResponse, unknown>
          >(conversationKeys.messages(targetConvId), {
            ...cached,
            pages: cached.pages.map((p, i) =>
              i === 0
                ? {
                    ...p,
                    items: [row, ...p.items],
                    // Intentionally NOT bumping `total` here. The
                    // refetch below reconciles to the real count in the
                    // same tick, and an inflated total can trick
                    // `getNextPageParam` into asking for a page that
                    // doesn't exist on conversations whose newest page is
                    // exactly full (loadedSoFar `N` < total `N+1` ⇒
                    // phantom next page, endless scroll attempt).
                    // Patching `items` alone renders the optimistic row.
                  }
                : p,
            ),
          });
        }
      }

      // Pull the fresh history so the canonical row (with its server
      // id + timestamp) replaces our optimistic insert.
      void reconcileAfterDone(queryClient, targetConvId, eventStreamId);
    });
  }, [subscribe, queryClient]);
}

/* ---------- Terminal-event reconciliation ---------- */

/**
 * Refetch the conversation history after a terminal `analysis.*` event
 * and verify the persisted row actually showed up. Retry a few times
 * if it hasn't.
 *
 * Why the retry loop is necessary — a real ordering hazard, not
 * paranoia:
 *
 *   The backend publishes `analysis.done` over Redis *before* it
 *   commits the assistant row. `analyse_query_with_conversation` yields
 *   the `AnalysisDoneEvent` (which the request handler publishes to WS)
 *   and only afterwards opens a fresh session and runs
 *   `insert_message(...)` in a transaction. Between those two points the
 *   row is not yet in Postgres.
 *
 *   So an `invalidateQueries()` fired straight off the event races the
 *   commit. When it loses, the refetch returns a history that still has
 *   no assistant row, TanStack replaces the whole page array, and the
 *   optimistic insert we just wrote is thrown away — the reply visibly
 *   disappears and only comes back on a later refresh. That is exactly
 *   the bug this function exists to prevent.
 *
 * Strategy:
 *   1. Invalidate, await the refetch, and inspect the result.
 *   2. If a row for this stream is present (either the optimistic
 *      `streamId` id, or any assistant row newer than the snapshot we
 *      took before invalidating), we are done.
 *   3. Otherwise back off and invalidate again, up to
 *      `MAX_RECONCILE_ATTEMPTS` times. The row lands within a few
 *      hundred ms in practice; the exponential-ish delay just gives the
 *      backend commit time to finish.
 *
 * The loop is fire-and-forget (the WS handler doesn't await it) so a
 * slow backend never blocks the WS fan-out to other subscribers.
 */
const MAX_RECONCILE_ATTEMPTS = 5;
const RECONCILE_BASE_DELAY_MS = 150;

async function reconcileAfterDone(
  queryClient: QueryClient,
  conversationId: string,
  streamId: string,
): Promise<void> {
  const queryKey = conversationKeys.messages(conversationId);

  // Ids present right now, so we can tell whether the row we see on a
  // later attempt is genuinely new (i.e. belongs to this stream) rather
  // than leftover history from earlier turns.
  const before = new Set<string>();
  const snapshot = queryClient.getQueryData<
    InfiniteData<ConversationMessageListResponse, unknown>
  >(queryKey);
  for (const page of snapshot?.pages ?? []) {
    for (const item of page.items) before.add(item.id);
  }

  for (let attempt = 0; attempt < MAX_RECONCILE_ATTEMPTS; attempt++) {
    if (attempt > 0) {
      await sleep(RECONCILE_BASE_DELAY_MS * attempt);
    }

    await queryClient.invalidateQueries({ queryKey });

    const current = queryClient.getQueryData<
      InfiniteData<ConversationMessageListResponse, unknown>
    >(queryKey);
    const pages = current?.pages ?? [];
    if (pages.length === 0) continue;

    // Did a row attributable to THIS stream land? Either the
    // optimistic id the bridge just inserted, or any assistant row
    // that wasn't in the pre-invalidate snapshot.
    let landed = false;
    for (const page of pages) {
      for (const item of page.items) {
        if (item.id === streamId) {
          landed = true;
          break;
        }
        if (item.role === "assistant" && !before.has(item.id)) {
          landed = true;
          break;
        }
      }
      if (landed) break;
    }

    if (landed) return;
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/* ---------- Helpers ---------- */

/**
 * The backend serialises `QueryCitation` to a JSON object; we
 * re-hydrate it into our typed `ConversationCitation` shape. Anything
 * missing or wrong-typed becomes a safe default rather than throwing.
 *
 * Duplicated from `useAnalysisStream` (where the same normalisation
 * lived before this bridge was lifted to the root). Kept local rather
 * than exported because no other module needs it today.
 */
function normaliseCitation(raw: unknown): ConversationCitation {
  const obj = (raw ?? {}) as Record<string, unknown>;
  const num = (v: unknown, fallback: number): number =>
    typeof v === "number" && Number.isFinite(v) ? v : fallback;

  return {
    index: num(obj.index, 1),
    chunk_id: String(obj.chunk_id ?? ""),
    document_id: String(obj.document_id ?? ""),
    document_title: String(obj.document_title ?? "Untitled"),
    page_number:
      typeof obj.page_number === "number" ? obj.page_number : null,
    section_path: Array.isArray(obj.section_path)
      ? (obj.section_path as unknown[]).map((s) => String(s))
      : [],
    snippet: String(obj.snippet ?? ""),
    score: num(obj.score, 0),
  };
}