import { useCallback, useRef } from "react";
import { useFocusEffect } from "expo-router";

/**
 * Smart refetch-on-focus hook.
 *
 * Calls `fetcher` every time the screen gains focus, but skips the call if
 * the most recent SUCCESSFUL fetch happened less than `cacheWindowMs` ago.
 * Defaults to 30 000 ms — matches the PerksLocks product spec:
 *   • Re-fetch on screen open / tab focus
 *   • Suppress duplicate hits inside a short window
 *   • Never serve stale state without checking the API
 *
 * ─── MAIN 39 · P0.6 (2026-06) — success/failure contract ────────────
 *
 * Failure detection was broken before this rev because most screen
 * `load()` functions catch their own errors internally and resolve
 * successfully (they set an in-component `error` state instead of
 * re-throwing).  The old hook only reset its cooldown stamp when the
 * returned Promise REJECTED, so a failed refresh was silently treated
 * as a success and the user was stuck behind a 30-second cooldown.
 *
 * New contract (backward compatible):
 *   fetcher resolves `true`      → success (cooldown respected)
 *   fetcher resolves `false`     → failure (next focus retries)
 *   fetcher resolves `undefined` → success (legacy screens)
 *   fetcher REJECTS              → failure (next focus retries)
 *
 * Extra safety:
 *   • An in-flight guard prevents two overlapping focus refetches
 *     from firing when a user thrashes tabs while a slow API is
 *     still resolving.  Only one focus-refetch may be pending at a
 *     time; concurrent focuses no-op (they will be re-evaluated on
 *     the next focus after the pending one settles).
 *   • The stamp is written only when the fetcher resolves with an
 *     explicit success signal — never speculatively before the
 *     promise settles.  This guarantees a failed refresh cannot lock
 *     out the next focus by pre-stamping.
 *
 * Usage:
 *   useFocusRefetch(() => load(filters), [filters]);
 *
 * Pass `cacheWindowMs = 0` to force a fetch on every focus.
 */
export function useFocusRefetch(
  fetcher: () => void | boolean | Promise<unknown> | Promise<boolean> | Promise<void>,
  deps: ReadonlyArray<unknown>,
  cacheWindowMs: number = 30_000,
) {
  const lastFetchRef = useRef<number>(0);
  const inFlightRef = useRef<boolean>(false);

  useFocusEffect(
    // eslint-disable-next-line react-hooks/exhaustive-deps
    useCallback(() => {
      const now = Date.now();
      // Cooldown window still active — skip.
      if (now - lastFetchRef.current < cacheWindowMs) {
        return undefined;
      }
      // A previous focus refetch is still pending — skip so we
      // don't stack concurrent calls.  The pending call's settle
      // handler will decide whether the stamp advances or resets.
      if (inFlightRef.current) {
        return undefined;
      }

      inFlightRef.current = true;
      // NOTE: we do NOT stamp `lastFetchRef` speculatively.  We only
      // stamp AFTER a successful settle so a failure truly resets.
      Promise.resolve()
        .then(() => fetcher())
        .then((result) => {
          // Explicit `false` from the fetcher signals failure so the
          // next focus may retry immediately without waiting for
          // the cooldown to elapse.  `undefined` and `true` count
          // as success (backward compatible with legacy screens).
          if (result === false) {
            lastFetchRef.current = 0;
          } else {
            lastFetchRef.current = Date.now();
          }
        })
        .catch(() => {
          // Rejection → failure; next focus retries immediately.
          lastFetchRef.current = 0;
        })
        .finally(() => {
          inFlightRef.current = false;
        });

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
