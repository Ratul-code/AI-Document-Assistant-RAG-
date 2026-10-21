"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

/**
 * Drives the "show N more" pattern used by the chat history and
 * documents sections in the sidebar.
 *
 *   - Visible count starts at `min(initial, items.length)` so an empty
 *     list renders as `0/0`, not `3/0`.
 *   - When items load late (query resolves after first render), `visible`
 *     is bumped up to `min(initial, items.length)` — never stuck at 0.
 *   - When items shrink (delete), `visible` is clamped down to fit.
 *   - Each expand: +`step` more items (e.g. 10 for chats, 5 for docs)
 *   - `canExpand` is false once everything is shown
 */
export function useExpandableList<T>(
  items: T[],
  options: { initial: number; step: number },
) {
  const { initial, step } = options;
  const cap = Math.min(initial, items.length);

  const [visible, setVisible] = useState<number>(cap);

  // Reconcile `visible` with the current list shape:
  //   - On first non-empty load (prev was 0, now N): snap up to `initial`
  //     (or all of them), so the user sees 2/2 instead of 0/2.
  //   - On shrink (delete, refetch with fewer): clamp down.
  //   - On no-op (visible already fits): leave alone so user-initiated
  //     "show more" expansions aren't clobbered.
  //
  // We track the previous length in a ref so the "first non-empty load"
  // branch fires exactly once on the 0 → N transition.
  const prevLen = useRef<number>(items.length);
  useEffect(() => {
    const wasEmpty = prevLen.current === 0;
    const isNonEmpty = items.length > 0;
    prevLen.current = items.length;
    setVisible((prev) => {
      if (wasEmpty && isNonEmpty) {
        return Math.min(initial, items.length);
      }
      return Math.min(prev, items.length);
    });
  }, [items.length, initial]);

  const shown = useMemo(() => items.slice(0, visible), [items, visible]);
  const canExpand = visible < items.length;

  const expand = useCallback(() => {
    setVisible((prev) => Math.min(prev + step, items.length));
  }, [step, items.length]);

  return { shown, canExpand, expand, total: items.length, visible };
}