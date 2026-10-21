"use client";

import { useEffect, type ReactNode } from "react";

import { useWSStore } from "@/lib/realtime/ws-store";

/**
 * Mounts the global WebSocket connection at the root of the app.
 *
 * - Calls `wsStore.connect()` on mount.
 * - Calls `wsStore.disconnect()` on unmount (so closing the tab /
 *   navigating away tears the socket down cleanly).
 *
 * This component owns NO UI of its own. Connection status and
 * incoming-message handling live in their own hooks/components
 * (see `useWSToasts` and the documents activity log bridge).
 *
 * Place this inside `<QueryProvider>` in `app/layout.tsx`.
 */
export function WSProvider({ children }: { children: ReactNode }) {
  const connect = useWSStore((s) => s.connect);
  const disconnect = useWSStore((s) => s.disconnect);

  useEffect(() => {
    connect();
    return () => {
      disconnect();
    };
  }, [connect, disconnect]);

  return <>{children}</>;
}