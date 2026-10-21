/**
 * Auth API calls → FastAPI backend.
 *
 * Token handling:
 * - FastAPI sets access_token + refresh_token as HttpOnly cookies on
 *   /login. The browser stores them automatically. We never read or
 *   write the tokens in JS — HttpOnly cookies are invisible to
 *   JavaScript anyway.
 * - The browser attaches them to every request that uses
 *   `credentials: "include"` (handled by apiClient).
 */

import { API_BASE_URL, API_PREFIX } from "@/lib/config";
import { apiClient } from "@/lib/api/client";
import type { LoginInput, SignupInput } from "@/lib/validations/auth";

export interface AuthUser {
  id: string;
  email: string;
  name: string;
}

/* ---------- Sign in ---------- */

export async function signIn(_input: LoginInput): Promise<AuthUser> {
  // /login sets the cookies. We then fetch the user via /auth/me.
  await apiClient("/auth/login", {
    method: "POST",
    body: _input,
  });
  return getMe();
}

/* ---------- Sign up ---------- */

export async function signUp(_input: SignupInput): Promise<void> {
  // /register creates the account but does NOT log the user in.
  // The signup form redirects to /login on success.
  await apiClient("/auth/register", {
    method: "POST",
    body: {
      name: _input.name,
      email: _input.email,
      password: _input.password,
    },
  });
}

/* ---------- Current user ---------- */

export async function getMe(): Promise<AuthUser> {
  return apiClient<AuthUser>("/auth/me", {
    method: "GET",
  });
}

/* ---------- Sign out ---------- */

export async function signOut(): Promise<void> {
  // /logout clears the cookies via Set-Cookie with max-age=0.
  await apiClient("/auth/logout", {
    method: "POST",
  });
}

/* ---------- Refresh ----------
 *
 * Standalone /auth/refresh call used by the WebSocket reconnect path.
 *
 * Why not use `apiClient`?
 * - `apiClient` already auto-refreshes + retries on 401 for normal REST
 *   requests, but for the WS path we want to know the *raw* outcome
 *   (did refresh succeed?) so we can decide whether to reconnect.
 *   Using apiClient here would either re-enter the refresh logic or
 *   re-throw on a 401 we'd already swallowed.
 * - This mirrors the server-side pattern in proxy.ts: send the
 *   refresh_token cookie, observe the new Set-Cookie header.
 *
 * Cookie strategy:
 * - The browser attaches the `refresh_token` HttpOnly cookie
 *   automatically because we use `credentials: "include"`. The
 *   server's response Set-Cookie overwrites `access_token` if it
 *   issues a new one. We don't read or write cookies here.
 */
export async function refreshSession(): Promise<boolean> {
  try {
    const res = await fetch(`${API_BASE_URL}${API_PREFIX}/auth/refresh`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
    });
    return res.ok;
  } catch {
    return false;
  }
}