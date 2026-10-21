"use client";

import { useQuery } from "@tanstack/react-query";
import { Check, FileText, Loader2 } from "lucide-react";
import { useMemo, useState } from "react";

import {
  documentKeys,
  listDocuments,
  type Document,
} from "@/lib/api/documents";
import { cn } from "@/lib/utils";

const PICKER_LIMIT = 200;

/* ---------- Public types ---------- */

export type DocumentScope = {
  /**
   * Legacy flag. The picker no longer exposes a separate "All
   * documents" toggle row; the source of truth for "scope to every
   * ready doc" is now `documentIds.length === 0`. This field is kept
   * on the type so callers don't have to update, but the picker body
   * always emits `allDocuments: false` and `describeScope` reads
   * `documentIds.length`. Saved on the wire for the create/PATCH
   * payload (the three wire sites translate empty `documentIds` into
   * `scope_all_documents: true`).
   */
  allDocuments: boolean;
  /** Explicit doc selection. Empty array means "all documents". */
  documentIds: string[];
};

/**
 * Inner body of the document scope picker.
 *
 * Renders the ready-docs list with tick checkboxes. Owns the ready-
 * docs query and the local checkbox / select-all / clear state.
 *
 * There is no separate "All documents" toggle row: no ticks selected
 * means the scope is every ready document (the default). Ticking any
 * doc narrows the scope to that explicit subset.
 *
 * Stateless w.r.t. the persisted conversation — the parent owns
 * `scope` and decides what to do on save (PATCH vs. create). The
 * parent can mount this directly in a dialog (edit existing chat)
 * or inline on a page (create new chat).
 */
export function DocumentScopePickerBody({
  scope,
  onChange,
  className,
}: {
  scope: DocumentScope;
  onChange: (next: DocumentScope) => void;
  className?: string;
}) {
  const readyDocsQuery = useQuery({
    queryKey: documentKeys.list({
      status: "ready",
      limit: PICKER_LIMIT,
      offset: 0,
    }),
    queryFn: () =>
      listDocuments({ status: "ready", limit: PICKER_LIMIT, offset: 0 }),
    staleTime: 30_000,
  });

  const sortedDocs = useMemo(() => {
    const items = readyDocsQuery.data?.items ?? [];
    return [...items].sort((a, b) =>
      a.title.localeCompare(b.title, undefined, { sensitivity: "base" }),
    );
  }, [readyDocsQuery.data]);

  const selectedIds = useMemo(
    () => new Set(scope.documentIds),
    [scope.documentIds],
  );

  function toggleDoc(id: string) {
    const next = new Set(selectedIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onChange({ allDocuments: false, documentIds: Array.from(next) });
  }

  function selectAll() {
    onChange({
      allDocuments: false,
      documentIds: sortedDocs.map((d) => d.id),
    });
  }

  function clearAll() {
    onChange({ allDocuments: false, documentIds: [] });
  }

  return (
    <div className={cn("flex flex-col gap-2", className)}>
      <div className="flex items-center justify-between">
        <span className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
          Documents
        </span>
        <div className="flex items-center gap-2 text-xs">
          <button
            type="button"
            onClick={selectAll}
            disabled={
              sortedDocs.length === 0 ||
              selectedIds.size === sortedDocs.length
            }
            className="text-muted-foreground hover:text-foreground disabled:opacity-50 transition-colors"
          >
            Select all
          </button>
          <span className="text-muted-foreground/30">·</span>
          <button
            type="button"
            onClick={clearAll}
            disabled={selectedIds.size === 0}
            className="text-muted-foreground hover:text-foreground disabled:opacity-50 transition-colors"
          >
            Clear
          </button>
        </div>
      </div>

      <div className="max-h-64 overflow-y-auto rounded-md border border-border">
        {readyDocsQuery.isPending ? (
          <div className="flex items-center justify-center gap-2 px-3 py-6 text-xs text-muted-foreground">
            <Loader2 className="size-3 animate-spin" />
            Loading your documents…
          </div>
        ) : sortedDocs.length === 0 ? (
          <div className="px-3 py-6 text-center text-xs text-muted-foreground">
            You haven&apos;t uploaded any documents yet. Upload one from the{" "}
            <a
              href="/documents"
              className="text-primary underline-offset-4 hover:underline transition-colors"
            >
              Documents
            </a>{" "}
            page first.
          </div>
        ) : (
          <ul className="flex flex-col">
            {sortedDocs.map((doc) => (
              <DocRow
                key={doc.id}
                doc={doc}
                selected={selectedIds.has(doc.id)}
                onToggle={() => toggleDoc(doc.id)}
              />
            ))}
          </ul>
        )}
      </div>

      {selectedIds.size > 0 ? (
        <p className="text-[11px] text-muted-foreground">
          {selectedIds.size} selected
        </p>
      ) : null}
    </div>
  );
}

/* ---------- Subcomponents ---------- */

function DocRow({
  doc,
  selected,
  onToggle,
}: {
  doc: Document;
  selected: boolean;
  onToggle: () => void;
}) {
  return (
    <li>
      <button
        type="button"
        onClick={onToggle}
        className={cn(
          "flex w-full items-center gap-3 px-3 py-2 text-left",
          "hover:bg-muted/40 focus-visible:bg-muted/40 transition-colors",
          selected && "bg-primary/5",
        )}
      >
        <span
          className={cn(
            "grid size-4 shrink-0 place-items-center rounded border transition-colors",
            selected
              ? "border-primary bg-primary text-primary-foreground"
              : "border-border bg-background",
          )}
        >
          {selected ? <Check className="size-3" strokeWidth={3} /> : null}
        </span>
        <FileText
          className={cn(
            "size-3.5 shrink-0 transition-colors",
            selected ? "text-primary" : "text-muted-foreground",
          )}
        />
        <span
          className={cn(
            "min-w-0 flex-1 truncate text-sm",
            selected ? "text-foreground font-medium" : "text-foreground/90",
          )}
        >
          {doc.title}
        </span>
      </button>
    </li>
  );
}

/* ---------- Helpers ---------- */

/**
 * Compact badge describing a scope. Used by both the chat header pill
 * and the welcome-screen pill.
 *
 * "All documents" is the implicit default — represented in picker
 * state as `documentIds.length === 0` (the picker no longer exposes a
 * separate "All documents" toggle row). The single source of truth is
 * the array length; the legacy `allDocuments` flag on `DocumentScope`
 * is no longer used by the picker.
 */
export function describeScope(scope: DocumentScope): {
  label: string;
  variant: "success" | "muted";
} {
  const n = scope.documentIds.length;
  if (n === 0) {
    return { label: "All documents", variant: "success" };
  }
  return {
    label: n === 1 ? "1 document" : `${n} documents`,
    variant: "muted",
  };
}

/**
 * Re-export a small hook so parents that don't want to manage local
 * state can use this in tandem with the picker body.
 */
export function useDocumentScopeState(
  initial: DocumentScope = { allDocuments: true, documentIds: [] },
) {
  return useState<DocumentScope>(initial);
}