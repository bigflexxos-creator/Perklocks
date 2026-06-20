// Single source of truth for the score we render in any UI surface.
//
// Why this exists: the backend pick document carries TWO lock scores —
//   • `lock_score`     — legacy V1 score (what the writer wrote on creation)
//   • `lock_score_v2`  — recomputed V2 score (the canonical bet-quality grade)
// V1 and V2 can drift apart between learning passes, and we don't want
// every screen to invent its own max() / fallback logic (we just lived
// that bug — card showed 94, detail showed 85).
//
// Use `getDisplayLock(pick)` EVERY time you render a lock score to the
// user. Never read `pick.lock_score` directly in render code.

export type LockScoreShape = {
  lock_score?: number | string | null;
  lock_score_v2?: number | string | null;
};

/**
 * Returns the score the user should see on screen.
 * Always: max(lock_score, lock_score_v2), coerced to a finite number,
 * clamped to [0, 99].
 */
export function getDisplayLock(pick: LockScoreShape | null | undefined): number {
  if (!pick) return 0;
  const v1 = Number(pick.lock_score);
  const v2 = Number(pick.lock_score_v2);
  const safe1 = Number.isFinite(v1) ? v1 : 0;
  const safe2 = Number.isFinite(v2) ? v2 : 0;
  const best = Math.max(safe1, safe2);
  return Math.min(99, Math.max(0, best));
}

/** Rounded integer convenience for compact UI badges. */
export function getDisplayLockRounded(pick: LockScoreShape | null | undefined): number {
  return Math.round(getDisplayLock(pick));
}
