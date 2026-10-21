"use client";

import { ChevronDown, ChevronRight } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

type Props = {
  label: string;
  /** Total number of items in this section. Omit (or pass undefined) while
   * the count is being fetched, so we don't render "0" before we know. */
  totalCount: number | undefined;
  /** How many are currently visible — used to compute remaining. */
  visibleCount: number;
  /** Whether the section is mounted in a collapsed sidebar. */
  collapsed: boolean;
  onExpand: () => void;
  /** If false, hides the expand chevron (e.g. when everything is shown). */
  canExpand: boolean;
  /**
   * If provided, the section header becomes a navigation link to this href,
   * with a small chevron indicating it's clickable. Use this for sections
   * that also have a dedicated page (e.g. "Documents" → /documents).
   */
  href?: string;
  children: ReactNode;
};

/**
 * Wraps a labeled list with a sticky-ish section header and a "Show more"
 * affordance at the bottom. When the sidebar is collapsed, only an icon
 * column is rendered — the list contents are hidden entirely.
 *
 * When `href` is set, the header label becomes a Next.js Link so the
 * section doubles as a navigation entry into its dedicated page.
 */
export function SidebarSection({
  label,
  totalCount,
  visibleCount,
  collapsed,
  onExpand,
  canExpand,
  href,
  children,
}: Props) {
  if (collapsed) return null;

  const remaining =
    totalCount === undefined ? 0 : Math.max(totalCount - visibleCount, 0);

  return (
    <section className="flex flex-col gap-1 px-2">
      <header className="flex items-center justify-between px-2 pt-4 pb-1.5">
        {href ? (
          <Link
            href={href}
            className={cn(
              "group -mx-1 inline-flex items-center gap-1 rounded-sm px-1",
              "text-[11px] font-medium uppercase tracking-wider",
              "text-muted-foreground hover:text-foreground transition-colors",
            )}
          >
            <span>{label}</span>
            <ChevronRight
              className={cn(
                "size-3 transition-transform",
                "group-hover:translate-x-0.5",
              )}
            />
          </Link>
        ) : (
          <h2 className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
            {label}
          </h2>
        )}
        <span className="text-[11px] tabular-nums text-muted-foreground/60">
          {visibleCount}/{totalCount ?? "·"}
        </span>
      </header>

      <div className="flex flex-col gap-0.5">{children}</div>

      {canExpand && remaining > 0 ? (
        <button
          onClick={onExpand}
          className={cn(
            "mt-1 inline-flex items-center gap-1.5 self-start rounded-md px-2 py-1.5",
            "text-xs font-medium text-muted-foreground",
            "hover:bg-muted hover:text-foreground transition-colors",
          )}
        >
          <ChevronDown className="size-3.5" />
          <span>Show more</span>
          <span className="text-muted-foreground/60">({remaining})</span>
        </button>
      ) : null}
    </section>
  );
}
