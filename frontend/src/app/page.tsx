/**
 * Root landing page. The protected post-login experience lives under
 * /(app)/chat; this page simply redirects there so users land in the
 * right place regardless of whether they hit "/" or "/chat".
 */

import { redirect } from "next/navigation";

export default function HomePage() {
  redirect("/chat");
}