# AI Document Analyser

> Upload documents → parse → chunk → embed → ask questions and get answers
> grounded on the document content with inline citations.

A full-stack application built as a personal learning project. Users sign up, upload documents (PDF, DOCX, Markdown, plain text), and chat with an assistant that retrieves relevant snippets and answers questions grounded in the user's own documents.

---

## What's in the box

- **Authentication** — cookie-based sessions with JWT access tokens, refresh tokens, and token-family reuse detection. Bcrypt password hashing.
- **Document pipeline** — signed-URL uploads to Supabase Storage, async parsing and chunking via Celery, embeddings produced by a pluggable `EmbeddingModel` (deterministic SHA-256 stub by default, real models behind the same Protocol).
- **Retrieval-augmented chat** — dense + optional BM25 hybrid retrieval with RRF fusion, an opt-in cross-encoder reranker, and conversational query rewriting so follow-up questions resolve pronouns. Answers stream token-by-token over the existing `/ws` WebSocket.
- **Real-time UI** — live progress strip on `/documents`, sticky detail dialog with per-doc logs and stats that converge on every worker event, optimistic chat bubbles that survive cross-route navigation.

---

## Project structure

```
.
├── backend/         # FastAPI + SQLAlchemy 2.0 + Celery (Python)
├── frontend/        # Next.js 16 + React 19 + TypeScript
├── docs/            # DESIGN.md (visual source of truth), progress.md, decisions.md
├── compose.yaml     # Shared Docker Compose base
├── compose.dev.yaml # Dev overlay (bind mounts, hot reload)
├── compose.prod.yaml# Prod overlay (pinned tags, no mounts)
├── Dockerfile       # Multi-stage backend image
├── DOCKER.md        # Operator cheat sheet for the compose stack
└── AGENTS.md        # How AI coding agents should work on this repo
```

- `AGENTS.md` — workflow + boundaries for AI coding agents.
- `backend/architecture.md` — backend layout, conventions, async patterns.
- `frontend/architecture.md` — frontend layout, components, realtime bridges.
- `docs/DESIGN.md` — single source of truth for visual direction (design tokens, colors, spacing, motion). The UI conforms to this file.
- `docs/progress.md` — current status, recent changes, next tasks.
- `docs/decisions.md` — architecture decision records (ADRs).

---

## Architecture at a glance

```
Browser
  ├── /chat  ─────────────┐
  └── /documents          │
                          ▼
                  frontend (Next.js 16, App Router)
                  REST + WebSocket only
                          │
                          ▼
                  backend (FastAPI, async)
                  ├── REST  /api/v1/*
                  ├── WS    /ws             (cookie auth, per-user channel)
                  └── Celery workers ── Redis pub/sub ── WS fan-out
                          │
        ┌─────────────────┼─────────────────┐
        ▼                 ▼                 ▼
   PostgreSQL          Supabase          Redis (broker + cache)
   (prod) / SQLite    Storage           + pgvector (or JSON-col TBD)
   (dev)              (files)
                          │
                          ▼
                  LLM / embedding provider
                  (OpenAI-compatible, pluggable)
```

- The frontend never speaks to the database, Redis, Supabase Storage, the vector store, or the LLM directly.
- The backend never renders UI — it serves JSON and pushes events.
- Auth is enforced by the backend; the frontend stores tokens but does not authorize.
- Heavy work (parsing, chunking, embedding, LLM calls) goes through Celery, never inline.

---

## Quick start (Docker, recommended)

Requires Docker Desktop (or any OCI-compatible engine with Compose v2).

```bash
# 1. Copy and fill the backend env template
cp backend/.env.example backend/.env
$EDITOR backend/.env     # set DATABASE_URL, *_API_KEY, Supabase keys, etc.

# 2. Start the full stack (Postgres, Redis, API, worker, beat)
docker compose -f compose.yaml -f compose.dev.yaml up --build
```

Then in another terminal:

```bash
cd frontend
npm install
npm run dev
```

Open:

- Frontend → http://localhost:3000
- Backend API docs (Swagger UI) → http://localhost:8000/docs
- Backend health check → http://localhost:8000/api/v1/health/live

See [`DOCKER.md`](./DOCKER.md) for the full operator cheat sheet — env handling, prod overlay, common operations, VPS deployment.

### Quick start (without Docker, dev mode)

```bash
# Backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env && $EDITOR .env      # DATABASE_URL_SYNC=sqlite+aiosqlite:///./app.db for dev
alembic upgrade head
uvicorn app.main:app --reload            # in one terminal
celery -A app.tasks.celery_app worker -l info   # in another
celery -A app.tasks.celery_app beat -l info     # in a third (for periodic sweeps)

# Frontend
cd frontend
npm install
npm run dev
```

---

## Tech stack

| Layer | Tech |
|-------|------|
| Frontend | Next.js 16, React 19, TypeScript, Tailwind v4, shadcn/ui, TanStack Query, Zustand (WS store), react-hook-form + Zod, Sonner toasts |
| Backend | FastAPI (async), SQLAlchemy 2.0, Alembic, Pydantic v2, Celery (Redis broker), tiktoken chunker |
| Realtime | Native browser WebSockets + Redis pub/sub fan-out |
| Database | PostgreSQL (prod) / SQLite (dev) — same models via `with_variant` |
| Storage | Supabase Storage (signed-URL upload) |
| Vector store | JSON-column embeddings (NumPy cosine, ADR-013). Re-evaluate when cross-doc queries become a product requirement. |
| LLM | Any OpenAI-compatible provider — `OpenAICompatibleProvider` over `httpx.AsyncClient.stream` SSE |
| Embeddings | Pluggable `EmbeddingModel` Protocol — `DeterministicStubModel` (default), Qwen3 / other sentence-transformers models |
| CI | GitHub Actions — two parallel jobs (backend + frontend), pip + ML-model caching to keep runtimes under a few minutes |

---

## CI / CD

Every push and PR to `main` runs two parallel jobs defined in [`.github/workflows/ci.yml`](./.github/workflows/ci.yml).

**Backend job**
- `ruff` — lint + format check
- `mypy` — type check
- `pytest` — full backend suite
- `eval` — retrieval-eval smoke (Doc2Dial)
- Smoke test — API boots and `/api/v1/health/live` returns 200

**Frontend job**
- `eslint` — lint
- `tsc --noEmit` — type check
- `next build` — production build

Two layered caches keep CI under a few minutes even though the embedding stack ships ~1.2 GB of PyTorch + sentence-transformers:

1. **pip cache** — downloaded Python packages.
2. **HuggingFace model cache** — the embedding model files (no re-download on every run).

Only changed paths trigger jobs (small commits on the frontend don't wake the backend runner, and vice versa).

---

## Repo conventions (TL;DR — read `AGENTS.md` for the full contract)

- Frontend and backend have separate ownership boundaries — they speak only over REST and WebSocket.
- All visual direction lives in `docs/DESIGN.md`. UI code conforms to it; the doc does not chase the code.
- All external input is validated at the system boundary. Backend uses Pydantic v2 schemas; frontend uses Zod.
- Background work goes through Celery, never inline.
- Database schema changes require Alembic migrations in `backend/alembic/`.
- Secrets stay out of the repo. `backend/.env.example` is the template.
- Commits bundle only the requested change. No drive-by cleanups.
- Verify with project tooling (`npm run lint`, `tsc --noEmit`, `next build`, `pytest`, `mypy`, `ruff`) before considering a task done.

---

## Contributing

1. Fork & branch from `main`.
2. Make focused commits (one logical change each).
3. Run the project's existing verification commands locally — see `AGENTS.md` §"Verification".
4. Open a PR. CI will run backend and frontend jobs in parallel based on which paths you changed.

---

## License

Private / unlicensed. If you fork this for your own learning project, please respect the upstream licenses of the libraries it depends on (see each package's own license).
