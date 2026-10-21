"use client";

/**
 * One-shot in-memory handoff for a user's first question between
 * `<ChatWelcome>` and `<ChatWorkspace>`. The welcome screen stashes
 * the question keyed by the new conversation id; the workspace
 * drains it on mount for the auto-submit + textarea prefill.
 *
 * Kept off the URL on purpose: putting the question in `?q=<text>`
 * made it part of browser history, shareable, and subject to a
 * React 19 strict-mode double-submit. Drained eagerly in the
 * workspace's `useState` initializer; state is preserved across
 * strict-mode's simulated remount, so the prefill survives.
 */

import { create } from "zustand";

type PendingQuestionStore = {
  byId: Record<string, string>;
  set: (conversationId: string, question: string) => void;
  drain: (conversationId: string) => string | undefined;
};

export const usePendingQuestionStore = create<PendingQuestionStore>(
  (set, get) => ({
    byId: {},

    set: (conversationId, question) => {
      const trimmed = question.trim();
      if (trimmed.length === 0) return;
      set((state) => ({
        byId: { ...state.byId, [conversationId]: trimmed },
      }));
    },

    drain: (conversationId) => {
      const question = get().byId[conversationId];
      if (question === undefined) return undefined;
      set((state) => {
        const next = { ...state.byId };
        delete next[conversationId];
        return { byId: next };
      });
      return question;
    },
  }),
);