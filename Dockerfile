# =============================================================================
# AI Document Analyser — backend image
#
# One image, three jobs:
#   * api    → uvicorn app.main:app --host 0.0.0.0 --port 8000
#   * worker → celery -A app.core.celery_app worker --loglevel=info
#   * beat   → celery -A app.core.celery_app beat   --loglevel=info
#
# The default CMD runs the API. docker compose overrides CMD for the
# worker and beat services — no separate images needed.
#
# Multi-stage build (BuildKit required for cache mounts):
#
#   ┌────────────┐      ┌────────────┐      ┌──────────────┐
#   │  ml-deps   │ ───▶ │  builder   │ ───▶ │    final     │
#   │ torch,     │      │ rest of    │      │ slim runtime │
#   │ sentence-  │      │ backend    │      │ + venv + app │
#   │ trans-     │      │ deps       │      │ + tini       │
#   │ formers,   │      │            │      │ + non-root   │
#   │ tiktoken   │      │            │      │   user       │
#   └────────────┘      └────────────┘      └──────────────┘
#
# Why three stages?
#   * `ml-deps` isolates the slow, ~1-2 GB install of torch +
#     sentence-transformers + tiktoken into a layer that is reused
#     across every build unless those specific pins change. Editing
#     requirements.txt (e.g. bumping fastapi) does NOT bust this layer.
#   * `builder` reuses the ML layer and only installs the lightweight
#     backend stack. It is a separate stage so that fastapi/celery/
#     sqlalchemy changes don't bust the ML install either.
#   * `final` is a clean slim runtime image with NO compilers, NO
#     pip cache, NO build tools. Only the finished virtualenv and the
#     application source. Minimal CVE surface.
#
# BuildKit features used:
#   * `RUN --mount=type=cache,target=/root/.cache/pip` — pip's HTTP
#     download cache persists across `docker build` invocations so
#     wheels are downloaded once per machine, not once per build.
#   * `COPY --from=ml-deps` — reuse the ML venv layer between stages.
#
# Build with:   DOCKER_BUILDKIT=1 docker build .
#   (BuildKit is the default on Docker 23+; the env var is shown for
#   older daemons.)
# =============================================================================


# -----------------------------------------------------------------------------
# syntax — enable BuildKit-only features (cache mounts, --mount=from).
# -----------------------------------------------------------------------------
# syntax=docker/dockerfile:1.7


# -----------------------------------------------------------------------------
# Stage 1 — ml-deps
# -----------------------------------------------------------------------------
# Installs ONLY the heavy ML stack into /opt/venv. Pinned versions here
# are the slowest-changing layer in the project, so isolating them
# gives the best Docker layer cache hit rate.
FROM python:3.12-slim AS ml-deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# We need gcc / build-essential in THIS stage only — wheels for torch's
# C++ extensions aren't always published for every Python patch version
# on every platform, so we keep a fallback path to compile from sdist.
# These don't end up in the final image because the final stage starts
# from a fresh slim image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# Copy ONLY the ML requirements file. The main requirements.txt
# changes more often and lives in the next stage.
COPY backend/requirements-ml.txt ./requirements-ml.txt

# BuildKit cache mount: pip's HTTP download cache persists across
# builds on this machine, so we don't re-download torch (~800 MB)
# every time. `--mount=type=cache` is a BuildKit-only feature.
# `--no-cache-dir` is intentionally NOT set here — we WANT the cache
# to be populated so subsequent cache-mount runs hit it. The final
# image doesn't carry the cache over because the venv is a self-
# contained set of installed packages.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip wheel && \
    pip install --progress-bar on -r requirements-ml.txt


# -----------------------------------------------------------------------------
# Stage 2 — builder
# -----------------------------------------------------------------------------
# Reuses the ML venv from `ml-deps` and layers the rest of the backend
# stack on top of it. Splitting this stage from `ml-deps` means a
# change to fastapi/celery/sqlalchemy versions does NOT invalidate the
# multi-GB ML layer above.
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Same compiler toolchain rationale as ml-deps: some backend wheels
# (psycopg, lxml, python-docx, pillow) need a C compiler as a
# fallback for the rare platform where a wheel isn't published.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Reuse the ML venv from the previous stage. This is the whole point
# of having a separate ml-deps stage: we get torch/sentence-transformers
# pre-installed, free, in seconds (just a tar copy).
COPY --from=ml-deps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# Copy the full requirements tree. requirements.txt `-r`s
# requirements-ml.txt, but everything ML is already in /opt/venv so
# pip is smart enough to skip those installs and only fetch the
# remaining (lightweight) backend deps. The cache mount covers the
# rest.
COPY backend/requirements.txt       ./requirements.txt
COPY backend/requirements-ml.txt    ./requirements-ml.txt

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip wheel && \
    pip install -r requirements.txt


# -----------------------------------------------------------------------------
# Stage 3 — final
# -----------------------------------------------------------------------------
# Clean slim runtime image. NO compilers, NO headers, NO pip cache.
# We carry over only the finished virtualenv and the application code.
FROM python:3.12-slim AS final

LABEL maintainer="AI Document Analyser" \
      description="Backend image: FastAPI api + Celery worker + Celery beat"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Virtualenv path — the same /opt/venv we built in earlier stages.
    PATH="/opt/venv/bin:$PATH" \
    # HuggingFace / sentence-transformers cache location. We mount a
    # named volume here so the ~1.2GB model download persists across
    # container restarts instead of being re-downloaded every time.
    HF_HOME=/home/app/.cache/huggingface \
    TRANSFORMERS_CACHE=/home/app/.cache/huggingface \
    SENTENCE_TRANSFORMERS_HOME=/home/app/.cache/huggingface

# Runtime-only system libs. `libpq5` is the Postgres client library
# that psycopg links against. `tini` gives us proper signal handling
# / PID 1 behavior (clean shutdowns, zombie reaping). `curl` is for
# healthchecks. No compilers, no headers.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        tini \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user. Running as root inside a container is a
# common security smell — anything that escapes the process gets
# root on the host. uid/gid 1001 is the standard "first non-system
# user" choice.
RUN groupadd --system --gid 1001 app && \
    useradd  --system --uid 1001 --gid app --home-dir /home/app --shell /bin/bash app && \
    mkdir -p /home/app/.cache/huggingface && \
    chown -R app:app /home/app

# Copy the fully-populated virtualenv from the builder stage. This is
# the whole point of multi-stage: we get installed packages without
# build tools. `--chown` keeps the venv owned by root (it's read-
# only at runtime; the app user just runs things out of it via PATH).
COPY --from=builder --chown=root:root /opt/venv /opt/venv

WORKDIR /app

# Copy backend source AFTER the venv so the dependency layer is
# established first. The entrypoint script also goes here so a
# bind-mount of /app/backend in dev (compose.dev.yaml) can overlay
# the python source without clobbering the entrypoint.
COPY --chown=app:app backend/ ./backend/
COPY --chown=app:app backend/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER app

# WORKDIR is /app (set above) so the entrypoint's `cd /app` and
# `cd /app/backend` resolve. `app.*` imports resolve because we set
# PYTHONPATH=/app/backend in docker-compose / docker run.
EXPOSE 8000

# Healthcheck: curl the OpenAPI doc endpoint. FastAPI returns 200
# once the app has finished loading (lifespan startup included).
# The schema is at `/openapi.json` (the root), NOT under `/api/v1/...`
# — the /api/v1 prefix is for our hand-written routers; FastAPI's
# auto-generated schema always lives at the root.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8000/openapi.json || exit 1

# tini as PID 1 for proper signal handling. The entrypoint script
# then runs migrations and execs the actual command.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]

# Default command: run the API. Worker and beat override this in
# compose.
CMD ["api"]
