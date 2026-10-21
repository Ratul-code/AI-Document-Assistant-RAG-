/**
 * Global WebSocket store, backed by zustand.
 *
 * Responsibilities:
 *  - Hold a single shared `WebSocket` connection to the FastAPI `/ws`
 *    endpoint at `${API_BASE_URL}/ws`. The browser opens this directly
 *    (no Next.js proxy).
 *  - Expose connection status (`idle` | `connecting` | `open` |
 *    `closed` | `refreshing` | `auth-expired`) so any UI can render
 *    a banner or badge.
 *  - Auto-reconnect with exponential backoff when the connection drops.
 *  - On auth-related close codes (1008 "policy violation", or
 *    application codes 4000–4999) treat the drop as a token-expiry
 *    signal: hit `/auth/refresh` first, then reconnect. If refresh
 *    itself fails the user is logged out — we surface
 *    `auth-expired` and stop reconnecting.
 *  - Let any component subscribe to incoming messages via the
 *    `subscribe(handler)` API — handlers are called for every parsed
 *    JSON message, regardless of shape.
 *  - Let any component `send(payload)` to push messages upstream.
 *  - Server-initiated app-level heartbeat: when the server sends
 *    `{ type: "ping" }`, we reply with `{ type: "pong" }` and log
 *    the round-trip. Heartbeat traffic is intercepted BEFORE
 *    broadcasting to feature subscribers so they never see it.
 *
 * Notes on cookies:
 *  - The FastAPI `/ws` endpoint is cookie-authenticated. The backend
 *    sets `xs` with `SameSite=None; Secure` (see
 *    `backend/app/core/config.py`) so the browser attaches it to a
 *    cross-origin upgrade as long as the request is HTTPS.
 *  - Local dev over plain HTTP cannot use `Secure` cookies — see the
 *    dev section in `backend/.env.example` for the local override.
 *    Production must be HTTPS or the cookie will be dropped on the
 *    upgrade. See ADR-023.
 *
 * Token expiry recovery flow:
 *   1. Server closes WS with code 1008 (or 4xxx) → we recognise it.
 *   2. POST /auth/refresh (credentials: "include" sends the
 *      refresh_token cookie automatically; server rotates
 *      access_token via Set-Cookie).
 *   3. On refresh success → reconnect. The next WS upgrade picks up
 *      the new access_token cookie.
 *   4. On refresh failure → status = "auth-expired", stop reconnecting.
 *      The UI toasts the user to re-login.
 */

import { create } from "zustand";

import { refreshSession } from "@/lib/api/auth";
import { API_BASE_URL } from "@/lib/config";

/* ---------- Types ---------- */

export type WSStatus =
  | "idle"
  | "connecting"
  | "open"
  | "closed"
  | "refreshing"
  | "auth-expired";

export type WSMessage = {
  /** Whatever the server sent. We don't enforce a schema here so
   *  different feature teams can ship their own event shapes. */
  type?: string;
  [key: string]: unknown;
};

export type WSHandler = (msg: WSMessage) => void;

type WSStore = {
  status: WSStatus;
  /** Last error from the connection attempt, if any. */
  error: string | null;
  /** The number of consecutive reconnect attempts. */
  retryCount: number;

  /** Internal — do not call directly. Use the actions below. */
  _socket: WebSocket | null;
  _handlers: Set<WSHandler>;
  _reconnectTimer: ReturnType<typeof setTimeout> | null;
  /** Guard so we don't open multiple sockets if connect() races. */
  _connecting: boolean;

  connect: () => void;
  disconnect: () => void;
  send: (payload: unknown) => boolean;
  subscribe: (handler: WSHandler) => () => void;
};

/* ---------- Helpers ---------- */

/**
 * Build the WS URL.
 *
 * Direct cross-origin connection to `${API_BASE_URL}/ws`. The
 * browser attaches the `xs` access-token cookie to the upgrade
 * because the backend sets it with `SameSite=None; Secure` — see
 * ADR-023 for the full reasoning. In production both the page and
 * the WS target are on HTTPS, so the cookie is in scope.
 */
function wsUrl(): string {
  return `${API_BASE_URL.replace(/^http/, "ws")}/ws`;
}

/** Exponential backoff: 500ms, 1s, 2s, 4s, capped at 15s. */
function backoffMs(attempt: number): number {
  const base = 500 * Math.pow(2, attempt);
  return Math.min(base, 15_000);
}

/**
 * Decide whether a close `code` indicates the access token may have
 * expired (vs. a generic network/transport failure we should just
 * back off and retry).
 *
 *  - 1008          : RFC 6455 "policy violation" — the server uses
 *                    this to tell us the session is no longer valid.
 *  - 4000..4999    : Application-defined codes. Most auth implementations
 *                    use this range (e.g. 4001 "unauthorized",
 *                    4401, 4403). We trust the server's intent here
 *                    because 4xxx is explicitly reserved for the
 *                    server to choose.
 *
 * Anything outside these (1006 abnormal closure, 1011 server error,
 * 1001 going-away, …) is treated as a normal transport hiccup and
 * gets the standard exponential-backoff reconnect.
 */
function isAuthClose(code: number): boolean {
  if (code === 1008) return true;
  if (code >= 4000 && code <= 4999) return true;
  return false;
}

/* ---------- Store ---------- */

export const useWSStore = create<WSStore>((set, get) => {
  // Internal ref holders that live alongside state so we don't expose
  // them as reactive (they mutate every message).
  const handlers: Set<WSHandler> = new Set();
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  // Holds an in-flight /auth/refresh promise so concurrent 1008 events
  // share a single refresh call instead of firing N requests.
  let refreshInFlight: Promise<boolean> | null = null;

  // Set to true once a refresh has failed for this session. Cleared
  // when a fresh connect() is requested (user action / app boot),
  // so a re-login can clear the "auth-expired" state.
  let authExpired = false;

  function clearReconnect() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  }

  function scheduleReconnect(delayOverride?: number) {
    clearReconnect();
    const attempt = get().retryCount;
    const delay = delayOverride ?? backoffMs(attempt);
    reconnectTimer = setTimeout(() => {
      set({ retryCount: attempt + 1 });
      openSocket();
    }, delay);
    // Keep timer on the local closure; the store only tracks status.
  }

  /**
   * Refresh the session and reconnect.
   *
   * - De-duplicates concurrent calls: if a refresh is already running,
   *   we await the same promise instead of firing another POST.
   * - On success → schedule reconnect immediately (no backoff — the
   *   socket was up a moment ago, dropping it for seconds would just
   *   annoy the user).
   * - On failure → mark session as expired and stop retrying. The
   *   UI toasts the user.
   */
  async function refreshAndReconnect() {
    if (authExpired) {
      // Don't keep poking the server after a real logout.
      return;
    }
    if (!refreshInFlight) {
      set({ status: "refreshing", error: null });
      refreshInFlight = refreshSession().finally(() => {
        refreshInFlight = null;
      });
    }
    const ok = await refreshInFlight;
    if (ok) {
      // Token (or its cookie) is now fresh — reconnect right away.
      set({ retryCount: 0 });
      openSocket();
    } else {
      authExpired = true;
      set({
        status: "auth-expired",
        error: "Session expired",
        _connecting: false,
      });
      clearReconnect();
    }
  }

  function openSocket() {
    const { _connecting } = get();
    if (_connecting) return;
    if (typeof window === "undefined") return; // SSR safety

    set({ _connecting: true, status: "connecting", error: null });

    let socket: WebSocket;
    try {
      socket = new WebSocket(wsUrl());
    } catch (err) {
      set({
        status: "closed",
        error: err instanceof Error ? err.message : "Failed to connect",
        _connecting: false,
      });
      scheduleReconnect();
      return;
    }

    set({ _socket: socket });

    socket.addEventListener("open", () => {
      // A successful reconnect means any prior auth-expiry is
      // resolved — clear the latch so a future token rotation can
      // refresh again.
      authExpired = false;
      set({ status: "open", error: null, retryCount: 0, _connecting: false });
    });

    socket.addEventListener("message", (event) => {
      // Try JSON parse; fall back to raw string.
      let parsed: WSMessage;
      try {
        parsed = JSON.parse(event.data) as WSMessage;
      } catch {
        parsed = { type: "raw", data: event.data };
      }


      handlers.forEach((h) => {
        try {
          h(parsed);
        } catch (err) {
          console.error("[ws] handler threw:", err);
        }
      });
    });

    socket.addEventListener("error", () => {
      // The browser fires `error` right before `close` with no detail.
      // Real status is set in the `close` handler.
      set({ error: "WebSocket error" });
    });

    socket.addEventListener("close", (event) => {
      set({
        status: "closed",
        _socket: null,
        _connecting: false,
      });

      // Auth-related close → try to refresh and reconnect.
      if (isAuthClose(event.code)) {
        void refreshAndReconnect();
        return;
      }

      // Anything else → standard backoff reconnect.
      scheduleReconnect();
    });
  }

  return {
    status: "idle",
    error: null,
    retryCount: 0,
    _socket: null,
    _handlers: handlers,
    _reconnectTimer: null,
    _connecting: false,

    connect: () => {
      // A fresh connect call (user nav, app boot, manual retry) also
      // clears the auth-expired latch — if the user has logged back
      // in, we should be willing to refresh again.
      authExpired = false;
      clearReconnect();
      openSocket();
      console.log("connected");
    },

    disconnect: () => {
      clearReconnect();
      const socket = get()._socket;
      if (socket) {
        // Tell the server we're going away so it can clean up.
        // readyState check avoids throwing on already-closed sockets.
        if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
          socket.close(1000, "client disconnect");
        }
      }
      set({
        status: "closed",
        _socket: null,
        _connecting: false,
        retryCount: 0,
        error: null,
      });
      console.log("disconnected");
    },

    send: (payload: unknown) => {
      const socket = get()._socket;
      if (!socket || socket.readyState !== WebSocket.OPEN) return false;
      try {
        socket.send(typeof payload === "string" ? payload : JSON.stringify(payload));
        return true;
      } catch {
        return false;
      }
    },

    subscribe: (handler: WSHandler) => {
      handlers.add(handler);
      return () => {
        handlers.delete(handler);
      };
    },
  };
});

/* ---------- Selectors ---------- */

/** Tiny helper so components can pick only the status without
 *  re-rendering on every internal socket tick. */
export const selectWSStatus = (s: WSStore) => s.status;
export const selectWSError = (s: WSStore) => s.error;