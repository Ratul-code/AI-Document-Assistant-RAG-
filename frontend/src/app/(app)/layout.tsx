/**
 * Protected layout for every post-login page.
 *
 * Route protection:
 *   - The middleware in src/proxy.ts is the primary gate. It runs on
 *     the edge, calls /auth/me (and /auth/refresh) on FastAPI, and
 *     redirects to /login if the session is invalid. By the time we
 *     render here, the user is authenticated and the access_token
 *     cookie is fresh.
 *
 * Why we still fetch the user here:
 *   - The sidebar footer needs the user object (name, email) for
 *     display. The middleware doesn't pass data through, so we fetch
 *     it ourselves with the same forward-Cookie pattern.
 *
 * Failure mode:
 *   - If fetchCurrentUser() throws, that means the proxy failed to
 *     authenticate the request — same as an unauthenticated user.
 *     Redirect to /login with the `next` param so they bounce back.
 *
 * Renders:
 *   ┌──────────┬──────────────────────────────────┐
 *   │ Sidebar  │ <main>{children}</main>          │
 *   └──────────┴──────────────────────────────────┘
 */

import { redirect } from "next/navigation";

import { Sidebar } from "@/components/app/sidebar";
import { fetchCurrentUser, } from "@/lib/auth/server";

export default async function AppLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  let user;
  try {
    user = await fetchCurrentUser();
  } catch (err) {
    // Proxy failed to authenticate — treat as unauthenticated and
    // redirect. The middleware should normally catch this earlier;
    // this is the belt-and-braces fallback.
    redirect("/login");
  }

  return (
    <div className="flex h-svh w-full overflow-hidden bg-background">
      <Sidebar user={user} />
      <main className="flex-1 min-w-0 overflow-y-auto">{children}</main>
    </div>
  );
}