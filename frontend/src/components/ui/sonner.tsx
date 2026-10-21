"use client";

import {
  AlertCircle,
  AlertTriangle,
  Check,
  Info,
  Loader2,
  X,
} from "lucide-react";
import { Toaster as Sonner, type ToasterProps } from "sonner";

import { cn } from "@/lib/utils";

/**
 * App-wide Sonner toaster, bound to the design-system tokens.
 *
 * The site is always dark (`<html className="dark">` in
 * `app/layout.tsx`), so we set `theme="dark"` rather than `"system"`.
 *
 * `toastOptions.unstyled` strips Sonner's default stylesheet so the
 * `classNames` below fully own the surface. The variant signal
 * (success / error / info / warning) is carried entirely by the
 * icon's color; the surface itself stays monochromatic
 * (`bg-popover`, `border-border`) to match the rest of the app's
 * floating panels, per `docs/DESIGN.md` §4 and §9. Tinting the
 * surface itself was deliberately avoided — the craft floor flags
 * colored `border-left` / `border-right` strips as decoration that
 * reads worse than a clean surface.
 *
 * Icons come from the lucide family already used elsewhere
 * (`AlertCircle`, `Check`, `Loader2`) so the toast matches the rest
 * of the app's iconography instead of inheriting Sonner's bespoke
 * SVGs.
 */
const Toaster = ({ ...props }: ToasterProps) => {
  return (
    <Sonner
      theme="dark"
      position="bottom-right"
      icons={{
        success: <Check className="size-4" />,
        info: <Info className="size-4" />,
        warning: <AlertTriangle className="size-4" />,
        error: <AlertCircle className="size-4" />,
        loading: <Loader2 className="size-4 animate-spin" />,
        close: <X className="size-3.5" />,
      }}
      toastOptions={{
        unstyled: true,
        classNames: {
          toast: cn(
            "group/toast flex w-[min(360px,calc(100vw-2rem))] items-start gap-2.5",
            "rounded-md border border-border bg-popover text-popover-foreground p-3.5 shadow-lg",
          ),
          content: "flex flex-1 flex-col gap-0.5 min-w-0",
          title: "text-sm font-medium leading-tight text-foreground",
          description:
            "text-xs leading-snug text-muted-foreground [&:not(:first-child)]:mt-0.5",
          icon: cn(
            "shrink-0 mt-0.5 text-muted-foreground",
            // Icon-only variant signal. Success = brand lime (used
            // sparingly per DESIGN.md §3); error = destructive;
            // warning = foreground; info = muted (neutral).
            "group-data-[type=success]/toast:text-primary",
            "group-data-[type=error]/toast:text-destructive",
            "group-data-[type=warning]/toast:text-foreground",
            "group-data-[type=info]/toast:text-muted-foreground",
          ),
          closeButton: cn(
            "absolute right-2 top-2 inline-flex size-5 items-center justify-center",
            "rounded-sm text-muted-foreground/70 opacity-70 transition-opacity",
            "hover:text-foreground hover:opacity-100",
            "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
          ),
          actionButton: cn(
            "inline-flex h-7 items-center justify-center rounded-md",
            "bg-primary px-2.5 text-xs font-medium text-primary-foreground",
            "transition-colors hover:bg-primary/90",
            "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
          ),
          cancelButton: cn(
            "inline-flex h-7 items-center justify-center rounded-md",
            "bg-muted px-2.5 text-xs font-medium text-muted-foreground",
            "transition-colors hover:bg-muted/80 hover:text-foreground",
            "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
          ),
          success: "",
          error: "",
          warning: "",
          info: "",
        },
      }}
      {...props}
    />
  );
};

export { Toaster };
