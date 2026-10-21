"use client";

import { Loader2, Pencil } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { Document } from "@/lib/api/documents";
import { useUpdateDocument } from "@/lib/realtime/use-update-document";
import { humanError } from "@/lib/utils";

type Props = {
  /** Document being edited. The dialog pre-fills the input with
   *  `doc.title`. Pass `null` when the dialog is closed but mounted
   *  to keep Radix happy. */
  doc: Document | null;
  /** Controlled by the row's dropdown menu — see `DocumentRow`. */
  open: boolean;
  /** Called when the dialog should close (cancelled, ESC, backdrop, success). */
  onOpenChange: (open: boolean) => void;
};

/**
 * Confirmation dialog for editing a document's title.
 *
 * The only editable user-facing attribute today is `title` — the
 * human-readable label the UI shows on each row. The original
 * `filename` is NOT editable (it's a property of the file on disk,
 * not a label).
 *
 * Flow:
 *   1. User clicks "Edit" in the row's 3-dot menu → opens this dialog
 *      with the current title pre-filled in the input.
 *   2. User edits the title and clicks "Save changes" → PATCH
 *      /documents/{id}. The button shows a spinner + "Saving…" while
 *      the request is in flight. Cancel is disabled so the user can't
 *      abandon mid-flight.
 *   3. On success: a toast lands and the dialog auto-closes. The row
 *      repaints with the new title immediately via the cache splice
 *      in `useUpdateDocument`.
 *   4. On failure: an error toast lands and the dialog STAYS OPEN so
 *      the user can retry. We do NOT auto-close on error.
 *
 * The mutation is a thin React-Query wrapper around
 * `PATCH /documents/{id}`; see `useUpdateDocument` for the cache
 * invalidation contract.
 *
 * `DocumentRow` mounts one of these per document, keyed by `doc.id`,
 * so the local `title` state never collides across rows. Initialising
 * from `doc?.title ?? ""` with a lazy initializer is enough — we don't
 * need a re-sync effect because each instance is bound to one row.
 */
export function EditDocumentDialog({ doc, open, onOpenChange }: Props) {
  const updateMutation = useUpdateDocument();
  const inputRef = useRef<HTMLInputElement>(null);
  const inputId = useId();

  // Local state for the in-flight title. Lazy initialiser so we read
  // `doc?.title` exactly once at mount time; subsequent renders use
  // local state (controlled-input pattern).
  const [title, setTitle] = useState<string>(() => doc?.title ?? "");

  // Focus the input when the dialog opens so the user can start
  // typing immediately. We focus + select so a quick replace works.
  useEffect(() => {
    if (open && doc) {
      // Defer to next frame so Radix's open animation has settled.
      requestAnimationFrame(() => {
        inputRef.current?.focus();
        inputRef.current?.select();
      });
    }
  }, [open, doc]);

  // Submit via Cmd/Ctrl+Enter from inside the input. Same affordance
  // the user gets in the upload dialog.
  function handleKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (
      (event.metaKey || event.ctrlKey) &&
      event.key === "Enter" &&
      !updateMutation.isPending
    ) {
      event.preventDefault();
      void handleSubmit();
    }
  }

  async function handleSubmit() {
    if (!doc) return;
    const trimmed = title.trim();
    if (!trimmed) {
      toast.error("Title can't be empty");
      return;
    }
    if (trimmed.length > 200) {
      toast.error("Title is too long", {
        description: "Keep it under 200 characters.",
      });
      return;
    }
    // No-op when the user didn't actually change anything. Saves a
    // round-trip and avoids bumping `updated_at` for nothing.
    if (trimmed === doc.title) {
      onOpenChange(false);
      return;
    }
    try {
      await updateMutation.mutateAsync({
        documentId: doc.id,
        input: { title: trimmed },
      });
      toast.success("Document renamed", {
        description: `Now called “${trimmed}”.`,
      });
      onOpenChange(false);
    } catch (err) {
      toast.error("Couldn't save changes", {
        description: humanError(
          err,
          "Something went wrong while saving the new title.",
        ),
      });
    }
  }

  function handleOpenChange(nextOpen: boolean) {
    // Block closing via ESC / backdrop while a save is in flight —
    // closing then would leave the cached row stale.
    if (!nextOpen && updateMutation.isPending) return;
    // Clear stale mutation state when the user dismisses the dialog
    // without submitting (or after a settled success).
    if (!nextOpen) updateMutation.reset();
    onOpenChange(nextOpen);
  }

  // Render an empty Dialog body when there's no doc — keeps Radix
  // happy (mounted + open prop). The form is gated on `doc` so we
  // never display an editable title for a missing doc.
  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        // The dialog is mounted INSIDE the row's clickable container
        // (a `role="button"` div in `DocumentRow`). Radix's Portal
        // renders the DOM elsewhere, but React's synthetic event
        // system still bubbles clicks up the React parent chain —
        // so a click anywhere inside this dialog would also fire
        // the row's `setDetailOpen(true)`, re-opening the detail
        // dialog on top of the edit dialog. Stopping pointer + click
        // propagation here keeps the row's handler out of the picture
        // for every interaction inside the dialog. The detail dialog
        // already does this; the edit dialog needs the same guard.
        onPointerDown={(event) => event.stopPropagation()}
        onClick={(event) => event.stopPropagation()}
      >
        <DialogHeader>
          <DialogTitle>Edit document</DialogTitle>
          <DialogDescription>
            Change the title for this document. The original filename is
            kept as-is.
          </DialogDescription>
        </DialogHeader>
        {doc ? (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void handleSubmit();
            }}
            className="flex flex-col gap-2"
          >
            <Label htmlFor={inputId} className="text-sm font-medium">
              Title
            </Label>
            <Input
              ref={inputRef}
              id={inputId}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              onKeyDown={handleKeyDown}
              disabled={updateMutation.isPending}
              maxLength={200}
              placeholder="Document title"
              autoComplete="off"
            />
          </form>
        ) : null}
        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => handleOpenChange(false)}
            disabled={updateMutation.isPending}
          >
            Cancel
          </Button>
          <Button
            onClick={() => void handleSubmit()}
            disabled={
              updateMutation.isPending ||
              !doc ||
              !title.trim() ||
              title.trim() === doc?.title
            }
          >
            {updateMutation.isPending ? (
              <>
                <Loader2 className="size-3.5 animate-spin" />
                Saving…
              </>
            ) : (
              <>
                <Pencil className="size-3.5" />
                Save changes
              </>
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}