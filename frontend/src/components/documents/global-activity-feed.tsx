"use client";

import { useQuery } from "@tanstack/react-query";
import { AlertCircle, Loader2 } from "lucide-react";
import { useCallback, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  documentKeys,
  getMyLogs,
  type DocumentLogItem,
} from "@/lib/api/documents";
import { cn } from "@/lib/utils";

import { DocumentLogRow } from "./document-log-row";

/**
 * Global "Activity" feed — logs across all of the caller's documents,
 * newest first.
 *
 * - First page goes through `useQuery` and is explicitly refreshed
 *   via `refetchMyLogs` from page actions. This feed does NOT
 *   subscribe to per-doc WS pushes — `document.log` events are
 *   intentionally scoped to the per-document Activity panel only.
 * - "Load more" walks `offset` forward by `PAGE_SIZE` until
 *   `items.length === total`.
 * - Each row already carries its own `document_title` and `file_type`
 *   from the backend, so we don't need to join against the
 *   documents list here.
 */

const PAGE_SIZE = 50;

type Props = {
  className?: string;
};

export function GlobalActivityFeed({ className }: Props) {
  const firstPage = useFirstPage();
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
      const next = await getMyLogs({ limit: PAGE_SIZE, offset: items.length });
      setExtraPages((prev) => [...prev, next.items]);
    } catch (err) {
      setLoadMoreError(
        err instanceof Error ? err.message : "Could not load more activity.",
      );
    } finally {
      setLoadingMore(false);
    }
    // `items.length` is read at call-time; including it in deps would
    // re-create this handler mid-render and could double-fetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadingMore, fetchedAll]);

  return (
    <aside
      className={cn(
        "flex h-full min-h-0 flex-col rounded-lg border border-border bg-card",
        className,
      )}
    >
      <Header count={items.length} total={total} />

      <div className="flex-1 overflow-y-auto p-3">
        {firstPage.isPending ? (
          <Skeleton />
        ) : firstPage.isError ? (
          <ErrorState
            message={
              firstPage.error instanceof Error
                ? firstPage.error.message
                : "Could not load activity."
            }
            onRetry={() => firstPage.refetch()}
            isRetrying={firstPage.isFetching}
          />
        ) : items.length === 0 ? (
          <Empty />
        ) : (
          <ul className="flex flex-col gap-2">
            {items.map((it) => (
              <DocumentLogRow
                key={it.id}
                item={it}
                title={it.document_title ?? "Untitled document"}
                subtitle={subtitleFor(it)}
              />
            ))}
          </ul>
        )}

        {loadMoreError ? <LoadMoreError message={loadMoreError} /> : null}
      </div>

      {items.length > 0 && !fetchedAll ? (
        <Footer
          loading={loadingMore}
          remaining={Math.max(total - items.length, 0)}
          onClick={() => void onLoadMore()}
        />
      ) : null}
    </aside>
  );
}

/* ---------- Fetcher ---------- */

function useFirstPage() {
  return useQuery({
    queryKey: [...documentKeys.logs(), { limit: PAGE_SIZE, offset: 0 }],
    queryFn: () => getMyLogs({ limit: PAGE_SIZE, offset: 0 }),
    staleTime: 30 * 1000,
  });
}

/* ---------- Helpers ---------- */

/** Show file type + server message when we have both, or just one. */
function subtitleFor(item: DocumentLogItem): string | null {
  const parts: string[] = [];
  if (item.file_type) parts.push(item.file_type.toUpperCase());
  if (item.message) parts.push(item.message);
  return parts.length > 0 ? parts.join(" · ") : null;
}

/* ---------- Sub-components ---------- */

function Header({ count, total }: { count: number; total: number }) {
  return (
    <header className="flex items-center justify-between border-b border-border px-4 py-3">
      <h2 className="text-sm font-semibold">Activity</h2>
      <span className="text-xs tabular-nums text-muted-foreground">
        {count}
        {total > count ? ` / ${total}` : ""}
      </span>
    </header>
  );
}

function Footer({
  loading,
  remaining,
  onClick,
}: {
  loading: boolean;
  remaining: number;
  onClick: () => void;
}) {
  return (
    <footer className="border-t border-border px-4 py-2">
      <Button
        variant="ghost"
        size="sm"
        className="w-full"
        onClick={onClick}
        disabled={loading}
      >
        {loading ? (
          <>
            <Loader2 className="size-3.5 animate-spin" />
            Loading…
          </>
        ) : (
          `Load more (${remaining} remaining)`
        )}
      </Button>
    </footer>
  );
}

function Skeleton() {
  return (
    <div className="flex items-center justify-center gap-2 py-8 text-xs text-muted-foreground">
      <Loader2 className="size-3.5 animate-spin" />
      <span>Loading activity…</span>
    </div>
  );
}

function Empty() {
  return (
    <p className="px-2 py-6 text-center text-xs text-muted-foreground">
      No activity yet. Upload a document to see entries here.
    </p>
  );
}

function LoadMoreError({ message }: { message: string }) {
  return (
    <p
      role="alert"
      className="mt-2 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive"
    >
      {message}
    </p>
  );
}

function ErrorState({
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
      <Button size="sm" variant="outline" onClick={onRetry} disabled={isRetrying}>
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