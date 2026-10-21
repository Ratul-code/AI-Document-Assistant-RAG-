/**
 * /chat — the chat workspace.
 *
 * Two render modes, picked by the `?c=<uuid>` search param:
 *
 *   - No `?c`              → `<ChatWelcome>` (start a new chat).
 *   - `?c=<uuid>` present  → server-fetch the conversation via
 *                            `fetchConversation`. If it 404s we fall
 *                            back to the welcome state. Otherwise we
 *                            hand the freshly-loaded conversation to
 *                            `<ChatWorkspace>` as initial data so the
 *                            first render is fully populated.
 *
 * The first-question handoff from `<ChatWelcome>` is in-memory
 * (`usePendingQuestionStore`), not a `?q=<text>` URL param — keeps
 * the URL clean and avoids a React 19 strict-mode double-submit.
 * The page itself is RSC; the chat workspace is a client component
 * that mounts its own WS subscription and TanStack Query.
 */

import { ChatWelcome } from "@/components/chat/chat-welcome";
import { ChatWorkspace } from "@/components/chat/chat-workspace";
import { fetchConversation } from "@/lib/auth/server";

type SearchParams = Promise<{
  c?: string | string[];
}>;

export default async function ChatPage({
  searchParams,
}: {
  searchParams: SearchParams;
}) {
  const params = await searchParams;
  const raw = params.c;
  const conversationId = Array.isArray(raw) ? raw[0] : raw;

  // No `?c` → welcome state.
  if (!conversationId) {
    return <ChatWelcome />;
  }

  // Validate it's a UUID. Anything else (random query string) falls
  // back to the welcome state — we never want to hit the API with a
  // malformed id and surface the 404 as the page error.
  const uuidLike =
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  if (!uuidLike.test(conversationId)) {
    return <ChatWelcome />;
  }

  const conversation = await fetchConversation(conversationId);
  if (!conversation) {
    // 404 — deleted, or owned by someone else. Treat it the same way
    // as the no-`?c` case so the user can start a new chat.
    return <ChatWelcome />;
  }

  // The workspace reads the pending question from
  // `usePendingQuestionStore` on mount. We don't pipe it through as a
  // prop anymore — the store is the single source of truth.
  return <ChatWorkspace initialConversation={conversation} />;
}