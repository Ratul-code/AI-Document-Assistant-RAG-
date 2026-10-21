"use client";

import { LogOut } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Mutation handlers — typically wired to useMutation in the parent. */
  onConfirm: () => void;
  isPending: boolean;
};

/**
 * Sign-out confirmation dialog.
 *
 * Open state is controlled by the parent (sidebar). When "Sign out" is
 * clicked, `onConfirm` runs; while the mutation is in flight, both
 * buttons are disabled and the confirm button shows "Signing out...".
 */
export function SignOutDialog({ open, onOpenChange, onConfirm, isPending }: Props) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-lg">Sign out</DialogTitle>
          <DialogDescription className="leading-relaxed">
            Are you sure you want to sign out? You&apos;ll need to log in
            again to access your chats and documents.
          </DialogDescription>
        </DialogHeader>

        <DialogFooter className="gap-2 sm:gap-2">
          <Button
            type="button"
            variant="outline"
            disabled={isPending}
            onClick={() => onOpenChange(false)}
          >
            Cancel
          </Button>
          <Button
            type="button"
            variant="destructive"
            disabled={isPending}
            onClick={onConfirm}
          >
            <LogOut className="size-4" />
            {isPending ? "Signing out..." : "Sign out"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}