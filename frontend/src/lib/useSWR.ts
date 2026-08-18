/**
 * useSWR — Stale-While-Revalidate hook for tab screens.
 *
 * μ-closure P3 (2026-06): Warm revisits to primary tabs must show
 * previous content INSTANTLY while a background refresh runs silently.
 * The full skeleton state is reserved for the FIRST visit only.
 *
 * Contract:
 *  • First mount with no cached snapshot → `data = undefined`, `loading = true`.
 *  • Cached snapshot exists → `data = <cached>`, `loading = false`, background
 *    refresh runs immediately (silent) and updates when it lands.
 *  • Dep-change with cache for the new key → also instant.
 *  • `refetch(force = true)` bypasses the cache-window.
 *
 * The cache is module-scope in-memory only; on hard reload / cold boot
 * the cache is empty which correctly falls back to skeleton. Persistence
 * to AsyncStorage is intentionally OUT of scope — the fresh-line
 * integrity contract requires real-time provider data on cold boot.
 *
 * NOTE: This hook is intentionally minimal and framework-agnostic —
 * no React Query dependency, no serialization overhead.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useFocusEffect } from "expo-router";

type Snapshot<T> = {
  data: T;
  ts: number;         // last successful load millis
};

// Module-scope cache. Small (<= 32 entries) and keyed by caller-provided
// stable string. Not exposed globally — tests use `swrCacheClear()`.
const _cache: Map<string, Snapshot<unknown>> = new Map();

export function swrCacheClear(): void {
  _cache.clear();
}

/** Read the current cached snapshot for `key` (returns undefined if absent). */
export function swrCacheRead<T>(key: string): T | undefined {
  const snap = _cache.get(key) as Snapshot<T> | undefined;
  return snap?.data;
}

/** Imperatively seed the cache (used by primary-tab preload). */
export function swrCacheWrite<T>(key: string, data: T): void {
  _cache.set(key, { data, ts: Date.now() });
}

type UseSWROptions = {
  /** Ms until a cached snapshot is considered stale and background refresh runs on focus. */
  staleAfterMs?: number;   // default 15 000
  /** Suppress focus-driven refetch when a snapshot is fresher than this. */
  focusWindowMs?: number;  // default 30 000
  /** Called when a fetcher throws — main state stays on the previous snapshot. */
  onError?: (e: unknown) => void;
};

export function useSWR<T>(
  key: string | null,
  fetcher: () => Promise<T>,
  deps: ReadonlyArray<unknown>,
  opts: UseSWROptions = {},
) {
  const {
    staleAfterMs = 15_000,
    focusWindowMs = 30_000,
    onError,
  } = opts;

  // Seed synchronously from cache so the first render is instant on warm
  // revisits — no skeleton flash.
  const initial = key ? (swrCacheRead<T>(key) as T | undefined) : undefined;
  const [data, setData] = useState<T | undefined>(initial);
  const [loading, setLoading] = useState<boolean>(initial === undefined);
  const [error, setError] = useState<Error | null>(null);
  const mountedRef = useRef(true);
  const lastFetchRef = useRef<number>(
    key ? (_cache.get(key)?.ts ?? 0) : 0,
  );

  useEffect(() => () => { mountedRef.current = false; }, []);

  const run = useCallback(async (silent: boolean) => {
    if (!key) return;
    if (!silent) setLoading(true);
    try {
      const next = await fetcher();
      if (!mountedRef.current) return;
      swrCacheWrite(key, next);
      lastFetchRef.current = Date.now();
      setData(next);
      setError(null);
    } catch (e) {
      if (!mountedRef.current) return;
      const err = e instanceof Error ? e : new Error(String(e));
      setError(err);
      onError?.(err);
      // Keep any previously-good data visible (SWR contract).
    } finally {
      if (mountedRef.current && !silent) setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, ...deps]);

  // Dep-change: swap to cached snapshot for the new key if present, else
  // full load. Runs on mount too.
  useEffect(() => {
    if (!key) return;
    const cached = swrCacheRead<T>(key);
    if (cached !== undefined) {
      setData(cached);
      setLoading(false);
      lastFetchRef.current = _cache.get(key)?.ts ?? 0;
      // Silent background refresh only if data is older than staleAfterMs.
      if (Date.now() - lastFetchRef.current >= staleAfterMs) {
        void run(true);
      }
    } else {
      // Cold: full loading state, no cached data.
      setData(undefined);
      void run(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, ...deps]);

  // Focus revisit: silent refresh only if outside focusWindowMs.
  useFocusEffect(
    // eslint-disable-next-line react-hooks/exhaustive-deps
    useCallback(() => {
      if (!key) return;
      const age = Date.now() - lastFetchRef.current;
      if (age >= focusWindowMs) {
        void run(true);
      }
      return undefined;
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [key, focusWindowMs, run]),
  );

  const refetch = useCallback((force = false) => {
    if (force) lastFetchRef.current = 0;
    return run(!!data);   // silent if we already have cached data
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run, data]);

  return { data, loading, error, refetch };
}
