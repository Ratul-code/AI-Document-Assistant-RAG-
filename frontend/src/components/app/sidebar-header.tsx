"use client";

import { PanelLeftClose, PanelLeftOpen, PenSquare } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import Link from "next/link";

type Props = {
  collapsed: boolean;
  onToggleCollapsed: () => void;
};

/**
 * Top of the sidebar. Holds the brand mark, a "New chat" button,
 * and the chevron toggle that collapses the sidebar to a narrow rail.
 */
export function SidebarHeader({ collapsed, onToggleCollapsed }: Props) {
  return (
    <div className="flex flex-col gap-2 px-2 pt-2">
      <div
        className={cn(
          "flex items-center gap-2",
          collapsed ? "justify-center" : "justify-between",
        )}
      >
        {/* Brand / app mark — visible in both states. The lime mark is
            used selectively per the design system (it's the only spot in
            the chrome that leans into the brand). */}
        {!collapsed ? (
          <Link
            href="/chat"
            className="flex items-center gap-2 px-1 rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          >
            <div className="grid size-6 place-items-center rounded-md bg-primary text-primary-foreground text-[11px] font-bold shadow-[0_0_0_1px_oklch(0.8444_0.2084_130_/_0.25)]">
              AI
            </div>
            <span className="text-sm font-semibold tracking-tight">
              Doc Assistant
            </span>
          </Link>
        ) : (
          <div
            aria-label="Doc Assistant"
            title="Doc Assistant"
            className="grid size-6 place-items-center rounded-md bg-primary text-primary-foreground text-[11px] font-bold shadow-[0_0_0_1px_oklch(0.8444_0.2084_130_/_0.25)]"
          >
            AI
          </div>
        )}

        <Button
          variant="ghost"
          size="icon-sm"
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          onClick={onToggleCollapsed}
          className="text-muted-foreground hover:text-foreground"
        >
          {collapsed ? (
            <PanelLeftOpen className="size-4" />
          ) : (
            <PanelLeftClose className="size-4" />
          )}
        </Button>
      </div>

      {/* New chat — only when expanded */}
      {!collapsed && (
        <Link href={`/chat`}>
          <Button
            variant="outline"
            className="w-full justify-start gap-2"
            onClick={() => { }}
          >
            <PenSquare className="size-4" />
            <span>New chat</span>
          </Button>
        </Link>
      )}

      {/* Collapsed: a single icon-only button to start a new chat */}
      {collapsed && (
        <Button
          variant="outline"
          size="icon-sm"
          aria-label="New chat"
          onClick={() => { }}
          className="self-center"
        >
          <PenSquare className="size-4" />
        </Button>
      )}
    </div>
  );
}
