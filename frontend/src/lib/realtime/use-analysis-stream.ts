/**
 * `useAnalysisStream` — client-side glue between the `/ws` socket and
 * a single conversation's chat UI. Owns the in-flight streaming state
 * (the live bubble + phase transitions); does NOT touch the messages
 * cache.
 *
 * Wire format (matches `backend/app/schemas/ws_events.py`):
 *
 *   { type: "analysis.token",
 *     data: { stream_id, conversation_id?, text }, ts }
 *   { type: "analysis.done",
 *     data: { stream_id, conversation_id, answer, citations }, ts }
 *     — handled by `useAssistantMessageBridge`, NOT here.
 *   { type: "analysis.error",
 *     data: { stream_id, conversation_id, code, message }, ts }
 *     — handled by `useAssistantMessageBridge`, NOT here.
 *   { type: "analysis.user_message",
 *     data: { stream_id, conversation_id, message }, ts } — handled by
 *     `useUserMessageBridge`, NOT here.
 *
 * Flow this hook drives:
 *
 *   1. `submit(question)`  → POST /conversations/{id}/messages
 *                            → backend returns { stream_id }
 *   2. The hook subscribes to the WS, filtering by `stream_id`.
 *      As `analysis.token` events arrive, `useStreamStore.appendToken`
 *      accumulates the live assistant text under the conversation's
 *      slot.
 *   3. `analysis.done`     → store flips to `done` with answer + citations.
 *   4. `analysis.error`    → store flips to `error` with code + message.
 *
 * Messages-cache reconciliation (splice the persisted row into the
 * infinite-query pages + invalidate so the canonical row replaces it)
 * is owned by the root-level bridges — `useUserMessageBridge` for the
 * user row and `useAssistantMessageBridge` for the assistant/error row.
 * Those hooks live in `RealtimeBridge` so the cache stays in sync even
 * when the user navigates off `/chat` mid-stream. This hook only deals
 * with the live streaming bubble + phase transitions; the bridges deal
 * with what's persisted.
 *
 * Why the store, not React state:
 *   Streaming state used to live in `useState` here, which meant a
 *   mid-stream navigation tore down the state — the user would return
 *   to an idle chat even though the orchestrator was still streaming.
 *   The store lives outside the React tree, so navigating away and
 *   back shows the live bubble (and its accumulated text) intact.
 *
 * Cancellation:
 *   `cancel()` issues DELETE /conversations/{id}/messages/{stream_id}
 *   on the backend, which signals the orchestrator to abort. The
 *   orchestrator's `CancelledError` branch publishes a CANCELLED
 *   `analysis.error` event AND drops the partial assistant turn
 *   (the user message stays — it was persisted before the LLM
 *   started). The local store slot is reset so the chat UI returns
 *   to idle immediately, without waiting for the terminal event to
 *   round-trip. The terminal event still arrives, but the store's
 *   stale-event guard rejects it because there's no slot to update.
 */

import {
  useMutation,
} from "@tanstack/react-query";
import { useCallback, useEffect } from "react";

import {
  cancelMessage,
  submitMessage,
  type ConversationCitation,
  type ConversationMessage,
  type ConversationMessageSubmitResponse,
} from "@/lib/api/conversations";
import { useWSStore, type WSMessage } from "@/lib/realtime/ws-store";
import {
  selectPhase,
  useStreamStore,
  type StreamPhase,
} from "./stream-store";

/* ---------- Hook surface ---------- */

type Args = {
  conversationId: string;
};

/**
 * Drive the in-flight stream state for one conversation.
 *
 * Returns:
 *   - `state`         — current phase + any accumulated text. Read
 *                       from the global stream store; survives
 *                       unmount so navigating away mid-stream and
 *                       back shows the live bubble.
 *   - `submit(text)`  — kick off a new question; resolves once the
 *                       submit POST returns (NOT once the answer
 *                       finishes — the state phase will tick to
 *                       `done`/`error` as WS events arrive).
 *   - `reset()`       — clear this conversation's slot in the store.
 *                       Useful when the chat page navigates to a
 *                       different chat (avoids stale bubbles).
 *   - `cancel()`      — placeholder. With no backend cancel endpoint
 *                       yet, this only clears the local store slot;
 *                       the orchestrator will still finish + persist.
 */
export function useAnalysisStream({ conversationId }: Args) {
  const subscribe = useWSStore((s) => s.subscribe);

  // Subscribe to THIS conversation's phase only — so the component
  // doesn't re-render on unrelated chats' state changes.
  const state: StreamPhase = useStreamStore(selectPhase(conversationId));

  // Stable references to store actions. Pulling from the store
  // directly each render would create new function refs and could
  // trip effects that depend on them.
  const startSubmit = useStreamStore((s) => s.startSubmit);
  const startStream = useStreamStore((s) => s.startStream);
  const appendToken = useStreamStore((s) => s.appendToken);
  const finishDone = useStreamStore((s) => s.finishDone);
  const finishError = useStreamStore((s) => s.finishError);
  const resetStream = useStreamStore((s) => s.reset);

  /* ---------- WS subscription ---------- */

  useEffect(() => {
    return subscribe((msg: WSMessage) => {
      const type = msg.type;
      if (
        type !== "analysis.token" &&
        type !== "analysis.done" &&
        type !== "analysis.error"
      ) {
        return;
      }

      const data = (msg.data ?? {}) as Record<string, unknown>;
      const eventStreamId = data.stream_id as string | undefined;
      const eventConvId = data.conversation_id as string | undefined;

      // Resolve which conversation this event belongs to. The chat
      // flow always carries `conversation_id`; if it's missing but
      // we're only one conversation deep in this hook (i.e. the
      // workspace owns exactly one), fall back to the workspace's
      // `conversationId` so legacy one-shot events still flow.
      // The legacy `/analysis/query` endpoint emits events without
      // `conversation_id` and would otherwise be silently dropped.
      const targetConvId = eventConvId ?? conversationId;
      if (!eventStreamId) return;

      // The store action owns the stale-event guard (checks that
      // there's a `streaming` slot for this conversation+streamId),
      // so we just route the event in.
      if (type === "analysis.token") {
        const text = (data.text as string | undefined) ?? "";
        appendToken(targetConvId, eventStreamId, text);
        return;
      }

      if (type === "analysis.done") {
        const answer = (data.answer as string | undefined) ?? "";
        const rawCitations = (data.citations as unknown[]) ?? [];
        const citations = rawCitations.map((c) => normaliseCitation(c));
        finishDone(targetConvId, eventStreamId, answer, citations);
        // Cache reconciliation (splice + invalidate the messages
        // infinite query) is owned by `useAssistantMessageBridge`,
        // which lives at the root so it survives navigation away
        // from `/chat` mid-stream.
        return;
      }

      if (type === "analysis.error") {
        const code = (data.code as string | undefined) ?? "INTERNAL";
        const message =
          (data.message as string | undefined) ?? "Something went wrong.";
        finishError(targetConvId, eventStreamId, code, message);
        // Same — `useAssistantMessageBridge` owns the cache update.
        return;
      }
    });
  }, [
    subscribe,
    appendToken,
    finishDone,
    finishError,
    conversationId,
  ]);

  /* ---------- Submit mutation ---------- */

  const submitMutation = useMutation({
    mutationFn: (question: string): Promise<ConversationMessageSubmitResponse> =>
      submitMessage(conversationId, { question }),
    onSuccess: (resp) => {
      // Store has the in-flight streamId — that's how the WS handler
      // knows which conversation to route events for, even after we
      // unmount.
      startStream(conversationId, resp.stream_id);
    },
    onError: () => {
      // The orchestrator couldn't even start — surface as a stream
      // error so the UI shows a banner rather than silently swallowing.
      finishError(
        conversationId,
        // No stream_id was issued; use empty string as a sentinel.
        "",
        "INTERNAL",
        "Couldn't send your question. Please try again.",
      );
    },
  });

  const submit = useCallback(
    (question: string) => {
      // Reset before starting a new round so an old `done` from the
      // previous question doesn't bleed into this one's UI.
      resetStream(conversationId);
      startSubmit(conversationId);
      submitMutation.mutate(question);
    },
    [submitMutation, resetStream, startSubmit, conversationId],
  );

  const reset = useCallback(() => {
    resetStream(conversationId);
  }, [resetStream, conversationId]);

  const cancel = useCallback(() => {
    // Capture the streamId BEFORE the reset, then clear the local
    // store slot so the UI returns to idle immediately. We don't gate
    // on the network call — the user's stop gesture should feel
    // instant. The DELETE request still goes out below so the
    // orchestrator actually aborts and the partial assistant turn
    // never gets persisted; the terminal `CANCELLED` event that
    // follows is dropped by the store's stale-event guard because
    // there's no slot to update.
    const phase = useStreamStore.getState().byId[conversationId];
    resetStream(conversationId);

    // If there's no active phase (idle, done, or error) or the phase
    // is still in the brief submit window, there's nothing to cancel
    // server-side — the orchestrator either never started or finished
    // already. (A `submitting` phase means the POST is in flight; the
    // server will refuse a cancel for a non-existent stream_id, so we
    // skip the DELETE in that case. The local reset is still
    // appropriate so the user can immediately retry.)
    if (!phase) return;
    if (phase.kind === "submitting" || phase.kind === "idle") return;
    if (phase.kind === "error") return;
    const streamId = phase.streamId;

    // Fire-and-forget: the server-side cancel is best-effort. The
    // UI is already back to idle, so a 404/401 from a stale stream
    // or expired session surfaces nowhere — which is the right
    // behaviour. We log the failure so devs can spot a regression
    // in the cancel path.
    void cancelMessage(conversationId, streamId).catch((err) => {
      console.warn("cancelMessage failed:", err);
    });
  }, [resetStream, conversationId]);

  return {
    state,
    submit,
    reset,
    cancel,
    isSubmitting: submitMutation.isPending,
  };
}

/* ---------- Helpers ---------- */

/**
 * The backend serialises `QueryCitation` to a JSON object; we
 * re-hydrate it into our typed `ConversationCitation` shape. Anything
 * missing or wrong-typed becomes a safe default rather than throwing.
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

/* ---------- Re-exports ---------- */

export { type StreamPhase };
export type { ConversationCitation, ConversationMessage };
