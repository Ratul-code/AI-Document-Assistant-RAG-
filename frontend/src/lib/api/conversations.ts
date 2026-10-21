/**
 * Conversations API calls → FastAPI backend.
 *
 * Wire shapes match `backend/app/schemas/conversation.py`. We keep the
 * query-key factory here (mirroring `lib/api/documents.ts`) so all
 * conversation cache keys live in one place.
 *
 * Endpoints used:
 * - GET    /conversations                          → sidebar list (newest first)
 * - POST   /conversations                          → create a chat
 * - GET    /conversations/{id}                     → full metadata + scope
 * - PATCH  /conversations/{id}                     → rename / change scope
 * - DELETE /conversations/{id}                     → drop chat + cascade messages
 * - GET    /conversations/{id}/messages            → paginated history
 * - POST   /conversations/{id}/messages            → submit a question
 *                                                    (returns { stream_id })
 */

import { apiClient } from "@/lib/api/client";

/* ---------- Query keys ---------- */

/**
 * Centralised query-key factory. Invalidation patterns:
 *
 *   - `conversationKeys.all`        → nuke every conversation cache
 *                                    (rare; e.g. logout).
 *   - `conversationKeys.lists()`     → sidebar list, after create/delete.
 *   - `conversationKeys.detail(id)`  → one chat's metadata; invalidated
 *                                    by PATCH and the WS bridge.
 *   - `conversationKeys.messages(id)` → one chat's history; invalidated
 *                                    after submit or after the WS bridge
 *                                    patches in a finalised turn.
 */
export const conversationKeys = {
  all: ["conversations"] as const,
  lists: () => [...conversationKeys.all, "list"] as const,
  list: (params: ListConversationsParams) =>
    [...conversationKeys.lists(), params] as const,
  details: () => [...conversationKeys.all, "detail"] as const,
  detail: (id: string) => [...conversationKeys.details(), id] as const,
  messages: (id: string) =>
    [...conversationKeys.detail(id), "messages"] as const,
  messageList: (id: string, params: ListMessagesParams) =>
    [...conversationKeys.messages(id), params] as const,
};

/* ---------- Wire types ---------- */

export interface ConversationCreateInput {
  title?: string | null;
  document_ids?: string[];
  scope_all_documents?: boolean;
}

export interface ConversationUpdateInput {
  title?: string | null;
  document_ids?: string[] | null;
  scope_all_documents?: boolean | null;
}

export interface Conversation {
  id: string;
  title: string | null;
  scope_all_documents: boolean;
  document_ids: string[];
  created_at: string;
  updated_at: string;
  last_message_at: string;
}

export interface ConversationListItem {
  id: string;
  title: string | null;
  last_message_at: string;
  document_count: number;
}

export interface ConversationListResponse {
  items: ConversationListItem[];
  total: number;
  limit: number;
  offset: number;
}

export type ConversationRole = "user" | "assistant";

/**
 * One citation attached to an assistant message. Same shape as the
 * streaming `analysis.done` payload (`backend/app/schemas/analysis.py`).
 */
export interface ConversationCitation {
  index: number;
  chunk_id: string;
  document_id: string;
  document_title: string;
  page_number: number | null;
  section_path: string[];
  snippet: string;
  score: number;
}

export type ConversationErrorCode =
  | "NO_DOCUMENTS_READY"
  | "PROVIDER_MISMATCH"
  | "PROVIDER_ERROR"
  | "CANCELLED"
  | "INTERNAL"
  | "UNAUTHORIZED";

export interface ConversationMessage {
  id: string;
  role: ConversationRole;
  content: string;
  citations: ConversationCitation[];
  error_code: ConversationErrorCode | null;
  created_at: string;
}

export interface ConversationMessageListResponse {
  items: ConversationMessage[];
  total: number;
  limit: number;
  offset: number;
}

export interface ConversationMessageSubmitInput {
  question: string;
  /** Override the conversation's saved scope for THIS question only. */
  document_ids?: string[] | null;
}

export interface ConversationMessageSubmitResponse {
  stream_id: string;
}

/* ---------- Params ---------- */

export interface ListConversationsParams {
  limit?: number;
  offset?: number;
}

export interface ListMessagesParams {
  limit?: number;
  offset?: number;
  /**
   * Sort direction by `created_at`. The chat workspace uses
   * `order=desc` for the first page so the newest turns render
   * immediately, then `order=asc` for subsequent pages as the user
   * scrolls up and older history is prepended.
   */
  order?: "asc" | "desc";
}

/* ---------- Queries ---------- */

/**
 * GET /conversations?limit=&offset=
 * Sidebar list, most-recently-active first.
 */
export async function listConversations(
  params: ListConversationsParams = {},
): Promise<ConversationListResponse> {
  const search = new URLSearchParams();
  if (params.limit !== undefined) search.set("limit", String(params.limit));
  if (params.offset !== undefined) search.set("offset", String(params.offset));

  const qs = search.toString();
  return apiClient<ConversationListResponse>(
    `/conversations${qs ? `?${qs}` : ""}`,
    { method: "GET" },
  );
}

/**
 * GET /conversations/{id}
 * Returns null when the server replies 404 (chat deleted, or belongs
 * to another user — the backend deliberately uses 404, not 403, so we
 * don't leak the existence of other users' chats).
 */
export async function getConversation(id: string): Promise<Conversation | null> {
  try {
    return await apiClient<Conversation>(`/conversations/${id}`, {
      method: "GET",
    });
  } catch (err) {
    if (
      err &&
      typeof err === "object" &&
      "status" in err &&
      (err as { status?: number }).status === 404
    ) {
      return null;
    }
    throw err;
  }
}

/**
 * GET /conversations/{id}/messages?limit=&offset=&order=
 * History of one chat. The endpoint accepts an `order` query param:
 *   - `asc`  (default): oldest → newest.
 *   - `desc`:           newest → oldest. Used by the first page of the
 *                       chat UI so the newest turns render immediately.
 *
 * The chat workspace paginates with `useInfiniteQuery`; each page is
 * a single call to this helper.
 */
export async function listMessages(
  conversationId: string,
  params: ListMessagesParams = {},
): Promise<ConversationMessageListResponse> {
  const search = new URLSearchParams();
  if (params.limit !== undefined) search.set("limit", String(params.limit));
  if (params.offset !== undefined) search.set("offset", String(params.offset));
  if (params.order !== undefined) search.set("order", params.order);

  const qs = search.toString();
  return apiClient<ConversationMessageListResponse>(
    `/conversations/${conversationId}/messages${qs ? `?${qs}` : ""}`,
    { method: "GET" },
  );
}

/* ---------- Mutations ---------- */

/**
 * POST /conversations
 * Returns the freshly-created chat (full `Conversation` shape including
 * the saved doc selection).
 */
export async function createConversation(
  input: ConversationCreateInput,
): Promise<Conversation> {
  return apiClient<Conversation>("/conversations", {
    method: "POST",
    body: input,
  });
}

/**
 * PATCH /conversations/{id}
 * Returns the updated `Conversation`. Throws `ApiError(404)` when the
 * chat doesn't exist or isn't owned by the caller.
 */
export async function updateConversation(
  id: string,
  input: ConversationUpdateInput,
): Promise<Conversation> {
  return apiClient<Conversation>(`/conversations/${id}`, {
    method: "PATCH",
    body: input,
  });
}

/**
 * DELETE /conversations/{id}
 * Backend returns 204 with no body; `apiClient` resolves to `undefined`.
 */
export async function deleteConversation(id: string): Promise<void> {
  await apiClient<void>(`/conversations/${id}`, { method: "DELETE" });
}

/**
 * POST /conversations/{id}/messages
 * Submits a question. Backend returns 202 + `{ stream_id }` immediately;
 * the assistant reply comes back via WebSocket `analysis.*` events
 * tagged with the same `stream_id` and `conversation_id`.
 *
 * Throws `ApiError(429)` when the per-user stream cap is hit, and
 * `ApiError(404)` for unknown / not-owned conversations.
 */
export async function submitMessage(
  conversationId: string,
  input: ConversationMessageSubmitInput,
): Promise<ConversationMessageSubmitResponse> {
  return apiClient<ConversationMessageSubmitResponse>(
    `/conversations/${conversationId}/messages`,
    { method: "POST", body: input },
  );
}

/**
 * DELETE /conversations/{id}/messages/{stream_id}
 * Cancels an in-flight assistant stream so the orchestrator stops
 * generating AND the partial answer is dropped from the DB (no
 * assistant row is persisted on cancellation).
 *
 * Idempotent: 204 on success, 404 if the stream is already finished
 * or never existed (we don't distinguish those so a client polling
 * after a normal `done` event can't probe for other users' streams).
 * Throws `ApiError(401)` if the session has expired.
 *
 * Used by the chat composer's stop button — the user message
 * itself was persisted before the LLM stream started, so cancelling
 * just removes the in-progress assistant bubble; the question stays
 * in the chat history.
 */
export async function cancelMessage(
  conversationId: string,
  streamId: string,
): Promise<void> {
  await apiClient<void>(
    `/conversations/${conversationId}/messages/${streamId}`,
    { method: "DELETE" },
  );
}