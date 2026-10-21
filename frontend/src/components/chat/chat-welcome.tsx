"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronDown,
  FileText,
  Globe2,
  Loader2,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  createConversation,
  type ConversationCreateInput,
} from "@/lib/api/conversations";
import { getReadyDocuments } from "@/lib/api/documents";
import { usePendingQuestionStore } from "@/lib/realtime/pending-question-store";
import { useWSStore, type WSMessage } from "@/lib/realtime/ws-store";

import { Composer } from "./composer";
import { DocumentPickerDialog } from "./document-picker-dialog";
import {
  describeScope,
  type DocumentScope,
} from "./document-scope-picker";

/**
 * Landing state for `/chat` when no `?c=<id>` is present.
 *
 * Layout (top → bottom):
 *
 *   ┌─────────────────────────────────────┐
 *   │   Hero heading + sub-text           │
 *   │   ┌─────────────────────────────┐   │
 *   │   │  [Scope pill ▾]  · change   │   │  ← clickable; opens picker
 *   │   └─────────────────────────────┘   │
 *   │   ┌─────────────────────────────┐   │
 *   │   │  Composer (textarea + send)  │   │
 *   │   └─────────────────────────────┘   │
 *   └─────────────────────────────────────┘
 *
 * On submit we create a conversation with the chosen scope, stash the
 * question in `usePendingQuestionStore` keyed by the new conv id, and
 * navigate to `/chat?c=<id>`. The workspace drains + auto-submits on
 * mount. We don't submit from here directly — the WS subscription
 * lives in the workspace, so a premature submit would emit
 * `analysis.*` events with no listener.
 *
 * No docs uploaded → falls back to the existing "upload a document"
 * empty state (no composer, no picker — there's nothing to pick).
 */
export function ChatWelcome() {
  const router = useRouter();
  const queryClient = useQueryClient();

  // Ready-docs count → drives the "any docs to ground on?" check.
  const readyDocsQuery = useQuery({
    queryKey: ["documents", "ready", "for-welcome", { limit: 1 }],
    queryFn: () => getReadyDocuments(1),
    staleTime: 60_000,
  });

  // If we're sitting on the "no ready docs" empty branch, a document
  // finishing processing should swap us into the main welcome state
  // without forcing the user to reload. Subscribe to the global WS
  // and re-fetch our ready-docs probe on any `document.status` event.
  // `getReadyDocuments(1)` is cheap and `document.status` is the
  // single source of truth for "did the library just gain a ready
  // doc?" — so a single refetch is the right granularity here.
  const subscribe = useWSStore((s) => s.subscribe);
  useEffect(() => {
    return subscribe((msg: WSMessage) => {
      if (msg.type !== "document.status") return;
      void queryClient.invalidateQueries({
        queryKey: ["documents", "ready", "for-welcome"],
      });
    });
  }, [subscribe, queryClient]);

  const hasAnyReady = (readyDocsQuery.data?.total ?? 0) > 0;

  // Document scope (defaults to "all documents" — most users want
  // open-ended exploration on a fresh chat).
  const [scope, setScope] = useState<DocumentScope>({
    allDocuments: true,
    documentIds: [],
  });
  const [pickerOpen, setPickerOpen] = useState(false);

  // Create-and-go mutation. The promise resolves with the freshly-
  // minted Conversation; the parent decides how to navigate.
  //
  // `usePendingQuestionStore` is a stable module-scope Zustand store,
  // so the selector returns a stable function ref across renders —
  // safe to put in the mutation's deps.
  //
  // The wire mapping must agree with the dialog's create-mode payload:
  // empty `documentIds` → `scope_all_documents: true` (the new
  // implicit "all documents" semantics — see ADR-024).
  const stashPendingQuestion = usePendingQuestionStore((s) => s.set);
  const createAndOpen = useMutation({
    mutationFn: async (input: { question: string }) => {
      const isAll = scope.documentIds.length === 0;
      const payload: ConversationCreateInput = {
        title: null,
        document_ids: isAll ? [] : scope.documentIds,
        scope_all_documents: isAll,
      };
      const conv = await createConversation(payload);
      return { conv, question: input.question };
    },
    onSuccess: ({ conv, question }) => {
      // Stash the question in the in-memory handoff BEFORE
      // navigating, so the workspace can drain it on mount. The
      // store's `set` silently no-ops on empty input, so we don't
      // need a guard here.
      stashPendingQuestion(conv.id, question);
      // `router.replace` (not push) so the back button stays on the
      // welcome state — pushing would create a "go back from a chat
      // I just made" footgun.
      void router.replace(`/chat?c=${conv.id}`);
    },
  });

  const isBusy = createAndOpen.isPending;

  function handleSubmit(question: string) {
    if (isBusy) return;
    createAndOpen.mutate({ question });
  }

  function handlePickScope() {
    if (!hasAnyReady) return;
    setPickerOpen(true);
  }

  // ────────────────────────────────────────────────
  // No-ready-docs branch: keep the original prompt.
  // ────────────────────────────────────────────────
  if (!readyDocsQuery.isPending && !hasAnyReady) {
    return (
      <div className="grid h-full place-items-center p-6">
        <div className="flex w-full max-w-md flex-col items-center gap-5 text-center">
          <div className="grid size-12 place-items-center rounded-full bg-primary/10 text-primary ring-1 ring-inset ring-primary/20">
            <FileText className="size-6" />
          </div>
          <div className="flex flex-col gap-1.5">
            <h1 className="text-[28px] font-semibold tracking-tight leading-tight">
              Start a new chat
            </h1>
            <p className="text-sm text-muted-foreground leading-relaxed">
              Upload a document first — the AI needs at least one to
              ground its answers on.
            </p>
          </div>
          <Button asChild variant="default" className="mt-1">
            <a href="/documents">Upload a document</a>
          </Button>
        </div>
      </div>
    );
  }

  // ────────────────────────────────────────────────
  // Main welcome state.
  // ────────────────────────────────────────────────
  // The picker body emits `scope = { allDocuments: false, documentIds: [] }`
  // when nothing is selected, which in the new model means "all
  // documents". `describeScope` and the composer placeholder need to
  // read that state as "all documents" too, so we project the picker's
  // raw state into a display-friendly form. The `scope` itself stays
  // the raw picker output and is what gets sent to the create
  // mutation.
  const displayScope: DocumentScope = {
    allDocuments: scope.documentIds.length === 0,
    documentIds: scope.documentIds,
  };
  const { label: scopeLabel, variant: scopeVariant } = describeScope(displayScope);
  const ScopeIcon = displayScope.allDocuments ? Globe2 : FileText;

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto flex w-full max-w-3xl flex-col items-center gap-7 px-4 pt-20 pb-8">
          <div className="grid size-12 place-items-center rounded-full bg-primary/10 text-primary ring-1 ring-inset ring-primary/20">
            <FileText className="size-6" />
          </div>
          <div className="flex flex-col items-center gap-2 text-center">
            <h1 className="text-[28px] font-semibold tracking-tight leading-tight">
              What do you want to know?
            </h1>
            <p className="max-w-md text-sm text-muted-foreground leading-relaxed">
              Ask anything about your documents. Pick which ones to
              search, then send your question.
            </p>
          </div>

          {/* Scope pill — clickable, opens the picker dialog. We use a
              real <button> so keyboard + screen-reader users get the
              same affordance as mouse users. */}
          <button
            type="button"
            onClick={handlePickScope}
            disabled={readyDocsQuery.isPending}
            className={cn(
              "group inline-flex items-center gap-1.5 rounded-full",
              "focus-visible:outline-none focus-visible:ring-2",
              "focus-visible:ring-ring focus-visible:ring-offset-2",
              "focus-visible:ring-offset-background",
              "disabled:cursor-not-allowed disabled:opacity-60",
            )}
            aria-label="Change document scope"
          >
            {readyDocsQuery.isPending ? (
              <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
                <Loader2 className="size-3 animate-spin" />
                Checking your library…
              </span>
            ) : (
              <>
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
                <span className="text-xs text-muted-foreground group-hover:text-foreground">
                  Change
                </span>
              </>
            )}
          </button>

          {createAndOpen.isError ? (
            <p className="text-xs text-destructive" role="alert">
              Couldn&apos;t start the chat. Please try again.
            </p>
          ) : null}
        </div>
      </div>

      {/* Composer pinned to the bottom (mirrors the workspace layout). */}
      <Composer
        onSubmit={handleSubmit}
        status={isBusy ? "submitting" : "idle"}
        hint="Press ⌘+Enter to send."
        placeholder={
          displayScope.allDocuments
            ? "Ask anything about all your documents…"
            : displayScope.documentIds.length === 1
              ? "Ask about the selected document…"
              : `Ask about ${displayScope.documentIds.length} documents…`
        }
      />

      <DocumentPickerDialog
        open={pickerOpen}
        onOpenChange={setPickerOpen}
        initialScope={scope}
        onScopeChange={setScope}
        // The dialog's Save commits the chosen scope to the welcome's
        // state and closes — it does NOT create a conversation. The
        // actual create happens when the user sends the first question
        // via the composer (see `createAndOpen` below). This prevents
        // an empty "untitled" chat from being stranded in the sidebar
        // every time the picker is closed.
        onConfirm={() => {
          /* no-op — `onScopeChange` already keeps `scope` in sync. */
        }}
      />
    </div>
  );
}
