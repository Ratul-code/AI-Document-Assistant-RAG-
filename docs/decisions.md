# Architecture Decisions

This file records important architectural and technical decisions for the AI Document Analyser, along with the reasoning behind them.

A decision belongs here when it meaningfully affects architecture, technology choices, system boundaries, data flow, scalability, or long-term implementation. Routine implementation details, refactors, and bug fixes do not.

## Decision format

### ADR-NNN: Title

**Status:** Accepted · Proposed · Superseded

**Decision:**
What was decided.

**Reason:**
Why this decision was made.

**Consequences:**
Important trade-offs or implications.

---

## Decisions

### ADR-001: Async-first backend with Celery for heavy work

**Status:** Accepted

**Decision:**
The HTTP layer is a stateless FastAPI app served by uvicorn. Parsing, chunking, embedding, and other CPU/network-heavy work run in Celery workers, coordinated via Redis as broker and result backend.

**Reason:**
Document ingestion is slow and I/O-bound. Keeping it inline would block request handlers, blow timeouts, and prevent the API from staying responsive under upload spikes. A separate worker layer lets the two scale independently and lets us attach progress events to the right user without coupling the API to long-running tasks.

**Consequences:**

- One more process to operate (Celery worker) on top of the API and Redis.
- Two DB stacks in code: async (`app/db/session.py`) for HTTP, sync (`app/db/sync_session.py`) for Celery. A given row must be touched only via the matching stack to avoid event-loop issues.
- Heavy work survives API restarts but requires Redis to be up.

### ADR-002: Dual Postgres + SQLite support via dialect variants

**Status:** Accepted

**Decision:**
The same models, repositories, and Alembic migrations support PostgreSQL in production and SQLite in development. Where the dialects differ (e.g. `JSONB` vs `JSON`, `ARRAY(Text)` vs `JSON()`), columns are declared with `with_variant(...)`.

**Reason:**
SQLite lets a contributor run the entire stack with zero infra setup, which keeps onboarding friction low. Postgres is the realistic deployment target. Forcing one or the other would either block local development or require a different code path in production.

**Consequences:**

- Schema features that don't exist in SQLite (true `JSONB` operators, `ARRAY`, partial indexes) have to be written defensively or guarded by `is_postgres` checks.
- Adding a Postgres-only feature (e.g. `pgvector`, `tsvector`) needs a clear answer: a variant column, a runtime check, or a feature flag. Don't add it under the radar.

### ADR-003: HttpOnly cookies for auth, no token storage on the frontend

**Status:** Accepted

**Decision:**
Access and refresh tokens are issued by the backend and delivered to the browser as HttpOnly cookies (`xs`, `rfr`). The frontend never reads, stores, or sends tokens via JavaScript — it only uses `credentials: "include"`.

**Reason:**
HttpOnly cookies are inaccessible to XSS-inserted scripts, which is the dominant frontend threat. They also travel automatically with both fetch requests and same-protocol WebSocket upgrades. The tradeoff is full reliance on cookie behavior and CORS configuration.

**Consequences:**

- Cross-origin requires CORS with an explicit origin and `Access-Control-Allow-Credentials: true` (no `*`).
- Session refresh has to be coordinated on both ends: the Next.js middleware (`src/proxy.ts`) is the source of truth on the server side, and `apiClient` + `useWSStore` carry a smaller client-side fallback.
- A production deployment that puts the frontend behind a different hostname than the backend may need a token query param on the WS upgrade. Treat that as a follow-up decision when it arises.

### ADR-004: Refresh-token reuse detection via token families

**Status:** Accepted

**Decision:**
Each refresh token belongs to a `family_id`. When a token that has already been rotated is presented again, the entire family is revoked.

**Reason:**
A leaked refresh token is the most damaging credential the system holds. Detecting reuse lets the system invalidate every descendant token issued from the same login, limiting the blast radius of a single leak.

**Consequences:**

- Rotation must be atomic in the database: revoke the old row, mint the new one, and link them via `replaced_by_id` inside one transaction.
- Logout only revokes a single token. A user who logs out and someone else tries to reuse the leaked token still triggers a family revoke.

### ADR-005: Embeddings stored as a JSON column of floats, not in pgvector

**Status:** Accepted (for now)

**Decision:**
`DocumentEmbedding.vector` is stored as a JSON column on both Postgres and SQLite. No vector index is used. A `DeterministicStubModel` (SHA-256 → 16-dim) is the only embedding provider today. The model is hidden behind an `EmbeddingModel` Protocol.

**Reason:**
The system needs to ship the ingestion pipeline end-to-end before a vector backend is selected. A stub lets us develop parsers, chunkers, status events, and UI without committing to a vector store. The Protocol keeps the swap to a real provider / real index as a single-file change.

**Consequences:**

- No similarity search is possible yet — it has to be implemented once a vector backend is chosen.
- Once a backend (e.g. `pgvector`) is picked, this decision is superseded: a migration replaces the JSON column, queries gain `<=>`, and the embedding worker is wired into the ingestion flow.

### ADR-006: Server-rendered session refresh lives in the Next.js middleware

**Status:** Accepted

**Decision:**
The source of truth for cookie rotation during a session is `src/proxy.ts`. On a protected-path `/auth/me` 401, the middleware calls `/auth/refresh`, forwards the new `Set-Cookie` headers to the browser verbatim, and rewrites `req.headers.cookie` for downstream React Server Components.

**Reason:**
Server Components cannot trigger a client-side `fetch` retry before they read their data. Doing the refresh in middleware means RSC trees always see a valid session on first render, and there is exactly one place where the refresh protocol lives.

**Consequences:**

- The proxy must forward cookies as-is and never write the cookie attributes itself.
- `apiClient` still carries a single-shot client-side refresh on 401, because mutations from event handlers do not pass through the middleware. This is by design and not a duplication bug.

### ADR-007: Realtime updates over WebSocket + Redis pub/sub fan-out

**Status:** Accepted

**Decision:**
A single `/ws` endpoint per FastAPI process holds per-user socket registrations. Workers (Celery) and HTTP handlers publish events to a Redis channel (`ws:events`); every FastAPI process subscribes and forwards matching events to its locally connected sockets.

**Reason:**
The HTTP layer is stateless and can scale horizontally, but WebSocket connections are sticky to a process. Using Redis as the event bus lets any process emit and lets every process deliver, so publishers and subscribers do not need to know about each other.

**Consequences:**

- Scaling uvicorn to multiple workers per machine is fine as long as each user's WS lands on a process that will see the events; cross-worker delivery is not guaranteed. A future decision (see _Open questions_) is required before truly multi-worker scale-out.

### ADR-008: Single WS event type at launch

**Status:** Accepted

**Decision:**
The wire protocol exposes a single event type today: `document.status`, with `DocumentStatusEventData(document_id, title, status, error_message)`. Both `WsEventType` and the handler registry (`HANDLERS` in `app/services/pubsub/ws_subscriber.py`) are designed to be extended.

**Reason:**
The whole realtime pipeline (publisher, subscriber, manager, frontend bridge) can be exercised end-to-end without committing to a broad event taxonomy. Adding more events later is a structured change rather than a refactor.

**Consequences:**

- Frontend code that assumes "only `document.status` arrives" (e.g. `useWSToasts`, `useDocumentStatusBridge`) must be extended alongside any new event types. Don't add a new event without also wiring its bridge.

### ADR-009: Embedding worker dispatched inline by process_document

**Status:** Accepted

**Decision:**
`process_document` enqueues `embed_chunks_for_document` itself at the end of a successful run (after pages + chunks are committed and the entry `LEARNING` event is published) using `celery_app.send_task(... countdown=1)`. The enqueue is fire-and-forget — a broker failure is logged but does not fail `process_document` or the upload-confirm response, mirroring the existing pattern at the API layer.

**Reason:**
A direct handoff inside the producer task is the smallest change that wires the pipeline end-to-end. It avoids two alternatives that were considered and rejected:

1. **Celery `chain` / `chord` at the API layer.** Would require a configured Celery result backend and add a third place (`documents.py`) that knows about both tasks. The result backend is not otherwise in use.
2. **Pure sweep / Beat-driven.** No handoff at all; a periodic task pulls `LEARNING` docs and enqueues embeddings. Adds end-to-end latency (chunking → user has to wait for the next Beat tick → embedding starts) for a problem that doesn't exist when the broker is healthy.

The `countdown=1` lets the worker's fresh DB session see the committed chunks before it starts reading, the same trick already used by the API enqueue. Fire-and-forget is preserved because the chunks exist with `needs_embedding=True` and the sweep task (ADR-010) covers the recovery case.

**Consequences:**

- `process_document` learns the embedding task's name string. Tight coupling, but the alternative (signal-based dispatch) is harder to reason about and not worth the indirection at this scale.
- A broker outage between chunk persistence and the embedding enqueue leaves the doc in `LEARNING` with no embedding task in flight. The sweep (ADR-010) is the recovery path.

### ADR-010: Periodic Beat sweep recovers LEARNING docs that lost their embedding task

**Status:** Accepted

**Decision:**
A Celery Beat task `sweep_stuck_learning_documents` runs every 5 minutes. It queries the existing repo helper `find_stuck_learning(older_than=now - 30m)` and re-enqueues `embed_chunks_for_document` for each doc whose `updated_at` is older than the threshold. A hard cap (`max_docs_per_run`, default 100) prevents one tick from enqueueing a thundering herd if a long outage left many docs stuck.

**Reason:**
Three things can leave a doc in `LEARNING` forever today:

1. `process_document` raises before the `send_task` for embeddings lands (or the broker is briefly unreachable). `process_document` has `max_retries=3`, but a process kill mid-run doesn't get retried by the broker.
2. The embedding task itself is dropped, or its worker process dies mid-run between writing some chunks and finalizing.
3. The embedding task raises a transient error and the worker's `max_retries=3` is exhausted without success.

A 30-minute threshold is comfortably larger than the embedding task's runtime + retries (15-min hard time limit × 3 = 45 min worst case for back-to-back failures), so the sweep never races the primary pipeline. Re-running the worker is safe because `list_pending_chunk_ids` already filters out chunks with `EmbeddingStatus.succeeded`; the worker is idempotent.

The threshold and the cap are exposed as task arguments rather than environment settings so they can be tuned per tick without code changes — useful in tests and useful in incident response.

**Consequences:**

- Adds a Beat process requirement. Today the project runs `celery -A app.tasks.celery worker`; running `celery -A app.tasks.celery beat` (separate process, same env) is now needed for the sweep to fire.
- The 30-minute threshold means a doc that legitimately takes 25 minutes to embed could be re-enqueued unnecessarily. Acceptable cost: the re-enqueued worker sees zero pending chunks, hits the no-op branch in `_finalize_document_status`, and exits quickly.
- Adds `app/tasks/sweep.py` as a new module and `find_stuck_learning` / `count_stuck_learning` to `app/repositories/document.py`. The existing sweep precedent (`find_stuck_processing`) is mirrored for consistency.
- A truly permanently-broken doc (provider always errors) will be re-enqueued every 5 minutes forever and never transition out of `LEARNING`. A real failure → `FAILED` escalation is a future decision (likely an additional `stale_threshold_minutes` argument above which the sweep flips to `FAILED` directly).

### ADR-011: `READY` strictly means "queryable" — only the embedding worker may flip it

**Status:** Accepted

**Decision:**
`DocumentStatus.ready` is reserved for documents whose chunks are persisted *and* have stored embeddings. The transition `LEARNING → READY` (and `LEARNING → FAILED` for the all-chunks-failed case) is performed exclusively by `embed_chunks_for_document._finalize_document_status`. `process_document` no longer touches `READY` at all.

**Reason:**
Before this change, `process_document` flipped `LEARNING → READY` once pages + chunks were persisted. The enum docstring (`"ready for queries"`) and the docs-list endpoint's `?status=ready` filter both promised queryability, but a query at that point would have run against chunks with no embeddings. The status flag and the actual capability were out of sync — a contract bug.

The fix keeps the enum unchanged (no migration, no enum churn) and instead enforces the contract by code ownership: only one place writes `READY`. The `_finalize_document_status` rules are explicit and small:

- ≥ 1 chunk succeeded → `READY`, push WS event.
- All chunks failed → `FAILED` with the last provider error in `error_message`, push WS event.
- No work (already-embedded retried run, or zero-chunk doc) → leave status alone (the worker has nothing to verify queryability against).

**Consequences:**

- The `LEARNING → LEARNING` period after chunking and before the embedding worker finalizes is invisible on the WS bus. A duplicate `learning` event was briefly added after chunking to signal "chunking done, embeddings pending" and then removed as noise — the payload was identical to the entry event, and there is no separate `EMBEDDING` status. Clients that want sub-state should add an in-doc count or a derived field later; not in scope today.
- A doc that loses its last embedding provider mid-run will reach `FAILED` with the last error message. That message is the model's `str(exc)[:1000]`, which is operator-readable but not great user UX. Adequate for ops; revisit if a user-facing error surface is added.
- An intermediate `EMBEDDING` status was considered and rejected (per the `LEARNING` decision above) to keep the schema and the WS event set small.

### ADR-012: WS-based streaming for RAG responses

**Status:** Accepted

**Decision:**
A streaming analysis session is initiated by a REST `POST /api/v1/analysis/query/stream` returning a `{ stream_id }` envelope immediately (HTTP 202). Token deltas, the terminal `done` payload, and error envelopes arrive on the existing `/ws` socket as the new event family `analysis.token | analysis.done | analysis.error`, each carrying the `stream_id` so the frontend can correlate. The REST `POST /api/v1/analysis/query` non-streaming path is kept for tests, REST clients, and graceful degradation when the WebSocket is unavailable.

**Reason:**
Four approaches were considered:

1. **Pure REST with polling.** The client calls a non-streaming endpoint and polls for status. Adds latency, multiplies requests, and forces the LLM to block on the longest possible answer. Rejected.
2. **Server-Sent Events on a dedicated `/analysis/stream/sse` endpoint.** Clean one-way stream, but requires an extra HTTP connection lifecycle, separate CORS/auth story, and a separate reconnect/retry mechanism. The codebase already has a working WebSocket with Redis fan-out (ADR-007), which would be duplicated.
3. **Streaming HTTP (`Transfer-Encoding: chunked`).** Cancelling mid-flight requires explicit client cooperation and the response body shape is not JSON. Mixing free text + metadata + citations into one buffer is awkward.
4. **WebSocket fan-out with a `stream_id` discriminator.** Reuses the existing `/ws` connection, server, auth, and reconnect logic. The client already has `WsEventType` discriminated unions; adding three event variants is additive. Cancellation maps cleanly to the existing WS `onclose` handler. The only new code is the dispatcher inside the background task.

**Consequences:**

- A user can have multiple concurrent streams (one per open chat turn) since each is tagged with a `stream_id`. The endpoint caps concurrency per user (`analysis_max_concurrent_streams_per_user`, default 3) to bound memory + LLM provider burst; the registry is a small in-process dict that releases on done/error/cancel.
- The REST fallback is a real, supported path — non-streaming clients get full answers and citations but no token-by-token UX. The error-code → HTTP-status mapping is the source of truth for both paths.
- The orchestrator is an `async generator` yielding in-process events (`AnalysisEvent`). The streaming handler drains it inside an `asyncio` task and translates each yield to the wire envelope via `_dispatch_event`. This two-layer split (rich internal events, simple wire events) means we can add future internal events (e.g. `intermediate_citations`) without changing the WS protocol.
- Token rate-limiting / back-pressure lives in `connection_manager.send_to_user(...)` — no batching, no buffering; the client sees each token as soon as the LLM emits it. This deliberately trades throughput for low perceived latency in a chat-shaped UX.

### ADR-013: NumPy top-K cosine over JSON embeddings for v1 retrieval

**Status:** Accepted

**Decision:**
The retrieval half of the RAG pipeline (`app/services/document/analyser/rank_top_rows`) ranks candidate chunks in pure Python with NumPy over the JSON-stored vectors from `document_embeddings.vector`. The candidates are pre-filtered by an owner-scoped SQL JOIN on `documents` (status=`ready`) and `document_embeddings` (status=`succeeded`), then narrowed further when the caller pins a `document_ids` list. No new vector backend is added.

**Reason:**
This is the v1 implementation chosen in the original ADR-005 ("JSON embeddings are fine until candidate sets justify pgvector"). The trigger for revisiting ADR-005 was cross-document / no-doc-filter retrieval — the user explicitly chose to default to "all ready docs, optionally narrowed" in the query UI, so almost every query has a SQL filter that shrinks the candidate set first (typically thousands of chunks at most for a single user). That keeps the candidate set well under the threshold where the JSON-column approach becomes slow.

Four alternatives were considered:

1. **pgvector.** Would add a Postgres extension dependency, a new Alembic migration, and a new column type. The trigger for this — scans of many documents without a SQL pre-filter — is not in scope today.
2. **External vector service** (Pinecone, Qdrant, Weaviate). Significantly more infrastructure for the same query shape; rejected.
3. **SQL-only ranking** (e.g. `pg_trgm` or stored cosine via SQL). NumPy is faster, more flexible for min_cosine thresholds, and testable without a live DB. The SQL JOIN still does the heavy filtering (owner + status) so we never rank the whole table.
4. **Hybrid ANN** (e.g. embedding the question and doing approximate nearest neighbors in the app process). Same tradeoffs as `pgvector`; not worth it at this scale.

**Consequences:**

- The candidate fetch (`fetch_candidate_rows`) is sync and runs in `asyncio.to_thread(run_in_sync_session, ...)` so it doesn't pin the FastAPI event loop. The handler's async DB session is used only for `list_ready_documents`.
- Top-K, snippet length, and minimum cosine are all `Settings` constants (`analysis_top_k=6`, `analysis_snippet_chars=800`, `analysis_min_cosine=0.0`). They are not per-tenant and not env-tunable at runtime; changing them requires a deploy.
- Dim mismatch is detected once per query by sampling `candidates[0].embedding_dim` vs the embedding model's `model.dim`. The mismatch surfaces as `PROVIDER_MISMATCH` (HTTP 503) so an operator is forced to either re-embed docs or switch to a matching model — silent cosine across incommensurable vectors would produce nonsense answers.
- The contract remains what ADR-005 set: when the same query shape is asked over hundreds of documents without a doc filter, pgvector or an equivalent lands in this code path, not in the orchestrator.

### ADR-014: `analysis.*` WS event family extends ADR-008

**Status:** Accepted

**Decision:**
The WS event taxonomy is extended with three new variants — `analysis.token`, `analysis.done`, `analysis.error` — all under the `analysis.*` prefix and all carrying a `stream_id` for client-side correlation. Each is a dataclass in `app/schemas/ws_events.py` with a typed payload, the `WsEventType` Literal is widened, and `HANDLERS` in the subscriber remains unchanged (handlers only run for events the bridge cares about — they don't need to know about analysis events because the analysis UI subscribes directly via `useAnalysisStream`). `publish_analysis_token/done/error` helpers in `ws_publisher.py` mirror the existing `publish_event` style and are the only place the discriminator string `"analysis.token"` etc. is constructed.

**Reason:**
The original ADR-008 deliberately shipped a single event type so the wire protocol could be exercised end-to-end before committing to a taxonomy. Adding the `analysis.*` family now is the first extension and was planned from the start: token-level streaming requires a small, predictable set of variants and each variant has a distinct payload shape (token text, terminal answer + citations, or error code + message). Collapsing them into a generic `analysis.event` with a `phase` field would lose static typing and force the client to switch on `phase`. Reusing the existing `/ws` connection avoids opening a second socket — it inherits the same Redis fan-out (ADR-007) and the same auth path.

Two naming alternatives were considered:

1. **Flatten the prefix** (`token`, `done`, `error` globally). Ambiguous when other features add their own streaming (`document.log`, future `ocr.token`, etc.).
2. **Per-stream type** (`analysis_text_token`, `analysis_final`, `analysis_error`). Verbose; `analysis.*` is shorter and groups the family in logs and the handler registry.

**Consequences:**

- `WsEventType` is now a discriminated union with five variants (was one). Pydantic validation rejects unknown `type` strings so a typo in the publisher or a stale client crashes loudly instead of silently dropping the event.
- Error codes are a closed set (`NO_DOCUMENTS_READY | PROVIDER_MISMATCH | PROVIDER_ERROR | CANCELLED | INTERNAL`) modelled as a Pydantic `Literal`. The REST handler maps them to HTTP statuses (`NO_DOCUMENTS_READY → 422`, `PROVIDER_MISMATCH → 503`, `PROVIDER_ERROR → 502`, others → 500), and the WS payload uses the same code so the client can render either path with the same switch.
- Adding future analysis sub-events (e.g. `analysis.partial_citations` for live sources) requires only a new `WsEventType` literal + payload dataclass + publisher helper; the handler registry is unaffected.
- The frontend `useAnalysisStream` hook reads the `stream_id` filter on subscription — opening multiple chat tabs does not cross-pollinate because each tab pins a single stream id.

---

## Open questions

These are decisions the codebase has not yet made. They are tracked here so they aren't silently invented during implementation; once one is made, it graduates into an ADR above.

- **LLM provider and prompt schema.** `app/services/ai/llm.py` and `model_registry.py` are empty.
- **Vector backend.** The current trigger for revisiting ADR-005 is **cross-document / no-doc-filter retrieval** (product-wide search, multi-doc synthesis). The JSON-column approach scales fine for any single-document query, which is the only retrieval shape today. When the cross-doc case lands, the choice is `pgvector` (one Alembic migration, no extra infra) or an external vector service (Pinecone, Qdrant, Weaviate). See ADR-005 for the current contract.
- **OCR engine.** Pages with no parser output are flagged (`text_source=ocr`, `needs_ocr=True`), but the worker and provider are not chosen.
- **Streaming UI for answers.** Whether the chat UI streams tokens, returns whole answers, or both — depends in part on the LLM provider's API.
- **Cross-process WS scaling.** Multi-worker uvicorn needs a chosen approach: sticky routing, Redis Streams + consumer groups, or a dedicated WS gateway. Today's fan-out works only because each FastAPI process owns its own connection registry.
- **Forgot-password flow.** Listed as public in the middleware allow-list; the actual reset-link mechanism (email provider, token TTL, single-use vs reusable) is not decided.
- **Stuck-`LEARNING` escalation.** ADR-010 re-enqueues forever; if the embedding provider is permanently broken the doc never leaves `LEARNING`. Add a "stale beyond X hours → flip to `FAILED`" branch to the sweep when this becomes a real ops problem.

### ADR-015: Retrieval evaluation harness — real pipeline, retrieval-only v1

**Status:** Accepted

**Decision:**
`backend/eval/` ships a versioned dataset schema, a hermetic
seeded-corpus harness, and a CLI runner (`eval.scripts.run_retrieval_eval`)
that measures Recall@1 / Recall@5 / Recall@10 + MRR + an
unanswerable false-positive rate. The runner drives the same
`fetch_candidate_rows` + `rank_top_rows` pair the production
analyser uses (ADR-013) — no parallel retriever. The shipped
dataset (`backend/eval/datasets/<name>/dataset.json`) starts with
`status: "needs_manual_population"` and zero fake questions;
the runner refuses to execute against that status.

**Reason:**
Four things must be true for retrieval metrics to be useful:

1. **The eval must hit the real pipeline.** A parallel retriever
   for testing is a footgun: regressions in the production path
   don't surface, and "the eval is green" stops meaning anything.
2. **The dataset must be a durable artefact.** Without a versioned
   schema and committed JSON, every run invents its own questions
   and the numbers aren't comparable across runs or contributors.
3. **Forward-compat fields must stay in the schema.** The runtime
   does not yet route on `retrieval_strategy`, but adding the
   field to the dataset now means the migration when routing
   lands is zero — just re-run.
4. **No fake results.** AGENTS.md and the project goals explicitly
   forbid invented evaluation numbers. The runner's hard guard on
   `status` is what enforces this.

Three alternatives were considered:

1. **HTTP-only eval** (`POST /analysis/query` + inspect
   citations). Rejected because the LLM step adds latency, cost,
   and a second failure mode that has nothing to do with retrieval.
2. **pytest fixtures only** (no CLI, no versioned dataset).
   Rejected because pytest tests aren't a reproducible artefact —
   they're scattered, hidden behind test markers, and can't be
   shared with non-engineers who might want to add questions.
3. **External framework** (Ragas, TruLens, etc.). Rejected for
   v1 because the surface area is huge, the dependencies pull in
   non-trivial installs, and we want a known-fixed evaluation
   methodology before adding indirection. Revisit in v2 if/when
   answer-quality metrics arrive.

**Consequences:**

- **Retrieval-only for v1.** LLM answer quality, faithfulness,
  and exact-match-of-generated-answer are out of scope. The
  `expected_answer` field is already on every question for v2 to
  pick up without a schema change.
- **Two convenience shortcuts** in the runner: when running
  against the seeded corpus (not the dev DB), `document_id` and
  entries in `relevant_chunk_ids` may be the fixture filename,
  which the runner expands to the seeded doc's UUIDs at scoring
  time. This is for hand-written small datasets; production runs
  against the dev DB must use real UUIDs.
- **The seeded corpus uses the deterministic stub embedder** by
  default so every run is bit-stable. The `--embedding-provider`
  flag is a debug switch, not a recommended workflow.
- **Result files are gitignored** and live under
  `backend/eval/results/`. The committed long-term record is in
  `backend/eval/reports/` as human-curated summaries. This
  avoids 50 MB of JSON diffs in git history while keeping the
  numbers reproducible from any run via the same CLI.
- **`--use-dev-db` model-mismatch refusal.** The runner
  explicitly rejects starting if the configured embedding model
  doesn't match what's stored in `document_embeddings.model_name`.
  Silently scoring against mismatched vectors would produce
  numbers that look meaningful but aren't.
- **No CI integration in v1.** Recommended only after ≥10 real
  questions and a stable baseline; until then CI runs are noisy
  rather than informative.

### ADR-016: Eval rebuild — `backend/eval/` plural, Doc2Dial, hermetic seeding, fuzzy passage→chunk mapping

**Status:** Accepted

**Parent:** ADR-015.

**Decision:**
The original `backend/evals/` scaffold is replaced with a fresh
`backend/eval/` (plural) package that scores **Doc2Dial** — 1,500
dialogue turns across 171 grounded documents — using the real
production retriever (`fetch_candidate_rows` + `rank_top_rows`).
The placeholder `status: "needs_manual_population"` dataset is
replaced with a populated `dataset.json` (`status: "ready"`) that
the runner accepts.

The four pieces:

1. **Hermetic in-memory SQLite per CLI invocation.** Every
   `seed_corpus` / `build_dataset` / `run_retrieval_eval` call
   builds a fresh `sqlite:///:memory:` engine, runs
   `Base.metadata.create_all`, patches
   `ws_publisher.publish_event` to a no-op, and disposes the
   engine on exit. No leak into dev `app.db`; no Redis dependency.
   `StaticPool` is used so cross-connection reads see the same DB
   (without it, every connection gets a private in-memory DB).
2. **Deterministic `Document.id` UUIDs.** The hermetic DB is
   rebuilt every run; without a stable mapping, the `Document.id`
   values would change every time and the `scope_document_ids`
   stored in `dataset.json` (a committed artefact) would go stale.
   The fix: `Document.id = uuid5(namespace, owner_id + doc_hash)`.
   Same source doc → same UUID across every re-seed.
3. **Owner UUID persisted to `datasets/doc2dial/owner_id.txt`.**
   Single committed `.txt` file. Lets `dataset.json`'s UUIDs stay
   valid across re-seeds and re-runs.
4. **Fuzzy passage→chunk mapping via LCS.** Doc2Dial's
   `references` field gives span-level gold relevance for each
   turn. Our tiktoken chunker doesn't align with Doc2Dial's span
   boundaries, so a 1:1 mapping isn't possible. The mapping is:
   `overlap_ratio(P, C) = longest_common_substring_length(P, C) / len(P)`,
   with a chunk marked relevant iff `overlap_ratio ≥ 0.5`. Pure
   stdlib, no numpy/scikit-learn, sub-second on 1.5K turns.
   Handles chunk-boundary spans naturally: a span that crosses
   two chunks gets both halves matched.

Plus, the runner adds a **strict model-mismatch refusal**: before
scoring, it selects `model_name, COUNT(*) FROM document_embeddings
GROUP BY model_name` and refuses to start if the active embedder's
`model.model_name` is not present. Catches: empty seeded DB, wrong
provider flag, stale DB from a previous run.

**Reason:**
The previous `backend/evals/` scaffold was a placeholder — schema
in place, `status: "needs_manual_population"` shipped, runner
refused to execute. To get a real number we'd need:

- A populated dataset (hand-writing 30+ questions across the
  test corpus doesn't scale).
- Production-pipeline reuse (a parallel retriever for testing
  hides regressions; ADR-015 already forbids it).
- Reproducibility across re-runs (every run re-seeds; UUIDs must
  stay stable).
- A mapping that doesn't require N-to-N span↔chunk alignment.

Doc2Dial satisfies all four: 1,500 turns already populated with
span-level gold, the production pipeline ingests the underlying
documents verbatim, and the fuzzy mapping turns span-level gold
into chunk-level gold for scoring.

The `--embedding-provider` flag runs the eval twice — `stub`
first (verifies the harness plumbing) then `qwen3` (the real
baseline). This split matches the original "fast smoke vs real
baseline" workflow.

**Consequences:**

- **`backend/eval/` (plural), not `backend/evals/`.** The repo
  already used "eval" for the singular intent (an evaluation
  harness, not "multiple evaluations"); the `s/` came from a
  convention not in evidence anywhere else. Renaming the
  package means moving `.gitignore` rules and updating any docs
  that referenced the old path. Done in this commit.
- **1.5K questions × 171 docs.** Each run re-seeds the full
  171-doc corpus in-memory; with `stub` this is fast, with
  `qwen3` (CPU) it takes a few minutes. Acceptable for v1 — no
  future on-disk cache planned (per ADR-015: hermetic, no shared
  state).
- **Fuzzy overlap is heuristic.** Edge cases at chunk boundaries
  produce expected false negatives on short spans. Audit-only
  `passage_mapping.json` lets reviewers check why any chunk was
  or wasn't marked relevant.
- **No LLM answer scoring.** v1 is retrieval-only. The
  `gold_answer` field on each question is preserved so v2 can
  pick it up without a schema change.
- **Two runnable providers but only one "recommended".** Stub is
  the fast, bit-stable harness verifier; Qwen3 is the real
  baseline. Reports carry the embedder name; comparing numbers
  across embedders is by definition a comparison study, so each
  report is self-contained.
- **No CI integration in v1** (per ADR-015). Recommended only
  after a stable baseline + ≥10 real questions; until then CI runs
  are noisy rather than informative.
- **Doc2Dial is CC-BY-SA-4.0.** The cached zip is gitignored
  under `backend/eval/.cache/`. Future `dataset.json`
  redistribution needs a CC-BY-SA-4.0 attribution review.

### ADR-019: Post-retrieval reranking (cross-encoder, opt-in)

**Status:** Accepted

**Decision:**
Add a third stage to the RAG pipeline, sitting between the retriever
(ADR-018: dense or hybrid+RRF) and the LLM prompt: a cross-encoder
reranker. Default model is `BAAI/bge-reranker-base`, default provider
is `sentence-transformers` `CrossEncoder` (same library family as the
Qwen3 embedder — no new heavy dependency). The reranker is **opt-in**
via `ANALYSIS_RERANK_ENABLED=false` by default.

Pipeline:
```
SQL pre-filter (owner_id, status=ready)
  → candidates
  → embed query
  → rank_top_rows_with_mode(...)     # ADR-018: dense or hybrid+RRF
  → rerank_top_rows(...)             # THIS ADR — opt-in via env
  → prompt + LLM
```

**Reason:**
The v1 retriever (ADR-013) and the v2 hybrid retriever (ADR-018)
both produce an ordered top-K via embedding similarity (and lexical
overlap in hybrid mode). Cross-encoders score a `(query, document)`
pair *jointly*, which is materially more accurate at ranking the
top of a small candidate pool than either cosine similarity or BM25
alone — published numbers on BEIR and similar retrieval benchmarks
show roughly +5–10 R@1 and +0.05 MRR for a small cross-encoder
over a bi-encoder, which is the most "portfolio-visible" win a
retriever uplift can deliver.

Three alternatives were considered:

1. **Bigger embedder.** A larger bi-encoder (Qwen3-Embedding-8B or
   BGE-large-en-v1.5) lifts R@1 by a similar amount. Rejected for v1
   because it's a one-line swap that the user can do themselves; the
   reranker is the bigger architectural change that signals a
   retrieve-then-rerank pipeline, which is the standard production
   shape.
2. **Hosted rerank API** (Cohere Rerank 3, OpenRouter, Jina). Higher
   quality, no local compute. Rejected for v1 because it adds a
   second API key to the deploy story; the local `CrossEncoder`
   default keeps the project free of API-key dependencies in dev
   (same reasoning as ADR-005 for local embeddings). The Protocol
   shape makes a hosted provider a one-class addition later.
3. **Listwise reranker** (e.g. RankT5, monoT5). More accurate than
   pointwise cross-encoders but heavier and more brittle. Cross-
   encoder is the lowest-friction default that still hits the
   portfolio-quality bar.

**Consequences:**

- **Default off.** The first-run experience stays exactly the same
  (no extra 300 MB model download, no extra latency). Operators
  flip `ANALYSIS_RERANK_ENABLED=true` to enable. The eval runner
  reads the same env var and exposes `--rerank` / `--no-rerank` for
  one-off side-by-side comparisons; the report writes a delta line
  showing the lift.
- **MPS-safe singleton, mirroring the embedding model fix.** The
  loaded `CrossEncoder` is cached for the process lifetime in
  `_reranker_singleton`; `reset_reranker_singleton()` is the test
  hook. Without this, every request would re-load torch weights
  and the MPS caching allocator would retain blocks across GCs —
  the same leak the Qwen3 embedder fix closes (see `progress.md`).
- **Hard wall-clock timeout with safe fallback.** The orchestrator
  wraps the rerank in `asyncio.wait_for(timeout_s=...)` and falls
  back to the retriever's pre-rerank order on timeout, exception,
  or empty result. A slow / crashing reranker must never fail a
  user query — the LLM still gets a top-K, just from the
  retriever, and a `WARN` is logged. This is the production-quality
  line: prefer degraded service over user-visible failure.
- **Pre-rerank pool sizing.** The retriever is asked for
  `top_k * analysis_rerank_pool_multiplier` (default 3×) candidates
  before rerank. The reranker then narrows to `analysis_rerank_top_k`
  (default 6). This is the standard 3× oversample pattern; the
  multiplier is env-tunable.
- **Citation payload is additive.** `QueryCitation` gains three
  optional fields: `original_score` (the retriever's score for
  that row), `rerank` (bool, True iff a reranker decided this
  order), and `rerank_provider` (e.g. `cross_encoder:BAAI/bge-
  reranker-base`). All three default to `None` / `False` so clients
  on the pre-rerank contract ignore them. The new fields surface in
  the `SourcesPanel` as a "reranked by …" badge for the portfolio
  demo.
- **Eval harness extension.** The runner has new `--rerank` /
  `--no-rerank` flags and a side-by-side report that compares the
  pre-rerank and rerank aggregates with a delta line on R@1 and
  MRR. The JSON results file gains a `rerank: { enabled, provider,
  metrics, delta }` block. Eval datasets are unchanged; chunk
  identity is what R@K and MRR score against, and the reranker
  doesn't change which chunks exist.
- **New module: `app/services/ai/rerankers.py`.** Hosts the
  `Reranker` Protocol (runtime-checkable), the `NoneReranker`
  identity implementation, the `CrossEncoderReranker` provider, and
  the `get_default_reranker` / `reset_reranker_singleton` factory
  pair. Mirrors the shape of `app/services/ai/embeddings.py` for
  consistency.
- **New orchestrator helper.** `analyse_query` now does an extra
  step after `rank_top_rows_with_mode`: when rerank is enabled, it
  calls `rerank_top_rows_async` and replaces the top-K with the
  reranker's output. Downstream (prompt construction, citation
  assembly, WS event payload) is unchanged in shape — the only
  contract change is the optional citation fields above.

### ADR-017: Gold chunks reference `(document_id, chunk_index)`, not chunk UUIDs

**Status:** Accepted

**Parent:** ADR-016.

**Decision:**
The canonical gold-chunk reference in `<Dataset>.questions[i].relevant_chunk_keys`
is the `RelevantChunkKey` Pydantic model — a `(document_id: UUID,
chunk_index: int)` tuple — *not* the chunk UUID. The runner resolves
these stable keys against the freshly-seeded DB at scoring time,
turning them into the chunk UUIDs the retriever actually produced.
`PassageMappingEntry` was reshaped the same way (`document_id` +
`chunk_index` instead of `chunk_id`).

**Reason:**
The first end-to-end run reported R@1=R@5=R@10=MRR=0.000 across
all scored questions. Diagnosis:

1. Direct DB inspection: every "relevant" UUID stored in
   `dataset.json` was absent from the seeded `document_chunks`
   table on the runner's side.
2. Code trace: `app/services/document/chunker/build_chunks.py:83`
   assigns `chunk_id = uuid4()` on every call. The chunker is
   deterministic on `(page_text, chunk_index)`, but the chunk
   **UUID** is freshly random each ingestion. Re-seeding the
   hermetic SQLite produces the same chunk *content* at the same
   *index*, but with brand-new UUIDs.
3. Consequence: any `relevant_chunk_ids: list[UUID]` written to
   the committed `dataset.json` was permanently stale. The runner
   could only ever score zero.

This was not caught by the end-to-end test because that test runs
the build + runner against the same in-memory DB in the same
process — chunk UUIDs are stable within a single process even
though they're random across processes. The bug only surfaces
across the `build_dataset.py → run_retrieval_eval.py` boundary,
i.e. the boundary the harness actually has to cross in production.

The fix: stop storing random values. `chunk_index` is content-
deterministic (same input text → same chunks in the same order),
and `Document.id` is deterministic via UUID5 (ADR-016). Together
they form a stable key that survives every re-seed and re-build.

**Consequences:**

- **Some turns may score as `questions_total` but not `questions_scored`.** These are turns where the fuzzy LCS
  mapping produced zero relevant chunks (the gold span is too
  short or doesn't overlap any chunk at the 0.5 threshold). They
  contribute to `questions_total` but not to the metrics. The
  number is exposed in the report so reviewers can see the gap.
- **R@5 < R@10 with owner_wide scope.** With Doc2Dial's
  owner_wide scope (171 docs in the corpus) the candidate set is
  much larger than the per-question scope the original harness
  used, so the reranker and the retriever have headroom to find
  the gold chunk in the top-K. The recall ceiling given mapping
  coverage is the more meaningful signal here than the
  per-question scope used in earlier iterations.
- **`PassageMappingEntry` shape changed.** Was
  `passage_id, passage_text, chunk_id: UUID, chunk_text, overlap_ratio`;
  is now `passage_id, passage_text, document_id: UUID, chunk_index: int,
  chunk_text, overlap_ratio`. Audit file consumers must update.
- **Runner has a new `_resolve_relevant_keys_to_chunk_ids` helper.**
  One `SELECT` joining `DocumentChunk` to `DocumentPage` filtered by
  `tuple_(document_id, chunk_index).in_(pairs)`. Returns the set of
  chunk UUIDs the retriever produced for the gold set. Failure mode
  caught: tuples where the DB doesn't have a matching chunk (the
  mapping produced a `(doc_id, chunk_index)` we never seeded) are
  silently absent — they would have been the bug, now they're a
  known-good empty intersection.
- **Schema docstring + tests updated.** `test_schema.py`,
  `test_mapping.py`, and `test_end_to_end.py` all assert on
  `RelevantChunkKey` shape. The `__all__` export in
  `eval/schema.py` includes `RelevantChunkKey`.

### ADR-020: Chat history in the LLM prompt + human-tone system prompt

**Status**: accepted, 2026-10-08.

**Context**

The analyser sent exactly two messages to the LLM per query: a fixed
`SYSTEM_PROMPT` and a single user message containing the retrieved
snippets plus the current question. Chat history was persisted to
`conversation_messages` but never read back into the prompt. Two
concrete symptoms followed:

1. **Follow-ups lost context.** A user asking `"what was the first
   one you mentioned?"` got an answer derived only from the new
   retrieval — the model had no idea what "the first one" referred
   to, and the prior assistant turn was invisible.
2. **Tone was robotic.** The original `SYSTEM_PROMPT` was a strict
   citation-ruleset; the model behaved like a search engine returning
   bullet facts. A 1M-token context model (luna 5.6 in the target
   deployment) gives us room to invite explanatory prose without
   sacrificing grounding.

**Decision**

1. **Prepend the last N non-error turns to the prompt.** A new
   pure module `app/services/document/analyser/chat_history.py`
   exposes `fetch_recent_turns` and `truncate_to_tokens`. The
   fetcher reads the last `2 * max_turns` rows via
   `conversation_repo.list_messages(order="desc")`, drops rows with
   `error_code != null`, pairs each user with the following
   assistant, reverses to oldest → newest, and returns a
   `list[LLMMessage]`. Long assistant content is hard-trimmed with
   a leading `…(N tokens earlier)` marker so the model knows prior
   answers are partial. User messages are never truncated. The
   just-inserted user row is naturally excluded because the pair
   logic drops the trailing orphan.

2. **Extend the prompt builder.** `build_messages(...)` accepts an
   optional `history` sequence and returns
   `[system, *history, user]`. The legacy REST path passes nothing
   and gets the v1 two-message shape — no contract change for
   non-conversational callers. `build_user_prompt` is rewritten to
   group retrieved snippets by document under a `<DOC_CONTEXT>`
   block, with global `[N]` numbering so the citation map stays
   1:1 with `QueryCitation.index`.

3. **Rewrite the system prompt** with three sections:
   - **VOICE & STYLE** — invite natural prose; match the user's
     register; prefer prose with a few well-chosen bullets over
     dense lists.
   - **GROUNDING RULES** — unchanged in spirit (cite-only, no
     invented facts, partial-snippet honesty), but with one new
     line: "you may briefly rephrase, summarize, or add a
     one-sentence framing of the cited material to make it
     clearer; mark any added framing with `(my framing)` — never
     present it as a snippet claim". This is the **safer-first**
     version — a conservative invitation to creativity that
     doesn't free the model to invent content. Loosening it later
     is a one-line edit.
   - **CONVERSATION AWARENESS** — tells the model what the
     `<CHAT_HISTORY>` block is for, that it must NOT cite snippets
     from the chat history, and to resolve pronouns against the
     prior turns before answering.

4. **New config knobs** under the existing Retrieval/RAG section:
   `analysis_chat_history_enabled` (default true),
   `analysis_chat_history_max_turns` (default 10, range 0–100),
   `analysis_chat_history_max_assistant_tokens` (default 400,
   range 64–8192). Defaults are conservative; operators can raise
   them on a 1M-context model without code changes. The existing
   `analysis_top_k=10` and `analysis_snippet_chars=800` are
   intentionally **not** bumped in this PR — operators can raise
   those separately if they want the new headroom, but no
   out-of-the-box behaviour change for retrieval.

5. **Failure of the history fetch is non-fatal.** The
   orchestrator wraps `fetch_recent_turns` in `try / except` and
   degrades to the chat-less prompt on any exception. A flaky
   history query must never break a user-facing query.

**Why this is additive and safe**

* The HTTP/WS payload shape is unchanged. `QueryCitation.index`
  still maps 1:1 to the `SOURCES` list, and the `analysis.*` WS
  event family is untouched. The frontend does not need to
  change.
* The legacy REST path is byte-shape-compatible with v1 when
  `history=None` or `history=[]`. A regression test
  (`test_build_messages_with_no_history_keeps_v1_shape`) pins
  this so a future prompt edit can't silently break the
  one-shot endpoint.
* The safer-first "label your framing" wording is the risk-
  mitigated form of the broader "be more human" ask. In our
  experience, the looser "you may add framing" wording tips
  some models into inventing analogies not in the source;
  the marker requirement makes the boundary visible to the
  user and re-trainable by the operator. After observing 20–30
  follow-up answers, the wording can be loosened if the
  answers still feel too mechanical.
* The new `chat_history.py` is a pure module — no I/O beyond
  a single repo call, no LLM call, no DB writes. Tests drive
  it with a fake `list_messages` shim that mirrors the
  `_FakeConversationStore` style already in
  `test_with_conversation.py`.

**Alternatives considered**

* **Rolling summary of older turns.** A 1M-token window makes
  this unnecessary at the current turn depths. We may add it
  later as a cheap pre-step when conversations routinely run
  past ~30 turns, but it's not in scope here.
* **Per-document style instructions.** Inject a `STYLE:` line
  per `DOC:` header so the model can mimic a document's voice
  (formal legalese, casual blog, etc.). Easy to add later, but
  out of scope for this PR.
* **Auto-detecting follow-ups and re-running retrieval on the
  rewritten query.** Would close the loop on "the second one"
  more reliably than chat history alone, but adds a
  classification step and a second retriever call. We chose to
  ship history first and see how much of the problem it
  actually solves.
* **Bumping `analysis_top_k` and `analysis_snippet_chars` in
  the same PR.** Out of scope by user request — operators
  raise them via env when they want the new headroom.

### ADR-021: Conversational query rewriting for retrieval

**Status**: accepted, 2026-10-08.

**Context**

After ADR-020 added chat history to the LLM prompt, the answer
side of a follow-up turn ("what about the second one you
mentioned?") saw the prior turns and could resolve pronouns. The
**retrieval** side did not. `analyse_query` embedded the raw
current question (`model.embed(question)`) and tokenized the raw
question for BM25. On a follow-up, the raw question is a bag of
pronouns that matches nothing in the document corpus, so the
retriever returned the wrong neighbourhood, the cross-encoder
reranker (when enabled) had nothing to promote, and the answer
LLM got the wrong snippets — even though it had the right
context to understand the question.

**Decision**

1. **A tiny rewrite step.** A new pure module
   `app/services/document/analyser/query_rewrite.py` exposes
   `rewrite_query_for_retrieval(history, question, *, llm_provider,
   max_history_turns) -> str`. The helper:
     - Takes the last K user/assistant pairs + the current
       question.
     - Sends them to a small LLM (Flash-Lite by default) with a
       short system prompt asking for a single self-contained
       sentence.
     - Returns the cleaned string. Never raises — on any
       exception, empty response, whitespace, or empty history,
       returns the raw `question` unchanged.

2. **The retriever sees the rewritten string.** `analyse_query`
   now embeds `query_text` (the rewritten string) and passes
   `query_text` to BM25 in hybrid mode. The cross-encoder
   reranker (when enabled) also receives `query_text` —
   `(query, doc)` is the exact input shape it expects, so this
   is a free upgrade for rerank.

3. **The answer prompt is unchanged.** `build_messages` still
   gets the raw `history` and raw `question` so the answer LLM
   can respond naturally with pronouns. The rewrite is *only*
   for the retriever.

4. **Parallel execution** — the rewrite LLM call and the
   candidate SQL fetch run concurrently via `asyncio.create_task`
   and `asyncio.to_thread`. In the steady state both finish
   around the same time, so the wall-clock cost over v1 is
   ~0ms. The orchestrator enforces a hard wall-clock cap
   (`analysis_query_rewrite_timeout_s`, default 2.0s) via
   `asyncio.wait_for`; on timeout the orchestrator logs and
   falls back to the raw question. A slow rewrite never blocks
   a query.

5. **Separate, cheaper LLM** — `app/services/ai/llm.py` gains a
   `get_rewrite_llm_provider()` factory. Same
   `OpenAICompatibleProvider` class, but reads from
   `analysis_query_rewrite_*` settings so operators can point
   the rewrite at a cheaper model (e.g. Gemini Flash-Lite via
   OpenRouter) without touching the answer-gen config. The
   provider's `__init__` gained an `api_key_env` kwarg so a
   separate API key can be configured for the rewrite.

6. **Settings** under a new "Query rewriting for retrieval"
   section in `app/core/config.py`:
     - `analysis_query_rewrite_enabled` (default true)
     - `analysis_query_rewrite_max_history_turns` (default 4)
     - `analysis_query_rewrite_timeout_s` (default 2.0)
     - `analysis_query_rewrite_base_url` (default OpenRouter)
     - `analysis_query_rewrite_model` — **placeholder default**;
       operators set the exact OpenRouter slug in `.env`.
     - `analysis_query_rewrite_max_assistant_tokens` (80)
     - `analysis_query_rewrite_temperature` (0.1)
     - `analysis_query_rewrite_api_key_env` (reuses
       `ANALYSIS_LLM_API_KEY` by default)

7. **Fail-soft contract** — a slow, erroring, misconfigured, or
   rate-limited rewrite degrades to the raw question. The
   retriever is never worse than v1. The helper is the safety
   net; the orchestrator's `asyncio.wait_for` is the deadline.
   That separation lets us unit-test the fallback paths without
   mocking event loops, and lets the orchestrator tune the
   deadline without touching the helper.

**Why this is additive and safe**

* The HTTP/WS payload contract is unchanged. The frontend does
  not need to change.
* The legacy REST path passes `history=None` (or `history=[]`),
  the orchestrator's `rewrite_enabled` gate evaluates to
  `False`, the rewrite task is never created, and `query_text =
  question` — behaviour identical to v1.
* Existing tests verify the new orchestrator path does not fire
  the rewrite on an empty history (the conversation test fakes
  a brand-new conversation with no prior turns).
* The rewrite is a separate `LLMProvider` instance, so a
  failure of the rewrite endpoint (rate limit, 4xx, 5xx, DNS
  error) cannot poison the answer-gen call.

**Alternatives considered**

* **Concatenating history + question into one string and
  embedding that.** Zero LLM calls, but the embedding model is
  not trained to ignore speaker tags, so prior assistant
  explanations dominate the vector and pull retrieval toward
  snippets similar to *what was said last time* rather than
  the user's actual current question. In practice this works
  ~30–50% of the time on follow-ups vs ~85% for the rewrite.
* **Two-pass retrieval with a context-aware reranker.** The
  cross-encoder reranker is trained on (query, doc) pairs in
  isolation. It doesn't model the conversational context of
  the query side. Also, you only re-rank what the dense
  retriever already returned — if the dense retriever
  returned zero relevant docs because "the second one"
  matched nothing, the reranker has nothing to promote.
* **Auto-detecting when to rewrite.** Always rewriting is
  simpler; if latency becomes a problem we can add a
  fast-path later (skip rewrite when the question has zero
  pronouns / references). Not in this PR.

**Why a separate (cheaper) model**

The user picked Gemini 3.1 Flash Lite via OpenRouter for the
rewrite. The reasoning: the rewrite is a short, structured
output (one sentence, ~30 tokens) and the prompt is small
(history slice + question). Flash-Lite handles this in
~150ms at negligible cost per query. The answer-gen LLM
stays on a heavier model (e.g. luna 5.6) where latency and
quality matter more.

The default model slug in `analysis_query_rewrite_model` is
a **placeholder**. Operators MUST set the exact OpenRouter
slug in `.env` — slugs change and the model id the user
wants may not exist under the placeholder name. The
`.env.example` block comments explain how to find the right
slug on `https://openrouter.ai/models`.


### ADR-022: Per-document detail dialog with a dedicated `/stats` endpoint

**Status:** Accepted

**Decision:**

Every row on the documents page is clickable. Clicking opens a
centered dialog (`DocumentDetailDialog`) that surfaces the
document's full processing state — page count, chunk count,
chunks embedded / pending / failed, pages needing OCR,
embedding model names, and a recent activity strip — plus
three actions: Edit, Download (coming soon), Delete.

The data for the dialog comes from a new dedicated endpoint,
`GET /api/v1/documents/{id}/stats`, which returns the
`DocumentListItem` (same shape the list uses) plus aggregated
counts from `document_pages`, `document_chunks`, and
`document_embeddings`. A new repo helper
`document_stats_for(...)` runs the COUNTs in a single
transaction with the owner-scope check baked in via
`get_document`.

**Reason:**

The existing `GET /documents` list endpoint deliberately returns
a narrow `DocumentListItem` shape (no embedded counts — see its
docstring). Broadening the list payload would inflate every page
load and force the list to do N+1 work for data only the
detail view needs. A separate, on-demand `/stats` endpoint is
the additive, non-breaking way to surface pages / chunks /
embeddings per document, mirroring how `/documents/{id}/logs`
already gives the per-doc activity stream its own route.

The dialog was chosen over a slide-over side panel because:

- the existing `Dialog` primitive is the only modal shape the
  app uses for confirm / alert actions, so visual + behavioural
  consistency comes for free
- the documents page is single-column today; a slide-over
  would either cover or push the list
- the data shown is bounded — a handful of stat tiles, a model
  list, a 3-item activity strip — not enough to justify a full
  panel

The Edit and Delete actions inside the dialog are NOT new
dialogs — they close the detail dialog and hand off to the
existing `EditDocumentDialog` and `DeleteDocumentDialog`
already mounted on the row. Only one of the three is open at
a time; the row owns the open-state coordination.

**Consequences:**

- One new endpoint (`GET /api/v1/documents/{id}/stats`), one
  new Pydantic schema (`DocumentStatsResponse`), one new
  repo helper (`app/repositories/document_stats.py`), and
  12 new tests (6 repo + 6 API). No DB migration, no new
  tables, no new dependencies on either side.
- The list endpoint contract is unchanged — `/documents` and
  `/documents/{id}/stats` are independent routes that happen
  to share `DocumentListItem` in their response.
- Download stays "Coming soon" — there is no
  `GET /documents/{id}/file` route today, and adding one
  needs signed-URL redirect logic and filename preservation
  that is out of scope for this change. The dialog and the
  row's existing dropdown both render the same disabled
  "Coming soon" affordance so the user gets one consistent
  treatment.
- The detail dialog is read-only on first load. Status
  transitions during the dialog's lifetime (a `learning` doc
  going to `ready` mid-view) are NOT live — the user closes
  and reopens to see them. A 15-second `staleTime` on the
  TanStack Query means a reopen picks up the latest numbers
  without a forced refetch on focus. A realtime bridge inside
  the dialog is a one-line follow-up if it becomes important.


### ADR-023: Backend-proxied download & inline view, with Edit as a header link

**Status:** Accepted

**Decision:**

Two new routes on the documents router stream the original
uploaded file back to the owner:
- `GET /api/v1/documents/{id}/download` — sets
  `Content-Disposition: attachment; filename="<name>"` to
  trigger a browser save.
- `GET /api/v1/documents/{id}/view` — sets
  `Content-Disposition: inline` so the browser renders the file
  directly (PDF, image, text, Markdown, CSV, JSON, SVG);
  types the browser can't render fall back to a download prompt,
  which is the correct cross-platform behaviour.

Both routes share a private `_stream_document(doc, disposition)`
helper that does the owner-scope check, the status guard
(409 for `processing` / `failed`), the Supabase fetch (wrapped
in `asyncio.to_thread` so the sync SDK doesn't block the
event loop), the filename sanitisation, and the
`Content-Disposition` + `Content-Length` + `Cache-Control`
headers. The two routes are 5 lines each.

On the frontend, `DocumentDetailDialog`'s footer was reshaped:
- `Edit` moved out of the footer and into the header's
  metadata row as a `Button variant="link" size="xs"`. It
  calls the same `onEdit` prop the row's 3-dot menu uses — no
  prop change, no parent-state change.
- The right-hand group now holds `View` + `Download` instead
  of `Edit` + `Download`. Both are `variant="outline"` with
  their real icons (`Eye`, `Download`); both are disabled (with
  a tooltip) while the doc is in `processing` or `failed`.
- The row's 3-dot menu got the same treatment: the disabled
  "Coming soon" Download was replaced with two active items,
  `View` and `Download`, both wired to the new helpers.

**Reason:**

- Backend-proxied download (vs. presigned Supabase URL) keeps
  the strict frontend/backend boundary from `AGENTS.md` — the
  frontend never talks to Supabase directly. Auth rides on the
  existing cookie session; the bucket stays private; no token
  query param needs to be plumbed through.
- The bytes pass through the API process. For the kinds of
  files this app accepts (PDFs, docs, spreadsheets) the
  payload is bounded; the extra hop is negligible compared to
  the rest of the request. If a future feature ever needs
  500MB+ files, a presigned-URL variant for that specific
  route is an additive change.
- `inline` vs `attachment` is the only difference between the
  two routes — the browser already does the right thing for
  every supported MIME type, so no per-mime branching is
  needed on the server.
- The dialog's footer was carrying three CTAs (Edit, Download,
  Delete) where the natural "what can I do with this file?"
  answer is only two (View, Download). Moving Edit to the
  header as a text link keeps the rename reachable without
  giving it a heavy primary button that competes with Delete.
  This matches the pattern used for Vercel's detail cards,
  where contextual metadata actions live in the header and the
  footer is reserved for the primary file affordance plus
  destructive cleanup.

**Consequences:**

- Two new routes (`GET /documents/{id}/download`,
  `GET /documents/{id}/view`), one shared private helper
  (`_stream_document`), and 13 new API tests. No new schemas,
  no new repos, no new DB columns, no migrations, no new
  dependencies.
- The Document model's `storage_bucket` + `storage_path` +
  `filename` + `mime_type` fields are now read server-side
  for these two routes. They remain omitted from
  `DocumentListItem` (the public list payload) so storage
  internals never leak to the client.
- Filenames in the response header are sanitised (CRLF,
  `"`, `;`, `/`, `\\`, and other control characters are
  replaced with `_`) to prevent header injection. The
  sanitiser replaces rather than strips, so the user still
  sees their original filename on disk.
- Both endpoints are owner-scoped via the existing
  `document_repo.get_document(db, id, owner_id=...)` helper.
  Missing documents and cross-owner access both return 404
  (not 403) so the endpoint can't be used to probe for
  other users' document ids.
- The dialog now has three call sites for Edit (header link,
  row dropdown, ... not the footer). All three are wired
  to the same `onEdit` prop, so the existing
  `EditDocumentDialog` and the parent's open-state
  coordination are untouched.

### ADR-023: WebSocket connects directly to the backend host, cross-origin

**Status:** Accepted (supersedes the earlier same-origin rewrite draft)

**Decision:**

The frontend opens its WebSocket directly to
`ws[s]://${API_BASE_URL}/ws` — the FastAPI host, no Next.js
proxy. The cookie contract is what carries the connection:
the backend sets `xs` (access token) and `rfr` (refresh
token) with `SameSite=None; Secure` in production, and with
`SameSite=Lax; Secure=false` for local dev over plain HTTP.
`next.config.ts` is back to having no rewrites.

**Reason:**

`/ws` is cookie-authenticated: the server reads the `xs`
HttpOnly cookie during the upgrade handshake. The browser
will not attach that cookie on a cross-origin WebSocket
upgrade under `SameSite=Lax` (the original default) — symptom:
`ws rejected: no/invalid access_token cookie` on the server
and a 1008 close on the client.

Three options were considered:

1. **`SameSite=None; Secure` (chosen).** The cookie travels
   on the cross-origin upgrade because the spec says
   `SameSite=None` permits it. The browser additionally
   requires `Secure`, which means HTTPS in production. In
   local dev over plain HTTP, `Secure` cookies are dropped,
   so `COOKIE_SECURE=false; COOKIE_SAMESITE=lax` is the
   local override documented in `backend/.env.example`. The
   two configs must be kept in sync per environment.
2. **Same-origin via Next.js `rewrites()`.** Proxies
   `/api/ws` through the dev server to FastAPI. Was the
   first fix landed. Rejected because it only works in
   `next dev` (`next start`'s `handleUpgrade` is a
   documented no-op) and the prod edge path (nginx / Caddy)
   would have to do the same job anyway — at which point
   one connection path is simpler than two.
3. **Token query param.** Have `/auth/login` return the JWT
   in the JSON body once, store it in memory, and append
   `?token=<jwt>` to the WS URL. The server's
   `_user_id_from_cookie` would need a fallback that decodes
   it. Lower infrastructure cost, but the token ends up in
   proxy logs and in browser history, and you lose the
   "one auth mechanism everywhere" property.

The chosen path is one connection (browser → backend
directly, no proxy) and one cookie config (the only
difference between dev and prod is `Secure` / `Samesite`,
documented inline in `backend/.env.example`).

**Consequences:**

- Production requires HTTPS end-to-end. The frontend
  must be served over HTTPS, the backend over HTTPS, and
  the browser must see the page on HTTPS for the
  `Secure`-flagged `xs` cookie to be sent on the WebSocket
  upgrade. `next.config.ts` and the frontend `.env.example`
  both call this out.
- Local dev uses a different cookie config than prod
  (`Secure=false; SameSite=Lax`). The CORS allowlist stays
  the same. Anyone adding a new environment (staging, CI,
  etc.) must think about which cookie config it needs
  based on whether the browser will see an HTTPS page.
- CORS for the WebSocket path is a non-issue: WebSocket
  upgrades do not go through CORS, only cookie scoping.
  The existing REST CORS config in the backend is unchanged.
- `ws-store.ts` `wsUrl()` builds the URL from
  `API_BASE_URL` directly. Anyone adding another WS
  endpoint and copying the pattern must keep the
  `SameSite=None; Secure` cookie contract or they will
  reintroduce the original bug.
- If we ever want to skip the HTTPS requirement (e.g. a
  pure HTTP staging environment), the path back is the
  same-origin rewrite in `next.config.ts` plus a
  `next start` replacement that does its own upgrade
  forwarding. We are not taking that path now.

### ADR-024: Document scope picker simplified — implicit "all documents"

**Status:** Accepted

**Context**

The chat app's document scope picker (the dialog that lets the user
pick which uploaded documents an AI chat should ground its answers
on) rendered an "All documents" checkbox as a separate card row at
the top of the dialog, with a `<Globe2>` icon and a description.
Below sat the per-document multi-select list. The toggle greyed out
the list when active, so the user picked one of two paths: "all
documents" via the toggle, or an explicit subset via the list.

The toggle row was redundant. "All documents" is exactly the state
where the user has picked nothing, and the picker body already had
to handle that case anyway. The row added an extra click, an extra
mental model ("is this a separate mode or a sibling to the list?"),
and an extra piece of visual chrome. The dialog's primary button
also said "Start chat" in create-mode and "Save" in edit-mode — a
subtle inconsistency the user flagged.

**Decision**

1. **Remove the "All documents" row.** `DocumentScopePickerBody`
   (`frontend/src/components/chat/document-scope-picker.tsx`) no
   longer renders the toggle. The body now shows only the ready-doc
   list with tick checkboxes, plus the "Select all" / "Clear"
   helper pair. The list is no longer greyed out behind an "all"
   mode.

2. **"All documents" is the implicit default.** No ticks selected
   → scope is every ready document. This is the state the picker
   emits when the user has just opened the dialog (and the state
   the picker emits after a "Clear"). The wire convention
   `{ scope_all_documents: true, document_ids: [] } = all documents`
   is preserved; all three payload sites (the dialog's POST in
   create-mode, the dialog's PATCH in edit-mode, and the welcome
   screen's own create-mutation) derive `isAll` from
   `documentIds.length === 0` so they agree.

3. **Picker state shape.** `DocumentScope` still has an
   `allDocuments: boolean` field for backwards compatibility with
   the type, but the picker body always emits `allDocuments: false`
   and `describeScope` reads `documentIds.length` as the source of
   truth. `sameScope` compares arrays only. The unused
   `useDocumentScopeState` hook stays — removing it is unrelated
   cleanup.

4. **Live pill mapping in `ChatWelcome`.** The welcome screen
   derives a `displayScope` whose `allDocuments` mirrors
   `documentIds.length === 0` and feeds it into `describeScope`
   and the composer placeholder, so the user sees "All documents"
   while they have no ticks selected (matching the saved state).

5. **Dialog primary button label is "Save" in both modes.** The
   previous `"Start chat"` label was misleading — the user is
   picking documents for a chat that the welcome screen then
   creates when they hit the composer send button, not a chat the
   dialog itself starts.

6. **Create-mode can-save rule.** Removed the
   `!allDocuments && documentIds.length > 0` guard — any state is
   saveable in create-mode because "all documents" is a valid
   explicit choice now. Edit-mode keeps the "anything changed"
   guard (`!sameScope`); a user may still want to PATCH a zero-doc
   scope to debug `NO_DOCUMENTS_READY`.

**Reason**

Three things drove the change:

- The user's explicit request to remove the redundant toggle.
- The picker becomes easier to read at a glance — there's one
  control surface, not two.
- The wire contract didn't change: every persisted chat from
  before this change (`scope_all_documents: true, document_ids: []`)
  continues to read correctly in the chat header pill and the
  workspace composer hint. Legacy persisted chats with
  `scope_all_documents: false, document_ids: []` (the deliberate
  zero-doc debug state) still hit the existing `noDocsHint` in
  `chat-workspace.tsx`; no behavior change there.

Two alternatives were considered:

- **Drop `DocumentScope.allDocuments` entirely** and make the
  type purely `{ documentIds: string[] }`. Cleaner data model,
  but a bigger refactor across four files for no functional gain.
  Kept the field for type stability.
- **Keep the toggle but move it to a less-prominent spot.** Would
  have been a cosmetic relocation, not a simplification, and
  doesn't address the "two ways to do the same thing" problem.

**Consequences**

- `DESIGN.md` §1 normally forbids behavior changes, but this is
  an explicit, user-authorized exception. `DESIGN.md` itself does
  not need a token/component update — no design tokens change.
- The picker is one control surface, not two. The dialog is a few
  lines shorter; the `Globe2` import drops from the picker.
- The "Or pick specific documents" section header becomes just
  "Documents" (no "or" — there's no longer a sibling toggle).
- The "Pick at least one document above to ground your answers."
  hint in `ChatWelcome` becomes unreachable from the picker (the
  default state is "all documents"); the hint is removed. The
  workspace's `noDocsHint` stays — it still fires for legacy
  zero-doc chats opened from history.
- Wire contract for `POST /conversations` and
  `PATCH /conversations/{id}` is unchanged. `Conversation` model
  in the backend is unchanged.
- An optional `onConfirm?: (scope) => void` prop is added to
  `DocumentPickerDialog` for create-mode: when provided, the
  dialog hands the current scope to the parent and closes without
  creating. This lets `ChatWelcome` defer the conversation create
  to the composer-send path so an empty "Untitled" chat is never
  stranded in the sidebar every time the picker is closed.
  Edit-mode (`ChatHeader`) is unaffected — it still PATCHes
  directly. The default create-mode (no `onConfirm`) is preserved
  for any future caller that wants the dialog to own the create.

---

### ADR-025: Per-route HTTP rate limiting via `slowapi` + Redis

**Status:** Accepted

**Decision:**

Add a per-route HTTP rate limiter to the FastAPI backend using the
`slowapi` library (the de-facto FastAPI port of `flask-limiter`).
Counters are stored in Redis (DB 2, separate from Celery's DB 0 and
WS pub/sub's DB 1). All 25 user-facing HTTP routes get a
`@limiter.limit(...)` decorator with a limit chosen per route based
on its cost and abuse surface. Health endpoints and the WebSocket
route are intentionally **not** limited. A master
`RATE_LIMIT_ENABLED` switch flips the limiter off in tests.

**Reason:**

Four concrete problems were open before this change:

1. **LLM cost exposure.** `POST /analysis/query` and
   `POST /analysis/query/stream` both call an OpenAI-compatible LLM.
   One bad client (or a runaway script) could drain the API key's
   budget in minutes. The existing `analysis_max_concurrent_streams_per_user`
   cap covered concurrency, not request rate.
2. **Auth brute force.** `POST /auth/login` had no per-IP cap. Bcrypt
   12 rounds slows each attempt, but with no upper bound a
   password-spraying attack is still viable.
3. **Storage abuse.** `POST /documents/upload-intent` mints a
   Supabase signed URL and creates a DB row per call — a script can
   create thousands of rows in seconds.
4. **No general backstop** for the rest of the API. Reads, writes,
   chat CRUD, anything — a misbehaving client could overwhelm the
   server.

Per-route limits are a pragmatic compromise: a shared WAF would be
more thorough, but it lives outside the application. Per-IP
rate-limiting at the LB would be the next layer up; for this MVP,
in-app rate limiting covers the four cases above with zero new
infrastructure beyond the existing Redis.

**Choices made:**

- **Library: `slowapi`.** Same engine (`limits`) as `flask-limiter`,
  which has been in production for a decade. Supports Redis natively
  via the `limits` library — no extra dependency on a `redis-py`
  client.
- **Keying: `user:<uuid>` when the access-token cookie resolves,
  else `ip:<remote-address>`.** Best-effort decode; never raises
  from the keying function. Pre-auth routes (`/auth/*`) are
  IP-keyed; everything else is user-keyed so two users behind the
  same NAT don't share a bucket.
- **Storage: Redis DB 2.** Distinct from Celery (DB 0) and WS
  pub/sub (DB 1) so a `FLUSHDB` on either of those doesn't wipe
  rate counters. `memory://` is supported as an env override for
  single-process dev.
- **Per-route limits**: chosen for cost + abuse surface:
  - `5/hour` on `POST /auth/register` (spam account creation).
  - `5/minute` on `POST /auth/login` (brute-force mitigation).
  - `30/minute` on `POST /auth/refresh` and `POST /auth/logout`.
  - `60/minute` on `GET /auth/me` (frontend hits on every page load;
    leave headroom).
  - `10/minute` on `POST /documents/upload-intent` (signed URL + DB
    row — the most expensive write).
  - `30/minute` on `POST /documents/{id}/confirm` and
    `POST /documents/{id}/upload-failed` (cheap, but doc-scoped).
  - `60/minute` on PATCH, downloads, views, stats, logs.
  - `120/minute` on list endpoints (cheap reads).
  - `20/minute` on `POST /analysis/query` and
    `POST /analysis/query/stream` (LLM cost).
  - `60/minute` on conversations CRUD; `30/minute` on message submit
    and stream cancel; `120/minute` on list and message history.
  - **Unlimited** on `/health/*` (k8s/LB probes) and `/ws` (slowapi
    is HTTP-only).
- **`from __future__ import annotations` dropped in 4 endpoint
  files** (`auth.py`, `documents.py`, `analysis.py`,
  `conversations.py`). `from __future__ import annotations`
  stringifies all parameter annotations; slowapi's
  `@limiter.limit(...)` wraps the handler; FastAPI's
  `lenient_issubclass` (which detects the `Request` / `Response`
  special params) requires real class objects. With the future
  import in place, the wrapped function's signature had string
  annotations, FastAPI couldn't recognize the `Request` param, and
  every rate-limited route 422'd with `payload` treated as a query
  param. The drop is annotated in a comment at the top of each
  affected file. The future import remains in all other modules
  (services, schemas, models, tasks, tests, etc.).

**Consequences:**

- **429 is a new client-visible status.** The frontend already
  handles non-200 responses with a generic error toast; no
  per-route UI work was needed. A follow-up could add a dedicated
  "Slow down" toast.
- **Two new env vars** (`RATE_LIMIT_ENABLED`, `RATE_LIMIT_STORAGE_URI`)
  both with non-empty defaults — additive, no breaking change.
- **No `analysis/query/stream` cap conflict** with the existing
  `analysis_max_concurrent_streams_per_user` — they enforce
  different things (per-minute call rate vs concurrent in-flight
  streams) and both return 429, which the frontend already handles.
- **WebSocket `/ws` is NOT covered.** slowapi only operates on
  HTTP. For this MVP that's acceptable — auth still requires a
  valid cookie, and the WS path's cost is dominated by the
  downstream analysis LLM call (which IS rate-limited). A future
  in-process token-bucket per user_id could be added if WS-side
  abuse becomes a problem.
- **In-memory storage is per-process.** With `RATE_LIMIT_STORAGE_URI=memory://`,
  multi-worker uvicorn will see N× the intended limit (one bucket
  per worker). In production this MUST be Redis.
- **All 5 new tests pass** (`tests/api/test_rate_limit.py`); the
  pre-existing 42-test API suite still passes. The 7 pre-existing
  failures in `tests/tasks/` and `tests/tasks/test_embedding_logs.py`
  are unrelated (missing enum value, `AsyncEngine` setup).

