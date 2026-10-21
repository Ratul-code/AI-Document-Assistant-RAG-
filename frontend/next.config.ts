import type { NextConfig } from "next";

/**
 * Next.js config.
 *
 * No rewrites. The frontend reaches the backend directly via
 * `NEXT_PUBLIC_API_BASE_URL` for both REST (`/api/v1/*`) and the
 * WebSocket (`/ws`). The WebSocket upgrade carries the `xs` access
 * token because the cookie is set with `SameSite=None; Secure` on the
 * backend (see backend/app/core/config.py) — browsers attach that
 * cookie to cross-site upgrades as long as the request is HTTPS.
 *
 * Earlier this config proxied `/api/ws` through the Next dev server
 * to dodge the cross-origin cookie drop. With the cookie flipped to
 * `SameSite=None; Secure`, the proxy is unnecessary in dev too, so
 * the rewrite was removed to keep one connection path across all
 * environments. See ADR-023.
 */
const nextConfig: NextConfig = {
  // Standalone output emits a self-contained `server.js` plus a
  // minimal `node_modules` tree under `.next/standalone/`. The
  // production Dockerfile only copies that directory into the
  // runtime image (no `npm install` at runtime, no full
  // node_modules in the image). Dev still works unchanged —
  // `next dev` ignores the standalone output.
  output: "standalone",
  /* config options here */
};

export default nextConfig;