"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";

/**
 * Wraps the app in a TanStack Query QueryClientProvider.
 *
 * The QueryClient is created once per mount via `useState(() => new QueryClient(...))`.
 * This is the standard pattern for App Router apps — using `useState` ensures
 * the client is stable across re-renders within the same browser session, and
 * is never shared between server renders (which would leak state between users).
 */
export function QueryProvider({ children }: { children: ReactNode; }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 60 * 1000,
            refetchOnWindowFocus: false,
          },
        },
      }),
  );

  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}