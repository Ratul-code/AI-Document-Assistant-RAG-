/**
 * Centralized fetch wrapper for the FastAPI backend.
 *
 * Token strategy:
 * - FastAPI sets `access_token` and `refresh_token` as HttpOnly cookies
 *   on the login response.
 * - The browser stores them automatically and attaches them to all
 *   same-origin requests (because we use `credentials: "include"`).
 * - The frontend never reads or writes the tokens — it can't anyway,
 *   because HttpOnly cookies are invisible to JavaScript.
 * - We rely on the browser to send the cookies. We do NOT inject an
 *   Authorization header manually.
 *
 * Auto-refresh:
 * - On 401, we call /auth/refresh. The browser sends the refresh_token
 *   cookie automatically; the server replies with a new Set-Cookie for
 *   access_token, and we retry the original request once.
 *
 * Cross-origin note:
 * - The frontend and FastAPI are on different origins (localhost:3000
 *   vs localhost:8000). For the browser to send/receive cookies across
 *   origins, BOTH sides need: `credentials: "include"` on the fetch,
 *   and the server's `Access-Control-Allow-Credentials: true` header
 *   plus an explicit (not `*`) Allow-Origin. FastAPI CORS must be set
 *   accordingly.
 */

import { API_BASE_URL, API_PREFIX } from "@/lib/config";

export class ApiError extends Error {
  status: number;
  fieldErrors?: Record<string, string[] | undefined>;

  constructor(
    message: string,
    status: number,
    fieldErrors?: Record<string, string[] | undefined>,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.fieldErrors = fieldErrors;
  }
}

type RequestInitWithJson = Omit<RequestInit, "body"> & {
  body?: unknown;
};

type ApiClientOptions = { retried?: boolean };

export async function apiClient<T>(
  path: string,
  init: RequestInitWithJson = {},
  options: ApiClientOptions = {},
): Promise<T> {
  const { body, headers, ...rest } = init;

  const doFetch = () =>
    fetch(`${API_BASE_URL}${API_PREFIX}${path}`, {
      ...rest,
      credentials: "include", // browser attaches cookies automatically
      headers: {
        "Content-Type": "application/json",
        ...headers,
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });

  let res = await doFetch();

  // Auto-refresh on 401 — try once, then retry the original request.
  // The browser sends the refresh_token cookie with /auth/refresh
  // automatically because of credentials: "include".
  if (res.status === 401 && !options.retried) {
    const refreshed = await tryRefresh();
    if (refreshed) {
      res = await doFetch();
    }
  }

  if (res.status === 204) {
    return undefined as T;
  }

  const data = await res.json().catch(() => null);

  if (!res.ok) {
    throw new ApiError(
      data?.detail ?? data?.message ?? `Request failed (${res.status})`,
      res.status,
      data?.errors,
    );
  }

  return data as T;
}

async function tryRefresh(): Promise<boolean> {
  try {
    const res = await fetch(`${API_BASE_URL}${API_PREFIX}/auth/refresh`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
    });
    return res.ok;
  } catch {
    return false;
  }
}

/**
 * Same auth shape as `apiClient` (auto-refresh-on-401 + retry once) for
 * callers that need to read the raw `Response` instead of JSON — used
 * today by the document download / view helpers, which stream binary
 * bodies that `apiClient`'s `res.json()` would corrupt.
 *
 * Returns the response. Callers check `res.ok` themselves; this helper
 * only adds the refresh-and-retry loop, not error parsing.
 */
export async function rawClient(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  const doFetch = () =>
    fetch(`${API_BASE_URL}${API_PREFIX}${path}`, {
      ...init,
      credentials: "include",
    });
  let res = await doFetch();
  if (res.status === 401) {
    if (await tryRefresh()) res = await doFetch();
  }
  return res;
}