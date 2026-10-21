"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  conversationKeys,
  createConversation,
  updateConversation,
  type Conversation,
} from "@/lib/api/conversations";

import {
  describeScope,
  DocumentScopePickerBody,
  type DocumentScope,
} from "./document-scope-picker";

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /**
   * Existing conversation. Required for the edit-mode (PATCH); omitted
   * for the create-mode (POST a new conversation).
   */
  conversation?: Conversation;
  /**
   * Initial scope for create-mode (no `conversation` provided). Ignored
   * in edit-mode — the conversation's saved scope is the source of
   * truth and the picker resets to it on every open.
   */
  initialScope?: DocumentScope;
  /**
   * Create-mode only: called with the freshly-created conversation
   * when the user confirms. The parent (e.g. ChatWelcome) is then
   * expected to navigate to `/chat?c=<id>`.
   */
  onCreated?: (conversation: Conversation) => void;
  /**
   * Fires whenever the user mutates the scope inside the picker.
   * The parent can use this to keep a sibling UI (e.g. the welcome
   * pill) in sync. NOT fired on programmatic resets.
   */
  onScopeChange?: (scope: DocumentScope) => void;
  /**
   * Optional override for create-mode. When provided, the dialog
   * hands the current scope to this callback and skips its own
   * create mutation — the parent owns the create (typically by
   * deferring to the composer-send path so an empty chat is never
   * created on scope selection alone).
   *
   * If omitted, the dialog creates a conversation on its own and
   * calls `onCreated(conv)`. The two are mutually exclusive: passing
   * `onConfirm` disables the dialog's create.
   */
  onConfirm?: (scope: DocumentScope) => void;
};

/**
 * Dialog to set a chat's document scope.
 *
 * Two modes:
 *
 *   1. "edit" — `conversation` is provided. Saves via
 *      `PATCH /conversations/{id}` and invalidates the conversation
 *      detail cache so the chat header badge re-renders.
 *
 *   2. "create" — no `conversation`. Saves by calling
 *      `POST /conversations` and invokes `onCreated(conv)` so the
 *      parent can navigate to the new chat. `initialScope` seeds the
 *      picker; defaults to "all documents".
 *
 * The actual checkbox + doc-list UI lives in
 * `<DocumentScopePickerBody>` so we can reuse it from inline pickers
 * (e.g. the welcome screen) without dragging the modal along.
 */
export function DocumentPickerDialog({
  open,
  onOpenChange,
  conversation,
  initialScope,
  onCreated,
  onScopeChange,
  onConfirm,
}: Props) {
  const isEditMode = conversation !== undefined;
  // `onConfirm` overrides the dialog's own create in create-mode.
  // When the parent owns the create (e.g. ChatWelcome defers the
  // conversation creation to the composer-send path), the dialog
  // just hands the chosen scope to the parent and closes.
  const parentOwnsCreate = !isEditMode && onConfirm !== undefined;
  const queryClient = useQueryClient();

  // Seed state from the conversation (edit) or the initial scope (create).
  const seed: DocumentScope = isEditMode
    ? {
      allDocuments: conversation.scope_all_documents,
      documentIds: conversation.document_ids,
    }
    : initialScope ?? { allDocuments: true, documentIds: [] };

  // We always read state from a single source; the picker body is
  // controlled so the dialog can keep "Save" disabled until something
  // changed (edit) or non-trivial (create).
  const [scope, setScopeState] = useState<DocumentScope>(seed);

  // Wrapper that fans the change out to the picker body AND to the
  // optional parent listener. The parent uses this to keep a sibling
  // UI in sync without the picker dialog owning that state.
  const setScope = (next: DocumentScope) => {
    setScopeState(next);
    onScopeChange?.(next);
  };

  // Reset to seed every time the dialog re-opens. Render-time branch
  // on `open` mirrors the pattern in `upload-document-dialog.tsx` to
  // avoid cascading re-renders flagged by
  // react-hooks/set-state-in-effect.
  const [lastOpen, setLastOpen] = useState(open);
  if (open && !lastOpen) {
    setScope(seed);
    setLastOpen(true);
  } else if (!open && lastOpen) {
    setLastOpen(false);
  }

  const saveMutation = useMutation({
    mutationFn: async (): Promise<Conversation> => {
      // Wire mapping: empty `documentIds` → `scope_all_documents: true`.
      // This is the new implicit "all documents" semantics — the picker
      // no longer exposes a separate "All documents" toggle row, so the
      // array length is the single source of truth. (See ADR-024.)
      const isAll = scope.documentIds.length === 0;
      const documentIds = isAll ? [] : scope.documentIds;
      if (isEditMode && conversation) {
        return updateConversation(conversation.id, {
          document_ids: documentIds,
          scope_all_documents: isAll,
        });
      }
      return createConversation({
        title: null,
        document_ids: documentIds,
        scope_all_documents: isAll,
      });
    },
    onSuccess: (conv) => {
      if (isEditMode) {
        void queryClient.invalidateQueries({
          queryKey: conversationKeys.detail(conv.id),
        });
        void queryClient.invalidateQueries({
          queryKey: conversationKeys.lists(),
        });
      } else {
        // Mirror the existing list-invalidation behaviour so the
        // sidebar reflects the new chat immediately when the parent
        // navigates.
        void queryClient.invalidateQueries({
          queryKey: conversationKeys.lists(),
        });
        onCreated?.(conv);
      }
      onOpenChange(false);
    },
  });

  function handleSave() {
    // Edit-mode: PATCH the existing conversation.
    if (isEditMode) {
      saveMutation.mutate();
      return;
    }
    // Create-mode with `onConfirm`: parent owns the create. Hand the
    // current scope to the parent and close — no conversation is
    // created here. (The parent, e.g. ChatWelcome, defers the actual
    // create to the composer-send path so an empty chat is never
    // stranded in the sidebar.)
    if (parentOwnsCreate) {
      onConfirm?.(scope);
      onOpenChange(false);
      return;
    }
    // Default create-mode: the dialog creates the conversation on
    // its own and notifies via `onCreated`.
    saveMutation.mutate();
  }

  // "Save" is meaningful when something actually changed (edit) or
  // any state is reachable (create — the default "all documents"
  // is a valid save target with no ticks selected). In edit-mode we
  // still allow saving a zero-doc selection — the user might be
  // deliberately clearing scope to debug NO_DOCUMENTS_READY.
  const hasChanges = isEditMode
    ? !sameScope(scope, seed)
    : true;

  const canSave = hasChanges && !saveMutation.isPending;
  const isSaving = saveMutation.isPending;

  const title = isEditMode ? "Choose documents for this chat" : "Choose documents";
  const description = isEditMode
    ? "The AI grounds its answers on the documents you pick here. You can change this any time."
    : "Pick which documents the AI should search for this new chat. You can change this any time.";
  const saveLabel = "Save";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="text-lg">{title}</DialogTitle>
          <DialogDescription className="leading-relaxed">
            {description}
          </DialogDescription>
        </DialogHeader>

        <DocumentScopePickerBody scope={scope} onChange={setScope} />

        {saveMutation.isError ? (
          <p className="text-xs text-destructive" role="alert">
            Couldn&apos;t save the new scope. Please try again.
          </p>
        ) : null}

        <DialogFooter className="gap-2 sm:gap-2">
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={isSaving}
          >
            Cancel
          </Button>
          <Button
            type="button"
            onClick={handleSave}
            disabled={!canSave}
          >
            {isSaving ? (
              <Loader2 className="size-4 animate-spin" />
            ) : null}
            {saveLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/* ---------- Helpers ---------- */

function sameScope(a: DocumentScope, b: DocumentScope): boolean {
  // The picker's source of truth is `documentIds.length === 0` for
  // "all documents"; the legacy `allDocuments` flag is no longer
  // meaningful. Compare arrays only.
  if (a.documentIds.length !== b.documentIds.length) return false;
  const aSet = new Set(a.documentIds);
  for (const id of b.documentIds) if (!aSet.has(id)) return false;
  return true;
}

/* Re-export so the welcome screen can render a matching pill without
   importing the picker internals directly. */
export { describeScope };
