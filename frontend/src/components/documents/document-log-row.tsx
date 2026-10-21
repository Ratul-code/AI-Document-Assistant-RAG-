"use client";

import {
  AlertCircle,
  CircleDashed,
  FileText,
  Scissors,
  Sparkles,
  Trash2,
  UploadCloud,
  type LucideIcon,
} from "lucide-react";

import { cn, formatRelativeTime } from "@/lib/utils";
import type { DocumentLogItem } from "@/lib/api/documents";

/**
 * One row of the activity feed.
 *
 * Renders a status icon (in a small colored chip so the category
 * reads at a glance), a short label derived from `event_type`, the
 * optional `message` from the server, and a relative timestamp.
 *
 * The per-doc feed is intentionally minimal: the backend emits at
 * most one row per stage (uploaded, parsing, chunking, embedding,
 * plus a `*_failed` row if a stage failed). The row supports
 * exactly those types — anything else falls through to a neutral
 * `FALLBACK_STYLE` so future additions never crash the UI.
 *
 * Styling model (in priority order):
 *   1. `EVENT_STYLES` — first-class entries for the 5 categories
 *      the backend actually emits (`uploaded`, `parsing`,
 *      `chunking`, `embedding`, plus their `*_failed` siblings
 *      and `processing_failed` / `document.deleted`). Each gets a
 *      hue-coded icon chip from the `--log-{category}-surface` /
 *      `--log-{category}-foreground` token family so the activity
 *      log reads as a pipeline at a glance. The 8-color palette
 *      is the documented exception to the monochromatic rule in
 *      `docs/DESIGN.md §4`.
 *   2. `FALLBACK_STYLE` — neutral dashed circle for unknown /
 *      future event types. No crash, no missing icon.
 *
 * All categories are pure functions of `event_type`; the row
 * never has to know about the document it belongs to (caller
 * passes `title` / `subtitle` overrides for the cross-doc feed).
 *
 * Visual rationale for the colored chips:
 * The design system is monochromatic + lime by default, but
 * `docs/DESIGN.md §4` allows a *data-driven* color exception when
 * each color is tied to a backend event category and never reused
 * as a general UI accent. Showing the same neutral icon for every
 * event makes the activity log visually flat; the per-category hue
 * keeps the pipeline readable.
 */

type Props = {
  item: DocumentLogItem;
  /**
   * Optional override for the title row — used by the global feed
   * where we want to show which document the event belongs to.
   * Falls back to the humanized `event_type` when omitted.
   */
  title?: string;
  /**
   * Optional secondary line (e.g. "PDF · 2.4 MB"). Falls back to
   * `item.message` when omitted so the per-document panel keeps
   * showing server messages unchanged.
   */
  subtitle?: string | null;
  className?: string;
};

type EventStyle = {
  Icon: LucideIcon;
  /** Tailwind classes for the icon glyph (color). */
  iconClassName: string;
  /** Tailwind classes for the small rounded square behind the icon
   *  (background). Pairs with `iconClassName` to give each category
   *  a unique color. */
  surfaceClassName: string;
};

/**
 * Per-event style. The backend only emits 9 event types (5 stage
 * rows + 4 `*_failed` rows + `document.deleted`); we map them all
 * to one of 5 hue-coded category chips. `*_failed` rows inherit
 * their stage's icon but use the destructive-red surface so the
 * outcome reads immediately.
 */
const EVENT_STYLES: Record<string, EventStyle> = {
  // ---- Stage rows (success) ----
  uploaded: {
    Icon: UploadCloud,
    surfaceClassName: "bg-log-upload-surface",
    iconClassName: "text-log-upload-foreground",
  },
  parsing: {
    Icon: FileText,
    surfaceClassName: "bg-log-parse-surface",
    iconClassName: "text-log-parse-foreground",
  },
  chunking: {
    Icon: Scissors,
    surfaceClassName: "bg-log-chunk-surface",
    iconClassName: "text-log-chunk-foreground",
  },
  embedding: {
    Icon: Sparkles,
    surfaceClassName: "bg-log-embed-surface",
    iconClassName: "text-log-embed-foreground",
  },

  // ---- Stage rows (failure) ----
  // The icon matches the stage, but the surface + foreground
  // switch to the destructive-red palette so the outcome reads
  // immediately without the user having to read the message.
  parsing_failed: {
    Icon: FileText,
    surfaceClassName: "bg-log-failed-surface",
    iconClassName: "text-log-failed-foreground",
  },
  chunking_failed: {
    Icon: Scissors,
    surfaceClassName: "bg-log-failed-surface",
    iconClassName: "text-log-failed-foreground",
  },
  embedding_failed: {
    Icon: Sparkles,
    surfaceClassName: "bg-log-failed-surface",
    iconClassName: "text-log-failed-foreground",
  },
  processing_failed: {
    // Catch-all for unknown-stage failures in `process_document`.
    // Same destructive palette as the other failure rows; uses a
    // neutral `AlertCircle` icon so the user understands the
    // stage is unknown.
    Icon: AlertCircle,
    surfaceClassName: "bg-log-failed-surface",
    iconClassName: "text-log-failed-foreground",
  },

  // ---- Operator / out-of-band ----
  document_deleted: {
    Icon: Trash2,
    surfaceClassName: "bg-log-failed-surface",
    iconClassName: "text-log-failed-foreground",
  },
};

const FALLBACK_STYLE: EventStyle = {
  Icon: CircleDashed,
  surfaceClassName: "bg-log-info-surface",
  iconClassName: "text-log-info-foreground",
};

function styleFor(eventType: string): EventStyle {
  return EVENT_STYLES[eventType] ?? FALLBACK_STYLE;
}

/**
 * Short, verb-style label for the 9 known event types. Used as the
 * row's default title; the row's `subtitle` (or `item.message`)
 * carries the variable detail.
 *
 * Exported so other surfaces (e.g. the toast bridge) can stay in
 * sync without re-declaring the mapping.
 */
export function humanizeLogType(t: string): string {
  switch (t) {
    // Success rows (one per stage).
    case "uploaded":
      return "Uploaded";
    case "parsing":
      return "Parsed";
    case "chunking":
      return "Chunked";
    case "embedding":
      return "Embedded";
    // Failure rows (one per failed stage).
    case "parsing_failed":
      return "Parsing failed";
    case "chunking_failed":
      return "Chunking failed";
    case "embedding_failed":
      return "Embedding failed";
    case "processing_failed":
      return "Processing failed";
    // Operator / out-of-band.
    case "document_deleted":
      return "Document deleted";
    default:
      return t || "Activity";
  }
}

export function DocumentLogRow({ item, title, subtitle, className }: Props) {
  const { Icon, iconClassName, surfaceClassName } = styleFor(item.event_type);
  const resolvedSubtitle = subtitle ?? item.message ?? null;
  // Default title resolution:
  //   - If the caller passed an override, use it.
  //   - Else, prefer the humanized verb ("Parsed 24 pages",
  //     "Embedded 1,840 chunks") because it carries the variable
  //     detail (count, error) that the icon alone can't.
  //   - Else, fall back to the event_type string itself — only
  //     reachable for unknown future events not covered by
  //     `humanizeLogType`.
  const resolvedTitle =
    title ?? (humanizeLogType(item.event_type) || "Activity");

  return (
    <li
      className={cn(
        "flex items-start gap-3 rounded-md border border-border/60 bg-background/50 p-3",
        className,
      )}
    >
      {/* Colored icon chip — the unique hue per category is what lets
          the activity log read as a pipeline at a glance. The surface
          uses the low-chroma `--log-{cat}-surface` token; the icon
          uses the matching foreground token so the two stay coupled
          even if the surface gets overridden by a parent. */}
      <div
        className={cn(
          "mt-0.5 grid size-7 shrink-0 place-items-center rounded-md",
          surfaceClassName,
        )}
        aria-hidden
      >
        <Icon className={cn("size-3.5", iconClassName)} />
      </div>
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-foreground">
          {resolvedTitle}
        </p>
        {resolvedSubtitle ? (
          <p className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">
            {resolvedSubtitle}
          </p>
        ) : null}
      </div>
      <span
        className="shrink-0 text-[11px] tabular-nums text-muted-foreground"
        title={item.created_at}
      >
        {formatRelativeTime(item.created_at) ?? ""}
      </span>
    </li>
  );
}
