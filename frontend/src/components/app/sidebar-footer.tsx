"use client";

import { LogOut } from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

type User = { name: string; email: string };

type Props = {
  collapsed: boolean;
  user: User;
  onSignOut: () => void;
};

function initials(name: string): string {
  const parts = name.trim().split(/\s+/);
  if (parts.length === 0) return "?";
  const first = parts[0]?.[0] ?? "";
  const last = parts.length > 1 ? (parts[parts.length - 1]?.[0] ?? "") : "";
  return (first + last).toUpperCase() || "?";
}

export function SidebarFooter({ collapsed, user, onSignOut }: Props) {
  if (collapsed) {
    return (
      <div className="flex flex-col items-center gap-2 border-t border-sidebar-border px-2 py-2">
        <div
          className="grid size-8 place-items-center rounded-full bg-sidebar-accent text-sidebar-foreground text-xs font-semibold ring-1 ring-inset ring-sidebar-border"
          aria-label={user.name}
          title={user.name}
        >
          {initials(user.name)}
        </div>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="Sign out"
          onClick={onSignOut}
          className="text-muted-foreground hover:text-foreground"
        >
          <LogOut className="size-4" />
        </Button>
      </div>
    );
  }

  return (
    <div
      className={cn(
        "flex items-center gap-2 border-t border-sidebar-border px-3 py-2.5",
      )}
    >
      <div className="grid size-8 shrink-0 place-items-center rounded-full bg-sidebar-accent text-sidebar-foreground text-xs font-semibold ring-1 ring-inset ring-sidebar-border">
        {initials(user.name)}
      </div>
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-sidebar-foreground">
          {user.name}
        </p>
        <p className="truncate text-[11px] text-muted-foreground">
          {user.email}
        </p>
      </div>
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label="Sign out"
        onClick={onSignOut}
        className="text-muted-foreground hover:text-foreground"
      >
        <LogOut className="size-4" />
      </Button>
    </div>
  );
}