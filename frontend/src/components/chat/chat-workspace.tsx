"use client";

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { AlertCircle, Loader2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import {
  conversationKeys,
  getConversation,
  listMessages,
  type Conversation,
  type ConversationMessage,
  type ConversationMessageListResponse,
} from "@/lib/api/conversations";
import { usePendingQuestionStore } from "@/lib/realtime/pending-question-store";
import { useAnalysisStream } from "@/lib/realtime/use-analysis-stream";
import { cn } from "@/lib/utils";

import { ChatHeader } from "./chat-header";
import { Composer } from "./composer";
import { MessagesList } from "./messages-list";

/**
 * Page size for the chat history infinite query. The first page
 * fetches the newest `MESSAGES_PAGE_SIZE` turns in descending order;
 * subsequent pages fetch older turns in ascending order. Keeping the
 * page small keeps initial paint cheap and gives the user something
 * interactive almost immediately.
 */
const MESSAGES_PAGE_SIZE = 20;

/**
 * Shared empty baseline for the pre-stream id snapshot, so the initial
 * ref value doesn't allocate a new Set on every render.
 */
const EMPTY_ID_SET: ReadonlySet<string> = new Set<string>();

/**
 * Cross-mount "did we already auto-submit the pending question for this
 * conversation?" latch.
 *
 * In React 19 strict mode (dev only) every component does
 * mount → unmount → remount on first commit. `useState` initializers
 * run only on the very first mount; the value is preserved across the
 * simulated remount, so the auto-submit question text survives. But
 * `useRef` does NOT survive unmount — its current value is discarded
 * with the fiber, so a `useRef(false)` latch resets to `false` on the
 * remount and the auto-submit effect fires a second time, kicking off
 * a parallel orchestrator with a different `stream_id`.
 *
 * That double-submit is the bug we hit:
 *   1. First mount  → effect runs, `stream.submit("What is X?")`,
 *                     orchestrator #1 starts, `useState` "autoSubmit"
 *                     = "What is X?" preserved.
 *   2. Remount      → effect runs again, `useRef` is reset to `false`,
 *                     `stream.submit("What is X?")` fires a second
 *                     time, orchestrator #2 starts in parallel.
 *   3. Both streams race to the UI; the cancel button only stops the
 *     one whose `stream_id` is in the store.
 *
 * `useEffect`-level latches don't work because the effect runs on every
 * mount in strict mode. The fix is to back the latch with a value that
 * lives OUTSIDE the React tree — a module-scope `Set` evaluated once
 * per page load. Set membership survives both the strict-mode unmount
 * and any legitimate remount (e.g. hot reload, route revalidation),
 * so the auto-submit is genuinely a one-shot per conversation.
 *
 * The set is unbounded; in a real session you'd never revisit enough
 * conversations to matter, and stale entries are harmless (the only
 * effect is "this conversation's pending question, if any, won't auto-
 * submit again" — which is exactly the intended behaviour).
 */
const _autoSubmittedConversations = new Set<string>();

/**
 * Page param for the chat-history infinite query. Every page is
 * fetched in descending `created_at` order so the newest turns land
 * on the first page and each subsequent page prepends older turns.
 * The client reverses each page before flattening (see the messages
 * selector below) so the rendered list always reads oldest → newest.
 */
type MessagesPageParam = {
  offset: number;
  order: "asc" | "desc";
};

type Props = {
  /** SSR-fetched conversation used as initial query data. */
  initialConversation: Conversation;
  /**
   * Optional first question, piped in via the
   * `usePendingQuestionStore` handoff. Auto-submitted once on mount
   * (gated by the module-scope `_autoSubmittedConversations` set so
   * React 19 strict-mode's simulated remount doesn't double-fire).
   * The submit runs after `useAnalysisStream`'s WS-subscribe effect
   * — a premature submit would emit `analysis.*` events with no
   * listener.
   *
   * Today the prop is never set; the welcome screen uses the store
   * handoff directly. The prop is kept for the future case where
   * SSR could read the question out of the URL and hand it down as
   * initial state (currently the question is intentionally kept off
   * the URL to avoid history leaks).
   */
  initialQuestion?: string;
  className?: string;
};

/**
 * Open-chat workspace. Owns the messages query + streaming state, and
 * renders the header / message list / composer in a column.
 *
 * Data flow:
 *
 *   SSR  → fetchConversation(id) → `initialConversation` prop
 *   CSR  → useQuery({ initialData: initialConversation })
 *          + useQuery({ messages }) → renders
 *   WS   → useAnalysisStream stitches live tokens into the streaming
 *          bubble. The root-level `useAssistantMessageBridge` splices
 *          the persisted row into the messages cache on `done`/
 *          `error` and invalidates so the canonical row replaces it.
 *          The virtual bubble stays rendered through `done` until the
 *          canonical row actually appears, eliminating a flash where
 *          the bubble vanishes before the refetch lands.
 */
export function ChatWorkspace({
  initialConversation,
  initialQuestion,
  className,
}: Props) {
  const conversationId = initialConversation.id;

  const conversationQuery = useQuery({
    queryKey: conversationKeys.detail(conversationId),
    queryFn: () => getConversation(conversationId),
    // `initialData` is non-null while this component is mounted, so
    // the cache is hot on first render. If the backend 404s later
    // (e.g. deleted from another tab) the query settles to `null`
    // and we render an empty-state below.
    initialData: initialConversation,
    staleTime: 30_000,
  });

  const conversation = conversationQuery.data ?? initialConversation;

  const messagesQuery = useInfiniteQuery<
    ConversationMessageListResponse,
    Error,
    { pages: ConversationMessageListResponse[]; pageParams: MessagesPageParam[] },
    ReturnType<typeof conversationKeys.messages>,
    MessagesPageParam
  >({
    queryKey: conversationKeys.messages(conversationId),
    // Every page is fetched newest-first (DESC). The client reverses
    // each page before flattening so the rendered list reads oldest →
    // newest, and "load older" pages prepend naturally.
    queryFn: ({ pageParam }) =>
      listMessages(conversationId, {
        limit: MESSAGES_PAGE_SIZE,
        offset: pageParam.offset,
        order: pageParam.order,
      }),
    initialPageParam: { offset: 0, order: "desc" },
    getNextPageParam: (lastPage, _pages, lastPageParam) => {
      // In DESC mode `offset` is the count of items we've already
      // asked for from the newest end. Advance by the page size only
      // when the server returned a full page AND we haven't hit the
      // total yet.
      const loadedSoFar = lastPageParam.offset + lastPage.items.length;
      if (lastPage.items.length < MESSAGES_PAGE_SIZE) return undefined;
      if (loadedSoFar >= lastPage.total) return undefined;
      return { offset: loadedSoFar, order: "desc" };
    },
  });

  // Flatten pages into a single oldest → newest array. Every page
  // arrives DESC from the server, so we reverse each before joining.
  const messages: ConversationMessage[] = (() => {
    const pages = messagesQuery.data?.pages ?? [];
    if (pages.length === 0) return [];
    return pages.flatMap((p) => [...p.items].reverse());
  })();

  const stream = useAnalysisStream({ conversationId });

  // Resolve the pending question once, from the handoff store if no
  // explicit prop was passed. Drained eagerly here so a later
  // legitimate remount of the same conversation starts clean even
  // if the store was repopulated by a navigation race.
  //
  // The auto-submit itself is gated by the module-scope
  // `_autoSubmittedConversations` set (see top of file) — a
  // `useRef(false)` latch would NOT work, because React 19 strict
  // mode discards ref state on the simulated unmount, letting the
  // effect fire twice. The module-scope set survives that, so this
  // is genuinely a one-shot per conversation.
  const drainPendingQuestion = usePendingQuestionStore((s) => s.drain);
  const [autoSubmitQuestion] = useState<string | undefined>(() => {
    if (initialQuestion && initialQuestion.trim().length > 0) {
      return initialQuestion;
    }
    return drainPendingQuestion(conversationId);
  });

  useEffect(() => {
    if (!autoSubmitQuestion) return;
    if (_autoSubmittedConversations.has(conversationId)) return;
    _autoSubmittedConversations.add(conversationId);
    stream.submit(autoSubmitQuestion);
    // `stream` is intentionally omitted from the dep array — the
    // submit must fire exactly once per conversation regardless of
    // how many times the effect re-runs (e.g. strict-mode remount).
    // `autoSubmitQuestion` is also omitted because the set above
    // makes the second-run a no-op anyway, and depending on the
    // captured closure would still allow the remount to slip
    // through if a future refactor changes when `useState` re-runs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Compute the live streaming bubble. We render it while the
  // orchestrator is mid-flight (`streaming`) AND through the brief
  // gap after `done` until the messages query refetch lands the
  // canonical assistant row — otherwise the user sees the bubble
  // vanish for a tick before the persisted answer reappears.
  //
  // The bubble also carries the final answer's citations (populated
  // by `finishDone` from the `analysis.done` payload) and switches
  // off the live-streaming indicator as soon as the orchestrator
  // finishes. Without this, citations would only appear once the
  // canonical row landed (the persisted row has them, but the
  // virtual bubble was hardcoded to `citations: []`) and the
  // blinking cursor would keep ticking after the answer was final.
  //
  // We also drop the virtual bubble on `error` since the ErrorBanner
  // is the canonical UI for that phase (the messages query will still
  // refetch and land the persisted error row, but the banner takes
  // precedence visually).
  //
  // ── Stop condition (this used to be the source of a bad bug) ────────
  // The bubble must stop ONLY once a persisted row for THIS stream is
  // actually in the messages cache. The previous implementation used an
  // "id !== streamId ⇒ canonical row landed" heuristic. That is wrong:
  // in any chat with prior history the most-recent assistant row is
  // some *older* turn whose id naturally differs from the current
  // streamId, so the heuristic latched on the very first render of a
  // new stream and suppressed the live bubble for the whole answer —
  // the reply looked like it vanished until the next refetch.
  //
  // Instead of guessing from an id mismatch, we check for a row that is
  // demonstrably THIS stream's row: either the bridge's optimistic
  // insert (`id === streamId`), or an assistant row the refetch brought
  // in — identified by its id not being one of the ids that were already
  // cached when the stream started (`preStreamIds`).
  const activeStreamId =
    stream.state.kind === "streaming" || stream.state.kind === "done"
      ? stream.state.streamId
      : null;

  // Snapshot the ids that predate the current stream, using React's
  // "adjust state during render" pattern: whenever `activeStreamId`
  // changes we re-snapshot `messages`, which at that moment still holds
  // only the history from before this question was asked. Doing it here
  // (rather than in an effect) means the very first render of a new
  // stream already has the correct snapshot — no extra paint where the
  // bubble could flicker, and no ref read during render.
  const [preStream, setPreStream] = useState<{
    streamId: string | null;
    ids: ReadonlySet<string>;
  }>({ streamId: null, ids: EMPTY_ID_SET });

  if (preStream.streamId !== activeStreamId) {
    setPreStream({
      streamId: activeStreamId,
      ids: activeStreamId ? new Set(messages.map((m) => m.id)) : EMPTY_ID_SET,
    });
  }

  const streamingBubble = useMemo(() => {
    if (!activeStreamId) return null;
    if (stream.state.kind !== "streaming" && stream.state.kind !== "done") {
      return null;
    }

    const phase = stream.state;
    const streamId = phase.streamId;
    const text = phase.text;
    const citations = phase.kind === "done" ? phase.citations : [];
    const isStreaming = phase.kind === "streaming";

    // Find the most-recent assistant message in chronological order
    // (the workspace flattens pages oldest → newest).
    let lastAssistant: ConversationMessage | undefined;
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === "assistant") {
        lastAssistant = messages[i];
        break;
      }
    }

    // This stream is covered once the cache holds a row attributable to
    // it: either the optimistic insert written by the assistant bridge,
    // or the canonical row the refetch brought back (its id is not in
    // the pre-stream snapshot, which is what rules out an unrelated
    // earlier turn being mistaken for this answer).
    const covered =
      !!lastAssistant &&
      (lastAssistant.id === streamId || !preStream.ids.has(lastAssistant.id));

    if (covered) return null;
    return { streamId, text, citations, isStreaming };
  }, [activeStreamId, stream.state, messages, preStream.ids]);

  // Map the stream phase onto the composer's three-state machine.
  // The composer drives its own progress affordances (spinner for
  // submitting, stop button for streaming, plain send for idle) so the
  // parent only needs to forward the current phase.
  const composerStatus: "idle" | "submitting" | "streaming" =
    stream.state.kind === "submitting"
      ? "submitting"
      : stream.state.kind === "streaming"
        ? "streaming"
        : "idle";

  // Surface a soft hint when the chat has zero docs and isn't in
  // "all docs" mode — submitting right now would surface
  // NO_DOCUMENTS_READY as an error, which is noisier than just
  // telling the user upfront.
  const noDocsHint =
    !conversation.scope_all_documents && conversation.document_ids.length === 0
      ? "Pick at least one document above to ground your answers."
      : undefined;

  return (
    <div
      className={cn(
        "flex h-full flex-col overflow-hidden",
        className,
      )}
    >
      <ChatHeader conversation={conversation} />

      {messagesQuery.isError ? (
        <div className="mx-6 mt-2 flex items-center gap-2 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive">
          <AlertCircle className="size-3.5 shrink-0" />
          Couldn&apos;t load this conversation&apos;s history. Try
          refreshing the page.
        </div>
      ) : null}

      {stream.state.kind === "error" ? (
        <ErrorBanner
          code={stream.state.code}
          message={stream.state.message}
          onDismiss={stream.reset}
        />
      ) : null}

      <MessagesList
        messages={messages}
        streaming={streamingBubble}
        hasMore={messagesQuery.hasNextPage ?? false}
        isLoadingMore={messagesQuery.isFetchingNextPage}
        onLoadMore={() => {
          void messagesQuery.fetchNextPage();
        }}
      />

      <Composer
        // No `initialValue` here. The pending question is already in
        // the user's own message bubble by the time the workspace
        // renders (the orchestrator persists the user turn before
        // the first `analysis.token` event, so the messages list
        // already shows what they asked). Prefilling the composer
        // made it look like "the input wasn't cleared after send",
        // which was the user-reported UX bug.
        onSubmit={(q) => stream.submit(q)}
        // Cancel is a no-op unless a stream is actually in flight —
        // the parent uses the same hook for submit + cancel, and
        // the hook itself is a no-op when there's no active stream.
        onCancel={() => stream.cancel()}
        status={composerStatus}
        hint={noDocsHint}
      />
    </div>
  );
}

/* ---------- Subcomponents ---------- */

function ErrorBanner({
  code,
  message,
  onDismiss,
}: {
  code: string;
  message: string;
  onDismiss: () => void;
}) {
  return (
    <div
      role="alert"
      className="mx-auto mt-2 flex w-full max-w-3xl items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive"
    >
      <AlertCircle className="mt-0.5 size-3.5 shrink-0" />
      <div className="flex-1">
        <p className="font-medium">
          {code === "PROVIDER_ERROR"
            ? "The AI provider hit an error."
            : code === "NO_DOCUMENTS_READY"
              ? "No documents to ground on."
              : "Something went wrong."}
        </p>
        {message ? (
          <p className="mt-0.5 text-destructive/80">{message}</p>
        ) : null}
      </div>
      <button
        type="button"
        onClick={onDismiss}
        className="rounded-md px-2 py-0.5 text-destructive/80 hover:bg-destructive/10 hover:text-destructive"
      >
        Dismiss
      </button>
    </div>
  );
}

/**
 * Inline spinner used while the messages query is pending for the
 * very first time. After that the cached `items` array keeps the
 * list rendered even during a refetch.
 */
export function ChatWorkspaceSkeleton() {
  return (
    <div className="grid h-full place-items-center text-sm text-muted-foreground">
      <Loader2 className="size-4 animate-spin" />
    </div>
  );
}