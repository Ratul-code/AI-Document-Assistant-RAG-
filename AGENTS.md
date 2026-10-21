# AGENTS.md

Global instructions for AI coding agents working on this repository.

## Project Overview

**AI Document Analyser** — a full-stack application that lets users upload documents, parse and embed them, and ask questions answered by an LLM grounded on the document content.

- **Frontend**: Next.js 16 (App Router) + React 19 + TypeScript, in `frontend/`.
- **Backend**: FastAPI (async Python) + SQLAlchemy 2.0 + Celery, in `backend/`.
- **Infra**: PostgreSQL (prod) / SQLite (dev), Redis, Supabase Storage, a vector DB, and an external LLM/embedding provider.
- **Real-time**: REST (`/api/v1/*`) plus a WebSocket (`/ws`) channel for job progress and results.

## Global Development Principles

- Follow the existing architecture; do not invent new layers or rename established concepts.
- Inspect existing code before changing it. Read the relevant module(s) end-to-end before editing.
- Reuse existing patterns (services, repositories, schemas, UI primitives) instead of introducing parallel ones.
- Avoid unnecessary dependencies, rewrites, or refactors that are not part of the requested task.
- Keep changes scoped to the requested task; do not bundle unrelated cleanups.
- Preserve existing contracts (API shapes, DB schema, public types) unless the task explicitly requires changing them. When changing a contract, update all callers.
- Prefer maintainable, simple solutions over clever ones.
- Do not commit secrets, `.env` files, local DB files, or build artifacts.

## Frontend / Backend Boundaries

The frontend and backend are separate concerns with strict ownership:

- The frontend never speaks directly to the database, Redis, Supabase Storage, the vector DB, or the LLM. It talks to the backend over REST and WebSockets.
- The backend never renders UI. It exposes JSON via REST and pushes events via WebSockets.
- Authentication is performed by the backend (JWT + bcrypt). The frontend stores tokens but does not validate them cryptographically for authorization decisions — that is the backend's job.
- File uploads flow: frontend → backend REST → storage backend (Supabase Storage). Do not bypass the backend.

For detailed architecture and implementation conventions in each area, consult the dedicated `architecture.md` **before** writing code in that area:

- `frontend/architecture.md` — frontend-specific architecture, conventions, and patterns.
- `backend/architecture.md` — backend-specific architecture, conventions, and patterns.

## Design System (Source of Truth for UI / Styling)

All visual direction for the frontend — design tokens, colors, typography, spacing, radii, shadows, component variants, motion, light/dark theme, and overall visual identity — is defined in **`docs/DESIGN.md`**.

Rules for agents:

- `docs/DESIGN.md` is the **single source of truth** for how the UI should look. It overrides any ad-hoc styling decisions, default framework aesthetics, or assumptions about colors/fonts/spacing.
- For **any** task that touches the UI — components, pages, layouts, styles, theming, tokens, shadcn/ui variants, Tailwind usage, CSS, animations, or visual states — read `docs/DESIGN.md` first, end-to-end, before writing or editing code.
- Do not invent new colors, fonts, spacing scales, radii, or component variants. Reuse what `docs/DESIGN.md` defines.
- If a design decision is not covered by `docs/DESIGN.md`, follow the closest documented pattern from that file rather than improvising. If the gap is meaningful, flag it to the user instead of silently inventing a new rule.
- Design tokens and visual primitives must flow from `docs/DESIGN.md` into the codebase (e.g. via Tailwind config, CSS variables, shadcn theme files). Treat the file as the contract; the code must conform to it, not the other way around.
- Changes to the visual system belong in `docs/DESIGN.md` first, then in code. Do not update the code in a way that diverges from the doc.

## Agent Workflow

When working on a task:

1. Read `AGENTS.md` (this file).
2. Identify whether the task is frontend, backend, or cross-cutting.
3. Read the relevant docs **before** touching any code:
   - For any **UI / styling / design-token** concern (frontend or cross-cutting), read **`docs/DESIGN.md`** — it is the source of truth for visual direction. Do this in addition to reading the architecture file.
   - For **frontend** architecture, components, routing, state, data fetching: read `frontend/architecture.md`.
   - For **backend** architecture, services, repositories, schemas, async work: read `backend/architecture.md`.
   - For **cross-cutting** tasks, read both architecture files and `docs/DESIGN.md`.
4. Inspect the existing implementation in the affected module(s) — do not edit code you haven't read.
5. Check for relevant skills (Next.js, FastAPI, SQLAlchemy, Celery, shadcn/ui, etc.) when the task requires specialized knowledge. **Note:** this repo's `frontend/AGENTS.md` flags that Next.js 16 has breaking changes — consult `node_modules/next/dist/docs/` before writing Next.js code.
6. Plan before making significant changes. For non-trivial work, prefer to outline the plan before implementing.
7. Implement the smallest appropriate change that satisfies the task.
8. Verify with the project's existing tooling:
   - Frontend: `npm run lint`, `npm run build`, and `tsc` checks.
   - Backend: `pytest`, type checks, and any existing scripts in `backend/scripts/`.
9. Review the diff before considering the work done.
10. Update documentation before considering the task complete:
    - Update `docs/progress.md` when meaningful implementation progress is made (completed work, current task, next tasks, remaining work).
    - If the implementation introduces or changes a meaningful architectural or technical decision, update `docs/decisions.md` with the decision, reasoning, and consequences.
    - If a UI/visual change introduces or modifies a design token, component variant, or visual rule, update `docs/DESIGN.md` so it remains the source of truth.
    - Keep `progress.md` factual and current; keep decision reasoning in `decisions.md`; keep visual direction in `docs/DESIGN.md`.

## Documentation Boundaries

Each piece of information has one clear home. Do not duplicate it.

- `AGENTS.md` — **how the agent should work** on this project. Global rules, workflow, and boundaries only.
- `frontend/architecture.md` — **how the frontend is built**. Frontend architecture, conventions, component patterns. Not the source of visual truth.
- `backend/architecture.md` — **how the backend is built**. Backend architecture, conventions, service patterns.
- `docs/DESIGN.md` — **how the UI should look**. The single source of truth for design tokens, colors, typography, spacing, radii, shadows, component variants, motion, and light/dark theme. UI code must conform to this file.
- `docs/progress.md` — **what has been done and what remains**. Current status, completed work, active task, next tasks. Not a design document.
- `docs/decisions.md` — **why important decisions were made**. Meaningful architectural decisions and their reasoning. Not a changelog of every change.

## Verification

- Agents must verify their changes using the project's existing tooling whenever verification is possible.
- Do not claim work is complete without running the appropriate checks (lint, type check, build, tests).
- If a verification step cannot be run, say so explicitly and explain why.

## Security and Reliability

Project-wide rules evident from the repository:

- **Secrets**: never commit `.env`, credentials, API keys, JWT secrets, or database files. Use `.env.example` as the template.
- **External/user input**: validate at the system boundary. Backend uses Pydantic v2 schemas; frontend uses Zod. Do not bypass validation.
- **Authentication/authorization**: enforced by the backend. Do not assume the frontend has authority to grant access. Respect existing auth middleware and dependency injection (`app/api/dependencies.py`).
- **Async work**: heavy work (parsing, embedding, LLM calls) must go through Celery, not inline in request handlers. Do not block the API on long-running tasks.
- **Database migrations**: schema changes require Alembic migrations in `backend/alembic/`. Do not mutate the schema ad hoc.
