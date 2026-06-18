import { useCallback, useRef } from "react";
import { useFocusEffect } from "expo-router";

/**
 * Smart refetch-on-focus hook.
 *
 * Calls `fetcher` every time the screen gains focus, but skips the call if
 * the most recent successful fetch happened less than `cacheWindowMs` ago.
 * Defaults to 30 000 ms — matches the PerksLocks product spec:
 *   • Re-fetch on screen open / tab focus
 *   • Suppress duplicate hits inside a short window
 *   • Never serve stale state without checking the API
 *
 * Usage:
 *   useFocusRefetch(() => load(filters), [filters]);
 *
 * Pass `cacheWindowMs = 0` to force a fetch on every focus.
 */
export function useFocusRefetch(
  fetcher: () => void | Promise<unknown>,
  deps: ReadonlyArray<unknown>,
  cacheWindowMs: number = 30_000,
) {
  const lastFetchRef = useRef<number>(0);

  useFocusEffect(
    // eslint-disable-next-line react-hooks/exhaustive-deps
    useCallback(() => {
      const now = Date.now();
      if (now - lastFetchRef.current >= cacheWindowMs) {
        lastFetchRef.current = now;
        // Fire-and-forget; caller manages its own loading state.
        Promise.resolve(fetcher()).catch(() => {
          // Reset stamp on failure so the next focus retries immediately.
          lastFetchRef.current = 0;
        });
      }
      // No cleanup needed — focus effects are idempotent.
      return undefined;
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [...deps, cacheWindowMs]),
  );

  /** Force the next focus to bypass the cache window. */
  const invalidate = useCallback(() => {
    lastFetchRef.current = 0;
  }, []);

  return { invalidate };
}
