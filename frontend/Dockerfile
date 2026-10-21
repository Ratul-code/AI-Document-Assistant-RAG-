# =============================================================================
# AI Document Analyser — frontend image
#
# Next.js 16 + React 19, App Router, built in standalone mode.
# One image, two jobs:
#   * prod (default CMD) → `node server.js` from .next/standalone,
#                            serving on port 3000.
#   * dev  (compose.dev.yaml overrides) → `next dev` for live reload.
#
# Multi-stage build, similar philosophy to the backend Dockerfile:
#   * `deps`   — installs npm deps. The slowest-changing layer
#                (only invalidates when package.json/lock changes).
#   * `builder`— runs `next build` with NEXT_PUBLIC_* baked in.
#   * `runner` — slim runtime: standalone output + .next/static +
#                public/, non-root user, tini for PID 1.
#
# NEXT_PUBLIC_API_BASE_URL is a build-time argument (Next inlines
# NEXT_PUBLIC_* values into the client bundle at build time). The
# compose file passes `http://api:8000` so the browser can reach the
# backend container by its compose-network hostname.
# =============================================================================


# -----------------------------------------------------------------------------
# syntax — enable BuildKit-only features (cache mounts).
# -----------------------------------------------------------------------------
# syntax=docker/dockerfile:1.7


# -----------------------------------------------------------------------------
# Stage 1 — deps
# -----------------------------------------------------------------------------
# Install ALL deps (dev included) into a dedicated stage. Both the
# builder (needs `next`, `typescript`, etc.) and the runner (when in
# dev) reuse this node_modules. By copying package.json + lockfile
# first, code edits don't bust the install layer.
FROM node:20-alpine AS deps

WORKDIR /app

# libc6-compat is needed by some npm packages on Alpine.
RUN apk add --no-cache libc6-compat

COPY package.json package-lock.json* ./
# `npm ci` is the deterministic counterpart to `npm install`: it
# throws if the lockfile is out of sync and installs exactly the
# pinned versions. Faster on warm caches than `npm install`.
RUN npm ci


# -----------------------------------------------------------------------------
# Stage 2 — builder
# -----------------------------------------------------------------------------
# Runs `next build` with NEXT_PUBLIC_API_BASE_URL supplied as a
# build arg. Next.js inlines NEXT_PUBLIC_* values into the JS bundle
# at build time — the URL is baked into the client code, not read
# at runtime.
FROM node:20-alpine AS builder

WORKDIR /app

RUN apk add --no-cache libc6-compat

# Reuse the populated node_modules from `deps`. Source is copied
# AFTER so package.json changes (re-running the deps stage) don't
# invalidate this layer.
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# The default points at the in-compose backend (`http://api:8000`).
# Override with `--build-arg NEXT_PUBLIC_API_BASE_URL=...` in
# compose.yaml or on the docker build command line. Must be declared
# as both ARG and ENV so `next build` sees it during inlining.
ARG NEXT_PUBLIC_API_BASE_URL=http://api:8000
ENV NEXT_PUBLIC_API_BASE_URL=${NEXT_PUBLIC_API_BASE_URL}

# `next build` is set up to produce standalone output (see
# next.config.ts). TELEMETRY_DISABLED_1 silences Next's anonymous
# telemetry during the build.
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build


# -----------------------------------------------------------------------------
# Stage 3 — runner
# -----------------------------------------------------------------------------
# Production runtime. Slim, non-root, no compilers, no npm cache.
# Just enough to run `node server.js` from the standalone build.
#
# ALSO serves as the dev base: compose.dev.yaml overrides CMD to
# `npx next dev`, which requires `node_modules/.bin/next` to exist.
# In standalone prod builds, Next bundles its own deps into
# `.next/standalone/`, so we technically don't need node_modules for
# prod — but copying them in costs ~30MB and unlocks the same image
# for dev without a separate Dockerfile. Acceptable trade-off.
FROM node:20-alpine AS runner

LABEL maintainer="AI Document Analyser" \
      description="Frontend image: Next.js standalone server, port 3000"

WORKDIR /app

# tini = proper PID 1 / signal handling / zombie reaping.
# wget = used by the HEALTHCHECK to probe the dev server in compose
# (busybox isn't in the alpine base by default; `wget` is the
# smallest option for a one-shot HTTP probe).
RUN apk add --no-cache tini wget libc6-compat

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# Non-root user. uid/gid 1001 matches the convention used in the
# backend Dockerfile.
RUN addgroup --system --gid 1001 nextjs && \
    adduser  --system --uid 1001 --ingroup nextjs --home /app --shell /sbin/nologin nextjs

# Standalone output layout (see Next docs):
#   .next/standalone/  → server.js + minimal node_modules
#   .next/static/      → hashed static assets
#   public/            → unprocessed static files served at /
#
# We also copy `node_modules/` from the builder so the dev override
# (`npx next dev`) can find the `next` CLI binary at
# `node_modules/.bin/next`. Production still launches `node
# server.js` from the standalone bundle (faster startup, fewer
# files), but the extra node_modules copy is what lets the same
# image serve both prod and dev.
#
# ORDER MATTERS: the standalone COPY is done FIRST because the
# standalone bundle contains its own slim `node_modules/` (just
# the runtime deps needed by `server.js`). We then OVERWRITE that
# slim set with the full builder `node_modules/` (which has the
# `next` CLI in `.bin/`). The standalone's `server.js` still
# works because Node's module resolution walks up from the file's
# location and finds `node_modules/` at the project root.
#
# Each COPY invalidates independently, so a source change to
# `public/` doesn't bust the larger server.js or node_modules
# layers.
COPY --from=builder --chown=nextjs:nextjs /app/public ./public
COPY --from=builder --chown=nextjs:nextjs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nextjs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nextjs /app/node_modules ./node_modules

# /app needs to be writable by the nextjs user for dev: `next dev`
# creates .next/ on the fly and Next's file-watcher writes there.
# chown -R is needed because the COPY above created files owned by
# nextjs, but the WORKDIR itself was created by root.
RUN chown -R nextjs:nextjs /app

USER nextjs

EXPOSE 3000

# Probe the root. In standalone mode Next returns 200 once the
# app is up. `/` works for both prod and dev (when this Dockerfile
# is reused by compose.dev.yaml, the override flips CMD to
# `next dev`, and the healthcheck still applies).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD wget -qO- http://127.0.0.1:3000/ >/dev/null 2>&1 || exit 1

# tini as PID 1 for clean signal handling. `node server.js` is the
# standalone entrypoint Next generates.
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["node", "server.js"]
