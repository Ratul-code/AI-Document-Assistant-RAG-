"use client";

import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { AlertCircle, Loader2 } from "lucide-react";
import { useCallback, useState } from "react";

import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  documentKeys,
  getDocumentLogs,
  type DocumentLogItem,
  type DocumentLogListResponse,
} from "@/lib/api/documents";
import { useDocumentLogBridge } from "@/lib/realtime/use-document-log-bridge";

import { DocumentLogRow } from "./document-log-row";

/**
 * Inline lifetime Logs panel for a single document.
 *
 * Lives inside the document detail dialog, ABOVE the stat grid in
 * the visual order (but rendered as its own section) and BELOW
 * the progress stepper. Unlike the page-level `DocumentActivityPanel`,
 * this panel:
 *
 *  - Has no outer `<aside>` and no card chrome — the dialog owns
 *    the surrounding surface, so the panel is just a labeled list.
 *  - Uses a `ScrollArea` capped at `max-h-[50vh] min-h-[12rem]` so
 *    the panel is always a useful size: tall enough to show a
 *    reasonable number of rows without scrolling, but bounded so
 *    the surrounding dialog never grows past its viewport cap.
 *    The `min-h` keeps the empty / loading state visually balanced
 *    so the dialog doesn't collapse while data is in flight.
 *  - Shows the doc's full event history (no client-side cap).
 *
 * Data flow mirrors `DocumentActivityPanel`:
 *
 *  - First page goes through `useQuery` so the realtime log bridge
 *    has a cache entry to patch into.
 *  - `useDocumentLogBridge` is mounted here (per-doc scope) and
 *    prepends incoming `document.log` events into the cache; the
 *    rendered list reflects those updates on the next render.
 *  - "Load more" walks `offset` forward by `PAGE_SIZE` until the
 *    total is exhausted.
 */

const PAGE_SIZE = 100;

type Props = {
  documentId: string;
  /** Optional className for the outer wrapper. */
  className?: string;
};

export function DocumentLogsPanel({ documentId, className }: Props) {
  // Per-doc realtime: subscribe to `document.log` events only while
  // this panel is mounted. The hook internally patches the
  // `[..., documentKeys.logs(), documentId, ...]` query cache on
  // every matching event, so new rows appear without a refetch.
  useDocumentLogBridge();

  const firstPage = useDocumentLogsFirstPage(documentId);

  const [extraPages, setExtraPages] = useState<DocumentLogItem[][]>([]);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null);

  const total = firstPage.data?.total ?? 0;
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
        err instanceof Error ? err.message : "Could not load more logs.",
      );
    } finally {
      setLoadingMore(false);
    }
    // `items.length` is read at call-time; including it in deps would
    // re-create this handler mid-render and could double-fetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [documentId, loadingMore, fetchedAll]);
  return (
    <div className={className}>
      <div className="flex items-center justify-between">
        <h4 className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
          Logs
          <span className="ml-1.5 tabular-nums">
            {items.length}
            {total > items.length ? ` / ${total}` : ""}
          </span>
        </h4>
        {/* Reserved for future filter chips (All / Errors / Progress).
            The hooks for those filters would live in `lib/realtime/`
            alongside the existing bridges; intentionally left out of
            this change to keep the diff small. */}
      </div>

      <ScrollArea className="mt-2 max-h-[50vh] min-h-[12rem] rounded-md border border-border/60">
        <div className="pr-3">
          {firstPage.isPending ? (
            <PanelSkeleton />
          ) : firstPage.isError ? (
            <PanelError
              message={
                firstPage.error instanceof Error
                  ? firstPage.error.message
                  : "Could not load logs."
              }
              onRetry={() => firstPage.refetch()}
              isRetrying={firstPage.isFetching}
            />
          ) : items.length === 0 ? (
            <PanelEmpty />
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
      </ScrollArea>

      {items.length > 0 && !fetchedAll ? (
        <div className="mt-2">
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
        </div>
      ) : null}
    </div>
  );
}

/* ---------- Local first-page fetcher ---------- */

/**
 * Fetches just the first page of per-document logs. Lives here
 * rather than in `lib/realtime/` because it's tightly coupled to
 * this panel's pagination model — the page-level `DocumentActivityPanel`
 * has its own copy with a different `PAGE_SIZE` and the global feed
 * uses a different query-key shape entirely.
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
    <div className="flex items-center justify-center gap-2 py-6 text-xs text-muted-foreground">
      <Loader2 className="size-3.5 animate-spin" />
      <span>Loading logs…</span>
    </div>
  );
}

function PanelEmpty() {
  return (
    <p className="rounded-md border border-dashed border-border bg-muted/30 px-3 py-4 text-center text-xs text-muted-foreground">
      No events yet.
    </p>
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
          Couldn&apos;t load logs
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
