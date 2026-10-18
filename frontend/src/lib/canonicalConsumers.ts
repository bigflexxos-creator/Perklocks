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
import { subscribeCanonicalEpoch, CanonicalEpoch, getCurrentEpoch } from "@/src/lib/canonicalEpoch";

export type ConsumerRevalidate = (targetRevision: number) => void | Promise<void>;

type Entry = { fn: ConsumerRevalidate; lastTargetInvoked: number };

const _consumers: Map<string, Entry> = new Map();

// ── 2026-10-18 Perklocks Defect 12 — storm coalescing ──────────────
// When the backend commits multiple canonical generations in rapid
// succession (N → N+1 → N+2 inside a few hundred ms) every
// subscribeCanonicalEpoch advance fires a full consumer flush.  With
// 7+ mounted consumers (Locks + Rollover + Parlay + Historical + …)
// that produces 21+ board fetches in flight at once — a thundering
// herd that both drowns the backend AND causes the request-dedupe
// map to churn.  We coalesce: schedule a single flush per microtask;
// if a newer advance lands before the flush fires, upgrade the
// target and keep waiting.  Guarantees AT MOST ONE flush per
// adjacent burst, at the LATEST known revision.
let _coalesceTimer: ReturnType<typeof setTimeout> | null = null;
let _pendingTarget: number = -1;
const COALESCE_WINDOW_MS = 120; // ~1 animation frame of grace; fast enough that
                                 // the user never notices, long enough that a
                                 // N→N+1→N+2 burst collapses to one flush.

function _scheduleFlush(target: number): void {
  // Upgrade target if newer; never regress.
  if (target > _pendingTarget) _pendingTarget = target;
  if (_coalesceTimer) return;
  _coalesceTimer = setTimeout(() => {
    _coalesceTimer = null;
    const effectiveTarget = _pendingTarget;
    _pendingTarget = -1;
    for (const [_key, entry] of _consumers.entries()) {
      if (entry.lastTargetInvoked === effectiveTarget) continue;
      entry.lastTargetInvoked = effectiveTarget;
      try {
        const p = entry.fn(effectiveTarget);
        if (p && typeof (p as Promise<void>).then === "function") {
          (p as Promise<void>).catch(() => { /* consumer errors isolated */ });
        }
      } catch { /* isolate */ }
    }
  }, COALESCE_WINDOW_MS);
}

let _wired = false;
function _wire(): void {
  if (_wired) return;
  _wired = true;
  subscribeCanonicalEpoch((next: CanonicalEpoch, previous) => {
    // origin_change: clear dedup memory (new cache space) + cancel
    // any pending coalesced flush — we need to reinvoke every
    // consumer fresh against the new origin.
    if (previous && previous.origin !== next.origin) {
      for (const e of _consumers.values()) e.lastTargetInvoked = -1;
      if (_coalesceTimer) { try { clearTimeout(_coalesceTimer); } catch {} _coalesceTimer = null; }
      _pendingTarget = -1;
    }
    // 2026-10-18 Perklocks Defect 12 — coalesce rapid-fire advances
    // into a single flush at the LATEST revision.
    _scheduleFlush(next.revision);
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
export function _resetForTests(): void {
  _consumers.clear();
  if (_coalesceTimer) { try { clearTimeout(_coalesceTimer); } catch {} _coalesceTimer = null; }
  _pendingTarget = -1;
}
/** TEST ONLY — synchronously drain any pending coalesced flush. */
export function _flushForTests(): void {
  if (_coalesceTimer) { try { clearTimeout(_coalesceTimer); } catch {} _coalesceTimer = null; }
  const effectiveTarget = _pendingTarget >= 0 ? _pendingTarget
    : (getCurrentEpoch()?.revision ?? -1);
  _pendingTarget = -1;
  if (effectiveTarget < 0) return;
  for (const [_key, entry] of _consumers.entries()) {
    if (entry.lastTargetInvoked === effectiveTarget) continue;
    entry.lastTargetInvoked = effectiveTarget;
    try { entry.fn(effectiveTarget); } catch { /* isolate */ }
  }
}
