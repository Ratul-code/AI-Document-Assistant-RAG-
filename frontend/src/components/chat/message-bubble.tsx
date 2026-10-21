"use client";

import { AlertCircle } from "lucide-react";

import { cn } from "@/lib/utils";
import type {
  ConversationErrorCode,
  ConversationMessage,
} from "@/lib/api/conversations";
import { CitationsList } from "./citations-list";

type Props = {
  message: ConversationMessage;
};

/**
 * One chat turn. Branches on `role`:
 *
 *   - `user`       → right-aligned, primary-tinted bubble, plain text.
 *   - `assistant`  → left-aligned, plain surface (no card chrome —
 *                    the chat background itself provides the rhythm),
 *                    plain text with a blinking lime cursor while the
 *                    live stream is still arriving. Below the text,
 *                    the citations panel (when present). If
 *                    `error_code` is set we render a small inline
 *                    alert instead of the text — the persisted content
 *                    is empty in that case.
 *
 * The `isStreaming` prop is read off the parent: while a stream is
 * active, the *last* assistant bubble may be either persisted (it
 * has an id from the DB) or live (no id yet). We pass it in so the
 * bubble can render a trailing cursor.
 *
 * Visual rules from DESIGN.md:
 *   - Avoid excessive rounding — `rounded-lg` with a sharper bottom
 *     corner reads as intentional rather than pillowy.
 *   - Use brand color selectively — the user bubble and the live
 *     streaming cursor are the only lime surfaces in the chat.
 *   - Prefer whitespace and borders over shadows for separation.
 */
export function MessageBubble({
  message,
  isStreaming = false,
}: Props & { isStreaming?: boolean }) {
  const isUser = message.role === "user";

  if (isUser) {
    return (
      <div className="flex justify-end">
        <div
          className={cn(
            "max-w-[80%] rounded-lg rounded-br-sm px-3.5 py-2.5 text-sm",
            "bg-primary text-primary-foreground whitespace-pre-wrap",
            "shadow-[0_0_0_1px_oklch(0.8444_0.2084_130_/_0.2)]",
          )}
        >
          {message.content}
        </div>
      </div>
    );
  }

  // Assistant: error state vs. normal text.
  if (message.error_code) {
    return (
      <div className="flex justify-start">
        <div
          className={cn(
            "max-w-[85%] rounded-lg rounded-bl-sm border border-destructive/30",
            "bg-destructive/5 px-4 py-2.5 text-sm",
          )}
        >
          <div className="flex items-start gap-2 text-destructive">
            <AlertCircle className="mt-0.5 size-4 shrink-0" />
            <div>
              <p className="font-medium">
                {humanErrorTitle(message.error_code)}
              </p>
              <p className="mt-0.5 text-xs text-destructive/80">
                {humanErrorHint(message.error_code)}
              </p>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex justify-start">
      <div
        className={cn(
          "max-w-[85%] rounded-lg rounded-bl-sm",
          "bg-card/50 px-4 py-2.5 text-sm text-foreground",
          "border-l-2 border-l-transparent",
        )}
      >
        <p className="whitespace-pre-wrap">
          {message.content}
          {isStreaming ? (
            <span
              aria-hidden
              className="ml-0.5 inline-block h-4 w-1.5 translate-y-0.5 animate-pulse rounded-sm bg-primary"
            />
          ) : null}
        </p>
        <CitationsList citations={message.citations} />
      </div>
    </div>
  );
}

/* ---------- Helpers ---------- */

function humanErrorTitle(code: ConversationErrorCode): string {
  switch (code) {
    case "NO_DOCUMENTS_READY":
      return "No documents to ground on";
    case "PROVIDER_ERROR":
      return "The AI provider hit an error";
    case "PROVIDER_MISMATCH":
      return "Embedding model mismatch";
    case "CANCELLED":
      return "Response stopped";
    case "UNAUTHORIZED":
      return "Session expired";
    case "INTERNAL":
    default:
      return "Something went wrong";
  }
}

function humanErrorHint(code: ConversationErrorCode): string {
  switch (code) {
    case "NO_DOCUMENTS_READY":
      return "Pick at least one document in the chat header to ground answers.";
    case "CANCELLED":
      return "You stopped this response early. Send another question to try again.";
    case "UNAUTHORIZED":
      return "Please sign in again to continue.";
    default:
      return "Please try again, or check the chat header if the document scope looks wrong.";
  }
}