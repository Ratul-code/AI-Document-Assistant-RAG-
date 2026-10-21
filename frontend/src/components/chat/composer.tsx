"use client";

import { Loader2, SendHorizonal, Square } from "lucide-react";
import { useCallback, useRef, useState, type KeyboardEvent } from "react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

type Props = {
  /**
   * The composer has three states:
   *
   *   1. `idle`              — send button enabled, icon = `Send`.
   *   2. `submitting`        — POST in flight. Send button replaced
   *                            with a spinner; user cannot type a new
   *                            question until the submit resolves.
   *   3. `streaming`         — orchestrator is generating tokens. The
   *                            send button is replaced with a STOP
   *                            button (filled square icon). Clicking
   *                            it issues the cancel endpoint and
   *                            drops the partial assistant turn.
   *
   * The string union is intentional: callers drive the state from the
   * stream-store phase (submitting vs streaming/done) and the composer
   * is a pure presentational component on top of it.
   */
  status?: "idle" | "submitting" | "streaming";
  /** Friendly placeholder shown when the input is empty. */
  placeholder?: string;
  /**
   * Optional inline hint shown below the textarea (e.g. "Pick a
   * document above to ground your answers."). When set, this hint
   * replaces the keyboard-shortcut footer so we don't compete for
   * the same visual real estate.
   */
  hint?: string;
  /**
   * One-shot initial value seeded into the textarea on first mount.
   * Used to prefill the user's first question when transitioning from
   * the welcome screen — so they can see what they asked while the
   * answer streams in. The composer still owns its value after that,
   * so the user can edit/clear it freely.
   */
  initialValue?: string;
  /**
   * Called with the trimmed question text when the user submits.
   * Parent owns the mutation; this component only handles UX.
   * Ignored when `status === "submitting" || "streaming"`.
   */
  onSubmit: (question: string) => void;
  /**
   * Called when the user clicks the STOP button while a stream is
   * active. Parent issues the cancel endpoint and resets local state.
   * Only used when `status === "streaming"`.
   */
  onCancel?: () => void;
};

/**
 * Chat composer — single textarea + action button. Cmd/Ctrl+Enter
 * submits; Enter alone inserts a newline (so multi-line questions
 * work naturally).
 *
 * Action button states:
 *
 *   - `idle`        → paper-plane `Send` icon. Enabled only when
 *                     there's non-whitespace text. Click / ⌘↵ triggers
 *                     `onSubmit(trimmed)`.
 *   - `submitting`  → spinner. Disabled. No way to type or stop the
 *                     submit — it usually resolves in <100ms (just a
 *                     `POST /messages` round-trip) and after that we
 *                     flip to `streaming`.
 *   - `streaming`   → filled `Square` icon (the universal "stop"
 *                     affordance). Click triggers `onCancel()`, which
 *                     the parent turns into a `DELETE
 *                     /conversations/{id}/messages/{stream_id}` call.
 *                     The partial assistant turn is dropped server-
 *                     side, not just hidden.
 *
 * The textarea auto-grows up to its max-h (set on the Textarea
 * primitive: `max-h-48`). We don't track height ourselves — the
 * primitive's `field-sizing-content` does it natively.
 *
 * A small "delivering…" / "generating…" line appears in place of the
 * keyboard hint while the user is mid-flight. It gives explicit
 * feedback that the click registered (which the spinner / square
 * icon already implies visually, but text is more accessible and
 * makes the state legible at a glance).
 */
export function Composer({
  status = "idle",
  placeholder = "Ask anything about your documents…",
  hint,
  initialValue,
  onSubmit,
  onCancel,
}: Props) {
  // Seed the textarea once on mount. `initialValue` is treated as
  // authoritative on the first render — after that the user owns the
  // value (they can edit, clear, or send), so we deliberately do NOT
  // sync it back via a `useEffect`. The store-backed handoff that
  // produces `initialValue` is also drain-on-read, so re-renders
  // don't get a fresh value either.
  const [value, setValue] = useState(() => initialValue ?? "");
  const formRef = useRef<HTMLFormElement | null>(null);

  const trimmed = value.trim();
  const isLocked = status === "submitting" || status === "streaming";
  const canSubmit = !isLocked && trimmed.length > 0;

  const handleSubmit = useCallback(() => {
    if (!canSubmit) return;
    onSubmit(trimmed);
    setValue("");
  }, [canSubmit, onSubmit, trimmed]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        handleSubmit();
      }
    },
    [handleSubmit],
  );

  return (
    <form
      ref={formRef}
      onSubmit={(e) => {
        e.preventDefault();
        handleSubmit();
      }}
      className={cn(
        "border-t border-border bg-background/80 px-4 py-3 backdrop-blur",
        "supports-[backdrop-filter]:bg-background/60",
      )}
    >
      <div
        className={cn(
          "mx-auto flex w-full max-w-3xl items-end gap-2",
          "rounded-lg border border-border bg-card p-2",
          "focus-within:border-primary/60 focus-within:ring-3",
          "focus-within:ring-primary/20",
          "transition-[box-shadow,border-color] duration-200",
        )}
      >
        <Textarea
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          disabled={isLocked}
          rows={1}
          aria-label="Message"
          className="min-h-9 border-0 bg-transparent px-2 py-1.5 shadow-none focus-visible:border-0 focus-visible:ring-0 disabled:opacity-100"
        />
        {/*
          Action button. Three visual states:
            - idle + can submit   → filled `Send` (primary brand).
            - submitting          → spinner. The submit is a single
                                    POST; we don't expose a stop here
                                    (the streaming phase that follows
                                    is where the user gets the stop
                                    button).
            - streaming           → filled `Square` (universal "stop"
                                    affordance). Brand-tinted so it's
                                    visually distinct from the idle
                                    send, but uses the same surface
                                    colour to keep the action area
                                    visually anchored.
        */}
        {status === "streaming" && onCancel ? (
          <Button
            type="button"
            size="icon"
            onClick={onCancel}
            aria-label="Stop generating"
            className="shrink-0"
          >
            <Square className="size-3.5 fill-current" />
          </Button>
        ) : (
          <Button
            type="submit"
            size="icon"
            disabled={!canSubmit}
            aria-label="Send"
            className="shrink-0"
          >
            {status === "submitting" ? (
              <Loader2 className="size-4 animate-spin" />
            ) : (
              <SendHorizonal className="size-4" />
            )}
          </Button>
        )}
      </div>
      {hint ? (
        <p className="mx-auto mt-1.5 max-w-3xl px-2 text-[11px] text-muted-foreground">
          {hint}
        </p>
      ) : status === "submitting" ? (
        <p
          className="mx-auto mt-1.5 flex max-w-3xl items-center gap-1.5 px-2 text-[11px] text-muted-foreground"
          aria-live="polite"
        >
          <Loader2 className="size-3 animate-spin" />
          Delivering your question…
        </p>
      ) : status === "streaming" ? (
        <p
          className="mx-auto mt-1.5 max-w-3xl px-2 text-[11px] text-muted-foreground"
          aria-live="polite"
        >
          Generating answer — click{" "}
          <span className="rounded border border-border bg-muted px-1 font-mono text-[10px] text-foreground/80">
            ■
          </span>{" "}
          to stop.
        </p>
      ) : (
        <p className="mx-auto mt-1.5 max-w-3xl px-2 text-[11px] text-muted-foreground">
          Press{" "}
          <kbd className="rounded border border-border bg-muted px-1 font-mono text-[10px] text-foreground/80">
            ⌘
          </kbd>
          +
          <kbd className="rounded border border-border bg-muted px-1 font-mono text-[10px] text-foreground/80">
            Enter
          </kbd>{" "}
          to send.
        </p>
      )}
    </form>
  );
}
