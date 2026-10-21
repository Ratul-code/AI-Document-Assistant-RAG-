"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef } from "react";

import { useWSStore } from "./ws-store";

/**
 * Bridges the WebSocket store's lifecycle status to silent recovery
 * actions. Today:
 *
 *   - `auth-expired` transition → push the user to `/login`. No
 *     toast: per product spec, the user only sees sonners that
 *     explicit feature code asks for (doc status changes, etc.).
 *     The URL change is enough context.
 *
 * Fires only on the TRANSITION into `auth-expired` — not on every
 * render while we're stuck there. The store's internal
 * `authExpired` latch keeps the source status sticky, and the
 * `lastStatus` ref dedupes further in case the store ever re-emits.
 */
export function useWSStatusToasts() {
  const status = useWSStore((s) => s.status);
  const lastStatus = useRef(status);
  const router = useRouter();

  useEffect(() => {
    const prev = lastStatus.current;
    lastStatus.current = status;

    if (prev === status) return;
    if (status !== "auth-expired") return;

    router.push("/login");
  }, [status, router]);
}