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
import {
  getCurrentEpoch,
  isEpochCurrent,
  subscribeCanonicalEpoch,
} from "@/src/lib/canonicalEpoch";

type Snapshot<T> = {
  data: T;
  ts: number;         // last successful load millis
  // ─── Ordered canonical stamp (2026-06-21 v2) ────────────────────
  // Snapshot of ``getCurrentEpoch()`` at write time.  On dep-change
  // and on cache-hit reads we compare the stamp's ``revision`` to
  // the current authority.  Missing stamp = pre-contract or
  // AsyncStorage-hydrated legacy row (treated as last-good only,
  // never as CURRENT).
  epoch?: {
    origin: string;
    revision: number;
    boardVersion: string;
    generationId: string;
  } | null;
};

// ── Universal freshness sweeper (2026-06-21) ─────────────────────
// One-time subscription: whenever the CanonicalEpoch ADVANCES (rev
// N → N+1) we clear stamped entries whose recorded revision is
// strictly LESS than the new one.  Entries with a null stamp
// (legacy / cross-boot AsyncStorage rows) are also swept — they
// cannot claim to be current under the ordered contract.  Entries
// stamped at exactly N+1 (already written under the new authority)
// are preserved.
let _sweeperInstalled = false;
function _installBoardVersionSweeper(): void {
  if (_sweeperInstalled) return;
  _sweeperInstalled = true;
  subscribeCanonicalEpoch((next, previous) => {
    let swept = 0;
    for (const [k, v] of _cache.entries()) {
      const stampRev = v.epoch?.revision;
      const stampOrigin = v.epoch?.origin;
      const originChanged = !!(previous && previous.origin !== next.origin);
      const originMismatch = stampOrigin && stampOrigin !== next.origin;
      if (
        // No stamp — pre-contract row, cannot be current.
        stampRev === undefined || stampRev === null ||
        // Origin changed — every prior stamp is by definition
        // from a different cache space.
        originChanged ||
        originMismatch ||
        // Ordered comparison — legitimate stale rows.
        stampRev < next.revision
      ) {
        _cache.delete(k);
        swept++;
      }
    }
    if (swept > 0) {
      try {
        // eslint-disable-next-line no-console
        console.log(
          `[freshness] canonical epoch advanced rev ${previous?.revision ?? "(none)"} → ${next.revision}; swept ${swept} stale cache entries`,
        );
      } catch {}
      _schedulePersist();
    }
  });
}
_installBoardVersionSweeper();

// Module-scope cache. Small (<= 32 entries) and keyed by caller-provided
// stable string. Not exposed globally — tests use `swrCacheClear()`.
const _cache: Map<string, Snapshot<unknown>> = new Map();

// ── Iteration 145-C · SELECTIVE last-known-good persistence ────────────
// Only small, trusted, user-facing summaries survive a cold boot so warm
// tabs (Rollover · Parlay · My Bets · Profile) paint instantly and stay
// navigable offline.  Deep/disposable data (HI, Lab analytics) is NOT
// persisted.  Writes happen only after a SUCCESSFUL fetch (never on
// error/timeout), so persisted state is always a valid response.
const PERSIST_KEY = "swr_lkg_v1";
const PERSIST_PREFIXES = ["rollover", "parlay", "my-bets", "profile|stats"];
const PERSIST_MAX_BYTES = 200_000;
let _persistTimer: ReturnType<typeof setTimeout> | null = null;
let _hydrated = false;

function _persistable(key: string): boolean {
  return PERSIST_PREFIXES.some((p) => key.startsWith(p));
}

function _schedulePersist(): void {
  if (_persistTimer) return;
  _persistTimer = setTimeout(async () => {
    _persistTimer = null;
    try {
      const { storage } = await import("@/src/utils/storage");
      const out: Record<string, Snapshot<unknown>> = {};
      for (const [k, v] of _cache.entries()) if (_persistable(k)) out[k] = v;
      const raw = JSON.stringify(out);
      if (raw.length <= PERSIST_MAX_BYTES) await storage.setItem(PERSIST_KEY, raw);
    } catch { /* persistence is best-effort */ }
  }, 400);
}

/** Hydrate persisted last-known-good snapshots into the module cache
 *  (idempotent; never overwrites fresher in-memory entries). */
export async function swrHydrateFromStorage(): Promise<void> {
  if (_hydrated) return;
  _hydrated = true;
  try {
    const { storage } = await import("@/src/utils/storage");
    const raw = await storage.getItem<string>(PERSIST_KEY, "");
    if (!raw) return;
    const parsed = JSON.parse(raw) as Record<string, Snapshot<unknown>>;
    for (const [k, v] of Object.entries(parsed)) {
      if (!_cache.has(k) && v && typeof v.ts === "number") _cache.set(k, v);
    }
  } catch { /* corrupt store → ignore */ }
}

export function swrCacheClear(): void {
  _cache.clear();
}

/** Delete a single cache entry.  Used by the shared consumer registry
 *  to invalidate a specific resource key right before triggering a
 *  mounted-consumer revalidation. */
export function swrCacheDelete(key: string): void {
  _cache.delete(key);
}

/** Read the current cached snapshot for `key` (returns undefined if absent). */
export function swrCacheRead<T>(key: string): T | undefined {
  const snap = _cache.get(key) as Snapshot<T> | undefined;
  return snap?.data;
}

/** Timestamp (ms) of the cached snapshot for `key`, or 0 when absent. */
export function swrCacheTs(key: string): number {
  return _cache.get(key)?.ts ?? 0;
}

/** Imperatively seed the cache (used by primary-tab preload). */
const DETAIL_PREFIXES = ["pick-detail|", "historical-intelligence|"];
const DETAIL_MAX_ENTRIES = 40;
const DETAIL_TTL_MS = 10 * 60_000;

/** Bounded, TTL-swept retention for deep detail objects (never persisted). */
function _sweepDetail(): void {
  const now = Date.now();
  const detail: Array<[string, number]> = [];
  for (const [k, v] of _cache.entries()) {
    if (!DETAIL_PREFIXES.some((p) => k.startsWith(p))) continue;
    if (now - v.ts > DETAIL_TTL_MS) { _cache.delete(k); continue; }
    detail.push([k, v.ts]);
  }
  if (detail.length > DETAIL_MAX_ENTRIES) {
    detail.sort((a, b) => a[1] - b[1]);
    for (const [k] of detail.slice(0, detail.length - DETAIL_MAX_ENTRIES)) _cache.delete(k);
  }
}

export function swrCacheWrite<T>(key: string, data: T): void {
  // Stamp every write with a snapshot of the CURRENT CanonicalEpoch
  // so the revision-ordered sweeper can distinguish CURRENT vs
  // LAST-GOOD entries on any subsequent read.  A null stamp is
  // acceptable pre-boot (before the first response arrives) — the
  // very first advance will discard it via the sweeper.
  const cur = getCurrentEpoch();
  const epoch = cur
    ? {
        origin: cur.origin,
        revision: cur.revision,
        boardVersion: cur.boardVersion,
        generationId: cur.generationId,
      }
    : null;
  _cache.set(key, { data, ts: Date.now(), epoch });
  if (_persistable(key)) _schedulePersist();
  if (DETAIL_PREFIXES.some((p) => key.startsWith(p))) _sweepDetail();
}

// ── Gate 2 P0 (2026-06) · Inflight promise map for TRUE coalescing ─────
// When two components (mount + focus, or index screen + preloader) hit
// the same key simultaneously, they must JOIN one underlying fetch
// rather than fire two identical network requests.  This map holds the
// promise from the FIRST caller; subsequent callers within the same
// tick tree await it.  Cleared on completion (success or error).
const _inflight: Map<string, Promise<unknown>> = new Map();

export function swrInflightCount(): number {
  return _inflight.size;
}

/** Coalesce concurrent fetches for the same key onto one promise. */
export function swrCoalesce<T>(key: string, fetcher: () => Promise<T>): Promise<T> {
  const existing = _inflight.get(key) as Promise<T> | undefined;
  if (existing) return existing;
  const p = (async () => {
    try {
      return await fetcher();
    } finally {
      _inflight.delete(key);
    }
  })();
  _inflight.set(key, p);
  return p;
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
      // GATE 2 P0 — TRUE coalescing: mount + focus + foreground firing
      // the same identity JOIN one underlying network request.
      const next = await swrCoalesce<T>(key, fetcher);
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
      // ─── Ordered epoch gate (2026-06-21 v2) ─────────────────────
      // If the cache entry was written under an OLDER revision (or
      // no stamp) than the currently observed authority, force a
      // silent refresh regardless of ``staleAfterMs``.  This closes
      // the Preview↔Expo drift window WITHOUT waiting for the
      // arbitrary 15 s TTL — the moment ANY canonical response has
      // advanced the in-memory revision, every stamped detail/HI
      // cache row becomes fetch-on-next-read.  A LATE stale response
      // (revision < current) cannot regress the authority.
      const entry = _cache.get(key);
      const stampRev = entry?.epoch?.revision;
      const stampBehind = !isEpochCurrent(stampRev ?? undefined);
      if (stampBehind) {
        void run(true);
      } else if (Date.now() - lastFetchRef.current >= staleAfterMs) {
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
