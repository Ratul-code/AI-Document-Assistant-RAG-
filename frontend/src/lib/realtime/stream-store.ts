"use client";

import { create } from "zustand";

import type { ConversationCitation } from "@/lib/api/conversations";

/**
 * Per-conversation in-flight analysis-stream state.
 *
 * Lives in a Zustand store (NOT React component state) so it survives
 * `ChatWorkspace` unmounting when the user navigates to another page
 * mid-stream. The chat UI reads from the store via selectors; the WS
 * handler in `useAnalysisStream` writes into it.
 *
 * Why a map keyed by conversationId:
 *   - Multiple chats can be in flight at once (the per-user stream
 *     cap is a server-side concern). The store owns ALL of them; the
 *     workspace only reads its own.
 *   - Cheap selector reads: `useStreamStore(s => s.byId[id])` only
 *     re-renders the calling component when that specific slot
 *     changes.
 *
 * Why we keep the same `StreamPhase` discriminated union the existing
 * hook returned: `ChatWorkspace` reads it directly, and refactoring
 * its consumer would be a much bigger blast radius. The store is a
 * pure structural change.
 */
export type StreamPhase =
  | { kind: "idle" }
  | { kind: "submitting" }
  | { kind: "streaming"; streamId: string; text: string }
  | {
      kind: "done";
      streamId: string;
      text: string;
      citations: ConversationCitation[];
    }
  | { kind: "error"; streamId: string; code: string; message: string };

export type StreamByConversation = Record<string, StreamPhase>;

type StreamStore = {
  /** Map of conversationId → current phase. Missing keys are treated as
   *  `{ kind: "idle" }` by the selector helpers below. */
  byId: StreamByConversation;
  /** Subscribe counter — bumped on every mutation so consumers can
   *  subscribe to "any stream anywhere changed" cheaply. */
  version: number;

  /** Begin a submit (POST in-flight). Drops any prior phase for this
   *  conversation. Called from `useAnalysisStream.submit()`. */
  startSubmit: (conversationId: string) => void;
  /** Submit succeeded; orchestrator returned a `stream_id`. Switch to
   *  streaming with empty text. */
  startStream: (conversationId: string, streamId: string) => void;
  /** Append one token delta to an in-flight stream. No-op if the
   *  stream isn't currently in `streaming` phase or the `stream_id`
   *  doesn't match (drops stale events). */
  appendToken: (
    conversationId: string,
    streamId: string,
    text: string,
  ) => void;
  /** Terminal: stream finished successfully. Stores the final text +
   *  citations so the UI can render a "done" bubble briefly until the
   *  messages-query refetch lands the canonical row. */
  finishDone: (
    conversationId: string,
    streamId: string,
    text: string,
    citations: ConversationCitation[],
  ) => void;
  /** Terminal: stream failed. Stores code + message for the banner. */
  finishError: (
    conversationId: string,
    streamId: string,
    code: string,
    message: string,
  ) => void;
  /** Reset to idle. Called by `reset()` (dismiss error banner) and
   *  `cancel()`. Also called internally on terminal events so the
   *  bubble flips from streaming → done → idle without manual
   *  intervention (the messages query refetch is what actually
   *  "completes" the UI). */
  reset: (conversationId: string) => void;
};

/** Returns `{ kind: "idle" }` for any conversation with no entry. */
const IDLE: StreamPhase = { kind: "idle" };

export const useStreamStore = create<StreamStore>((set) => ({
  byId: {},
  version: 0,

  startSubmit: (conversationId) =>
    set((state) => ({
      byId: { ...state.byId, [conversationId]: { kind: "submitting" } },
      version: state.version + 1,
    })),

  startStream: (conversationId, streamId) =>
    set((state) => ({
      byId: {
        ...state.byId,
        [conversationId]: { kind: "streaming", streamId, text: "" },
      },
      version: state.version + 1,
    })),

  appendToken: (conversationId, streamId, text) =>
    set((state) => {
      const current = state.byId[conversationId] ?? IDLE;
      // Drop stale events: ignore if not streaming for this stream_id.
      if (current.kind !== "streaming") return state;
      if (current.streamId !== streamId) return state;
      return {
        byId: {
          ...state.byId,
          [conversationId]: {
            ...current,
            text: current.text + text,
          },
        },
        version: state.version + 1,
      };
    }),

  finishDone: (conversationId, streamId, text, citations) =>
    set((state) => {
      const current = state.byId[conversationId] ?? IDLE;
      // Stale-event guard: only transition if this finish is for the
      // active stream (or if there's no active stream at all — e.g.
      // the user navigated away and came back, the store may have
      // been reset). Allow terminal updates from either `streaming`
      // or `submitting` (the latter covers the very narrow race
      // where the POST returns but the orchestrator emits `done`
      // before we've flipped the phase).
      if (
        current.kind === "streaming" &&
        current.streamId !== streamId
      ) {
        return state;
      }
      return {
        byId: {
          ...state.byId,
          [conversationId]: {
            kind: "done",
            streamId,
            text,
            citations,
          },
        },
        version: state.version + 1,
      };
    }),

  finishError: (conversationId, streamId, code, message) =>
    set((state) => {
      const current = state.byId[conversationId] ?? IDLE;
      if (
        current.kind === "streaming" &&
        current.streamId !== streamId
      ) {
        return state;
      }
      return {
        byId: {
          ...state.byId,
          [conversationId]: {
            kind: "error",
            streamId,
            code,
            message,
          },
        },
        version: state.version + 1,
      };
    }),

  reset: (conversationId) =>
    set((state) => {
      if (!(conversationId in state.byId)) return state;
      const next = { ...state.byId };
      delete next[conversationId];
      return { byId: next, version: state.version + 1 };
    }),
}));

/* ---------- Selectors ---------- */

/**
 * Read the current phase for a conversation. Returns `{ kind: "idle" }`
 * for unknown conversations. Use as `useStreamPhase(id)` so the calling
 * component only re-renders when THIS conversation's phase changes
 * (Zustand's default referential equality on the slice).
 */
export function selectPhase(id: string) {
  return (s: StreamStore): StreamPhase => s.byId[id] ?? IDLE;
}

/**
 * `true` if there is at least one conversation with an in-flight
 * stream right now (any kind except `idle`/`done`). Used by sidebar
 * "● typing" indicators — not wired today but cheap to keep available.
 */
export function selectAnyInFlight(s: StreamStore): boolean {
  for (const phase of Object.values(s.byId)) {
    if (phase.kind === "streaming" || phase.kind === "submitting") {
      return true;
    }
  }
  return false;
}