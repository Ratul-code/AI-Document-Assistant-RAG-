"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { deleteDocument, documentKeys } from "@/lib/api/documents";

/**
 * React Query mutation wrapping the document delete endpoint.
 *
 *   DELETE /documents/{id}
 *
 * On success, the document row is gone server-side. We mirror that
 * locally so any view that has the row in its cache drops it
 * immediately (rather than waiting for the next refetch):
 *
 *   1. `removeQueries` for the doc's detail key — the row is gone,
 *      so any cached response is stale.
 *   2. `invalidateQueries` for the doc-lists family — the next
 *      refetch sees the deletion; the doc-list sidebar / page just
 *      fades the row out.
 *
 * The server also pushes a `document.status` WS event on delete
 * (best-effort). The realtime subscriber picks that up and triggers
 * its own list invalidation, so even without this hook a connected
 * client sees the row disappear live.
 *
 * The mutation's `mutate` returns void — the server responds with
 * 204 No Content. Callers that need a "deleting…" indicator should
 * read `mutation.isPending`.
 *
 * Errors:
 *   - 401: not authenticated (apiClient auto-redirects to /login).
 *   - 404: already deleted, or never existed, or owned by someone
 *          else. The hook surfaces the original ApiError so the UI
 *          can decide whether to toast.
 */
export function useDeleteDocument() {
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: (documentId: string) => deleteDocument(documentId),
    onSuccess: (_void, documentId) => {
      // Drop any cached detail for this doc — the row no longer exists.
      void queryClient.removeQueries({
        queryKey: documentKeys.detail(documentId),
      });
      // Refetch every list view that could include this doc. We could
      // do an optimistic splice off the cached list, but invalidating
      // is simpler and the next refetch will already exclude the
      // deleted row.
      void queryClient.invalidateQueries({
        queryKey: documentKeys.lists(),
      });
    },
  });

  return mutation;
}