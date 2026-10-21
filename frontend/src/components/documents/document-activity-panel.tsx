"use client";

import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { AlertCircle, Loader2 } from "lucide-react";
import { useCallback, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  documentKeys,
  getDocumentLogs,
  type DocumentLogItem,
  type DocumentLogListResponse,
} from "@/lib/api/documents";
import { useDocumentLogBridge } from "@/lib/realtime/use-document-log-bridge";
import { cn } from "@/lib/utils";

import { DocumentLogRow } from "./document-log-row";

/**
 * Per-document "Activity" panel.
 *
 * - Renders the latest logs for ONE document, newest first.
 * - Fetches the first page on mount, then exposes a "Load more"
 *   button that walks `offset` forward by `PAGE_SIZE` until
 *   `items.length === total`.
 * - The realtime bridge (`useDocumentLogBridge`) prepends incoming
 *   `document.log` events into the underlying cache, so new
 *   entries show up without a refetch and pagination totals stay
 *   correct.
 *
 * Pagination model: simple offset/limit (no infinite scroll). The
 * per-doc feed is unlikely to be huge and a button lets users jump
 * back to the top without a scroll position to wrestle with.
 *
 * We accumulate pages in a `useState` keyed by offset, then derive
 * the rendered list in render. The first page also goes through
 * `useQuery` (so the realtime bridge can patch it).
 */

const PAGE_SIZE = 50;

type Props = {
  documentId: string;
  /** Optional className for the outer panel. */
  className?: string;
  title?: string;
};

export function DocumentActivityPanel({
  documentId,
  className,
  title = "Activity",
}: Props) {
  // Page-scoped realtime: subscribe to `document.log` events only
  // while this panel is mounted. The hook internally matches
  // `documentKeys.logs(), documentId, ...` so per-doc previews get
  // pushed in, and unsubscribes on unmount — leaving any other
  // page (e.g. the global feed) untouched.
  useDocumentLogBridge();

  // First page goes through useQuery so the realtime log bridge has
  // a query entry to patch into.
  const firstPage = useDocumentLogsFirstPage(documentId);

  // Additional pages the user has explicitly loaded.
  const [extraPages, setExtraPages] = useState<DocumentLogItem[][]>([]);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null);

  // Total reported by the first page is the authoritative count.
  const total = firstPage.data?.total ?? 0;

  // Derive the rendered list from the first page + the explicitly
  // loaded extra pages. The realtime bridge has already prepended
  // new items into firstPage.data, so the merge is just a flatten.
  const firstItems = firstPage.data?.items ?? [];
  const items = [...firstItems, ...extraPages.flat()];

  const fetchedAll = items.length >= total;

  const onLoadMore = useCallback(async () => {
    if (loadingMore || fetchedAll) return;
    setLoadingMore(true);
    setLoadMoreError(null);
    try {
      const offset = items.length;
      const next = await getDocumentLogs(documentId, {
        limit: PAGE_SIZE,
        offset,
      });
      setExtraPages((prev) => [...prev, next.items]);
    } catch (err) {
      setLoadMoreError(
        err instanceof Error ? err.message : "Could not load more activity.",
      );
    } finally {
      setLoadingMore(false);
    }
    // `items.length` is intentionally omitted from deps; we read it
    // fresh inside the handler via the latest closure. Including it
    // would cause `loadingMore` to flip inside a re-render and could
    // double-fetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [documentId, loadingMore, fetchedAll]);

  return (
    <aside
      className={cn(
        "flex h-full min-h-0 flex-col rounded-lg border border-border bg-card",
        className,
      )}
    >
      <header className="flex items-center justify-between border-b border-border px-4 py-3">
        <h2 className="text-sm font-semibold">{title}</h2>
        <span className="text-xs tabular-nums text-muted-foreground">
          {items.length}
          {total > items.length ? ` / ${total}` : ""}
        </span>
      </header>

      <div className="flex-1 overflow-y-auto p-3">
        {firstPage.isPending ? (
          <PanelSkeleton />
        ) : firstPage.isError ? (
          <PanelError
            message={
              firstPage.error instanceof Error
                ? firstPage.error.message
                : "Could not load activity."
            }
            onRetry={() => firstPage.refetch()}
            isRetrying={firstPage.isFetching}
          />
        ) : items.length === 0 ? (
          <p className="px-2 py-6 text-center text-xs text-muted-foreground">
            No activity yet for this document.
          </p>
        ) : (
          <ul className="flex flex-col gap-2">
            {items.map((it) => (
              <DocumentLogRow key={it.id} item={it} />
            ))}
          </ul>
        )}

        {loadMoreError ? (
          <p
            role="alert"
            className="mt-2 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive"
          >
            {loadMoreError}
          </p>
        ) : null}
      </div>

      {items.length > 0 && !fetchedAll ? (
        <footer className="border-t border-border px-4 py-2">
          <Button
            variant="ghost"
            size="sm"
            className="w-full"
            onClick={() => void onLoadMore()}
            disabled={loadingMore}
          >
            {loadingMore ? (
              <>
                <Loader2 className="size-3.5 animate-spin" />
                Loading…
              </>
            ) : (
              `Load more (${Math.max(total - items.length, 0)} remaining)`
            )}
          </Button>
        </footer>
      ) : null}
    </aside>
  );
}

/* ---------- Local first-page fetcher ---------- */

/**
 * Internal: fetches just the first page of per-document logs. Lives
 * here (rather than in `lib/realtime/`) because it's tightly coupled
 * to the panel's pagination model.
 *
 * The realtime log bridge patches any cache entry whose key starts
 * with `[..., documentKeys.logs(), documentId, ...]`, which is
 * exactly this query's key prefix, so WS events prepended in will
 * appear in the panel without a refetch.
 */
function useDocumentLogsFirstPage(
  documentId: string,
): UseQueryResult<DocumentLogListResponse, Error> {
  return useQuery<DocumentLogListResponse, Error>({
    queryKey: documentKeys.log(documentId, { limit: PAGE_SIZE, offset: 0 }),
    queryFn: () =>
      getDocumentLogs(documentId, { limit: PAGE_SIZE, offset: 0 }),
    enabled: Boolean(documentId),
    staleTime: 30 * 1000,
  });
}

/* ---------- Small presentational helpers ---------- */

function PanelSkeleton() {
  return (
    <div className="flex items-center justify-center gap-2 py-8 text-xs text-muted-foreground">
      <Loader2 className="size-3.5 animate-spin" />
      <span>Loading activity…</span>
    </div>
  );
}

function PanelError({
  message,
  onRetry,
  isRetrying,
}: {
  message: string;
  onRetry: () => void;
  isRetrying: boolean;
}) {
  return (
    <div
      role="alert"
      className="flex items-start gap-3 rounded-md border border-destructive/30 bg-destructive/5 px-4 py-3"
    >
      <AlertCircle className="mt-0.5 size-4 shrink-0 text-destructive" />
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium text-foreground">
          Couldn&apos;t load activity
        </p>
        <p className="truncate text-xs text-muted-foreground">{message}</p>
      </div>
      <Button
        size="sm"
        variant="outline"
        onClick={onRetry}
        disabled={isRetrying}
      >
        {isRetrying ? (
          <>
            <Loader2 className="size-3.5 animate-spin" />
            Retrying…
          </>
        ) : (
          "Retry"
        )}
      </Button>
    </div>
  );
}