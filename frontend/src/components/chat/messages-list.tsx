"use client";

import { Loader2 } from "lucide-react";
import {
  useEffect,
  useLayoutEffect,
  useRef,
} from "react";

import { cn } from "@/lib/utils";
import type {
  ConversationCitation,
  ConversationMessage,
} from "@/lib/api/conversations";
import { MessageBubble } from "./message-bubble";

type Props = {
  messages: ConversationMessage[];
  /**
   * The assistant reply currently being streamed (if any). This is
   * rendered as a *virtual* last bubble that lives ABOVE the persisted
   * history — i.e. the hook accumulates text from `analysis.token`
   * events, and we show it as a streaming bubble until `analysis.done`
   * flips it into the messages cache.
   *
   * `citations` and `isStreaming` track the in-flight bubble through
   * the terminal phase: once the orchestrator emits `done`, the
   * virtual bubble renders the final citations (instead of an empty
   * list) and drops the live-streaming cursor. The bubble is then
   * swapped out for the canonical persisted row once the messages
   * query refetch resolves.
   */
  streaming?: {
    streamId: string;
    text: string;
    citations: ConversationCitation[];
    isStreaming: boolean;
  } | null;
  /**
   * True when the backend has more history to load for this
   * conversation. Drives whether the top "load more" sentinel renders.
   */
  hasMore: boolean;
  /**
   * True while a `fetchNextPage()` request is in flight. We render an
   * inline spinner above the message list and freeze scroll-position
   * adjustments until it resolves.
   */
  isLoadingMore: boolean;
  /**
   * Called when the user scrolls to the top of the list. Wired to
   * `useInfiniteQuery.fetchNextPage()`.
   */
  onLoadMore: () => void;
  className?: string;
};

/**
 * Pixels from the bottom within which we still treat the user as
 * "near the bottom". If they're within this band when a new token
 * arrives, we keep them pinned to the latest bubble; if they have
 * scrolled further up (to read history), we leave them alone. ~3 lines
 * of text feels right for a chat-density UI.
 */
const NEAR_BOTTOM_PX = 80;

function isNearBottom(el: HTMLElement): boolean {
  return (
    el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_PX
  );
}

/**
 * Scrollable list of chat turns with infinite-scroll on top.
 *
 * Three behaviours the auto-scroll machinery has to get right:
 *
 *   1. **First render / page reload** — snap to bottom after layout
 *      so the user lands on the latest turn (which is where the
 *      streaming bubble will appear if a new question arrives).
 *   2. **Older pages prepending** — preserve the user's visible
 *      position. Capture `scrollHeight` BEFORE the new content lands,
 *      then set `scrollTop = scrollHeight - prevScrollHeight` after
 *      the DOM has updated. Without this, prepending 20 messages
 *      above would yank the user to the top of the list.
 *   3. **New token / new message appended at the bottom** — only snap
 *      to bottom if the user is already near the bottom. If they
 *      scrolled up to read history we must not yank them back down.
 *
 * The "load older" trigger is an `IntersectionObserver` on a 1px
 * sentinel at the top of the scroll container. We use a sentinel
 * (rather than watching `scrollTop === 0`) because it survives tiny
 * layout shifts without firing false positives.
 *
 * We intentionally never try to detect "user scrolled up to read" and
 * pause auto-scroll on tokens specifically — the `isNearBottom` check
 * is the same UX primitive for both new tokens and new messages, which
 * is what users expect from chat apps like ChatGPT / Claude.
 */
export function MessagesList({
  messages,
  streaming,
  hasMore,
  isLoadingMore,
  onLoadMore,
  className,
}: Props) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const sentinelRef = useRef<HTMLDivElement | null>(null);

  // Track the messages count we rendered the previous paint with so
  // we can tell "an older page just prepended" (count went up while
  // the user wasn't near the bottom — guaranteed, since they had to
  // scroll up to trigger load-more) apart from "a new message just
  // landed" (count went up while user was at the bottom).
  const prevCountRef = useRef<number>(0);

  // First-paint guard: snap to bottom exactly once after the initial
  // render. Without this the user lands at the top of a long chat and
  // has to scroll down to see the latest turn.
  const didInitialScrollRef = useRef<boolean>(false);

  // Was the previous render in "loading more" mode? When this flips
  // from true → false, we've just prepended an older page and need
  // to restore the user's scroll position.
  const wasLoadingMoreRef = useRef<boolean>(false);

  // Holds the scrollTop delta we need to apply on the next layout
  // pass. Set during the render in which we observed `isLoadingMore`
  // flip, applied in `useLayoutEffect` after the DOM updates.
  const pendingScrollRestoreRef = useRef<number | null>(null);

  /* ---------- 1) First-render snap to bottom ---------- */

  useLayoutEffect(() => {
    if (didInitialScrollRef.current) return;
    if (messages.length === 0 && !streaming) return;
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    didInitialScrollRef.current = true;
    prevCountRef.current = messages.length;
  }, [messages.length, streaming]);

  /* ---------- 2) Prepend-position preservation ---------- */

  // When `isLoadingMore` flips true → false, an older page has just
  // been merged in. We need to:
  //   a) Capture the scrollHeight BEFORE the new content paints.
  //      But we only learn about it AFTER the paint has happened (the
  //      flag toggles on completion). Workaround: the moment we see
  //      the flag go true, we record `scrollHeight` — by the time it
  //      flips back, the page is appended and the height has grown
  //      by exactly the new content.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (isLoadingMore && !wasLoadingMoreRef.current) {
      // Loading started. Snapshot scrollHeight so we can restore the
      // user's position once the new page lands.
      pendingScrollRestoreRef.current = el.scrollHeight;
      wasLoadingMoreRef.current = true;
      return;
    }
    if (!isLoadingMore && wasLoadingMoreRef.current) {
      // Loading finished — apply the restore on the next layout pass.
      wasLoadingMoreRef.current = false;
      // Defer to useLayoutEffect (below) to run AFTER React has
      // committed the new DOM.
    }
  }, [isLoadingMore]);

  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (pendingScrollRestoreRef.current == null) return;
    // We are post-commit. The prepended page is now in the DOM and
    // scrollHeight has grown by exactly the new content height.
    const prev = pendingScrollRestoreRef.current;
    pendingScrollRestoreRef.current = null;
    const delta = el.scrollHeight - prev;
    if (delta > 0) {
      el.scrollTop = el.scrollTop + delta;
    }
  }, [messages.length]);

  /* ---------- 3) Auto-scroll on new tokens / new messages ---------- */

  // Only snap to bottom if the user is already near the bottom. If
  // they've scrolled up to read history, leave them there.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    // Skip on the very first render — that's handled by effect #1.
    if (!didInitialScrollRef.current) return;
    // Skip when an older page just prepended — that's effect #2.
    if (pendingScrollRestoreRef.current != null) return;

    const grew = messages.length > prevCountRef.current;
    const grewText = streaming?.text;
    if (!grew && !grewText) return;

    if (isNearBottom(el)) {
      // Schedule on the next frame so the DOM has actually painted the
      // new content before we measure scrollHeight.
      requestAnimationFrame(() => {
        if (scrollRef.current) {
          scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
        }
      });
    }
    prevCountRef.current = messages.length;
  }, [messages.length, streaming?.text]);

  // Reset the first-paint latch when the conversation changes (the
  // `messages` array's identity will change because chat-workspace
  // re-fetches on conversation switch). The simplest reliable signal
  // is `messages[0]?.id` — if it differs from what we've seen, this
  // is a different chat.
  const firstId = messages[0]?.id ?? null;
  const lastFirstIdRef = useRef<string | null>(null);
  useEffect(() => {
    if (lastFirstIdRef.current === firstId) return;
    lastFirstIdRef.current = firstId;
    didInitialScrollRef.current = false;
    pendingScrollRestoreRef.current = null;
    wasLoadingMoreRef.current = false;
    prevCountRef.current = 0;
  }, [firstId]);

  /* ---------- IntersectionObserver for "load older" ---------- */

  // The observer callback reads the latest `onLoadMore` from a ref so
  // the effect doesn't have to re-subscribe every render when the
  // parent passes a fresh arrow function. We update the ref inside
  // an effect (not during render) so React's strict lint rules are
  // happy.
  const onLoadMoreRef = useRef(onLoadMore);
  useEffect(() => {
    onLoadMoreRef.current = onLoadMore;
  }, [onLoadMore]);

  useEffect(() => {
    const sentinel = sentinelRef.current;
    const root = scrollRef.current;
    if (!sentinel || !root) return;
    if (!hasMore) return;

    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            onLoadMoreRef.current();
            // Don't unobserve — once the new page lands the sentinel
            // is still near the top (the user's scroll position is
            // preserved), so re-entering view triggers the next page.
            // Stop only when `hasMore` flips false, which is handled
            // by the effect cleanup below.
            break;
          }
        }
      },
      {
        // Watch intersections with the SCROLL CONTAINER, not the
        // viewport — the list is its own scroll context.
        root,
        // 1px of slack above the top so the trigger fires the moment
        // the sentinel is even partially visible, not strictly at 0.
        rootMargin: "1px 0px 0px 0px",
        threshold: 0,
      },
    );
    observer.observe(sentinel);
    return () => {
      observer.disconnect();
    };
  }, [hasMore, isLoadingMore]);

  /* ---------- Render ---------- */

  return (
    <div
      ref={scrollRef}
      className={cn(
        "flex flex-1 flex-col gap-4 overflow-y-auto px-4 py-6",
        className,
      )}
    >
      {hasMore ? (
        <div
          ref={sentinelRef}
          aria-hidden
          className="flex h-6 w-full items-center justify-center"
        >
          {isLoadingMore ? (
            <Loader2
              className="size-4 animate-spin text-muted-foreground"
              aria-label="Loading earlier messages"
            />
          ) : null}
        </div>
      ) : messages.length > 0 ? (
        <div className="mx-auto inline-flex items-center gap-2 text-[11px] text-muted-foreground/70">
          <span className="h-px w-8 bg-border" aria-hidden />
          Beginning of conversation
          <span className="h-px w-8 bg-border" aria-hidden />
        </div>
      ) : null}

      {messages.length === 0 && !streaming ? (
        <div className="m-auto max-w-sm text-center text-sm text-muted-foreground">
          Send a message to get started.
        </div>
      ) : null}

      {messages.map((m) => (
        <MessageBubble key={m.id} message={m} />
      ))}

      {streaming ? (
        <MessageBubble
          message={{
            id: `streaming-${streaming.streamId}`,
            role: "assistant",
            content: streaming.text,
            citations: streaming.citations,
            error_code: null,
            created_at: new Date().toISOString(),
          }}
          isStreaming={streaming.isStreaming}
        />
      ) : null}

      <div ref={bottomRef} />
    </div>
  );
}
