"use client";

import { useQuery } from "@tanstack/react-query";

import {
  documentKeys,
  getDocumentLogs,
  type DocumentLogListResponse,
} from "@/lib/api/documents";

/**
 * Per-document activity log query.
 *
 * Wraps `GET /api/v1/documents/{id}/logs?limit=&offset=` behind a
 * standard `useQuery` so the per-document Activity panel can mount
 * it the same way it would mount any list query. The realtime log
 * bridge patches the same cache entry on `document.log` events.
 */

const DEFAULT_LIMIT = 50;

type UseDocumentLogsOptions = {
  /** Page size; clamped to 1..100 by the backend. */
  limit?: number;
  /** Pagination offset; defaults to 0. */
  offset?: number;
  /** Disable fetching (e.g. while waiting for a valid id). */
  enabled?: boolean;
};

export function useDocumentLogs(
  documentId: string | null | undefined,
  options: UseDocumentLogsOptions = {},
) {
  const { limit = DEFAULT_LIMIT, offset = 0, enabled = true } = options;

  return useQuery<DocumentLogListResponse>({
    queryKey: documentKeys.log(documentId ?? "", { limit, offset }),
    queryFn: () => {
      if (!documentId) {
        // Should be guarded by `enabled`; throw so React Query
        // surfaces it as an error rather than firing a 404.
        throw new Error("documentId is required");
      }
      return getDocumentLogs(documentId, { limit, offset });
    },
    enabled: enabled && Boolean(documentId),
    // The bridge prepends to the same cache, so keep it fairly
    // fresh but don't hammer the server while the tab is idle.
    staleTime: 30 * 1000,
  });
}