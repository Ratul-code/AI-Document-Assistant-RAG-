"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation } from "@tanstack/react-query";

import { signOut } from "@/lib/api/auth";
import { useWSStore } from "@/lib/realtime/ws-store";
import { cn } from "@/lib/utils";
import { ChatHistoryList } from "./chat-history-list";
import { DocumentsList } from "./documents-list";
import { SignOutDialog } from "./sign-out-dialog";
import { SidebarFooter } from "./sidebar-footer";
import { SidebarHeader } from "./sidebar-header";

type User = { name: string; email: string };

type Props = {
  user: User;
};

/**
 * The collapsible sidebar shown on every authenticated page.
 *
 * Width:
 *   - expanded  → w-64
 *   - collapsed  → w-14 (icon rail)
 *
 * Collapse state is local to this component for now. If we ever need
 * other UI to react to it (e.g. mobile), lift to context or a parent prop.
 */
export function Sidebar({ user }: Props) {
  const [collapsed, setCollapsed] = useState(false);
  const [signOutOpen, setSignOutOpen] = useState(false);
  const router = useRouter();

  // Sign-out is a mutation: FastAPI clears the cookies via Set-Cookie,
  // and the browser applies them. After that we close the WebSocket
  // (so it doesn't keep running with stale cookies) and navigate to
  // /login so the proxy re-runs for the next navigation.
  const signOutMutation = useMutation({
    mutationFn: signOut,
    onSuccess: () => {
      // Close the WS before navigating. We don't await — disconnect
      // is fire-and-forget and the next page will tear it down via
      // WSProvider's unmount anyway.
      useWSStore.getState().disconnect();
      setSignOutOpen(false);
      router.push("/login");
      router.refresh();
    },
  });

  // Footer button just opens the dialog. Confirm runs the mutation.
  const requestSignOut = () => setSignOutOpen(true);
  const confirmSignOut = () => signOutMutation.mutate();

  return (
    <aside
      aria-label="Primary"
      data-collapsed={collapsed}
      className={cn(
        "flex h-full flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground",
        "transition-[width] duration-200 ease-out",
        collapsed ? "w-14" : "w-64",
      )}
    >
      <SidebarHeader
        collapsed={collapsed}
        onToggleCollapsed={() => setCollapsed((prev) => !prev)}
      />

      <div className="mt-2 flex-1 overflow-y-auto">
        <ChatHistoryList collapsed={collapsed} />
        <DocumentsList collapsed={collapsed} />
      </div>

      <SidebarFooter collapsed={collapsed} user={user} onSignOut={requestSignOut} />

      <SignOutDialog
        open={signOutOpen}
        onOpenChange={(open) => {
          // Don't allow closing while the mutation is in flight.
          if (signOutMutation.isPending && !open) return;
          setSignOutOpen(open);
        }}
        onConfirm={confirmSignOut}
        isPending={signOutMutation.isPending}
      />
    </aside>
  );
}