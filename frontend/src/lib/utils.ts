import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

import { ApiError } from "@/lib/api/client"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * Coerce an unknown thrown value into a short, single-line string
 * suitable for a sonner toast description. Prefers the API's
 * `ApiError.message` (which is the backend's own `detail` field)
 * over a generic `Error.message`, and falls back to a caller-
 * supplied default for everything else.
 */
export function humanError(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

/**
 * Format an ISO timestamp as a short relative string ("3 min ago",
 * "2 hours ago", "yesterday", "Oct 4, 2026"). Returns `null` for
 * nullish / unparseable input so the caller can fall back to a
 * different label.
 *
 * The algorithm is intentionally identical to the in-row formatters
 * that used to live in `document-row.tsx` and `document-log-row.tsx`;
 * consolidating them here means the naive-ISO coercion below applies
 * to every consumer at once.
 *
 * Naive-ISO handling:
 *   The backend's SQLite path (and the dev DB fallback) round-trips
 *   datetimes without a tz suffix. The browser's `new Date(iso)`
 *   then treats the value as local time, which skews the diff by
 *   the user's UTC offset — e.g. a row stamped seconds ago reads as
 *   "6 hours ago" for a user ~UTC-6. We detect a naive ISO string
 *   (no `Z`, no `+`/`-` offset) and append `Z` so the browser
 *   interprets it as UTC.
 */
export function formatRelativeTime(iso: string | null | undefined): string | null {
  if (!iso) return null;
  // Force UTC for naive timestamps. A string is "tz-bearing" if it
  // ends with `Z`/`z` or a `±HH:MM` / `±HHMM` offset; anything else
  // (e.g. `2025-10-10T12:34:56.789012` from the SQLite path) is
  // treated as UTC.
  const hasTz = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(iso);
  const ts = new Date(hasTz ? iso : `${iso}Z`).getTime();
  if (Number.isNaN(ts)) return null;

  // Client-only formatter so SSR/CSR don't disagree on the exact
  // clock reading.
  const now = Date.now();
  const diffMs = now - ts;
  if (diffMs < 0) return "just now";

  const sec = Math.floor(diffMs / 1000);
  if (sec < 60) return "just now";
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} min ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} hour${hr === 1 ? "" : "s"} ago`;
  const day = Math.floor(hr / 24);
  if (day < 7) return `${day} day${day === 1 ? "" : "s"} ago`;
  return new Date(ts).toLocaleDateString();
}
