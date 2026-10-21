# Frontend Architecture

Next.js 16 (App Router) + React 19 + TypeScript client for the AI Document Analyser. Talks to the FastAPI backend over REST (`/api/v1`) and a single WebSocket (`/ws`). Owns no backend resources directly — all DB, storage, vector, and LLM access lives behind the backend.

> **Next.js 16 note** — this is not the Next.js from training data. APIs, conventions, and file structure may differ. Before writing code, consult `node_modules/next/dist/docs/` and the existing `frontend/AGENTS.md` rule. Concrete examples in this repo: middleware is at `src/proxy.ts` (renamed from `middleware.ts`), `eslint.config.mjs` uses the flat-config API, typed routes are wired via `.next/dev/types/routes.d.ts`.

## Stack

- **Next.js 16.2** (App Router) + **React 19.2** + **TypeScript 5**
- **TanStack Query 5** (server state) + **Zustand 5** (WebSocket client state)
- **React Hook Form** + **Zod 4** (forms + validation)
- **Tailwind CSS v4** (CSS-first config in `globals.css`, no `tailwind.config.ts`)
- **shadcn/ui** (style: `radix-vega`) on **Radix UI** primitives
- **Sonner** (toasts), **Lucide React** (icons)
- **clsx** + **tailwind-merge** via `cn()`

No test framework, no analytics, no i18n, no persistent client state — by design.

## Infrastructure / External

- **Backend API**: FastAPI at `NEXT_PUBLIC_API_BASE_URL` (default `http://localhost:8000`), prefix `/api/v1`. Single env var.
- **WebSocket**: direct cross-origin — `${API_BASE_URL}/ws`. The browser opens the upgrade to the backend host; the `xs` cookie travels because the backend sets it with `SameSite=None; Secure` (see `backend/app/core/config.py`). HTTPS is required in production; local dev uses the `COOKIE_SECURE=false; COOKIE_SAMESITE=lax` override described in `backend/.env.example`. ADR-023.
- **Auth cookies**: `access_token` and `refresh_token` are HttpOnly cookies set by the backend. The frontend never reads or writes them; the browser attaches them via `credentials: "include"` (REST) and via the WebSocket upgrade (cross-origin, gated by `SameSite=None; Secure`).
- No third-party services, no CDN config, no analytics SDK.

## Layout

```
frontend/
├── src/
│   ├── app/                       # App Router
│   │   ├── layout.tsx             # Root layout: providers + Toaster
│   │   ├── page.tsx               # / → redirect to /chat
│   │   ├── globals.css            # Tailwind v4 + theme variables
│   │   ├── (app)/                 # Protected route group
│   │   │   ├── layout.tsx         # Server: fetchCurrentUser → Sidebar + main
│   │   │   ├── chat/page.tsx      # Stub UI (chat not implemented yet)
│   │   │   └── documents/page.tsx # Full documents workspace
│   │   └── (auth)/                # Public route group
│   │       ├── layout.tsx         # Two-column brand + form shell
│   │       ├── login/page.tsx
│   │       └── signup/page.tsx
│   ├── components/
│   │   ├── ui/                    # shadcn primitives: button, card, dialog, input, label, sonner
│   │   ├── auth/                  # LoginForm, SignupForm (RHF + Zod)
│   │   ├── app/                   # Sidebar + chrome for protected routes
│   │   └── documents/             # Documents page, upload dialog, activity panel, global feed
│   ├── lib/
│   │   ├── config.ts              # API_BASE_URL, API_PREFIX (single source of truth)
│   │   ├── utils.ts               # cn() helper
│   │   ├── api/                   # REST client + per-domain helpers (client.ts, auth.ts, documents.ts)
│   │   ├── auth/server.ts         # Server-side fetchCurrentUser (uses React cache, headers, cookies)
│   │   ├── realtime/              # ws-store (Zustand), hooks, bridges
│   │   └── validations/           # Zod schemas (source of truth for form types)
│   ├── providers/
│   │   ├── query-provider.tsx     # TanStack Query (staleTime 60s, no refetchOnWindowFocus)
│   │   ├── ws-provider.tsx        # Opens/closes ws-store on mount/unmount
│   │   └── realtime-bridge.tsx    # Renders null; mounts WS-to-toast + status bridge hooks
│   ├── types/auth.ts              # Re-exports Zod-inferred auth types
│   └── proxy.ts                   # Next.js 16 middleware (renamed from middleware.ts)
├── public/                        # Static SVGs (boilerplate)
└── components.json                # shadcn config
```

Path alias: `@/* → ./src/*` (set in `tsconfig.json`).

## Provider Stack (mounted in `app/layout.tsx`)

```
html → body
  QueryProvider          (TanStack Query)
    WSProvider           (opens ws-store on mount)
      RealtimeBridge     (mounts WS-to-toast + status-bridge hooks; renders null)
      {children}
      Toaster            (Sonner, richColors, closeButton, bottom-right)
```

## Routing

- **Route groups**: `(app)` (protected, sidebar shell) and `(auth)` (public, brand shell). Group names are stripped from URLs.
- **No dynamic routes**, no parallel routes, no intercepting routes, no Next API routes, no server actions.
- **Root `/`**: server-side `redirect("/chat")`.
- **Public paths** (allow-listed in `proxy.ts`): `/login`, `/signup`, `/forgot-password`.
- **Protected paths**: all others. Middleware verifies via `${API}/auth/me`; on 401, attempts `${API}/auth/refresh` and forwards new `Set-Cookie` headers verbatim. On failure: `redirect("/login?next=<original>")`.

## Data Flow

### REST

- Single fetch wrapper `apiClient<T>(path, init?, options?)` in `lib/api/client.ts`.
- Always `credentials: "include"`, JSON body, JSON response. 204 → resolves to `undefined`.
- **Auto-refresh on 401**: if response is 401 and we haven't retried, calls `/auth/refresh` once with `credentials: "include"`, then replays the original request. Throws `ApiError` on final failure.
- Domain helpers in `lib/api/<domain>.ts` build on `apiClient`. Per-domain types live next to the helpers (e.g. `Document`, `DocumentStatus` in `documents.ts`).
- Cross-origin: `:3000` (Next) ↔ `:8000` (FastAPI) requires CORS with explicit origin and `Access-Control-Allow-Credentials: true` on the backend.

### TanStack Query

- `QueryClient` created in `useState` so it's stable per client but never shared across server renders.
- Query keys via the `documentKeys` factory (`all → lists / list(params) / logs / log(documentId, params)`).
- Mutations invalidate by prefix to refresh list views. WS bridges call `invalidateQueries` / `setQueriesData` directly on `document.status` and `document.log` events.

### WebSocket

- `useWSStore` (Zustand singleton) owns one socket. URL: `${API_BASE_URL.replace(/^http/, "ws")}/ws` — direct cross-origin to the FastAPI host. The browser attaches the `xs` access-token cookie because the backend sets it with `SameSite=None; Secure`. ADR-023.
- Auth: relies on the browser attaching the access-token HttpOnly cookie to the cross-origin WS upgrade request. No token query param today.
- Reconnect: exponential backoff 500ms → 15s cap.
- Auth-failure close (`1008` or `4000..4999`): `status = "refreshing"` → `refreshSession()` → reopen. On failure: `status = "auth-expired"`, stop reconnecting.
- Heartbeat: server-pinged, client silence >5s → server closes with `1011`. Client pong not implemented yet.
- All parsed messages are broadcast to every handler registered via `subscribe(handler)`. The store does not enforce a schema.

## State Layers

- **Server state**: TanStack Query (queries + mutations).
- **Real-time state**: a single Zustand store (`useWSStore`) for the WebSocket lifecycle and message fan-out.
- **No global user store**. The user is fetched server-side in `(app)/layout.tsx` and passed as a prop to `<Sidebar user={...} />`.
- **No persisted client state** (no `persist` middleware).

## Forms

- RHF + Zod via `@hookform/resolvers/zod`. Schemas live in `lib/validations/`, types are inferred with `z.infer` (re-exported from `types/auth.ts`).
- `LoginForm`, `SignupForm` follow the same shape. Server errors surface via `ApiError.fieldErrors`.
- The upload dialog is **not** RHF — it's local `useState` with manual validation. Form choice is intentional and scoped.

## UI

- shadcn primitives (`button`, `card`, `dialog`, `input`, `label`, `sonner`) live in `components/ui/`. Add more via the shadcn CLI (`components.json` is the manifest).
- Tailwind v4 with CSS-first config in `app/globals.css`. Theme variables (`--color-*`, `--font-*`, `--sidebar-*`) declared via `@theme inline`; `:root` and `.dark` palettes in `oklch()`.
- Sonner `Toaster` is mounted once in the root layout. Toasts come from `useWSToasts` (global WS-to-toast) and `useUploadDocument` (upload errors).
- Lucide React for all icons.

## Real-Time UX

- `useWSToasts` (global) maps `document.status` → Sonner toast (`uploaded` / `learning` / `ready` / `failed`).
- `useDocumentStatusBridge` (global) invalidates `documentKeys.lists()` on every `document.status`.
- `useDocumentLogBridge` (page-scoped, mounted only inside `DocumentActivityPanel`) prepends new log items into paginated caches via `setQueriesData` with a prefix match. Replay-safe (de-dupe by `id`).
- `useWSStatusToasts` (global) silently redirects to `/login` on transition into `auth-expired` (no toast).

## Auth on the Frontend

- **No token storage in JS**. Cookies are HttpOnly. The frontend never sets an `Authorization` header.
- **Server-side refresh** is owned by `proxy.ts`: it forwards cookies to `/auth/refresh`, then copies the raw `Set-Cookie` headers to the browser response and rewrites `req.headers.cookie` for downstream RSC code.
- **Client-side fallback** in `apiClient` covers client-side mutations and the WS reconnect path.
- Login → cookies set by FastAPI → `useWSStore.connect()` → navigate. Signup → no auto-login → land on `/login`. Logout → cookies cleared by FastAPI → `useWSStore.disconnect()` → `/login`.

## Document Upload Flow

1. `UploadDocumentDialog` collects title + file (≤ 25 MB, types: `.pdf .docx .txt .md`).
2. `POST /documents/upload-intent` → returns `{ document_id, upload_url, storage_path, status }`. Dialog closes on success.
3. `PUT` raw `File` to the signed URL with `credentials: "omit"` (signed URL authenticates itself; sending our cookies to a third-party host would leak them). On failure, `POST /documents/{id}/upload-failed` flips the doc to `FAILED`.
4. `POST /documents/{id}/confirm` enqueues the Celery job.
5. Status transitions appear via WebSocket events; the list revalidates on every `document.status`. Failed rows are filtered from the visible list at render time but stay in the cache so badges/toasts still see them.

## Conventions for New Code

- New route: add `page.tsx` under the right group. Server components by default; mark `"use client"` only when needed.
- New domain in the UI: add a folder under `components/<domain>/`, REST helpers under `lib/api/<domain>.ts` with a query-key factory, then a hook layer in `lib/realtime/` if WS events apply.
- New WS event: add a typed handler hook under `lib/realtime/`, register it in `RealtimeBridge` (global) or in the page-level component that needs it.
- New shadcn primitive: use the CLI; don't hand-roll Radix wrappers.
- Preserve the Next.js 16 rules: middleware goes in `src/proxy.ts` (function name `proxy`), use `next-env.d.ts` and the typed-routes convention, follow flat ESLint config.
- Keep `cn()` in `lib/utils.ts`; keep `API_BASE_URL` and `API_PREFIX` in `lib/config.ts` only.
- Never read or set cookies from client code; let the browser carry them.

## Verify

- `npm run dev` — local dev server.
- `npm run build` — production build (the strongest check; catches type + route errors).
- `npm run lint` — ESLint (flat config; `eslint-config-next/core-web-vitals` + `typescript`).
- `tsc` against `tsconfig.json` — `strict: true`; no `noUncheckedIndexedAccess` or `exactOptionalPropertyTypes` yet.
