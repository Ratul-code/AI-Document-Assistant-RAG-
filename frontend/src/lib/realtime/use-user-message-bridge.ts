"use client";

import { useEffect } from "react";
import { useQueryClient, type InfiniteData } from "@tanstack/react-query";

import {
  conversationKeys,
  type ConversationMessage,
  type ConversationMessageListResponse,
} from "@/lib/api/conversations";
import { useWSStore, type WSMessage } from "./ws-store";

/**
 * Bridges `analysis.user_message` WebSocket events to the chat-history
 * cache.
 *
 * Wire format (must match `app.schemas.ws_events.WsEvent` server-side):
 *   { type: "analysis.user_message",
 *     data: { stream_id, conversation_id, message: ConversationMessage }, ts }
 *
 * Why this hook exists:
 *   The backend persists the user row to the DB before the LLM stream
 *   starts, but the chat UI subscribes to messages via TanStack Query —
 *   a plain HTTP refetch. Without this hook, the user bubble would only
 *   appear once the assistant finishes and `analysis.done` triggers a
 *   refetch. With it, the user sees their own message + a typing
 *   indicator immediately on submit.
 *
 * Strategy:
 *   - Optimistically append the new user row into every cached
 *     `messageList(conversationId, ...)` so the UI renders the bubble
 *     synchronously with the WS delivery.
 *   - Then invalidate the conversation-detail and lists caches so a
 *     later `analysis.done` refetch reconciles the assistant row.
 *   - Dedupe on message id: if the optimistic insert arrives after a
 *     refetch already wrote the same row (rare race), we keep the
 *     server version.
 *
 * Mount once at app root alongside `useWSToasts` / `useDocumentStatusBridge`.
 */
export function useUserMessageBridge() {
  const subscribe = useWSStore((s) => s.subscribe);
  const queryClient = useQueryClient();

  useEffect(() => {
    return subscribe((msg: WSMessage) => {
      if (msg.type !== "analysis.user_message") return;

      const data = (msg.data ?? {}) as {
        stream_id?: unknown;
        conversation_id?: unknown;
        message?: ConversationMessage;
      };
      const conversationId =
        typeof data.conversation_id === "string"
          ? data.conversation_id
          : null;
      const userMessage = data.message;
      if (!conversationId || !userMessage) return;

      // Optimistic insert into the chat-history infinite query for this
      // conversation. The chat workspace fetches pages in DESC order,
      // so the NEWEST items live at the START of the FIRST page
      // (`pages[0].items[0]`). The workspace then reverses each page on
      // the way out before flattening, so this user row will render at
      // the END of the visible list — the "bottom" of the chat, just
      // above where the streaming assistant bubble will appear.
      //
      // We prepend into `pages[0].items` rather than appending to the
      // last page: appending to a "last page" that is already full
      // (e.g. 20 items with `hasNextPage=false`) would push the new
      // row into a logical page-2 that nobody fetches, and bumping
      // `total` would make `getNextPageParam` think there's more data
      // to load without actually placing the row in any rendered page.
      //
      // Dedupe across pages: if a refetch already populated this id
      // somewhere (e.g. a concurrent invalidation resolved), skip.
      const cached = queryClient.getQueryData<
        InfiniteData<ConversationMessageListResponse, unknown>
      >(conversationKeys.messages(conversationId));
      if (cached && cached.pages.length > 0) {
        const alreadyPresent = cached.pages.some((p) =>
          p.items.some((m) => m.id === userMessage.id),
        );
        if (!alreadyPresent) {
          queryClient.setQueryData<
            InfiniteData<ConversationMessageListResponse, unknown>
          >(conversationKeys.messages(conversationId), {
            ...cached,
            pages: cached.pages.map((p, i) =>
              i === 0
                ? {
                    ...p,
                    items: [userMessage, ...p.items],
                    total: p.total + 1,
                  }
                : p,
            ),
          });
        }
      }

      // The conversation metadata + sidebar list both have ordering
      // keys (`last_message_at`) that this message just bumped —
      // invalidate so they refetch next time they're mounted.
      void queryClient.invalidateQueries({
        queryKey: conversationKeys.detail(conversationId),
      });
      void queryClient.invalidateQueries({
        queryKey: conversationKeys.lists(),
      });
    });
  }, [subscribe, queryClient]);
}