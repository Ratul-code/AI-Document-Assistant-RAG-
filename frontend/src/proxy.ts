/**
 * Next.js middleware — runs on every matched request before the page
 * is rendered. This is where route protection lives.
 *
 * Token strategy:
 * - FastAPI sets `access_token` and `refresh_token` as HttpOnly cookies
 *   on /auth/login. The browser stores them and sends them back on
 *   every request to the same site automatically.
 * - The middleware is a server-to-server caller (Edge runtime), so the
 *   browser's automatic cookie behavior does NOT apply here. Instead
 *   we forward the incoming `Cookie` header verbatim to FastAPI when
 *   calling /auth/check and /auth/refresh. FastAPI sees the cookies and
 *   returns the appropriate response.
 *   - HttpOnly only blocks JS from reading document.cookie. It does NOT
 *     prevent the browser from sending the cookie in HTTP requests,
 *     including the `Cookie` header that reaches Next.js.
 * - The frontend JS never reads or writes the tokens. It only uses
 *   `credentials: "include"` on its fetches so the browser attaches
 *   the cookies automatically.
 *
 * Cookie ownership:
 * - FastAPI owns the cookie contract (HttpOnly, SameSite, Path, Max-Age,
 *   Secure, Domain, etc.). Next.js never inspects or rewrites attributes.
 * - On /auth/refresh, FastAPI returns new Set-Cookie headers. We pass them
 *   through verbatim to the response so the browser stores them exactly
 *   as FastAPI defined them.
 * - For the same request to also see the rotated values (so downstream
 *   RSC/layout code can read them via `headers().get("cookie")` /
 *   `(await cookies()).get(...)`), we rebuild the request's Cookie
 *   header from the name=value pairs of FastAPI's Set-Cookie lines.
 *   Attributes are intentionally dropped here — only the value matters
 *   for the request side.
 *
 * Why /auth/check and not /auth/me:
 * - /auth/me fetches the full user row from the DB and is rate-limited
 *   at 60/min per user. The proxy runs on every matched request
 *   (page nav + RSC fetches + prefetches + HMR), which trips the
 *   rate limit. The 429s get treated as "not authenticated" and
 *   bounce users to /login even when their session is fine.
 * - /auth/check just verifies the JWT signature (no DB hit) and is
 *   not rate-limited. It's a pure yes/no probe.
 *
 * Caching:
 * - Even with /auth/check being cheap, the proxy matcher triggers
 *   for every RSC/prefetch request, so we'd still make ~10-20
 *   round-trips per page load. We cache the result keyed by the
 *   access_token value for a few seconds. Different cookies (e.g.
 *   after a /refresh rotation) get a fresh check immediately
 *   because the cache key is the token, not the user.
 * - The cache lives in module scope, which in Next.js's dev mode
 *   survives HMR but is reset on full server restart. In prod it
 *   lives for the lifetime of the Node process. That's fine for a
 *   "is this token still valid" check — false positives (saying
 *   authed when the token expired within the cache window) are
 *   bounded by CACHE_TTL_MS.
 *
 * Flow:
 *   1. Public paths (login, signup, etc.) are always allowed.
 *   2. For protected paths, check the session. If 200, allow.
 *   3. If 401, try /auth/refresh with the same forwarded cookies.
 *      If refresh succeeds, allow (forward the new Set-Cookie headers
 *      to the browser and rewrite the request's Cookie header).
 *   4. If refresh fails, redirect to /login.
 *
 * Edge runtime: no Node APIs, only the standard fetch API.
 */

import { NextResponse, type NextRequest } from "next/server";

// This file runs SERVER-SIDE in the Next.js container. It must use
// the URL that's reachable FROM the container, which is the in-network
// Docker hostname (`api`). `NEXT_PUBLIC_API_BASE_URL` is for the
// browser bundle and points at `http://localhost:8000` in dev so the
// user's browser (running on the host) can reach the api via the
// host port mapping — that URL is meaningless from inside the
// frontend container because nothing is listening on its localhost:8000.
//
// In dev compose, we set `API_INTERNAL_BASE_URL=http://api:8000` in
// the service env. In prod, both URLs are the same (the frontend and
// api are reached over the same network) and the fallback below
// covers prod / missing-env-var cases.
const API_BASE_URL =
  process.env.API_INTERNAL_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://localhost:8000";
const API_PREFIX = "/api/v1";

const PUBLIC_PATHS = new Set<string>([
  "/login",
  "/signup",
  "/forgot-password",
]);

function isPublicPath(pathname: string): boolean {
  if (PUBLIC_PATHS.has(pathname)) return true;
  if (pathname.startsWith("/_next")) return true;
  if (pathname.startsWith("/api")) return true; // Next API routes, not FastAPI
  if (pathname.includes(".")) return true; // static assets
  return false;
}

// --- Session check (cached) -----------------------------------------------

/** How long a positive session check is cached. Short enough that
 *  an expired token gets caught within one page reload, long enough
 *  to coalesce the burst of RSC/prefetch requests a single page
 *  navigation generates. */
const CACHE_TTL_MS = 5_000;

/** Cache key = access_token value. Cache value = { ok, at } where
 *  `at` is the timestamp of the check. We don't cache negatives
 *  here — a failed check is rare (mostly only on first visit when
 *  the access token has actually expired and refresh is needed) and
 *  we want the next request to try again right away. */
const sessionCache = new Map<string, { ok: true; at: number; }>();

/** Extract just the access-token value from a Cookie header. We only
 *  need the JWT itself for the cache key; the rest of the cookie
 *  header is forwarded verbatim on the actual fetch. */
function extractAccessToken(cookieHeader: string | null): string | null {
  if (!cookieHeader) return null;
  // Cookie names are configurable in FastAPI settings. We don't
  // know the name here (the api is the source of truth for cookie
  // names), so we just look for any "=eyJ..." pair (JWTs start
  // with `eyJ` base64-url encoded). That's a deliberately loose
  // match — false positives in the cache key are fine, we just
  // over-fetch in that case.
  const match = cookieHeader.match(/=([A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)/);
  return match ? match[1] : null;
}

async function verifySession(cookieHeader: string | null): Promise<boolean> {
  if (!cookieHeader) return false;

  // Cache hit?
  const token = extractAccessToken(cookieHeader);
  if (token) {
    const cached = sessionCache.get(token);
    if (cached && Date.now() - cached.at < CACHE_TTL_MS) {
      return true;
    }
  }

  try {
    const res = await fetch(`${API_BASE_URL}${API_PREFIX}/auth/check`, {
      method: "GET",
      headers: { Cookie: cookieHeader },
      cache: "no-store",
    });
    if (res.ok && token) {
      sessionCache.set(token, { ok: true, at: Date.now() });
    }
    return res.ok;
  } catch {
    return false;
  }
}

async function tryRefreshSession(
  cookieHeader: string | null,
): Promise<string[] | null> {
  if (!cookieHeader) return null;

  // After a successful refresh, the access_token value changes.
  // Invalidate the old token's cache entry so the next request
  // re-verifies with the new value. (The new token gets cached
  // on the subsequent /auth/check.)
  const oldToken = extractAccessToken(cookieHeader);
  if (oldToken) sessionCache.delete(oldToken);

  try {
    const res = await fetch(`${API_BASE_URL}${API_PREFIX}/auth/refresh`, {
      method: "POST",
      headers: {
        Cookie: cookieHeader,
        "Content-Type": "application/json",
      },
      cache: "no-store",
    });
    if (!res.ok) return null;
    // Raw Set-Cookie lines, untouched. The caller forwards them to the
    // browser verbatim and rebuilds the request's Cookie header from
    // their name=value pairs.
    return res.headers.getSetCookie?.() ?? [];
  } catch {
    return null;
  }
}

/**
 * Extract just the `name=value` pair from a Set-Cookie line. Used to
 * rebuild the request's Cookie header so downstream RSC/layout code
 * in the same render sees the rotated values via headers().get("cookie").
 * We intentionally ignore everything after the first `;` — attributes
 * (HttpOnly, SameSite, Path, etc.) are meaningless on the request side.
 */
function nameValueFromSetCookie(line: string): string | null {
  const pair = line.split(";")[0]?.trim();
  if (!pair) return null;
  const eq = pair.indexOf("=");
  if (eq <= 0) return null;
  return pair;
}

/**
 * Forward FastAPI's raw Set-Cookie headers to the response verbatim
 * (browser sees them exactly as FastAPI set them) AND rebuild the
 * request's Cookie header from the name=value pairs so downstream
 * RSC/layout code on the same request sees the rotated values.
 */
function applyRefreshedSession(
  req: NextRequest,
  res: NextResponse,
  setCookieHeaders: string[],
) {
  // 1. Verbatim Set-Cookie to the browser.
  for (const sc of setCookieHeaders) {
    res.headers.append("set-cookie", sc);
  }

  // 2. Rebuild the request's Cookie header from name=value pairs.
  //    Replace, don't append: if FastAPI rotated a token, the new
  //    value supersedes the stale one.
  const incoming = req.headers.get("cookie") ?? "";
  const incomingPairs = incoming
    .split(";")
    .map((p) => p.trim())
    .filter((p) => p.length > 0 && p.includes("="));

  const rotatedNames = new Set<string>();
  const rotatedPairs: string[] = [];
  for (const sc of setCookieHeaders) {
    const nv = nameValueFromSetCookie(sc);
    if (!nv) continue;
    const name = nv.split("=")[0];
    rotatedPairs.push(nv);
    rotatedNames.add(name);
  }

  // Keep incoming cookies that weren't rotated.
  const kept = incomingPairs.filter((p) => {
    const name = p.split("=")[0];
    return !rotatedNames.has(name);
  });

  const merged = [...kept, ...rotatedPairs].join("; ");
  req.headers.set("cookie", merged);
}

export async function proxy(req: NextRequest) {
  const { pathname } = req.nextUrl;
  const cookieHeader = req.headers.get("cookie");

  // Public paths — let them through, but redirect logged-in users away.
  if (isPublicPath(pathname)) {
    if (
      cookieHeader && await verifySession(cookieHeader) && (pathname === "/login" || pathname === "/signup")
    ) {
      // Already authenticated — bounce to the chat workspace.
      console.log("Already authss");
      return NextResponse.redirect(new URL("/chat", req.url));
    }
    return NextResponse.next();
  }

  // Protected path — verify session with FastAPI.
  if (await verifySession(cookieHeader)) {
    console.log("already verifiedssss");
    return NextResponse.next();
  }

  // Try to refresh using the refresh_token cookie.
  const setCookies = await tryRefreshSession(cookieHeader);
  if (setCookies && setCookies.length > 0) {
    // Apply refreshed session: Set-Cookie to the browser verbatim, and
    // rebuild the request's Cookie header so downstream RSC/layout code
    // on this same request sees the rotated values.
    const res = NextResponse.next({
      request: { headers: req.headers },
    });
    applyRefreshedSession(req, res, setCookies);
    return res;
  }

  // Not authenticated — bounce to login, preserving intended destination.
  const loginUrl = new URL("/login", req.url);
  loginUrl.searchParams.set("next", pathname);
  return NextResponse.redirect(loginUrl);
}

export const config = {
  // Run on everything except Next internals, static files, and our own /api routes.
  matcher: ["/((?!_next/static|_next/image|favicon.ico|.*\\..*).*)"],
};
