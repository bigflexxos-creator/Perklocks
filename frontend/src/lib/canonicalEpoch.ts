/**
 * CanonicalEpoch — the ONE ordered authority for client freshness.
 * ═══════════════════════════════════════════════════════════════════
 *
 * Contract:
 *   type CanonicalEpoch = {
 *     origin:        string    // resolved API base URL (host+port)
 *     revision:      number    // integer, monotone non-decreasing
 *     boardVersion:  string    // opaque hash; correlates 1:1 with revision
 *     generationId:  string    // human-readable commit id
 *     committedAt?:  string
 *   }
 *
 * Transitions (comparing an INCOMING response epoch to CURRENT):
 *
 *   incoming.origin !== current.origin   → "origin_change"
 *     · Old origin caches lose CURRENT status (last-good only).
 *     · Client authority switches to the new origin's epoch as
 *       first sighting on that origin.
 *
 *   incoming.revision >  current.revision → "advance"
 *     · Ordered progress detected.  Notify subscribers, sweep older
 *       stamped caches, atomically commit the new epoch.
 *
 *   incoming.revision === current.revision
 *     · "same" if boardVersion & generationId agree.
 *     · "invariant_violation" if they disagree (same revision must
 *       imply same boardVersion & generationId — logs a warning and
 *       trusts the FIRST sighting; does NOT change authority).
 *
 *   incoming.revision <  current.revision → "stale"
 *     · A late response from a prior generation.  IGNORED COMPLETELY:
 *       do NOT change authority, do NOT sweep, do NOT revalidate.
 *
 *   incoming.revision missing OR incoming.origin missing → "ignored"
 *
 * Persistence: ONE object at ``canonical_epoch_v1`` in AsyncStorage
 * (JSON).  Cold-boot hydrate is atomic (no partial state).  Origin
 * changes clear the persisted epoch so a preview→prod switch cannot
 * silently retain stale caches across environments.
 */
import { storage } from "@/src/utils/storage";

const PERSIST_KEY = "canonical_epoch_v1";

export type CanonicalEpoch = {
  origin: string;
  revision: number;
  boardVersion: string;
  generationId: string;
  committedAt?: string | null;
};

export type EpochTransition =
  | "advance"
  | "same"
  | "stale"
  | "invariant_violation"
  | "origin_change"
  | "ignored";

/** In-memory current epoch.  Null until the first response arrives.
 *  Never regresses — only "advance" and "origin_change" mutate it. */
let _current: CanonicalEpoch | null = null;

/** Advance listeners — called with (next, previous) on every real
 *  progression.  useSWR & the shared consumer registry attach here. */
type AdvanceListener = (next: CanonicalEpoch, previous: CanonicalEpoch | null) => void;
const _listeners: Set<AdvanceListener> = new Set();

let _hydrated = false;

export async function hydrateCanonicalEpoch(): Promise<void> {
  if (_hydrated) return;
  _hydrated = true;
  try {
    const persisted = await storage.getItem<CanonicalEpoch | null>(PERSIST_KEY, null);
    if (
      persisted &&
      typeof persisted === "object" &&
      typeof persisted.revision === "number" &&
      typeof persisted.origin === "string" &&
      typeof persisted.boardVersion === "string" &&
      typeof persisted.generationId === "string"
    ) {
      _current = persisted;
    }
  } catch { /* corrupt store — ignore */ }
}

function _persist(): void {
  if (_current === null) {
    storage.removeItem(PERSIST_KEY).catch(() => undefined);
    return;
  }
  storage.setItem(PERSIST_KEY, _current).catch(() => undefined);
}

/** Compare an incoming epoch tuple to the current authority.  Returns
 *  the transition classification and, when appropriate, mutates the
 *  current authority + notifies subscribers.  ALL freshness decisions
 *  route through this function.
 *
 *  Never throws.  Never regresses.  Never trusts an opaque hash for
 *  ordering — only ``revision`` decides.
 */
export function noteCanonicalEpoch(input: {
  origin?: string | null;
  revision?: number | string | null;
  boardVersion?: string | null;
  generationId?: string | null;
  committedAt?: string | null;
}): EpochTransition {
  const origin = (input.origin || "").trim();
  const bv = (input.boardVersion || "").trim();
  const gid = (input.generationId || "").trim();
  let rev: number;
  if (typeof input.revision === "number") {
    rev = Math.floor(input.revision);
  } else if (typeof input.revision === "string" && input.revision.trim()) {
    rev = parseInt(input.revision, 10);
  } else {
    return "ignored";
  }
  if (!Number.isFinite(rev) || rev < 0) return "ignored";
  if (!origin) return "ignored";
  // NB: bv & gid may be empty in edge cases (initial commit / test
  // stub) — accept them but log an invariant note.  Never gate the
  // ordering on their presence.

  const prev = _current;
  const incoming: CanonicalEpoch = {
    origin, revision: rev, boardVersion: bv, generationId: gid,
    committedAt: input.committedAt || null,
  };

  if (!prev) {
    // First sighting this boot / after origin change / after
    // corrupted persisted state.  Adopt without sweeping — there's
    // no prior authority to protect.
    _current = incoming;
    _persist();
    return "advance"; // sweep-safe: no older stamps exist yet
  }

  if (prev.origin !== origin) {
    // API origin changed (preview → prod, tunnel host rotation).
    // Old-origin caches are ancient by definition; sweep them and
    // adopt the new epoch as a fresh authority.
    _current = incoming;
    _persist();
    for (const fn of _listeners) { try { fn(incoming, prev); } catch {} }
    return "origin_change";
  }

  if (rev > prev.revision) {
    _current = incoming;
    _persist();
    for (const fn of _listeners) { try { fn(incoming, prev); } catch {} }
    return "advance";
  }

  if (rev === prev.revision) {
    if ((bv && prev.boardVersion && bv !== prev.boardVersion) ||
        (gid && prev.generationId && gid !== prev.generationId)) {
      // Same revision but different fingerprint — this is a genuine
      // invariant violation (two different commits sharing a revision
      // number, or a backend that reset revision without a bump).
      // Log and TRUST THE FIRST SIGHTING.  Never regress.
      try {
        // eslint-disable-next-line no-console
        console.warn(
          "[canonical-epoch] invariant violation @ revision",
          rev, "prev", prev, "incoming", incoming,
        );
      } catch {}
      return "invariant_violation";
    }
    return "same";
  }

  // rev < prev.revision — LATE STALE RESPONSE.  Ignore completely.
  return "stale";
}

export function getCurrentEpoch(): CanonicalEpoch | null {
  return _current;
}

/** True iff a stamped resource carrying ``revision`` is CURRENT vs.
 *  the client's authoritative epoch.  A missing/undefined stamp is
 *  never current. */
export function isEpochCurrent(revision: number | null | undefined): boolean {
  if (typeof revision !== "number" || !Number.isFinite(revision)) return false;
  const cur = _current;
  if (!cur) return false;
  return revision === cur.revision;
}

/** True iff a stamped resource is a LEGITIMATE last-good — same
 *  origin, older revision.  These may paint temporarily; the caller
 *  MUST revalidate through the shared consumer registry. */
export function isEpochLastGood(input: {
  origin?: string | null; revision?: number | null;
}): boolean {
  const cur = _current;
  if (!cur) return false;
  if (typeof input.revision !== "number" || !Number.isFinite(input.revision)) return false;
  return (input.origin === cur.origin) && (input.revision < cur.revision);
}

export function subscribeCanonicalEpoch(fn: AdvanceListener): () => void {
  _listeners.add(fn);
  return () => { _listeners.delete(fn); };
}

// Test hooks — never used in production.
export function _resetForTests(next: CanonicalEpoch | null = null): void {
  _current = next;
}
