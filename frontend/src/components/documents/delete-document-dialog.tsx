"use client";

import { Loader2, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { Document } from "@/lib/api/documents";
import { humanError } from "@/lib/utils";
import { useDeleteDocument } from "@/lib/realtime/use-delete-document";

type Props = {
  /** Document the user wants to delete. The dialog reads `title` and `id`. */
  doc: Document | null;
  /** Controlled by the row's dropdown menu — see `DocumentRow`. */
  open: boolean;
  /** Called when the dialog should close (cancelled, ESC, backdrop, success). */
  onOpenChange: (open: boolean) => void;
};

/**
 * Confirmation dialog for permanently deleting a document.
 *
 * Flow:
 *   1. User clicks "Delete" in the row's 3-dot menu → opens this dialog.
 *   2. User clicks "Delete" inside the dialog → DELETE /documents/{id}.
 *      The button shows a spinner + "Deleting…" while the request is
 *      in flight, and the Cancel button is disabled so the user can't
 *      cancel mid-flight.
 *   3. On success: `useDeleteDocument`'s onSuccess invalidates the doc
 *      lists cache (so the row drops out of the list page); we toast
 *      the result here and auto-close the dialog.
 *   4. On failure: an error toast lands, the dialog STAYS OPEN so the
 *      user can retry. We don't auto-close on error.
 *
 * The mutation itself is a thin React-Query wrapper around
 * `DELETE /documents/{id}`; see `useDeleteDocument` for the cache
 * invalidation contract.
 */
export function DeleteDocumentDialog({ doc, open, onOpenChange }: Props) {
  const deleteMutation = useDeleteDocument();

  function handleOpenChange(nextOpen: boolean) {
    // Block closing via ESC / backdrop while a delete is in flight —
    // closing then would leave the row visible in the list (no
    // invalidation yet) AND no spinner to track progress.
    if (!nextOpen && deleteMutation.isPending) return;
    // Clear stale mutation state when the user dismisses the dialog
    // without submitting (or after a settled success). We deliberately
    // only reset on a closed transition — never on open — otherwise
    // a freshly-opened dialog could clobber an in-flight deletion.
    if (!nextOpen) deleteMutation.reset();
    onOpenChange(nextOpen);
  }

  function handleConfirm() {
    if (!doc) return;
    deleteMutation.mutate(doc.id, {
      onSuccess: () => {
        toast.success("Document deleted", {
          description: `“${doc.title ?? "Untitled"}” has been removed.`,
        });
        onOpenChange(false);
      },
      onError: (err) => {
        toast.error("Couldn't delete document", {
          description: humanError(
            err,
            "Something went wrong while deleting the document.",
          ),
        });
      },
    });
  }

  // Render an empty Dialog body when there's no doc — keeps Radix
  // happy (mounted + open prop). The header / footer guard so we
  // never display "delete undefined" copy.
  const title = doc?.title ?? "this document";

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        // The dialog is mounted INSIDE the row's clickable container
        // (a `role="button"` div in `DocumentRow`). Radix's Portal
        // renders the DOM elsewhere, but React's synthetic event
        // system still bubbles clicks up the React parent chain —
        // so a click anywhere inside this dialog would also fire
        // the row's `setDetailOpen(true)`, re-opening the detail
        // dialog on top of the delete dialog. Stopping pointer +
        // click propagation here keeps the row's handler out of the
        // picture for every interaction inside the dialog.
        onPointerDown={(event) => event.stopPropagation()}
        onClick={(event) => event.stopPropagation()}
      >
        <DialogHeader>
          <DialogTitle>Delete document</DialogTitle>
          <DialogDescription>
            Are you sure you want to delete{" "}
            <span className="font-medium text-foreground">“{title}”</span>?
            All of its pages, chunks, and embeddings will be removed and
            the original file will be deleted from storage. This action
            cannot be undone.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => handleOpenChange(false)}
            disabled={deleteMutation.isPending}
          >
            Cancel
          </Button>
          <Button
            variant="destructive"
            onClick={handleConfirm}
            disabled={deleteMutation.isPending || !doc}
          >
            {deleteMutation.isPending ? (
              <>
                <Loader2 className="size-3.5 animate-spin" />
                Deleting…
              </>
            ) : (
              <>
                <Trash2 className="size-3.5" />
                Delete
              </>
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}