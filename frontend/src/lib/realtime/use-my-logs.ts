"use client";

import { useQuery } from "@tanstack/react-query";

import {
  documentKeys,
  getMyLogs,
  type DocumentLogListResponse,
} from "@/lib/api/documents";

/**
 * Global activity feed query.
 *
 * Wraps `GET /api/v1/documents/logs?limit=&offset=` behind a standard
 * `useQuery`. Used by the right-side "Activity" panel on the
 * Documents page. This feed is **not** driven by WS pushes — the
 * realtime log bridge (`useDocumentLogBridge`) is page-scoped and
 * only updates the per-document cache, not the global feed.
 */

const DEFAULT_LIMIT = 50;

type UseMyLogsOptions = {
  limit?: number;
  offset?: number;
  enabled?: boolean;
};

export function useMyLogs(options: UseMyLogsOptions = {}) {
  const { limit = DEFAULT_LIMIT, offset = 0, enabled = true } = options;

  return useQuery<DocumentLogListResponse>({
    queryKey: [...documentKeys.logs(), { limit, offset }],
    queryFn: () => getMyLogs({ limit, offset }),
    enabled,
    staleTime: 30 * 1000,
  });
}