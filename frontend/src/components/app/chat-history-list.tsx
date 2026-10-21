"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, MessageSquareText, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";

import {
  conversationKeys,
  deleteConversation,
  listConversations,
  type ConversationListItem,
} from "@/lib/api/conversations";
import { useWSStore } from "@/lib/realtime/ws-store";
import { cn } from "@/lib/utils";
import { SidebarSection } from "./sidebar-section";
import { useExpandableList } from "./use-expandable-list";

const INITIAL = 3;
const STEP = 10;
const LIST_LIMIT = 50;

type Props = { collapsed: boolean; };

/**
 * Sidebar list of the user's chats, newest activity first.
 *
 * Data source: `GET /conversations` (TanStack Query). Rows link to
 * `/chat?c=<id>` — the chat page reads the id from the search params
 * and loads the conversation + history.
 *
 * Hover reveals a delete (trash) icon. Delete is a mutation that
 * invalidates the list cache; we don't navigate — the user might
 * have been browsing, not editing the active chat. (If the deleted
 * chat IS the active one, the chat page itself detects a 404 and
 * shows the welcome state.)
 *
 * Active row gets a subtle left-edge lime accent + a softer tint —
 * the design system asks for "restrained active navigation backgrounds",
 * not full-row lime backgrounds.
 */
export function ChatHistoryList({ collapsed }: Props) {
  const listQuery = useQuery({
    queryKey: conversationKeys.list({ limit: LIST_LIMIT, offset: 0 }),
    queryFn: () => listConversations({ limit: LIST_LIMIT, offset: 0 }),
  });

  const items = listQuery.data?.items ?? [];
  const { shown, canExpand, expand, total, visible } =
    useExpandableList<ConversationListItem>(items, {
      initial: INITIAL,
      step: STEP,
    });

  if (collapsed) {
    return (
      <div className="flex flex-col items-center gap-1 px-2 pt-4">
        <MessageSquareText className="size-4 text-muted-foreground" />
      </div>
    );
  }

  const isLoading = listQuery.isPending;
  const hasError = listQuery.isError;

  return (
    <SidebarSection
      label="Chats"
      totalCount={isLoading ? undefined : total}
      visibleCount={visible}
      collapsed={collapsed}
      canExpand={canExpand}
      onExpand={expand}
    >
      {isLoading ? (
        <p className="flex items-center gap-2 px-2 py-2 text-xs text-muted-foreground">
          <Loader2 className="size-3 animate-spin" />
          Loading chats…
        </p>
      ) : hasError ? (
        <p className="px-2 py-2 text-xs text-destructive">
          Couldn&apos;t load chats.
        </p>
      ) : shown.length === 0 ? (
        <p className="px-2 py-2 text-xs text-muted-foreground">
          No chats yet — start a new one.
        </p>
      ) : (
        shown.map((chat) => <ChatRow key={chat.id} chat={chat} />)
      )}
    </SidebarSection>
  );
}

function ChatRow({ chat }: { chat: ConversationListItem; }) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const isActive = searchParams.get("c") === chat.id;
  const queryClient = useQueryClient();
  // The WS store is opened by the global provider; we just need its
  // current status to disable the delete button while we're offline
  // (otherwise the mutation 401s and we have a brief flicker).
  const wsStatus = useWSStore((s) => s.status);

  const deleteMutation = useMutation({
    mutationFn: () => deleteConversation(chat.id),
    onSuccess: () => {
      // Drop every cache slice that could mention this chat.
      queryClient.removeQueries({
        queryKey: conversationKeys.detail(chat.id),
      });
      void queryClient.invalidateQueries({
        queryKey: conversationKeys.lists(),
      });
    },
  });

  function handleDelete(e: React.MouseEvent) {
    // The row itself is a Link — clicking the trash shouldn't follow it.
    e.preventDefault();
    e.stopPropagation();
    if (deleteMutation.isPending) return;
    deleteMutation.mutate();
    // We intentionally don't `router.push` away. If the user was
    // viewing this chat, the chat page will 404 on next refetch and
    // render the welcome state.
  }

  return (
    <div
      className={cn(
        "group/row relative flex items-center gap-1 rounded-md",
        "hover:bg-sidebar-accent/60 transition-colors",
        isActive && "bg-sidebar-accent",
      )}
    >
      {/* Left-edge active accent. Per DESIGN.md §5: "Use restrained
          active navigation backgrounds" — a 2px lime rail is more
          restrained than a full lime background and reads instantly
          as "selected". */}
      {isActive ? (
        <span
          aria-hidden
          className="absolute inset-y-1.5 left-0 w-0.5 rounded-r-full bg-primary"
        />
      ) : null}

      <Link
        href={`/chat?c=${chat.id}`}
        aria-current={isActive ? "page" : undefined}
        className={cn(
          "flex min-w-0 flex-1 items-center gap-2 rounded-md px-2.5 py-1.5 text-left",
          "text-sm text-sidebar-foreground/80 hover:text-sidebar-foreground",
          isActive && "font-medium text-sidebar-foreground",
        )}
        onClick={() => {
          // Pre-warm the conversation + messages cache so the chat
          // page can pick them up as initial data without a flash.
          router.prefetch(`/chat?c=${chat.id}`);
        }}
      >
        <MessageSquareText
          className={cn(
            "size-3.5 shrink-0",
            isActive ? "text-primary" : "text-muted-foreground",
          )}
        />
        <span className="truncate">
          {chat.title ?? "Untitled chat"}
        </span>
      </Link>

      <button
        type="button"
        onClick={handleDelete}
        disabled={
          deleteMutation.isPending ||
          wsStatus === "refreshing" ||
          wsStatus === "auth-expired"
        }
        aria-label={`Delete chat ${chat.title ?? "Untitled"}`}
        className={cn(
          "mr-1 grid size-6 shrink-0 place-items-center rounded-md",
          "text-muted-foreground hover:bg-destructive/10 hover:text-destructive",
          "opacity-0 group-hover/row:opacity-100 focus-visible:opacity-100",
          "transition-opacity disabled:opacity-50",
        )}
      >
        {deleteMutation.isPending ? (
          <Loader2 className="size-3 animate-spin" />
        ) : (
          <Trash2 className="size-3.5" />
        )}
      </button>
    </div>
  );
}