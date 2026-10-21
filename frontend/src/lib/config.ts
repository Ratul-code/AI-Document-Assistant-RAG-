/**
 * App-wide config. Reads from environment variables.
 * Public env vars (NEXT_PUBLIC_*) are exposed to the browser.
 */

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/**
 * Base URL for fetches that run on the Next.js SERVER (RSC layouts,
 * route handlers, server components). Do NOT use `API_BASE_URL` for
 * these — that constant is `NEXT_PUBLIC_*`, which Next.js inlines at
 * BUILD time and which is meant for the BROWSER.
 *
 * Why the distinction matters:
 * - In the browser, the correct address is whatever the host browser
 *   can reach — in local container dev that's `http://localhost:8000`
 *   (the api service's published host port).
 * - On the Next.js server (running inside its own container), that same
 *   `localhost:8000` points back at the FRONTEND's own port. Nothing
 *   listens there, so the fetch fails with ECONNREFUSED. The server
 *   needs the in-network address instead — `http://api:8000`, exposed
 *   as `API_INTERNAL_BASE_URL` in compose.
 *
 * `API_INTERNAL_BASE_URL` is a plain (non-NEXT_PUBLIC_) var, so it is
 * only readable server-side. That's correct: the browser has no business
 * using the Docker-network hostname. Next.js replaces
 * `process.env.API_INTERNAL_BASE_URL` with `undefined` in client
 * bundles, which makes the `typeof window` guard below the belt-and-
 * braces safeguard for this module.
 *
 * This mirrors what `src/proxy.ts` already does successfully — the
 * middleware reads `API_INTERNAL_BASE_URL` first for exactly this
 * reason. This constant applies the same rule to the rest of the
 * server-side code so the two halves agree on the backend address.
 *
 * Order of preference:
 *   1. API_INTERNAL_BASE_URL        (Docker/compose: http://api:8000)
 *   2. NEXT_PUBLIC_API_BASE_URL     (bare `next dev` on the host: both
 *                                    sides share localhost, so the
 *                                    browser-facing URL is also correct)
 *   3. http://localhost:8000        (last-resort local default)
 */
export const SERVER_API_BASE_URL =
  (typeof window === "undefined"
    ? process.env.API_INTERNAL_BASE_URL
    : undefined) ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://localhost:8000";

export const API_PREFIX = "/api/v1";