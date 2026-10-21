/**
 * Server-side auth helpers used by the (app) protected layout.
 *
 * Token strategy (matches proxy.ts):
 * - FastAPI owns the cookie contract: `access_token` and `refresh_token`
 *   are set as HttpOnly cookies on /auth/login. The browser stores them
 *   and sends them back on every request to the same site automatically.
 * - The middleware in src/proxy.ts is the single source of truth for
 *   session lifecycle: it verifies /auth/me, falls back to /auth/refresh
 *   when the access_token is stale, and rotates the cookies for both
 *   the request (so downstream RSC/layout code sees them via
 *   `headers().get("cookie")`) and the response (so the browser's jar
 *   gets the new values verbatim from FastAPI).
 * - By the time (app)/layout.tsx calls fetchCurrentUser(), the cookies
 *   are already in the right state — there is no need for this layer
 *   to refresh on its own. If fetchCurrentUser() somehow throws, the
 *   caller treats it as an auth failure and redirects to /login.
 *
 * Sign-out lives on the client (see lib/api/auth.ts → signOut()).
 * The sidebar calls signOut() with credentials: "include" so the
 * browser automatically applies FastAPI's clearing Set-Cookie headers.
 *
 * Cookie ownership:
 * - Next.js never inspects or rewrites cookie attributes here. FastAPI
 *   sets them, FastAPI defines them. We just read the values when
 *   needed to forward to FastAPI in the Cookie header.
 */

import { cache } from "react";
import { cookies, headers } from "next/headers";

import { SERVER_API_BASE_URL, API_PREFIX } from "@/lib/config";
import type { Conversation } from "@/lib/api/conversations";

export type AuthUser = {
  id: string;
  email: string;
  name: string;
};

/**
 * Build a Cookie header string from the request's cookies. Used by
 * server-side fetches to FastAPI so the backend sees the same
 * cookies the browser sent (after any rotation by the proxy).
 *
 * Prefers `headers().get("cookie")` since the proxy rotates via
 * `req.headers.set("cookie", ...)` which is what that returns;
 * falls back to enumerating cookies() for safety.
 */
async function buildCookieHeader(): Promise<string | null> {
  const fromHeaders = (await headers()).get("cookie");
  if (fromHeaders) return fromHeaders;

  const store = await cookies();
  const pairs: string[] = [];
  for (const { name, value } of store.getAll()) {
    pairs.push(`${name}=${value}`);
  }
  return pairs.length > 0 ? pairs.join("; ") : null;
}

/**
 * Fetch the current user from FastAPI by forwarding the request's
 * cookies.
 *
 * This function MUST always return a user — the proxy guarantees that
 * by the time we reach here, the session is valid. If it throws
 * instead, that's a bug or a bypass: the caller should treat it as
 * "not authenticated" and redirect to /login.
 *
 * Wrapped in React `cache()` so that within a single request, repeated
 * calls (e.g. dev-mode double render) only fire one /auth/me.
 */
export const fetchCurrentUser = cache(async (): Promise<AuthUser> => {
  const cookieHeader = await buildCookieHeader();

  const res = await fetch(`${SERVER_API_BASE_URL}${API_PREFIX}/auth/me`, {
    headers: { Cookie: cookieHeader ?? "" },
    cache: "no-store",
  });

  if (!res.ok) {
    throw new Error(
      `fetchCurrentUser: /auth/me returned ${res.status} (proxy should have handled refresh)`,
    );
  }

  return (await res.json()) as AuthUser;
});

/**
 * Fetch a single conversation server-side by forwarding the request's
 * cookies. Returns `null` on 404 (chat deleted or owned by another
 * user — backend intentionally uses 404 to avoid leaking existence).
 *
 * Wrapped in React `cache()` so repeated renders (RSC dev double
 * renders, navigation, etc.) only hit the backend once per request.
 *
 * Note: this is intentionally NOT typed with `cache()`'s auto-arg
 * hashing the way `fetchCurrentUser` is, because the `conversationId`
 * parameter changes per call. Calling `fetchConversation(id)` twice
 * with the same id within a request is the only dedup window — and
 * that's exactly what we want.
 */
export const fetchConversation = cache(
  async (conversationId: string): Promise<Conversation | null> => {
    const cookieHeader = await buildCookieHeader();

    const res = await fetch(
      `${SERVER_API_BASE_URL}${API_PREFIX}/conversations/${conversationId}`,
      {
        headers: { Cookie: cookieHeader ?? "" },
        cache: "no-store",
      },
    );

    if (res.status === 404) return null;
    if (!res.ok) {
      throw new Error(
        `fetchConversation: /conversations/${conversationId} returned ${res.status}`,
      );
    }

    return (await res.json()) as Conversation;
  },
);