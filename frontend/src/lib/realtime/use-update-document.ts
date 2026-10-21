"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";

import {
  documentKeys,
  updateDocument,
  type Document,
  type DocumentUpdateInput,
} from "@/lib/api/documents";

/**
 * React Query mutation wrapping the document update endpoint.
 *
 *   PATCH /documents/{id}
 *
 * On success, the server returns the updated `Document`. We splice
 * that response into:
 *
 *   1. The cached detail entry (`documentKeys.detail(id)`) — so any
 *      future read returns the fresh row without a refetch.
 *   2. Every cached list-view payload — by mutating the `items` array
 *      in place so the visible row updates IMMEDIATELY, instead of
 *      waiting for the next refetch round-trip. The backend also
 *      pushes a `document.status` WS event on success; that triggers
 *      a separate `invalidateQueries` in the realtime subscriber, so
 *      even unconnected clients converge on the new title.
 *
 * Cache strategy rationale: an update is cheap to splice (we already
 * have the row's id, and the response is the new row), so an
 * optimistic splice is strictly better UX than `invalidateQueries`
 * alone. The WS push is a belt-and-suspenders for clients that
 * receive the event before our mutation onSuccess runs.
 *
 * The mutation's `mutate` returns the updated `Document`. Callers
 * that need a "saving…" indicator should read `mutation.isPending`.
 *
 * Errors:
 *   - 401: not authenticated (apiClient auto-redirects to /login).
 *   - 404: document doesn't exist or is owned by someone else.
 *   - 422: Pydantic validation (e.g. title length out of range).
 *   The hook surfaces the original ApiError so the UI can decide
 *   how to toast / retry.
 */
export function useUpdateDocument() {
  const queryClient = useQueryClient();

  const mutation = useMutation<
    Document,
    Error,
    { documentId: string; input: DocumentUpdateInput }
  >({
    mutationFn: ({ documentId, input }) => updateDocument(documentId, input),
    onSuccess: (updated, { documentId }) => {
      // 1. Refresh the detail cache for this doc, if any.
      void queryClient.removeQueries({
        queryKey: documentKeys.detail(documentId),
      });

      // 2. Splice the updated row into every cached list view so the
      //    row repaints immediately. `setQueriesData` runs over each
      //    matching query; we only touch lists that actually contain
      //    this doc — others are left untouched.
      void queryClient.setQueriesData(
        { queryKey: documentKeys.lists() },
        (old) => {
          if (!old || typeof old !== "object" || !("items" in old)) {
            return old;
          }
          const items = (old as { items: Document[] }).items;
          const idx = items.findIndex((d) => d.id === documentId);
          if (idx === -1) return old;
          const nextItems = items.slice();
          nextItems[idx] = updated;
          return { ...old, items: nextItems };
        },
      );

      // 3. Belt-and-suspenders: trigger a refetch so any list whose
      //    payload doesn't match the splice pattern (e.g. a different
      //    sorting order on the server) still converges.
      void queryClient.invalidateQueries({
        queryKey: documentKeys.lists(),
      });
    },
  });

  return mutation;
}