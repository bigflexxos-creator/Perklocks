/**
 * Universal Canonical Freshness Observer
 * ═══════════════════════════════════════════════════════════════════
 * ONE monotonic client-side signal derived from the backend's
 * ``X-Board-Version`` response header.  Every SWR / AsyncStorage entry
 * that persists across app lifecycles is TAGGED with the board_version
 * it was written under.  When a subsequent response reports a NEWER
 * board_version, every entry stamped with an older version is
 * automatically discarded — the very next fetch re-populates from
 * canonical truth.
 *
 * Applies universally to:
 *   • MLB / NFL / CFB / Soccer / Tennis / NBA (all sports)
 *   • Locks board
 *   • Pick Breakdown / detail
 *   • Historical Intelligence
 *   • Rollover
 *   • Parlay
 *   • My Bets
 *   • Lab / Analytics
 *   • Any future canonical surface
 *
 * Offline last-good behaviour is preserved: a cached response is still
 * served instantly if the network is unreachable — the invalidation
 * only fires when the network delivers a NEWER canonical version.  The
 * observed "latest known" version persists to AsyncStorage so a cold
 * boot with a stale cache reconciles the moment the first API call
 * lands.
 */
import { storage } from "@/src/utils/storage";

const PERSIST_KEY = "board_version_v1";

/** In-memory latest observed board_version.  Compared against every
 *  future response.  Persisted to storage on change. */
let _current: string | null = null;

/** Subscribers notified whenever a new board_version supersedes the
 *  cached one.  useSWR registers a sweeper that clears stale entries. */
type Listener = (next: string, previous: string | null) => void;
const _listeners: Set<Listener> = new Set();

let _hydrated = false;

/** Hydrate the last observed board_version from AsyncStorage.  Called
 *  at app-boot from ``_layout.tsx`` alongside ``swrHydrateFromStorage``. */
export async function hydrateBoardVersion(): Promise<void> {
  if (_hydrated) return;
  _hydrated = true;
  try {
    const v = await storage.getItem<string>(PERSIST_KEY, "");
    if (v && typeof v === "string") _current = v;
  } catch { /* corrupt store — ignore */ }
}

/** Notify observer of a board_version seen on a response header.
 *  Returns:
 *    · "new"       — advanced past a previously known version (invalidation runs)
 *    · "first"     — first version we've ever seen this boot (no invalidation;
 *                     just remember it for future comparisons)
 *    · "same"      — matches the cached version (no-op)
 *    · "ignored"   — empty / null (no-op) */
export function noteBoardVersion(v?: string | null): "new" | "first" | "same" | "ignored" {
  if (!v || typeof v !== "string") return "ignored";
  const trimmed = v.trim();
  if (!trimmed) return "ignored";
  const prev = _current;
  if (prev === trimmed) return "same";
  _current = trimmed;
  // Persist best-effort — invalidation runs regardless of persistence.
  storage.setItem(PERSIST_KEY, trimmed).catch(() => undefined);
  if (!prev) return "first";
  // Advance detected — notify subscribers so they sweep stale caches.
  for (const fn of _listeners) {
    try { fn(trimmed, prev); } catch { /* isolate subscriber errors */ }
  }
  return "new";
}

/** Current known board_version (may be null before first response). */
export function getCurrentBoardVersion(): string | null {
  return _current;
}

/** Register a listener that runs whenever a NEWER version is observed. */
export function subscribeBoardVersion(fn: Listener): () => void {
  _listeners.add(fn);
  return () => { _listeners.delete(fn); };
}

/** Force-set for tests / imperative flushes (never used in production). */
export function _resetBoardVersionForTests(v: string | null = null): void {
  _current = v;
}
