// Single source of truth for the score we render in any UI surface.
//
// Why this exists: the backend pick document carries THREE lock scores —
//   • `published_lock_score` — CANONICAL frozen at publication (P0-*)
//   • `lock_score`     — legacy V1 score (may drift after learning passes)
//   • `lock_score_v2`  — SHADOW / diagnostic V2 score (challenger model)
//
// Phase B4 (2026-06 μ-closure): authoritative Lock Score UI must ONLY
// read the CANONICAL frozen value.  We must NEVER promote the shadow
// V2 challenger over canonical.  If canonical is missing (legacy row
// pre-P0-1 dual-write), we fall back to legacy V1 — never to V2.
//
// Phase B5 (2026-06 μ-closure): the display was silently clamped to
// 99, hiding legitimate Apex 100 canonical scores.  The clamp is now
// [0, 100] and only truncates values outside the canonical range.
//
// Use `getDisplayLock(pick)` EVERY time you render a lock score to the
// user. Never read `pick.lock_score` / `pick.lock_score_v2` directly
// in render code.

export type LockScoreShape = {
  published_lock_score?: number | string | null;
  lock_score?: number | string | null;
  lock_score_v2?: number | string | null;
};

/**
 * Returns the CANONICAL Lock Score for user-facing display.
 * Priority:
 *   1. `published_lock_score` — canonical frozen publication truth
 *   2. `lock_score`           — legacy V1 (only if canonical absent)
 * NEVER falls back to `lock_score_v2` (shadow / diagnostic only).
 * Clamped to [0, 100] (Apex 100 is a legitimate canonical peak).
 */
export function getDisplayLock(pick: LockScoreShape | null | undefined): number {
  if (!pick) return 0;
  // 1. Canonical published Lock Score is authoritative.
  const pub = Number(pick.published_lock_score);
  if (Number.isFinite(pub) && pub > 0) {
    return Math.min(100, Math.max(0, pub));
  }
  // 2. Legacy V1 fallback for pre-publication rows.  V2 is NEVER used
  //    for authoritative UI display — it is diagnostic only.
  const v1 = Number(pick.lock_score);
  const safe1 = Number.isFinite(v1) ? v1 : 0;
  return Math.min(100, Math.max(0, safe1));
}

/** Rounded integer convenience for compact UI badges. */
export function getDisplayLockRounded(pick: LockScoreShape | null | undefined): number {
  return Math.round(getDisplayLock(pick));
}
