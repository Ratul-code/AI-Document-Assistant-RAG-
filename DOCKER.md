# Docker — AI Document Analyser Backend

This doc explains how the Docker setup works and how to operate it. It's a
cheat sheet, not architecture — for the *why*, see `docs/decisions.md`.

---

## TL;DR

```bash
# 1. Make sure Docker Desktop is running
docker info

# 2a. Development (bind-mounts source, uvicorn --reload)
docker compose -f compose.yaml -f compose.dev.yaml up --build

# 2b. Production (versioned image, no bind mounts)
IMAGE_TAG=v1.4.2 docker compose -f compose.yaml -f compose.prod.yaml up -d

# 3. Hit the API
curl http://localhost:8000/api/v1/openapi.json

# 4. Stop everything (keeps data)
docker compose -f compose.yaml -f compose.dev.yaml down

# 5. Nuke everything (deletes volumes — loses redis cache, postgres data)
docker compose -f compose.yaml -f compose.dev.yaml down -v
```

Docker Compose v2 (the default on all modern installs) automatically
picks up `compose.yaml` as the project file, so plain `docker compose up`
works as expected when you only have `compose.yaml` present. To enable
dev or prod overlays, use the `-f` form as shown above.

---

## What runs in Docker

| Service    | Image                                   | Port (host) | Job                                |
| ---------- | --------------------------------------- | ----------- | ---------------------------------- |
| `api`      | built/pulled from `./Dockerfile`        | `8000` (dev only) | FastAPI on uvicorn          |
| `worker`   | built/pulled from `./Dockerfile`        | —           | Celery worker (background tasks)   |
| `beat`     | built/pulled from `./Dockerfile`        | —           | Celery beat (scheduler)            |
| `redis`    | `redis:7-alpine`                        | —           | Celery broker + result backend     |
| `postgres` | `postgres:16-alpine`                    | —           | Optional local DB (off by default) |
| `frontend` | built from `./frontend/Dockerfile`      | `3000` (dev only) | Next.js standalone server    |

The frontend lives in the same compose network as the backend and is reached
at `http://api:8000` from inside the `appnet` network. See
[Frontend in Docker](#frontend-in-docker) below.

---

## File map

```
repo-root/
├── Dockerfile                          # multi-stage: ml-deps → builder → final
├── .dockerignore                       # keeps the build context lean
├── compose.yaml                        # shared service definitions
├── compose.dev.yaml                    # dev overlay: bind mount, --reload
├── compose.prod.yaml                   # prod overlay: versioned image, no mounts
├── DOCKER.md                           # this file
├── backend/
│   ├── requirements.txt                # full dep list (includes -r ml)
│   ├── requirements-ml.txt             # heavy ML stack only (torch, sentence-transformers, tiktoken)
│   ├── requirements-eval.txt           # optional eval tooling (not built into image)
│   └── docker-entrypoint.sh            # waits for DB, runs migrations, starts the service
└── frontend/
    ├── Dockerfile                      # multi-stage: deps → builder → runner
    ├── .dockerignore                   # keeps the frontend build context lean
    └── next.config.ts                  # `output: 'standalone'` (consumed by the Dockerfile)
```

---

## How the image works (one image, three jobs)

The Dockerfile builds a single image. The `CMD` determines what runs:

| compose `command:` | what actually starts                                                          |
| ------------------ | ----------------------------------------------------------------------------- |
| `["api"]`          | `uvicorn app.main:app --host 0.0.0.0 --port 8000`                             |
| `["api", "--reload"]` | `uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload` (dev only)       |
| `["worker"]`       | `celery -A app.core.celery_app worker --loglevel=info --concurrency=2`        |
| `["beat"]`         | `celery -A app.core.celery_app beat --loglevel=info`                          |

The `docker-entrypoint.sh` script does the expansion and also runs
`alembic upgrade head` before handing off, so the schema is always
current when the process starts.

---

## How the multi-stage build works

The Dockerfile has three stages, layered so the slow ones are cached
as long as possible:

```
┌────────────┐      ┌────────────┐      ┌──────────────┐
│  ml-deps   │ ───▶ │  builder   │ ───▶ │    final     │
│ torch,     │      │ rest of    │      │ slim runtime │
│ sentence-  │      │ backend    │      │ + venv + app │
│ trans-     │      │ deps       │      │ + tini       │
│ formers,   │      │            │      │ + non-root   │
│ tiktoken   │      │            │      │   user       │
└────────────┘      └────────────┘      └──────────────┘
```

* **`ml-deps`** installs the heavy ML stack (torch ~800 MB,
  sentence-transformers, tiktoken) into `/opt/venv`. This is the
  slowest, most cacheable layer. It is **separate** from the rest of
  the backend deps so that bumping fastapi/celery/sqlalchemy does
  not invalidate the multi-GB ML install.
* **`builder`** reuses the ML venv from `ml-deps` and layers the
  lightweight backend stack on top. This is the second-slowest
  layer.
* **`final`** starts from a clean `python:3.12-slim`, copies only
  the finished virtualenv and the application code, and adds
  runtime-only system libs (`libpq5`, `tini`, `curl`). No
  compilers, no pip cache, no build tools → minimal CVE surface.

### Cache mounts (BuildKit)

Each install stage uses BuildKit's cache mount to keep pip's HTTP
download cache across builds:

```dockerfile
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt
```

This means the first build downloads every wheel; subsequent builds
download only the wheels that aren't already in the local cache.
Cache lives at `~/.cache/pip` (the BuildKit default) and is reused
across all images built on this machine.

**BuildKit is required** for cache mounts. On Docker 23+ (and the
current Docker Desktop) BuildKit is the default — no extra flags
needed. On older daemons, set `DOCKER_BUILDKIT=1`.

### What invalidates which layer

| Edit this…                  | Re-runs `ml-deps`? | Re-runs `builder`? | Re-runs `final`? |
| --------------------------- | ------------------ | ------------------ | ---------------- |
| `backend/requirements-ml.txt` | yes              | yes                | yes (venv copy)  |
| `backend/requirements.txt`    | no               | yes                | yes (venv copy)  |
| `backend/app/**/*.py`         | no               | no                 | yes              |
| `backend/docker-entrypoint.sh`| no               | no                 | yes              |
| `Dockerfile`                  | yes              | yes                | yes              |

In other words: the most common code edit (`*.py`) only invalidates
the `final` stage, which is just a copy of the source tree — fast.

---

## Requirements organization

Dependencies are split into two files so the multi-stage build can
cache them independently:

* **`backend/requirements-ml.txt`** — heavy ML stack: `torch`,
  `sentence-transformers`, `tiktoken`. Imported lazily inside
  `app/services/ai/embeddings.py`, `app/services/ai/rerankers.py`,
  `app/services/document/chunker/__init__.py`, and
  `app/services/document/analyser/chat_history.py`. These are slow
  to install (~1-2 GB on disk) and slow to change.
* **`backend/requirements.txt`** — the rest of the stack: web
  framework, DB, Celery, auth, Supabase, parsing, dev/test tools.
  This file starts with `-r requirements-ml.txt` so a plain
  `pip install -r requirements.txt` still installs everything (so
  local dev workflows are unaffected).

If you add a new dependency:

* If it's ML-related (torch, transformers, scikit-learn,
  sentence-transformers, spacy, etc.) → add it to
  `requirements-ml.txt`.
* Otherwise → add it to `requirements.txt` with a pinned
  `==<version>`.

There is also `backend/requirements-eval.txt` for optional
benchmark / eval tooling. It is **not** installed by the image
build; install it manually if you need it locally.

---

## Compose layout

The Compose configuration is split into three files for clear
separation of dev and prod concerns:

| File                | Purpose                                                    |
| ------------------- | ---------------------------------------------------------- |
| `compose.yaml`      | Shared service definitions (used by both dev and prod)     |
| `compose.dev.yaml`  | Dev overlay: source bind mounts, uvicorn `--reload`        |
| `compose.prod.yaml` | Prod overlay: versioned image, no bind mounts, `APP_ENV=prod` |

Merge them with `-f`:

```bash
# Development
docker compose -f compose.yaml -f compose.dev.yaml up --build

# Production
IMAGE_TAG=v1.4.2 docker compose -f compose.yaml -f compose.prod.yaml up -d
```

If you only have `compose.yaml` and no overlay, plain `docker compose up`
just works — Compose v2 picks it up as the default project file. The
`-f` form above is only needed when you want to layer in dev or prod
behaviour.

### Dev vs prod at a glance

| Setting                | `compose.dev.yaml`                          | `compose.prod.yaml`                              |
| ---------------------- | ------------------------------------------- | ------------------------------------------------ |
| `api.command`          | `["api", "--reload"]`                       | `["api"]`                                        |
| `api.ports`            | `"8000:8000"` (host binding)                | `[]` (no host binding; reverse proxy in front)   |
| `api.build`            | `build:` (rebuilds from local Dockerfile)   | `!reset null` (pulls `ai-doc-analyser-backend:${IMAGE_TAG}`) |
| `api.volumes`          | `./backend:/app/backend` (bind) + `hf_cache` | `hf_cache` only                                  |
| `api.environment.APP_ENV` | `dev`                                    | `prod`                                           |
| `*.restart`            | `unless-stopped`                            | `always`                                         |
| `api.environment.APP_DEBUG` | (default true)                          | `"false"`                                        |

---

## Frontend in Docker

The Next.js frontend is a first-class compose service alongside `api`, `worker`, `beat`, and `redis`. It joins the same `appnet` network so the in-container code can reach the backend at `http://api:8000` without any host-side port juggling.

### Image layout (`frontend/Dockerfile`)

Three stages, mirroring the backend Dockerfile's philosophy:

1. **`deps`** — `npm ci` with `package.json` + `package-lock.json` only. This is the slowest-changing layer.
2. **`builder`** — copies source on top of `deps` and runs `npm run build`. Output goes to `.next/standalone/` because `next.config.ts` sets `output: "standalone"`. This produces a self-contained `server.js` + minimal `node_modules`.
3. **`runner`** — slim `node:20-alpine`, copies only `.next/standalone/`, `.next/static/`, and `public/`. Runs as a non-root `nextjs` user (uid 1001), with `tini` as PID 1 and a `wget` healthcheck. Final image is ~70 MB.

### Build-time vs runtime API URL

`NEXT_PUBLIC_API_BASE_URL` is the only env var the frontend cares about, and it's a **build-time** concern: Next.js inlines `NEXT_PUBLIC_*` values into the JS bundle at `next build` time. They cannot be changed after the image is built without a rebuild.

The compose file passes it as a build arg, defaulting to `http://api:8000` (the in-network hostname of the `api` service):

```bash
# Default — works out of the box in dev:
docker compose -f compose.yaml -f compose.dev.yaml up --build

# Override at build time (e.g. for a hosted deployment):
NEXT_PUBLIC_API_BASE_URL=https://api.example.com \
  docker compose -f compose.yaml -f compose.prod.yaml build frontend
```

For dev, `compose.dev.yaml` overrides the CMD to `next dev -H 0.0.0.0 -p 3000` and bind-mounts `./frontend:/app` (with an anonymous volume over `/app/node_modules` so the host's platform-specific binaries don't clobber the image's install). Edits to `.tsx` / `.ts` files hot-reload.

### Running just the frontend

```bash
# Build and start the whole stack (recommended)
docker compose -f compose.yaml -f compose.dev.yaml up --build

# Frontend alone (assumes backend is reachable from your network)
docker build -f frontend/Dockerfile \
  --build-arg NEXT_PUBLIC_API_BASE_URL=http://localhost:8000 \
  -t frontend-dev ./frontend
docker run --rm -p 3000:3000 frontend-dev
```

Once the dev stack is up, the UI is at `http://localhost:3000` and the API at `http://localhost:8000`.

---

## Environment variables — what comes from where

There are **three sources** of env vars, in priority order:

1. `environment:` block in `compose.yaml` / overlay (highest priority — wins)
2. `env_file: backend/.env` (your existing local secrets)
3. Defaults baked into the code / Dockerfile

The compose file loads `backend/.env` for **everything** and only
overrides the things that are actually different inside Docker:
the Celery broker URL, the WS pub/sub URL, and `APP_ENV`.

| Var                  | Source         | Value                                |
| -------------------- | -------------- | ------------------------------------ |
| `CELERY_BROKER_URL`  | compose        | `redis://redis:6379/0`               |
| `CELERY_RESULT_BACKEND` | compose     | `redis://redis:6379/0`               |
| `REDIS_URL`          | compose        | `redis://redis:6379/1`               |
| `PYTHONPATH`         | compose        | `/app/backend`                       |
| `APP_ENV`            | overlay        | `dev` / `prod` depending on overlay  |
| `DATABASE_URL`       | `backend/.env` | (your Supabase URL — unchanged)      |
| `SUPABASE_*`         | `backend/.env` | (your Supabase keys — unchanged)     |
| `ANALYSIS_LLM_*`     | `backend/.env` | (your LLM keys — unchanged)          |

**Your non-Docker dev workflow is unaffected.** The `.env` file is
read the same way it always was; only the Celery/Redis URLs are
redirected.

---

## How api → redis works (and why it's not a problem)

Inside the compose network, every container has a hostname equal to
its service name. So:

- `redis://redis:6379/0` from `api` → reaches the redis container.
- `redis://redis:6379/0` from `worker` → same container.
- `redis://redis:6379/0` from `beat` → same container.

Docker's built-in DNS resolves these. The api still "sends tasks
directly to redis" — the only thing that changed is the address.

---

## Volumes (data that survives restarts)

| Volume          | Mounted at                                  | Purpose                              |
| --------------- | ------------------------------------------- | ------------------------------------ |
| `redis_data`    | `/data` in the redis container              | Persist queued tasks across restarts |
| `hf_cache`      | `/home/app/.cache/huggingface` in api/worker/beat | Persist the ~1.2GB model download |
| `postgres_data` | `/var/lib/postgresql/data` in postgres      | Local DB data (only if enabled)      |

Wipe them with `docker compose -f compose.yaml -f compose.dev.yaml down -v`.

---

## Common operations

### View logs
```bash
docker compose -f compose.yaml -f compose.dev.yaml logs -f api
docker compose -f compose.yaml -f compose.dev.yaml logs -f worker
```

### Rebuild after a code change
```bash
# In dev: most code edits don't need a rebuild because the source
# is bind-mounted. Just restart:
docker compose -f compose.yaml -f compose.dev.yaml restart api

# If you changed requirements or the Dockerfile:
docker compose -f compose.yaml -f compose.dev.yaml up --build api
```

### Open a shell inside a running container
```bash
docker compose -f compose.yaml -f compose.dev.yaml exec api bash
```

### Run a one-off command
```bash
# Trigger a manual alembic migration check
docker compose -f compose.yaml -f compose.dev.yaml run --rm api alembic current

# Open a Python REPL with the app loaded
docker compose -f compose.yaml -f compose.dev.yaml run --rm api python
```

### Wipe just the redis queue (force workers to redownload tasks)
```bash
docker compose -f compose.yaml -f compose.dev.yaml exec redis redis-cli FLUSHALL
```

### Clean up Docker build cache (frees disk space)
```bash
# Drop stopped containers, dangling images, and the BuildKit cache
# (the BuildKit cache holds downloaded pip wheels; you can keep
# this for faster rebuilds)
docker system prune
docker builder prune        # WARNING: drops the pip wheel cache
```

---

## Switching to a local Postgres (instead of Supabase)

By default the api/worker/beat talk to your Supabase Postgres. To
run everything fully self-contained:

1. Start the local-pg profile:
   ```bash
   docker compose -f compose.yaml -f compose.dev.yaml --profile local-pg up --build
   ```

2. Set `LOCAL_PG=true` in a `.env` file at the repo root (or in
   your shell). The api/worker/beat also need their
   `DATABASE_URL*` vars overridden to point at `db:5432` — do
   this in `compose.dev.yaml`'s `environment:` block.

3. To migrate existing Supabase data → local pg, use `pg_dump` /
   `psql`. Out of scope for this doc.

---

## VPS deployment (when you're ready)

The production stack is **Caddy + api + worker + beat + frontend +
redis**. Caddy is the only service published to the host — it
terminates TLS automatically and routes by path:

```
https://yourdomain.com/api/v1/*  ->  api:8000
https://yourdomain.com/ws        ->  api:8000
https://yourdomain.com/*         ->  frontend:3000
```

Everything else lives on the private `appnet` Docker network.

### Prerequisites

- A VPS with **at least 4 GB RAM** — the backend image carries
  ~1.2 GB of PyTorch + sentence-transformers, and you are running
  Postgres/Redis, the API, a Celery worker, beat, and the frontend.
- A domain whose **DNS A record already points at the VPS's public
  IP**. Caddy needs this to complete the Let's Encrypt challenge;
  without it Caddy will not start.
- Ports 80 and 443 open in the VPS firewall (80 is needed for the
  ACME HTTP-01 challenge, 443 serves the site).

### One-time setup

```bash
# 1. SSH into the VPS
ssh you@your-vps

# 2. Install Docker (if not already)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker you   # log out and back in after this

# 3. Install your project
git clone <your-repo-url>
cd "AI Document Assistent"

# 4. Create the deploy-time env file (domain + public API URL).
#    Copy the template and edit it:
cp .env.example .env
nano .env
#    → DOMAIN=yourdomain.com
#    → NEXT_PUBLIC_API_BASE_URL=https://yourdomain.com

# 5. Create the app's env file (secrets). See backend/.env.example
#    for the full list of what the app needs. The essentials:
nano backend/.env
#    → APP_ENV=prod
#    → APP_DEBUG=false
#    → JWT_SECRET=<python -c "import secrets; print(secrets.token_urlsafe(48))">
#    → COOKIE_SECURE=true
#    → COOKIE_SAMESITE=lax
#    → CORS_ORIGINS=https://yourdomain.com
#    → DATABASE_URL=... / DATABASE_URL_SYNC=...  (your Postgres)
#    → SUPABASE_URL=... / SUPABASE_SERVICE_ROLE_KEY=...
#    → EMBEDDING_PROVIDER=qwen3      (NOT "stub" — the stub returns
#                                     garbage from retrieval)
#
#    The app REFUSES TO BOOT if any of the critical ones are wrong;
#    the error message names each one.

# 6. Build + start in detached mode
docker compose -f compose.yaml -f compose.prod.yaml -f compose.vps.yaml up -d --build

# 7. Verify
docker compose -f compose.yaml -f compose.prod.yaml -f compose.vps.yaml ps
docker compose -f compose.yaml -f compose.prod.yaml -f compose.vps.yaml logs -f caddy
curl https://yourdomain.com/api/v1/health/live
```

**The first boot takes 10–20 minutes** — the image builds from
scratch and the `hf_cache` volume downloads the Qwen3 embedding model.
Subsequent rebuilds are much faster.

### Deploying a change (the routine)

```bash
# On your Mac
git push

# On the VPS
cd "AI Document Assistent"
git pull
docker compose -f compose.yaml -f compose.prod.yaml -f compose.vps.yaml up -d --build
```

Your `backend/.env` on the VPS is **not** touched by `git pull` — it
is gitignored, so your secrets stay put across deploys.

### The three compose layers

| Layer | Purpose |
|---|---|
| `compose.yaml` | Shared service definitions (both dev and prod) |
| `compose.prod.yaml` | Prod overrides: no bind mounts, `restart: always`, the `frontend` and `caddy` services, and the required `DOMAIN` / `NEXT_PUBLIC_API_BASE_URL` vars |
| `compose.vps.yaml` | Re-enables local `build:` so the images are built on the VPS instead of pulled from a registry |

`compose.prod.yaml` alone assumes a CI/CD pipeline that pushes images
to a registry. Since no registry is configured yet, layer 3
(`compose.vps.yaml`) is what makes a build-on-the-VPS deploy work.

### Where each variable's value comes from

Docker Compose has two completely different mechanisms, and mixing them
up is the classic source of "why is my variable empty?":

| Mechanism | Syntax | Resolved by | Supports `${VAR}`? |
|---|---|---|---|
| `environment:` | `KEY: value` or `KEY: ${VAR}` | **Compose, on the host**, at `up` time | ✅ Yes |
| `env_file:` | `- path/to/file` | **The container**, at start time | ❌ No |

Concretely, in this stack:

| Variable | Where it's set | Value | Why |
|---|---|---|---|
| `DOMAIN` | root `.env` → `${DOMAIN:?...}` | your domain | Caddy expands `{$DOMAIN}` in the Caddyfile at its own startup |
| `NEXT_PUBLIC_API_BASE_URL` | root `.env` → `${NEXT_PUBLIC_API_BASE_URL:?...}` | your public origin | Baked into the browser bundle **at build time** |
| `API_INTERNAL_BASE_URL` | hardcoded in `compose.prod.yaml` | `http://api:8000` | A Docker-internal name. Correct for every deployment, and never reaches the browser |
| `NODE_ENV` | hardcoded in `compose.prod.yaml` | `production` | Next.js mode — always production on a real deploy |
| `DATABASE_URL`, `JWT_SECRET`, API keys | `backend/.env` via `env_file:` | your secrets | Read by the api / worker / beat containers |

**The `:?` suffix is a guard.** If `DOMAIN` or `NEXT_PUBLIC_API_BASE_URL`
is missing from the root `.env`, Compose refuses to start with a clear
error instead of deploying a broken frontend:

```
error while interpolating services.frontend.environment.NEXT_PUBLIC_API_BASE_URL:
required variable NEXT_PUBLIC_API_BASE_URL is missing a value
```

**Two different env-file syntaxes, two different owners.** Compose's
interpolation is `${VAR}` and reads the **root** `.env`. Caddy's is
`{$VAR}` and reads the **container's** environment (which Compose
populated from the root `.env`). The app's own settings use pydantic's
`env_file=` and read `backend/.env`. Three syntaxes, three files, one
purpose each.

### Why the domain lives in `.env` and not in the code

`Caddyfile` uses `{$DOMAIN}`, which Caddy expands from the
environment at startup. Changing your domain means editing one line
in `.env` and restarting Caddy — no code change, and no rebuild of
the frontend for the *server-side* parts.

**One exception:** `NEXT_PUBLIC_API_BASE_URL` is inlined into the
browser's JavaScript bundle at **build** time, so changing it
requires a rebuild of the frontend image (`up -d --build`). That is
why the prod overlay declares it with Compose's `:?` syntax — the
stack refuses to start rather than shipping a frontend that points
browsers at the Docker-internal `http://api:8000`.

### Production hygiene

- **Log rotation** — configure `/etc/logrotate.d/docker` on the host.
  Docker Desktop does this automatically; a Linux VPS does not.
- **Backups** — if you use the in-stack Postgres, back up the
  `postgres_data` volume. If you use Supabase (the default here),
  Supabase handles that.
- **Redis** — `--maxmemory 256mb` with `allkeys-lru`. If the
  container is under memory pressure Redis can evict Celery tasks and
  rate-limit counters. Raise the limit or switch the policy to
  `noeviction` for a busy deployment.
- **Base image updates** — periodically rebuild to pick up security
  updates in the base images.
- **The `caddy_data` volume must persist** — it holds the ACME
  account key and the TLS certificates. If it is wiped, every restart
  re-requests certificates and burns Let's Encrypt rate limits.

---

## Troubleshooting

### "Connection refused" on the first `up`
- First boot is slow (5–10 min) because it downloads Python deps,
  torch, sentence-transformers. Watch
  `docker compose -f compose.yaml -f compose.dev.yaml logs -f api` —
  you'll see pip output. Don't kill it.

### "alembic.util.exc.CommandError: Can't locate revision"
- The DB URL might be wrong. Check
  `docker compose -f compose.yaml -f compose.dev.yaml exec api env | grep DATABASE`
  to see what the container actually sees.

### "redis.exceptions.ConnectionError: Error 111 connecting to redis:6379"
- The api started before redis was healthy. With `depends_on:
  condition: service_healthy` this shouldn't happen — if it does,
  restart:
  `docker compose -f compose.yaml -f compose.dev.yaml restart api worker beat`.

### Worker keeps dying with "ModuleNotFoundError: No module named 'app'"
- The image wasn't rebuilt after a code change. Run
  `docker compose -f compose.yaml -f compose.dev.yaml up --build worker`.

### Model download takes forever on first run
- That's expected: ~1.2GB. Watch
  `docker compose -f compose.yaml -f compose.dev.yaml logs -f worker` —
  you'll see the progress bar. It only happens once per volume.

### Port 8000 is already in use
- In dev, change the host side in `compose.dev.yaml`'s `ports:`
  list, e.g. `"8080:8000"`.

### "permission denied" on Linux
- Add yourself to the docker group: `sudo usermod -aG docker $USER`,
  then log out and back in.

### "BuildKit required" / cache-mount not working
- Old Docker daemons (< 23) need `DOCKER_BUILDKIT=1` set in the
  environment, or BuildKit enabled in `daemon.json`. On modern
  Docker Desktop, BuildKit is the default.
