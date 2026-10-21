# Backend Architecture

FastAPI backend for the AI Document Analyser. Handles auth, document upload, parsing, chunking, embedding, and real-time status push over WebSockets. Heavy work runs in Celery; the HTTP layer stays fast and stateless.

## Stack

- **FastAPI 0.115** (async) + **Pydantic v2** / **pydantic-settings**
- **SQLAlchemy 2.0** (async ORM, sync session for Celery) + **Alembic** migrations
- **Celery 5** + **Redis 8** (broker, result backend, pub/sub)
- **WebSockets** (Starlette, `websockets` lib) with Redis pub/sub fan-out
- **Supabase Python SDK** (storage, server-only via service-role key)
- **PyJWT** (HS256) + **bcrypt** (auth)
- **pypdf**, **python-docx** (+ `lxml`), **tiktoken** (cl100k_base), **Pillow**, **ReportLab** (tests)
- **pytest** + **pytest-asyncio**
- Driver: **psycopg** (Postgres) or **aiosqlite** (SQLite fallback)

## Infrastructure

- **Postgres** in production, **SQLite** in dev — both supported by all migrations and models. JSON/JSONB and ARRAY(Text)/JSON switching is done via `with_variant` per dialect.
- **Supabase Storage** for raw document files (bucket: `doc-uploads`).
- **Redis** for three things: Celery broker (DB 0), Celery result backend (DB 0), WebSocket pub/sub (`ws:events` channel on DB 1).
- **Vector DB**: not yet integrated. Embeddings are stored as JSON columns of floats on Postgres/SQLite, which is enough for single-document retrieval (candidate set is bounded by one doc's chunks; cosine in NumPy is sub-millisecond for any realistic document). A vector store is only justified when retrieval needs to scan many documents at once without a SQL filter to shrink the candidate set — see ADR-005 and the "Open questions" entry in `docs/decisions.md` for the trigger and migration plan.
- **LLM provider**: not yet integrated. `app/services/ai/llm.py` is a placeholder.
- **Embeddings**: a deterministic SHA-256 → 16-dim stub (`DeterministicStubModel`) behind an `EmbeddingModel` Protocol, ready to be swapped.
- **Reranker** (ADR-019): an opt-in cross-encoder rerank stage (`BAAI/bge-reranker-base` by default) sits between the retriever and the LLM prompt. Same `Reranker` Protocol + factory + singleton pattern as the embedding model. Off by default (`ANALYSIS_RERANK_ENABLED=false`); on, the orchestrator calls `rerank_top_rows_async` with a 5 s wall-clock cap and a safe fallback to the retriever's pre-rerank order on timeout or exception.
- **OCR**: not implemented. Pages with no parser output are flagged `text_source=ocr` / `needs_ocr=True` for a future worker.

## Layout

```
backend/
├── app/
│   ├── main.py                 # create_app(): middleware, routers, lifespan (subscriber + engine)
│   ├── api/
│   │   ├── dependencies.py     # DbDep, CurrentUserDep, get_current_user
│   │   └── v1/                 # health, auth, documents routers (mounted at /api/v1)
│   ├── core/                   # config (Settings), security, celery_app, logging
│   ├── db/                     # Base, async session, sync session (for Celery)
│   ├── models/                 # SQLAlchemy 2.0 ORM models
│   ├── repositories/           # Async for HTTP, sync for Celery; owner-scoped reads
│   ├── schemas/                # Pydantic request/response/event schemas
│   ├── services/
│   │   ├── auth/               # register/login/refresh/logout with token-family reuse detection
│   │   ├── document/           # upload validation, parsers (pdf/docx/md/txt/fallback), chunker
│   │   ├── storage/            # Supabase client wrapper (signed URLs, download, delete)
│   │   ├── pubsub/             # Redis publisher (sync) + subscriber (async)
│   │   └── ai/                 # embeddings stub, llm/ocr/model_registry placeholders
│   ├── tasks/                  # Celery tasks: process_document, embed_chunks_for_document, sweep_stuck_learning_documents, ping
│   ├── websockets/             # ConnectionManager + /ws route (cookie-auth, heartbeat)
│   └── utils/
├── alembic/                    # linear migration chain (head: c6d9e0f1a234)
├── tests/                      # mirrors app/; covers HTTP, repos, parsers, chunker, tasks
└── scripts/                    # wipe_data.sh for SQLite dev DBs
```

## Layering Rules

- HTTP handlers (`api/v1/endpoints/*`) validate input via Pydantic schemas and call services.
- Services own business logic. They call repositories for DB access and other services for cross-domain work (e.g. storage, pubsub).
- Repositories encapsulate SQL. Every read takes `owner_id` to enforce row-level scoping at the data layer.
- Models are SQLAlchemy declarative; schemas are Pydantic. Never leak ORM objects out of a handler — return schemas.
- Async code uses `app/db/session.py`. Celery uses `app/db/sync_session.py`. Never share sessions across the boundary.

## REST Surface (`/api/v1`)

- `GET /health` — service health.
- `POST /auth/register`, `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout`, `GET /auth/me`.
- `POST /documents/upload-intent` → returns signed upload URL + `document_id`.
- `POST /documents/{id}/confirm` → flips status, enqueues `process_document` Celery task.
- `POST /documents/{id}/upload-failed` → marks failed, deletes the row, cleans up storage.
- `GET /documents` → owner-scoped paginated list with optional `status` filter.

Analysis routes are stubbed (`api/v1/endpoints/analysis.py` is empty).

## Auth

- Passwords hashed with bcrypt (SHA-256 pre-hash to bypass the 72-byte limit, rounds=12).
- Access tokens: HS256 JWT (`type="access"`, 15 min default), sent as HttpOnly cookie `xs`.
- Refresh tokens: opaque random strings, stored hashed (SHA-256), 7-day TTL, sent as HttpOnly cookie `rfr`.
- Refresh tokens live in a `family_id`; presenting a previously-rotated token revokes the entire family (reuse detection).
- Logout revokes a single token and clears both cookies.
- `get_current_user` reads the `xs` cookie, validates the JWT, returns the `User`. Rejects disabled users.

## Document Ingestion Pipeline

1. **Upload intent** (`POST /documents/upload-intent`) — validate MIME allowlist, size cap, per-user doc-count and storage quota; mint UUID; get presigned Supabase URL; insert `Document` row in `processing` state.
2. **Direct upload** — client `PUT`s to the signed URL (Supabase authenticates).
3. **Confirm** (`POST /documents/{id}/confirm`) — flip to `uploaded`, push WS event, enqueue `process_document` with `countdown=1`.
4. **`process_document` (Celery)** — sync DB session: claim (`uploaded` → `learning`), publish WS event, download to tempfile, run the parser, persist pages, build chunks per page, persist chunks. Then enqueues `embed_chunks_for_document(document_id, owner_id)` with `countdown=1` (fire-and-forget; the BEAT sweep below is the recovery path). Does NOT flip to `ready` — see Lifecycle Contract below. On `EmptyDocumentError`: delete the row, publish `failed`, clean up storage. On other exceptions: mark failed, retry (max 3, delay 10s).
5. **`embed_chunks_for_document` (Celery)** — invoked by `process_document`. Iterates pending chunks in batches via `list_pending_chunk_ids` (owner-scoped, skips chunks with `EmbeddingStatus.succeeded`), embeds with the configured `EmbeddingModel`, upserts one embedding per chunk, flips `needs_embedding=False`. After all batches, runs `_finalize_document_status`: ≥1 succeeded → `learning` → `ready`; all failed → `learning` → `failed` with last provider error; no work → leave status alone. The matching WS event is published.
6. **`sweep_stuck_learning_documents` (Celery Beat)** — runs every 5 minutes. Finds `Document`s with `status='learning'` and `updated_at < now - 30 minutes`, re-enqueues `embed_chunks_for_document` for each (cap 100 per tick). Recovers from three failure modes: `process_document` crashing before enqueueing embeddings, broker dropping the embedding message, and the embedding worker process dying mid-run. See ADR-010.

### Lifecycle contract

`DocumentStatus.ready` strictly means *"all chunks have stored embeddings; the doc is queryable"*. The transition `LEARNING → READY` is performed **exclusively** by `_finalize_document_status` in the embedding worker. `process_document` keeps the doc in `LEARNING` after chunking; the entry `LEARNING` event on claim is the only WS event it publishes. This makes the docs-list `?status=ready` filter correct by construction. See ADR-011.

### Parsers (`services/document/parser/`)

- `pdf.py` (pypdf), `docx.py` (python-docx, Heading-1 page split, tables → GFM), `md.py`, `txt.py`, `fallback.py` (last-resort UTF-8).
- Routed by `PARSERS_BY_MIME` → `PARSERS_BY_EXT` → `FALLBACK_PARSER`.
- Output normalised by `renumber_and_finalize` (sets `final_text`, picks `text_source`, raises `EmptyDocumentError` if nothing usable).

### Chunker (`services/document/chunker/`)

- Token-aware recursive splitter using `tiktoken` (`cl100k_base`): `TARGET_TOKENS=500`, `MAX_TOKENS=800`, `OVERLAP_TOKENS=100`.
- Splits at paragraph → sentence → word → char boundaries.
- `SectionTracker` maintains the H1→H3 heading path per page; each chunk's `section_path` is set by matching its first block.
- Chunks linked via `prev_chunk_id` / `next_chunk_id`; `needs_embedding=True` by default.

## Real-Time Updates

- WebSocket endpoint at `/ws`, cookie-authenticated via the `xs` access token. Invalid/missing → close `1008 Policy Violation`.
- Per-user connection registry (`ConnectionManager`) in each FastAPI process. Application-level ping every 3s; client silence > 5s → close `1011 Going Away`.
- Publishers (Celery workers, HTTP handlers) call `services/pubsub/ws_publisher.publish_event(user_id, envelope)` → `redis.publish("ws:events", ...)`.
- Each FastAPI process runs a `WsSubscriber` started in the FastAPI lifespan. It SUBSCRIBEs to `ws:events`, parses each message, and routes to `ConnectionManager.send_to_user(user_id, event)`.
- Currently a single event type: `WsEventType.document_status = "document.status"` with `DocumentStatusEventData(document_id, title, status, error_message)`.
- Cross-process fan-out works only because Redis is the source of truth. Scaling uvicorn to N workers currently requires sticky WS routing or Redis Streams + consumer groups — not yet implemented.

## Configuration

Settings via `pydantic-settings` in `app/core/config.py`. Reads from `.env`. Key groups:

- App: `APP_ENV`, `APP_DEBUG`, `LOG_LEVEL`.
- DB: `DATABASE_URL`, `DATABASE_URL_FALLBACK` (default `sqlite+aiosqlite:///./app.db`), `DATABASE_URL_SYNC` (Postgres sync URL).
- CORS: origins, methods, headers, credentials.
- JWT: `JWT_SECRET`, `JWT_ALGORITHM`, `ACCESS_TOKEN_TTL_SECONDS`, `REFRESH_TOKEN_TTL_SECONDS`.
- Cookies: `COOKIE_SECURE`, `COOKIE_SAMESITE`, `ACCESS_COOKIE_NAME` (`xs`), `REFRESH_COOKIE_NAME` (`rfr`).
- Celery: `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`.
- Redis pub/sub: `REDIS_URL`, `WS_PUBSUB_CHANNEL`.
- Supabase: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_STORAGE_BUCKET` (`doc-uploads`).
- Uploads: `UPLOAD_MAX_SIZE_BYTES`, `UPLOAD_ALLOWED_MIME_TYPES`.

Hard caps in code: `_MAX_DOCUMENTS_PER_USER=100`, `_MAX_STORAGE_BYTES_PER_USER=1 GiB`, `MAX_LIST_LIMIT=100`.

## Run / Verify

- API: `uvicorn app.main:app --reload`.
- Worker: `celery -A app.tasks.celery worker --loglevel=info`.
- Beat (drives `sweep_stuck_learning_documents`): `celery -A app.tasks.celery beat --loglevel=info`. Required for the LEARNING-recovery sweep to run on schedule; without it, a doc whose embedding task was lost stays stuck until manual intervention.
- Migrations: `alembic upgrade head` (URL is injected from settings in `alembic/env.py`).
- Tests: `pytest` (autouse fixture resets settings cache and DB engines between tests).

## Supabase (Postgres) Migration Run-Book

The codebase is dual-dialect: every migration and model is written so the
same code creates identical tables on SQLite and on Postgres. Switching
the dev / staging / prod DB is purely a configuration change.

### 1. Get the connection strings

In the Supabase dashboard: **Project → Settings → Database → Connection string**.
Grab both pooler and direct, both with `?sslmode=require` appended:

| Env var | Driver | Host:port | Used by |
|---|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg` | pooler `:6543` (transaction mode) | FastAPI app (high-fan-out, short tx) |
| `DATABASE_URL_SYNC` | `postgresql+psycopg` | direct `:5432` | Celery workers + Alembic DDL |

DDL (`CREATE TYPE`, `ALTER TABLE`) does not work inside a
transaction-pooled session — keep Alembic on the direct URL. Long-running
Celery tasks should also avoid the pooler so they don't hog a pooled
slot.

### 2. Set the env vars

Copy `backend/.env.example` to `backend/.env` and fill in both URLs.
Leave `DATABASE_URL_FALLBACK` empty in production so a missing
`DATABASE_URL` fails loud instead of silently falling back to SQLite.

### 3. Run the migrations

```bash
cd backend
# Smoke-test the connection first — should print the URL without raising.
python -c "from app.db.session import get_engine; print(get_engine().url)"

# Apply every migration in order. The chain is linear (one head) so a
# fresh Supabase DB ends up at the latest revision in one step.
alembic upgrade head

# Confirm
alembic current
```

### 4. Verify

```bash
uvicorn app.main:app --reload
curl http://localhost:8000/api/v1/health  # diagnostics should report {"db": "postgres"}

celery -A app.tasks.celery worker --loglevel=info
# Worker should boot without "could not connect" errors and the first
# task that hits the DB should succeed.
```

### Postgres-specific tuning (already wired)

The async + sync engines in `app/db/session.py` and `app/db/sync_session.py`
auto-detect the Postgres URL and apply:

- `statement_timeout=30s` (server-side, via `connect_args["options"]`) —
  caps any single query so a stuck request can't hold a pooler slot.
- `pool_pre_ping=True` — transparently reconnects after the Supabase
  pooler's ~1 hour idle-kill instead of raising a 500.
- `pool_recycle=3600` — belt-and-braces, discards connections older
  than 1 hour.

### Wiping data

- **Dev (SQLite):** `scripts/wipe_data.sh` (unchanged).
- **Staging/Prod (Postgres):** `DATABASE_URL_SYNC=postgresql+psycopg://... scripts/wipe_data_pg.sh`
  (uses `psql` + `TRUNCATE ... RESTART IDENTITY CASCADE`, optional
  `pg_dump` backup to `.local/backups/`).

### Things that DO NOT need code changes

- Schema is identical on both dialects (JSONB on PG = JSON on SQLite;
  ARRAY(Text) on PG = JSON on SQLite; native ENUMs are NOT used —
  every enum is `VARCHAR(20) + CHECK`).
- `INSERT ... ON CONFLICT` in `repositories/conversation.py` is already
  dialect-branched.
- `PRAGMA foreign_keys=ON` is gated by an `is_sqlite` check (a no-op on
  PG, which enforces FKs natively).
- Primary keys are `Uuid` with Python-side `uuid4()` defaults — no
  `AUTOINCREMENT` ↔ `SERIAL` mismatch.

## Conventions for New Code

- New domain: add a model under `app/models/`, a schema under `app/schemas/`, a repository under `app/repositories/`, a service under `app/services/<domain>/`, then wire a router under `app/api/v1/endpoints/` and register it in `api/v1/router.py`.
- New Celery task: add under `app/tasks/`, decorate with `@shared_task`, ensure it gets imported in `app/tasks/__init__.py` for autodiscover.
- Any new DB change requires an Alembic migration. Do not mutate the schema outside Alembic.
- Owner-scoped reads: every read must filter by `owner_id`. Cross-owner access is a security bug.
- Secrets, `.env`, local `*.db` files, and `*.db.backup-*` files are gitignored — never commit them.
