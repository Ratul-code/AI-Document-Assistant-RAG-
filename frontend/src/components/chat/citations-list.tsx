"use client";

import { ChevronDown, FileText } from "lucide-react";
import { useState } from "react";

import { cn } from "@/lib/utils";
import type { ConversationCitation } from "@/lib/api/conversations";

type Props = {
  citations: ConversationCitation[];
};

/**
 * Renders the `Sources` panel under an assistant bubble. Collapsed by
 * default to keep the chat tidy; click to expand and see each cited
 * chunk's snippet.
 *
 * Citations are numbered the same way the LLM referenced them in its
 * answer (`[1]`, `[2]`, ...). We sort defensively by `index` so the UI
 * stays stable even if the backend reorders them.
 */
export function CitationsList({ citations }: Props) {
  const [open, setOpen] = useState(false);

  if (citations.length === 0) return null;

  const sorted = [...citations].sort((a, b) => a.index - b.index);

  return (
    <div className="mt-3 overflow-hidden rounded-md border border-border/60 bg-muted/20">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className={cn(
          "flex w-full items-center justify-between gap-2 px-3 py-1.5",
          "text-xs font-medium text-muted-foreground",
          "hover:bg-muted/40 hover:text-foreground transition-colors",
        )}
      >
        <span className="inline-flex items-center gap-1.5">
          <span className="size-1.5 rounded-full bg-primary" aria-hidden />
          Sources{" "}
          <span className="text-muted-foreground/60">({sorted.length})</span>
        </span>
        <ChevronDown
          className={cn(
            "size-3.5 transition-transform duration-200",
            open && "rotate-180",
          )}
        />
      </button>

      {open ? (
        <ol className="flex flex-col gap-3 border-t border-border/60 px-3 py-3 text-xs">
          {sorted.map((c) => (
            <li key={`${c.index}-${c.chunk_id}`} className="flex gap-2">
              <span className="grid size-5 shrink-0 place-items-center rounded-md bg-primary/10 text-[11px] font-semibold tabular-nums text-primary">
                {c.index}
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5 truncate font-medium text-foreground">
                  <FileText className="size-3 shrink-0 text-muted-foreground" />
                  <span className="truncate">
                    {c.document_title}
                    {typeof c.page_number === "number"
                      ? ` · p. ${c.page_number}`
                      : ""}
                  </span>
                  {c.section_path.length > 0 ? (
                    <span className="truncate text-muted-foreground/60">
                      · {c.section_path.join(" / ")}
                    </span>
                  ) : null}
                </div>
                <p className="mt-1 whitespace-pre-wrap text-muted-foreground">
                  {c.snippet}
                </p>
              </div>
            </li>
          ))}
        </ol>
      ) : null}
    </div>
  );
}