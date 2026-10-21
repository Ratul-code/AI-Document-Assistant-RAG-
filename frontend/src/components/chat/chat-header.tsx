"use client";

import { useState } from "react";
import { ChevronDown, FileText, Globe2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { Conversation } from "@/lib/api/conversations";
import { DocumentPickerDialog } from "./document-picker-dialog";
import { describeScope } from "./document-scope-picker";

type Props = {
  conversation: Conversation;
  className?: string;
};

/**
 * Header for an open chat.
 *
 * Renders the chat title and a clickable scope pill:
 *
 *   - `scope_all_documents === true` → "All documents" pill.
 *   - `scope_all_documents === false` → "N documents" pill.
 *
 * Clicking the pill opens `<DocumentPickerDialog>` (in edit-mode) so
 * the user can change the chat's scope without leaving the page. The
 * dialog PATCHes the conversation and invalidates the relevant query
 * keys; the header re-renders with the new scope on the next render.
 *
 * v1 intentionally does NOT expose title editing here — the backend
 * auto-titles the chat from the first question, and renaming is a
 * nice-to-have we can add later without a backend change.
 */
export function ChatHeader({ conversation, className }: Props) {
  const [pickerOpen, setPickerOpen] = useState(false);

  const title = conversation.title ?? "Untitled chat";

  const scope = {
    allDocuments: conversation.scope_all_documents,
    documentIds: conversation.document_ids,
  };
  const { label: scopeLabel, variant: scopeVariant } = describeScope(scope);
  const ScopeIcon = scope.allDocuments ? Globe2 : FileText;

  return (
    <header
      className={cn(
        "flex items-center justify-between gap-3 border-b border-border",
        "bg-background/80 px-6 py-3 backdrop-blur",
        "supports-[backdrop-filter]:bg-background/60",
        className,
      )}
    >
      <div className="flex min-w-0 items-center gap-3">
        <h1 className="truncate text-base font-semibold tracking-tight">
          {title}
        </h1>

        {/* Clickable pill — opens the picker. We use a real <button>
            (not a div) for keyboard + a11y. The chevron signals the
            dropdown affordance so users know it's interactive. */}
        <button
          type="button"
          onClick={() => setPickerOpen(true)}
          className={cn(
            "group inline-flex items-center gap-1 rounded-full",
            "focus-visible:outline-none focus-visible:ring-2",
            "focus-visible:ring-ring focus-visible:ring-offset-2",
            "focus-visible:ring-offset-background",
          )}
          aria-label="Change document scope"
        >
          <Badge variant={scopeVariant} className="gap-1">
            <ScopeIcon className="size-3" />
            {scopeLabel}
          </Badge>
          <ChevronDown
            className={cn(
              "size-3.5 text-muted-foreground transition-transform",
              "group-hover:text-foreground",
              pickerOpen && "rotate-180",
            )}
          />
        </button>
      </div>

      <DocumentPickerDialog
        open={pickerOpen}
        onOpenChange={setPickerOpen}
        conversation={conversation}
      />
    </header>
  );
}