# LockScore AI — Product Requirements (rolling)

## Vision
A sports-betting board that surfaces only high-conviction, model-justified "Lock"
picks (lock_score ≥ 85) across NFL / NBA / NHL / MLB / CFB / Soccer / Tennis / UFC.
Picks are scored by a canonical pipeline (`canonical_picks` → `picks`) governed by
sport-specific lock-score caps and floors so the public board never mixes legit
model-backed value with pure book-implied luck.

## Live board invariants
- `/api/picks/today` → returns on-board picks (`off_board=false`, `lock_score ≥ 85`).
- All sport-specific scoring defects must be caught in the orchestrator's
  post-processing layer; back-fill scripts bring already-published rows in line.

## Session: 2026-06 Lock-Score Governor Fixes
Three defects were reported & fixed in this session:

### A. NHL alt-goal improbability cap
- Issue: `Over N.5 Goals` (skater, N ≥ 1) reaching Lock status despite league
  base-rate of 3-5 % for a skater to score 2+ goals in a game.
- Fix: `pick_refresh_orchestrator.py` post-process caps lock to 84.9 for any
  NHL `Over 1.5+ Goals` pick.  Over 0.5 Goals (anytime scorer) is left uncapped.
- Verified: 0 offenders in `canonical_picks` after the fix.

### B. NHL anytime-scorer wp-floor
- Issue: Over 0.5 Goals clustered at exactly 85.0 regardless of model probability.
- Fix: wp ≥ 0.60 → floor = `min(96.0, 60 + wp × 40)` (raise-only).
- Verified: elite anytime scorers now differentiate above 85.

### C. NBA ALT LOCK reliability floor
- Issue: Hartenstein Over 4.5 Points · ALT LOCK showed lock=59 while
  model wp=90.28 %, edge=+9.18 %.  73 similar defects detected.
- Fix: NBA ALT LOCK picks with wp ≥ 0.70 AND edge ≥ 0 inherit the same
  reliability floor as NHL anytime (`60 + wp × 40`, cap 96).
- Verified: Hartenstein now lock=96.0; 104 picks lifted overall.

### D. Soccer player↔team gate soft-pass
- Issue: ~99 % of Soccer player-props (18 331 → 4 visible) quarantined by
  `roster_unverified` / `roster_conflict` — reasons that mean "unresolved
  evidence" not "confirmed-wrong".
- Fix: Three caller sites now hard-reject only on `player_team_mismatch` /
  `fixture_teams_unknown` / `player_name_missing`.  Everything else is
  soft-passed with a `player_team_evidence_soft` tag.
- Verified: 6 969 Soccer locks now visible (vs ~4 before).

## Credentials for test flows
- Email: `demo@lockscore.ai`
- Password: `demo123`

## Testing status
- `testing_agent` pass 150 — 11/11 pytest cases green.  Zero regressions on
  existing NFL leakage cap / NFL reliability floor blocks.
