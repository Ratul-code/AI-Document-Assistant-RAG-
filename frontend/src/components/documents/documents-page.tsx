"use client";

import { useQuery } from "@tanstack/react-query";
import { AlertCircle, Loader2, RefreshCw, Upload } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { documentKeys, listDocuments } from "@/lib/api/documents";
import { useUploadDocument } from "@/lib/realtime/use-upload-document";

import { DocumentRow } from "./document-row";
import { type FileKind } from "./documents-data";
import { UploadDocumentDialog } from "./upload-document-dialog";

/**
 * Documents workspace — list of uploaded documents.
 *
 * The global activity feed (`GlobalActivityFeed`) is intentionally
 * not rendered on this page for now. The component stays in the
 * codebase (so a future "Activity" sidebar / page can mount it),
 * but mounting it here would fire the `GET /documents/logs` query
 * via `useMyLogs` — which we don't want right now.
 *
 * Upload flow:
 *   1. Dialog submits → POST /upload-intent (via useUploadDocument).
 *      On success the dialog closes; the rest of the pipeline
 *      (PUT to storage + POST /confirm) runs in the background
 *      inside the hook.
 *   2. The documents list is invalidated after intent and again
 *      after confirm so the new row appears immediately and
 *      transitions to its final status.
 */
const PAGE_SIZE = 50;

/** MIME-type guess for a given picked kind. */
const MIME_BY_KIND: Record<FileKind, string> = {
  pdf: "application/pdf",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  txt: "text/plain",
  md: "text/markdown",
};

export function DocumentsPage() {
  const [dialogOpen, setDialogOpen] = useState(false);


  const documentsQuery = useQuery({
    queryKey: documentKeys.list({
      status: undefined,
      limit: PAGE_SIZE,
      offset: 0,
    }),
    queryFn: () => listDocuments({ limit: PAGE_SIZE, offset: 0 }),
  });



  const { start, isStarting, intentError, resetIntent } = useUploadDocument();

  /**
   * Called by the dialog when the user submits a valid form.
   * Fires the intent POST; on success the dialog closes and the
   * rest of the pipeline (PUT + confirm) runs in the background.
   */
  async function handleUpload(input: {
    title: string;
    kind: FileKind;
    file: File;
  }) {
    const result = await start({
      title: input.title,
      filename: input.file.name,
      mime_type: input.file.type || MIME_BY_KIND[input.kind],
      file_size_bytes: input.file.size,
      file: input.file,
    });
    // Close the dialog only if intent succeeded — otherwise leave
    // it open so the user sees the error and can retry.
    if (result !== null) {
      setDialogOpen(false);
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col gap-4 p-6">
      {/* Header */}
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Documents</h1>
          <p className="text-sm text-muted-foreground">
            Upload documents so the AI can use them as context for your
            questions.
          </p>
        </div>
        <Button onClick={() => setDialogOpen(true)}>
          <Upload className="size-4" />
          Upload document
        </Button>
      </header>

      {/* Body: documents list. (The global activity feed is
          intentionally not rendered here — see the file-level
          JSDoc above.) */}
      <section className="flex min-h-0 flex-1 flex-col gap-3">
        <DocumentsListHeader
          total={documentsQuery.data?.total}
          isFetching={documentsQuery.isFetching}
          onRefresh={() => documentsQuery.refetch()}
        />
        <div className="flex flex-col gap-2 overflow-y-auto pr-1">
          {documentsQuery.isPending ? (
            <LibrarySkeleton />
          ) : documentsQuery.isError ? (
            <LibraryError
              message={
                documentsQuery.error instanceof Error
                  ? documentsQuery.error.message
                  : "Could not load documents."
              }
              onRetry={() => documentsQuery.refetch()}
              isRetrying={documentsQuery.isFetching}
            />
          ) : documentsQuery.data &&
            documentsQuery.data.items.length === 0 ? (
            <p className="rounded-md border border-dashed border-border bg-muted/30 px-4 py-8 text-center text-sm text-muted-foreground">
              No documents yet. Click &ldquo;Upload document&rdquo; to add
              one.
            </p>
          ) : (
            documentsQuery.data?.items.map((doc) => {
              if (doc.status != "failed") {
                return (
                  <DocumentRow key={doc.id} doc={doc} />
                );
              }
            })
          )}
        </div>
      </section>

      <UploadDocumentDialog
        open={dialogOpen}
        onOpenChange={(open) => {
          setDialogOpen(open);
          // Clear any lingering error when the user reopens the dialog
          // so they don't see a stale failure from a previous attempt.
          if (open) resetIntent();
        }}
        onSubmit={handleUpload}
        isPending={isStarting}
        errorMessage={intentError}
      />
    </div>
  );
}

/* ---------- Pieces ---------- */

function DocumentsListHeader({
  total,
  isFetching,
  onRefresh,
}: {
  total: number | undefined;
  isFetching: boolean;
  onRefresh: () => void;
}) {
  return (
    <div className="flex items-center justify-between">
      <h2 className="text-sm font-semibold text-muted-foreground">
        Your library{total !== undefined ? ` (${total})` : ""}
      </h2>
      <button
        type="button"
        onClick={onRefresh}
        disabled={isFetching}
        aria-label="Refresh documents"
        className="grid size-7 place-items-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-50"
      >
        <RefreshCw className={`size-3.5 ${isFetching ? "animate-spin" : ""}`} />
      </button>
    </div>
  );
}

function LibrarySkeleton() {
  return (
    <div
      role="status"
      aria-live="polite"
      className="flex items-center justify-center gap-2 rounded-md border border-dashed border-border bg-muted/30 px-4 py-8 text-sm text-muted-foreground"
    >
      <Loader2 className="size-4 animate-spin" />
      <span>Loading documents…</span>
    </div>
  );
}

function LibraryError({
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
          Couldn&apos;t load documents
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