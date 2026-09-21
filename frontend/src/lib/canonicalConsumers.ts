/**
 * Canonical Consumer Registry — mounted-consumer revalidation.
 * ═══════════════════════════════════════════════════════════════════
 * Locks, Pick Detail, Historical Intelligence, Rollover, Parlay, My
 * Bets, Lab/Analytics all own mounted React state.  Sweeping the SWR
 * cache is not enough — the mounted view has already read the stale
 * data.  So each active consumer registers a ``revalidate(target)``
 * function keyed by a resource id.
 *
 * On CanonicalEpoch advance the registry:
 *   1. Reads the new epoch's revision (target).
 *   2. For every registered consumer, invokes revalidate(target).
 *   3. Deduplicates by (resource key, target revision) — a consumer
 *      that has already requested this target is skipped.
 *   4. Records the last invoked-target per key so repeated advances
 *      within the same epoch never fire twice.
 *
 * On origin_change the registry treats it identically to an advance
 * (target = new revision) but ALSO clears its dedup memory — new
 * origin is a fresh cache-space.
 *
 * Zero sport-specific coupling.  Zero endpoint-specific coupling.
 */
import { subscribeCanonicalEpoch, CanonicalEpoch } from "@/src/lib/canonicalEpoch";

export type ConsumerRevalidate = (targetRevision: number) => void | Promise<void>;

type Entry = { fn: ConsumerRevalidate; lastTargetInvoked: number };

const _consumers: Map<string, Entry> = new Map();

let _wired = false;
function _wire(): void {
  if (_wired) return;
  _wired = true;
  subscribeCanonicalEpoch((next: CanonicalEpoch, previous) => {
    // origin_change: clear dedup memory (new cache space).
    if (previous && previous.origin !== next.origin) {
      for (const e of _consumers.values()) e.lastTargetInvoked = -1;
    }
    const target = next.revision;
    for (const [_key, entry] of _consumers.entries()) {
      if (entry.lastTargetInvoked === target) continue; // dedup
      entry.lastTargetInvoked = target;
      try {
        const p = entry.fn(target);
        if (p && typeof (p as Promise<void>).then === "function") {
          (p as Promise<void>).catch(() => { /* consumer errors isolated */ });
        }
      } catch { /* isolate */ }
    }
  });
}
_wire();

/** Register a consumer for canonical revalidation.
 *  Returns an unregister function.  Called from mounted components'
 *  useEffect / useFocusEffect — always paired with cleanup. */
export function registerCanonicalConsumer(
  key: string,
  fn: ConsumerRevalidate,
): () => void {
  _consumers.set(key, { fn, lastTargetInvoked: -1 });
  return () => {
    const cur = _consumers.get(key);
    if (cur && cur.fn === fn) _consumers.delete(key);
  };
}

/** Number of active consumers — for tests / diagnostics only. */
export function _consumerCountForTests(): number { return _consumers.size; }
export function _resetForTests(): void { _consumers.clear(); }
