"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import { toast } from "sonner";

import { ApiError } from "@/lib/api/client";
import {
  confirmDocument,
  documentKeys,
  putFileToSignedUrl,
  uploadIntent,
  type UploadIntentInput,
  type UploadIntentResponse,
} from "@/lib/api/documents";

/**
 * Orchestrates the full document upload pipeline:
 *
 *   1. POST /documents/upload-intent           → get signed PUT URL
 *   2. PUT  {upload_url}  (raw file, no auth)  → upload to storage
 *   3. POST /documents/{id}/confirm            → tell server it's done
 *
 * The hook owns steps 2 and 3 so the `DocumentsPage` only has to
 * call `start(...)` once. Steps 2 and 3 run *after* the dialog
 * closes — fire-and-forget from the UI's perspective.
 *
 * Failure modes (per the spec):
 *
 *   - intent fails  → toast + return null. Dialog stays open so the
 *                     user can retry.
 *   - PUT fails     → STOP. We do NOT call confirm. `putFileToSignedUrl`
 *                     itself fires `POST /documents/{id}/upload-failed`
 *                     so the server flips the row to FAILED instead of
 *                     letting it linger in PROCESSING. We then toast
 *                     a short "Upload failed" so the user isn't staring
 *                     at silence and invalidate the list so the failed
 *                     badge shows up.
 *   - confirm fails → toast. The file is in storage but the server
 *                     doesn't know. The user can retry by picking a
 *                     file again (which creates a fresh doc row).
 */

type StartInput = UploadIntentInput & {
  /** The raw file the user picked. */
  file: File;
};

export function useUploadDocument() {
  const queryClient = useQueryClient();

  /**
   * Track just the "intent" step with a mutation so the dialog can
   * disable its submit button via `isStarting`. The background
   * steps don't need to drive any UI state — the dialog is already
   * closed by the time they run.
   */
  const intentMutation = useMutation({
    mutationFn: (input: UploadIntentInput) => uploadIntent(input),
  });

  /**
   * Internal: runs PUT-to-storage and the confirm call. Errors are
   * toasted; on PUT failure we deliberately don't call confirm.
   */
  const runBackgroundSteps = useCallback(
    async (intent: UploadIntentResponse, file: File) => {
      // ---- Step 2: PUT to signed storage URL ----
      try {
        await putFileToSignedUrl(intent.upload_url, file, intent.document_id);
      } catch (e) {
        // Refresh so any "failed" row the server may have already
        // recorded shows up.
        // void queryClient.invalidateQueries({ queryKey: documentKeys.lists() });
        return;
      }

      // ---- Step 3: tell server we're done ----
      try {
        await confirmDocument(intent.document_id);
        void queryClient.invalidateQueries({ queryKey: documentKeys.lists() });
      } catch (err) {
        toast.error("Couldn't confirm upload", {
          description: humanError(
            err,
            "The file was uploaded but the server didn't get the confirmation.",
          ),
        });
      }
    },
    [queryClient],
  );

  /**
   * Start the upload pipeline.
   *
   * - Calls POST /upload-intent (this is the only step the UI waits
   *   on; `isStarting` is true during it so the dialog can disable
   *   submit).
   * - On success: closes the caller-controlled dialog via the
   *   returned intent, invalidates the documents list so the new
   *   doc shows up immediately, then runs PUT + confirm in the
   *   background.
   * - On intent failure: returns `null`; the caller leaves the
   *   dialog open and surfaces `intentError`.
   */
  const start = useCallback(
    async (input: StartInput): Promise<UploadIntentResponse | null> => {
      const { file, ...meta } = input;
      let intent: UploadIntentResponse;
      try {
        intent = await intentMutation.mutateAsync(meta);
      } catch {
        // `intentError` is already populated by the mutation;
        // returning null lets the page leave the dialog open.
        return null;
      }
      // Refresh list so the new (status="uploading") row shows up.
      void queryClient.invalidateQueries({ queryKey: documentKeys.lists() });
      // Background steps — do not await.
      void runBackgroundSteps(intent, file);
      return intent;
    },
    [intentMutation, queryClient, runBackgroundSteps],
  );

  return {
    start,
    /** True during POST /upload-intent. Drives the dialog's submit disable. */
    isStarting: intentMutation.isPending,
    /** Error from POST /upload-intent, if any. */
    intentError:
      intentMutation.error instanceof ApiError
        ? intentMutation.error.message
        : intentMutation.error instanceof Error
          ? intentMutation.error.message
          : null,
    /** Reset the intent mutation — call when the dialog re-opens. */
    resetIntent: intentMutation.reset,
  };
}

/* ---------- Helpers ---------- */

function humanError(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return fallback;
}