"""Application settings, loaded from environment variables / .env file."""

import json
import os
import socket
from functools import lru_cache
from typing import Annotated, Literal, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Centralised application configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    app_env: str = "dev"
    app_debug: bool = True
    log_level: str = Field(
        default="INFO",
        description="Root logger level (DEBUG / INFO / WARNING / ERROR).",
    )

    # Database
    database_url: Optional[str] = Field(
        default=None,
        description=(
            "Primary DB URL. If empty, DATABASE_URL_FALLBACK is used. "
            "Examples: postgresql+psycopg://user:pwd@host:5432/dbname  |  "
            "sqlite+aiosqlite:///./app.db"
        ),
    )
    database_url_fallback: str = Field(
        default="sqlite+aiosqlite:///./app.db",
        description="Used when DATABASE_URL is empty.",
    )

    # Sync URL for Alembic. You may keep the same as async by
    # using psycopg (sync): postgresql+psycopg://...
    database_url_sync: Optional[str] = None

    # --- Auth / JWT ---
    # Used to sign access tokens. In production, generate a long random secret.
    jwt_secret: str = Field(
        default="dev-secret-change-me",
        description="HS256 secret for signing access tokens.",
    )
    jwt_algorithm: str = Field(
        default="HS256",
        description="JWT signing algorithm.",
    )
    access_token_ttl_seconds: int = Field(
        default=900,  # 15 minutes
        description="Access token lifetime in seconds.",
    )
    refresh_token_ttl_seconds: int = Field(
        default=60 * 60 * 24 * 7,  # 7 days
        description="Refresh token lifetime in seconds.",
    )

    # --- Cookie ---
    cookie_secure: bool = Field(
        default=False,
        description="Set Secure flag on cookies (true in production).",
    )
    cookie_samesite: str = Field(
        default="lax",
        description="SameSite attribute for auth cookies.",
    )
    refresh_cookie_name: str = Field(
        default="rfr",
        description="Name of the refresh-token cookie.",
    )
    access_cookie_name: str = Field(
        default="xs",
        description="Name of the access-token cookie.",
    )


    celery_broker_url:str = Field(
        default="redis://localhost:6379/0",
        description = "Celery broker URL (Redis)."
    )
    celery_result_backend: str = Field(
      default="redis://localhost:6379/0",
      description="Celery result backend URL.",
  )

    # --- WebSocket pub/sub ---
    # Redis URL used for the WS event bus. Separate DB from Celery so
    # pub/sub traffic doesn't share a connection pool with task queues.
    redis_url: str = Field(
        default="redis://localhost:6379/1",
        description="Redis URL for the WS event pub/sub channel.",
    )
    ws_pubsub_channel: str = Field(
        default="ws:events",
        description="Single global channel the worker PUBLISHes to and "
                    "every FastAPI instance SUBSCRIBEs to. Used as the "
                    "fallback channel when ws_b3_enabled is False.",
    )
    # --- B3 multi-worker WebSocket settings ---
    # Per-FastAPI-process identifier. Defaults to "<hostname>-<pid>" so
    # dev uvicorn --reload gets a fresh id. Prod can pin a stable
    # WORKER_ID via env so a rolling restart doesn't churn the registry.
    worker_id: str = Field(
        default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}",
        description="Unique per-FastAPI-process tag. Used to derive the B3 pub/sub channel name.",
    )
    # Per-worker channel pattern. {worker_id} is filled from settings.worker_id.
    ws_pubsub_channel_pattern: str = Field(
        default="ws:events:{worker_id}",
        description="Per-worker pub/sub channel template. The {worker_id} placeholder is filled from settings.worker_id.",
    )
    # Worker registry. Each FastAPI process SADDs its worker_id here on
    # startup and refreshes via a keepalive task. Publisher enumerates
    # this to fan out to live workers only.
    ws_worker_registry_key: str = Field(
        default="ws:workers",
        description="Redis SET key holding live worker_id values.",
    )
    ws_worker_registry_ttl_s: int = Field(
        default=60,
        description="TTL on the worker registry SET, refreshed by the keepalive.",
    )
    ws_worker_keepalive_s: int = Field(
        default=20,
        description="How often each worker refreshes its registry membership (must be < ttl).",
    )
    # Back-compat flag. When False, the subscriber and publisher fall
    # back to the v1 single global channel. Set to False for a one-line
    # rollback to the pre-migration single-process behavior.
    ws_b3_enabled: bool = Field(
        default=True,
        description="Enable the B3 broadcast-fanout multi-worker WebSocket path.",
    )
    # --- CORS ---
    # Accepts a JSON array string from the environment, e.g.:
    #   CORS_ORIGINS='["http://localhost:3000","http://localhost:5173"]'
    # Or a comma-separated list, e.g.:
    #   CORS_ORIGINS=http://localhost:3000,http://localhost:5173
    # The fields are typed as `str` here so pydantic-settings doesn't try
    # to JSON-decode the env value (that auto-decoding happens before our
    # validator and breaks comma-separated lists). We expose the parsed
    # list via the `*_parsed` properties below.
    cors_origins: Optional[str] = Field(
        default="http://localhost:3000,http://127.0.0.1:3000",
        description="Allowed origins (comma-separated or JSON array).",
    )
    cors_allow_credentials: bool = Field(
        default=True,
        description="Whether to allow credentials (cookies, Authorization) cross-origin.",
    )
    cors_allow_methods: Optional[str] = Field(
        default="*",
        description="Allowed HTTP methods (comma-separated, JSON array, or '*').",
    )
    cors_allow_headers: Optional[str] = Field(
        default="*",
        description="Allowed request headers (comma-separated, JSON array, or '*').",
    )

    # --- Supabase Storage ---
    # Used to generate presigned upload URLs and store user-uploaded documents.
    # The service-role key is SECRET: never expose it to the client.
    supabase_url: Optional[str] = Field(
        default=None,
        description="Supabase project URL, e.g. https://abc.supabase.co",
    )
    supabase_service_role_key: Optional[str] = Field(
        default=None,
        description="Supabase service-role key (server-only, never sent to clients).",
    )
    supabase_storage_bucket: str = Field(
        default="doc-uploads",
        description="Bucket name where user-uploaded documents are stored.",
    )

    # --- Embeddings ---
    # Pluggable embedding model selected by EMBEDDING_PROVIDER. The worker
    # only knows the EmbeddingModel Protocol, so adding a provider means
    # a new class in services/ai/embeddings.py + a branch in
    # get_default_embedding_model().
    #
    #   "stub"  → DeterministicStubModel (default; for tests + CI).
    #   "qwen3" → Qwen3-Embedding-0.6B via sentence-transformers (local).
    embedding_provider: str = Field(
        default="qwen3",
        description='Embedding provider key: "stub" or "qwen3".',
    )
    embedding_model_name: str = Field(
        default="Qwen/Qwen3-Embedding-0.6B",
        description="Hugging Face model id used by sentence-transformers.",
    )
    embedding_device: str = Field(
        default="cpu",
        description='Torch device for inference: "cpu" or "mps" (Apple Silicon).',
    )
    embedding_dim: int = Field(
        default=1024,
        description="Output vector size. Must match the loaded model "
                    "(Qwen3-Embedding-0.6B = 1024).",
    )

    upload_max_size_bytes: int = Field(
        default=50 * 1024 * 1024,  # 50 MB
        description="Maximum allowed file size for uploads (bytes).",
    )
    upload_allowed_mime_types: Optional[str] = Field(
        default=(
            "application/pdf,"
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
            "text/plain,"
            "text/markdown"
        ),
        description=(
            "MIME types accepted by the upload-intent endpoint. "
            "Must match what Supabase bucket is configured to allow."
        ),
    )

    # --- Retrieval / RAG (ADR-013) ---
    # Tunables for the question/answer pipeline. The HTTP handler reads
    # these on every request, so updates take effect without a restart.
    analysis_top_k: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum chunks surfaced to the LLM per query.",
    )
    analysis_snippet_chars: int = Field(
        default=800,
        ge=100,
        le=4000,
        description="Per-chunk character budget in the prompt.",
    )
    analysis_max_context_tokens: int = Field(
        default=3500,
        ge=500,
        le=16000,
        description="Soft cap on tokens fed to the LLM (rough heuristic).",
    )
    analysis_min_cosine: float = Field(
        default=0.0,
        ge=-1.0,
        le=1.0,
        description=(
            "Drop chunks below this cosine score (0 disables the gate). "
            "Use negative values to keep more candidates."
        ),
    )

    # --- Chat history in the prompt (companion to retrieval) ---
    # When `analyse_query_with_conversation` runs, it prepends the
    # most-recent non-error turns to the prompt so follow-up
    # questions can resolve pronouns and build on prior answers.
    # Disabled ⇒ zero history messages; the user prompt is identical
    # to the v1 (chat-less) prompt except for the new DOC_CONTEXT
    # framing. Tokens are deliberately a soft cap, not a hard one —
    # the formatter truncates assistant content but never splits a
    # user message in half.
    analysis_chat_history_enabled: bool = Field(
        default=True,
        description=(
            "Master switch for prepending prior turns to the LLM "
            "prompt. When false, the orchestrator behaves like v1: "
            "only the current question + retrieved snippets reach "
            "the model."
        ),
    )
    analysis_chat_history_max_turns: int = Field(
        default=10,
        ge=0,
        le=100,
        description=(
            "Maximum NUMBER OF USER+ASSISTANT PAIRS fetched into the "
            "prompt. The user turn being submitted is excluded so "
            "the model never sees its own in-flight question as a "
            "prior turn. Error rows are filtered out before counting. "
            "0 disables history."
        ),
    )
    analysis_chat_history_max_assistant_tokens: int = Field(
        default=400,
        ge=64,
        le=8192,
        description=(
            "Per-assistant-message token budget. Long answers are "
            "hard-trimmed with a leading '…(N tokens earlier)' "
            "marker so the model knows evidence is partial. User "
            "messages are never truncated."
        ),
    )
    # --- Retrieval mode (ADR-018) ---
    # `hybrid` = dense top-K + BM25 top-K → Reciprocal Rank Fusion
    # (recommended; lifts MRR on the Doc2Dial eval corpus). `dense` =
    # cosine-only (legacy v1 path; safe fallback if BM25 is
    # misbehaving in prod).
    # Override via ANALYSIS_RETRIEVAL_MODE=dense|hybrid in the env.
    analysis_retrieval_mode: Literal["hybrid", "dense"] = Field(
        default="hybrid",
        description=(
            "Default retriever. `hybrid` (recommended) = dense top-K + "
            "BM25 top-K → RRF. `dense` = cosine-only legacy path."
        ),
    )
    analysis_hybrid_dense_k: int = Field(
        default=15,
        ge=5,
        le=100,
        description="Hybrid: dense leg top-K before RRF.",
    )
    analysis_hybrid_bm25_k: int = Field(
        default=15,
        ge=5,
        le=100,
        description="Hybrid: BM25 leg top-K before RRF.",
    )
    analysis_hybrid_rrf_k: int = Field(
        default=60,
        ge=1,
        le=1000,
        description="Hybrid: Reciprocal Rank Fusion constant k (Cormack 2009 default 60).",
    )

    # --- Reranker (ADR-019) ---
    # Post-retrieval reordering: dense/BM25 → RRF (or cosine-only) surfaces
    # a pool of candidates, the reranker rescores the pool and returns the
    # final K that the LLM sees. Off by default so first-run demos don't
    # pay the model download cost. Cross-encoder BAAI/bge-reranker-base is
    # the default provider (~300 MB, runs on CPU/MPS).
    analysis_rerank_enabled: bool = Field(
        default=False,
        description=(
            "Master switch for the post-retrieval reranker. When false, "
            "the orchestrator returns the retriever's top-K unchanged."
        ),
    )
    analysis_rerank_provider: Literal["cross_encoder", "none"] = Field(
        default="cross_encoder",
        description=(
            'Reranker provider. "none" is the identity no-op (always '
            'returns the pre-rerank order). "cross_encoder" uses a '
            'sentence-transformers CrossEncoder.'
        ),
    )
    analysis_rerank_model: str = Field(
        default="BAAI/bge-reranker-base",
        description=(
            "HF model id for the cross-encoder reranker. Default is "
            "BAAI/bge-reranker-base (English, ~300 MB, solid baseline)."
        ),
    )
    analysis_rerank_device: str = Field(
        default="cpu",
        description='Torch device for inference: "cpu" or "mps" (Apple Silicon).',
    )
    analysis_rerank_top_k: int = Field(
        default=6,
        ge=1,
        le=20,
        description=(
            "Final K returned by the reranker. This is what the LLM sees. "
            "Must be <= settings.analysis_top_k (the pre-rerank pool cap)."
        ),
    )
    analysis_rerank_pool_multiplier: int = Field(
        default=3,
        ge=1,
        le=10,
        description=(
            "Pre-rerank pool size = analysis_top_k * this. The retriever "
            "surfaces up to (top_k * multiplier) chunks; the reranker "
            "narrows to analysis_rerank_top_k. Capped at len(candidates)."
        ),
    )
    analysis_rerank_max_length: int = Field(
        default=512,
        ge=64,
        le=2048,
        description="Max tokens per (query, doc) pair fed to the cross-encoder.",
    )
    analysis_rerank_batch_size: int = Field(
        default=16,
        ge=1,
        le=128,
        description="Batch size for the cross-encoder forward pass.",
    )
    analysis_rerank_timeout_s: float = Field(
        default=5.0,
        ge=0.1,
        le=60.0,
        description=(
            "Hard wall-clock cap on one rerank call. On timeout the "
            "reranker falls back to the pre-rerank order (warns, never "
            "fails the query)."
        ),
    )

    # --- LLM provider (analysis) ---
    # The key is NOT stored here. We read it from os.environ[llm_api_key_env]
    # inside get_llm_api_key() so it never appears in model_dump() / logs.
    analysis_llm_provider: Literal["openai_compatible"] = Field(
        default="openai_compatible",
        description="LLM provider key. Only openai_compatible is supported today.",
    )
    analysis_llm_model: str = Field(
        default="gpt-4o-mini",
        description="Model name sent in the chat-completions request.",
    )
    analysis_llm_base_url: str = Field(
        default="https://api.openai.com/v1",
        description=(
            "Base URL for the OpenAI-compatible chat-completions endpoint. "
            "Point this at OpenRouter (https://openrouter.ai/api/v1) to use "
            "any model hosted there — the wire format is identical."
        ),
    )
    # NAME of the env var that holds the API key. The key itself is
    # read at call time via os.environ and is never stored on this
    # Settings object.
    #
    # The field is named `..._key_env` (not `..._api_key`) so that
    # pydantic-settings does NOT collide with the env var holding the
    # actual secret. If this were called `analysis_llm_api_key`,
    # pydantic-settings would read `ANALYSIS_LLM_API_KEY` from the
    # environment into the field, replacing the env-var NAME with the
    # secret VALUE — and `get_llm_api_key()` would then look up
    # `os.environ["<the-secret>"]`, which is always unset.
    analysis_llm_api_key_env: str = Field(
        default="ANALYSIS_LLM_API_KEY",
        description=(
            "Name of the env var that holds the API key. The key itself "
            "is read at call time via os.environ and is never stored on "
            "this Settings object."
        ),
    )
    # OpenRouter (and a few other gateways) recommend extra attribution
    # headers so they can show your app on their leaderboard / rate-limit
    # you fairly. Both default to empty, which is fine for plain
    # OpenAI. Set them in .env when pointing at OpenRouter.
    analysis_llm_http_referer: Optional[str] = Field(
        default=None,
        description=(
            "Value sent as the `HTTP-Referer` header on each LLM call. "
            "OpenRouter uses this to attribute traffic to your app."
        ),
    )
    analysis_llm_x_title: Optional[str] = Field(
        default=None,
        description=(
            "Value sent as the `X-Title` header on each LLM call. "
            "OpenRouter shows this on its dashboard."
        ),
    )
    analysis_llm_request_timeout_s: float = Field(
        default=60.0,
        ge=1.0,
        le=600.0,
        description="Per-request timeout for the LLM HTTP call.",
    )
    analysis_llm_max_output_tokens: int = Field(
        default=800,
        ge=64,
        le=8192,
        description="max_tokens sent in the chat-completions request.",
    )
    analysis_llm_temperature: float = Field(
        default=0.2,
        ge=0.0,
        le=2.0,
        description="Sampling temperature for the LLM.",
    )
    # Concurrency cap for in-flight analysis streams per user. The cap
    # protects the worker pool and the LLM provider's rate limit.
    analysis_max_concurrent_streams_per_user: int = Field(
        default=3,
        ge=1,
        le=32,
        description="Hard cap on simultaneous analysis streams per user.",
    )

    # --- Query rewriting for retrieval (ADR-021) ---
    # The retriever embeds the user's current question and (in
    # hybrid mode) tokenizes it for BM25. On a follow-up turn
    # ("what about the second one you mentioned?") both legs miss
    # because the raw question is a bag of pronouns. The rewrite
    # step is a tiny LLM call that takes the last K turns + the
    # current question and returns a self-contained, decontextualised
    # query — what gets embedded and what gets tokenized. The
    # answer-generation prompt is unchanged: it still sees the raw
    # history so it can answer with pronouns.
    #
    # Default off so first-run demos don't pay the extra LLM hop.
    # Operators flip ANALYSIS_QUERY_REWRITE_ENABLED=true once the
    # rewrite model slug below is confirmed on their OpenRouter
    # dashboard. The rewrite runs concurrently with the candidate
    # fetch via asyncio.create_task, so the wall-clock cost is
    # ~0ms when both are warm.
    analysis_query_rewrite_enabled: bool = Field(
        default=True,
        description=(
            "Master switch for the conversational query rewrite. "
            "When true, the orchestrator fires a cheap LLM call "
            "to decontextualise the current question before "
            "embedding + BM25. When false, the retriever sees the "
            "raw question (v1 behaviour)."
        ),
    )
    analysis_query_rewrite_max_history_turns: int = Field(
        default=4,
        ge=0,
        le=20,
        description=(
            "Maximum NUMBER OF USER+ASSISTANT PAIRS sent to the "
            "rewrite model. Kept small (default 4) because the "
            "rewrite only needs enough context to resolve "
            "pronouns — full chat history is for the answer "
            "prompt, not the rewrite. 0 effectively disables "
            "the rewrite even when the master switch is on."
        ),
    )
    analysis_query_rewrite_timeout_s: float = Field(
        default=2.0,
        ge=0.1,
        le=30.0,
        description=(
            "Hard wall-clock cap on the rewrite LLM call. The "
            "orchestrator enforces this via asyncio.wait_for. On "
            "timeout the orchestrator falls back to the raw "
            "question and logs a warning — a slow rewrite must "
            "never block a query."
        ),
    )
    # The rewrite talks to the same OpenAI-compatible wire format
    # as the answer-gen LLM, but uses its own model + base URL +
    # key env var so operators can pick a cheaper model (e.g.
    # Gemini Flash-Lite via OpenRouter) without touching the
    # answer-gen config.
    analysis_query_rewrite_base_url: str = Field(
        default="https://openrouter.ai/api/v1",
        description=(
            "Base URL for the rewrite LLM's chat-completions "
            "endpoint. Defaults to OpenRouter; set to any other "
            "OpenAI-compatible gateway to use a different "
            "provider."
        ),
    )
    analysis_query_rewrite_model: str = Field(
        # The default is a PLACEHOLDER. Operators MUST set
        # ANALYSIS_QUERY_REWRITE_MODEL to the exact slug from
        # https://openrouter.ai/models — OpenRouter slugs change
        # and the model id you want may not exist under this name.
        default="google/gemini-2.5-flash-lite",
        description=(
            "Model id sent in the rewrite chat-completions "
            "request. Override via .env to the exact OpenRouter "
            "slug for the model you want — e.g. a Gemini "
            "Flash-Lite variant. The placeholder default may not "
            "be live; check the OpenRouter models page."
        ),
    )
    analysis_query_rewrite_max_assistant_tokens: int = Field(
        default=80,
        ge=16,
        le=1024,
        description=(
            "max_tokens for the rewrite call. The rewrite is a "
            "one-line sentence, so 80 is plenty; raising this "
            "wastes tokens and may cause the model to ramble."
        ),
    )
    analysis_query_rewrite_temperature: float = Field(
        default=0.1,
        ge=0.0,
        le=2.0,
        description=(
            "Sampling temperature for the rewrite. Kept low so "
            "the rewrite is deterministic-ish; the same question "
            "+ history should produce the same rewritten query."
        ),
    )
    # Name of the env var that holds the rewrite LLM's API key.
    # Defaults to the same env var as the answer-gen LLM
    # (ANALYSIS_LLM_API_KEY). Set this to a different env-var name
    # if you want a separate secret.
    analysis_query_rewrite_api_key_env: str = Field(
        default="ANALYSIS_LLM_API_KEY",
        description=(
            "Name of the env var holding the rewrite LLM's API "
            "key. The key itself is read at call time via "
            "os.environ and is never stored on Settings."
        ),
    )
    analysis_query_rewrite_http_referer: Optional[str] = Field(
        default=None,
        description=(
            "HTTP-Referer header for the rewrite LLM call. Set to "
            "your app's URL on OpenRouter to show up on the "
            "leaderboard."
        ),
    )
    analysis_query_rewrite_x_title: Optional[str] = Field(
        default=None,
        description=(
            "X-Title header for the rewrite LLM call. Shown on "
            "the OpenRouter dashboard."
        ),
    )
    # NOTE: Supabase's create_signed_upload_url hardcodes a 2-hour
    # validity on the returned URL and provides no override. If we ever
    # need an app-level expiry for uploads, track an `expires_at` on the
    # document row and reject stale upload-confirms server-side.

    # --- Rate limiting (slowapi) ---
    # Master switch. When false, the limiter is constructed with
    # `enabled=False` and decorators become no-ops. Use this to disable
    # rate limiting in tests that hammer endpoints in tight loops.
    # Per-route limits are documented in `app/core/rate_limit.py`.
    rate_limit_enabled: bool = Field(
        default=True,
        description=(
            "Master switch for the slowapi-based rate limiter. When "
            "false, all @limiter.limit(...) decorators are no-ops. "
            "Test suites should set this to false in conftest.py."
        ),
    )
    # Storage backend for the limiter counters. We use a separate
    # Redis DB (DB 2) so a Celery-side FLUSHDB on DB 0 or a WS pub/sub
    # flush on DB 1 does not wipe rate counters. Format follows the
    # `limits` library: "memory://" for in-process, "redis://host:port/db"
    # for Redis. In production this MUST point at the same Redis used
    # by Celery + WS pub/sub so all FastAPI workers share a count.
    rate_limit_storage_uri: str = Field(
        default="redis://localhost:6379/2",
        description=(
            "Storage URI for the slowapi rate limiter. Default uses "
            "Redis DB 2 (separate from Celery's DB 0 and WS pub/sub's "
            "DB 1). Set to 'memory://' for a single-process dev setup."
        ),
    )

    # --- Request ID middleware ---
    # Stamps every incoming request with a short unique ID, echoes it
    # back as a response header, and propagates it into every log line
    # emitted while the request is being processed. See
    # `app/core/middleware.py` and `app/core/request_id.py`.
    # Header name follows the de-facto `X-Request-ID` convention used
    # by AWS ALB, GCP Load Balancer, nginx `$request_id`, Heroku,
    # Cloudflare, etc. — any upstream proxy that already mints an ID
    # will pass it through and we'll reuse it (when it looks safe).
    request_id_header: str = Field(
        default="X-Request-ID",
        description=(
            "HTTP header used to read (from the incoming request) and "
            "write (on the outgoing response) the per-request "
            "correlation ID. Standard value is 'X-Request-ID'."
        ),
    )
    # Master switch for benchmarking. Off in normal dev/prod so the
    # ID is always available for log correlation; on in tight-loop
    # load tests where the ContextVar set/reset adds measurable
    # overhead and the log line decoration is wasted work.
    request_id_enabled: bool = Field(
        default=True,
        description=(
            "Master switch for the request-ID middleware. When "
            "false, no ID is minted, no ContextVar is set, and the "
            "response header is omitted. Useful for load tests; off "
            "in normal dev/prod."
        ),
    )

    @field_validator("database_url_fallback")
    @classmethod
    def _ensure_fallback_is_async(cls, v: str) -> str:
        """Ensure the fallback uses an async driver when it's a SQLAlchemy URL."""
        if v.startswith("sqlite:///") and "aiosqlite" not in v:
            return v.replace("sqlite:///", "sqlite+aiosqlite:///")
        return v

    @staticmethod
    def _to_list(v: Optional[str]) -> list[str]:
        """Parse an env var into a list.

        Accepts:
        - None / empty string → []
        - A literal list (e.g. from Python code constructing Settings) → as-is
        - A JSON array string like '["a","b"]' → parsed
        - A comma-separated string like 'a,b,c' → split
        - The literal string "*" → ["*"] (sentinel meaning "all")
        """
        if v is None or v == "":
            return []
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            s = v.strip()
            if s == "*":
                return ["*"]
            # Try JSON first
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return [str(x) for x in parsed]
            except (ValueError, TypeError):
                pass
            # Fall back to comma-separated
            return [item.strip() for item in s.split(",") if item.strip()]
        return [str(v)]

    @property
    def cors_origins_list(self) -> list[str]:
        return self._to_list(self.cors_origins)

    @property
    def cors_allow_methods_list(self) -> list[str]:
        return self._to_list(self.cors_allow_methods)

    @property
    def cors_allow_headers_list(self) -> list[str]:
        return self._to_list(self.cors_allow_headers)

    @property
    def upload_allowed_mime_types_list(self) -> list[str]:
        return self._to_list(self.upload_allowed_mime_types)

    @property
    def effective_database_url(self) -> str:
        return self.database_url or self.database_url_fallback

    @property
    def is_postgres(self) -> bool:
        return self.effective_database_url.startswith("postgresql")

    @property
    def is_sqlite(self) -> bool:
        return self.effective_database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_llm_api_key() -> Optional[str]:
    """Read the LLM API key from os.environ on demand.

    Stored outside the pydantic Settings object on purpose: keys must
    never appear in `Settings().model_dump()`, in logs, or in error
    responses. The env-var name itself is configured via
    `analysis_llm_api_key_env`.

    Returns `None` if the env var is unset. Callers should treat `None`
    as "analysis is not configured" and surface a `PROVIDER_ERROR` /
    503-style failure to the client.
    """
    settings = get_settings()
    return os.environ.get(settings.analysis_llm_api_key_env)