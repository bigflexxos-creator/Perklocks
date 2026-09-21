# PerksLocks — Product Requirements (Live Delta 2026-06-21)

## Canonical Epoch v2 Contract — CLOSED (unpublished)

Ordered-revision authority. Origin-bound. Single-flight refresh. Shared consumer registry that revalidates mounted React state on advance. Late N responses after N+1 are `stale` and IGNORED — authority never regresses.

Six root defects closed:
1. Opaque hash used as ordered version → replaced with integer `revision`.
2. Locks tab caches outside SWR → stamped with `CanonicalEpoch`.
3. Cache deletion didn't refresh mounted state → shared consumer registry.
4. Refresh single-flight → `targetRevision` guard + explicit ownership release.
5. `/api/version` no-cache + `canonical_epoch` in body.
6. Origin now part of the epoch key.

**Tests targeted at shared contract only: 31 (6 backend + 25 frontend). PASS.**

Full sport / model suites intentionally NOT rerun.

Handed back for one physical Expo Go check. NOT published.

Full report: `/app/memory/canonical_epoch_v2_closure_2026_06_21.md`.

---

## Prior — CFB Sign Fix + Soccer HI (still valid)
`/app/memory/p0_universal_root_closure_final_2026_06_21.md`

---

## 2026-06-21 · Backend DATA_VERSION sync (surgical)
- Support confirmed Expo stale-bundle root cause is external. Investigation CLOSED.
- Preview QR (`exp://canonical-parity.preview.emergentagent.com`) is the sole physical test target.
- Deployed snapshot (`bet-edge-ai-1.emergent.host`) remains intentionally frozen — DO NOT republish.
- `backend/server.py` `DATA_VERSION` bumped from `2026.06.11-soccer-shots-sot-assists-wired-v49` → `2026.06.21-canonical-epoch-v2-signfix` to align `/api/version` with the current Canonical Parity frontend.
- Verified on Preview:
  - `GET /api/version` → `data_version: 2026.06.21-canonical-epoch-v2-signfix`
  - Profile screen shows Build/Source/Backend all pointing at Canonical Parity.
- NOT touched: Canonical Epoch logic, board generation, DB, API origins, Metro config.



---

## 2026-06-21 · Rollover True Immutability + Parlay 3.0 Root Closure
- **Rollover**: Frozen slate v2 with FULL wager snapshot (line/odds/book/WP/Lock/publication_version) + DB unique index on (slate_date, scope) + PICK_MISSING no longer auto-invalidates + frozen-view reader (`services/rollover_frozen_view.py`) serves wager truth from `legs[]` — never from mutable `db.picks`. Migration `scripts.migrate_rollover_slates_frozen_wager_v2` idempotent, only backfills fields provably recoverable from FROZEN events; never manufactures historical truth.
- **Parlay 3.0**: Single `ModePolicy` authority (`services/parlay/mode_policy.py`) + Feasibility Engine (`services/parlay/feasibility.py`) with structured reason codes. HIGH_RISK MLB 10/15/20 now returns truthful 6-of-10/6-of-15/6-of-20 partial cards instead of empty state. Saved parlay leg snapshot marks `frozen_wager_version=2`.
- 17-test regression suite passes: `tests/test_rollover_immutability_and_parlay30.py`.
- Full memo: `memory/rollover_immutability_and_parlay30_closure_2026_06_21.md`.
- PRODUCTION PUBLISHED: NO.


---

## 2026-06-21 · Parlay 3.0 Universal Sport Closure
- **Universal Dependency Authority** (`services/parlay/dependency.py`): same-event fail-closed for MLB/NFL/NBA/NHL/UFC/Tennis/CFB/Soccer. No more scattered per-sport branches.
- **HIGH_RISK edge policy**: now `min_edge_pct=None` — edge is a ranking input, not a hard gate. ADVANCED_HIGH_EV remains the +edge gate.
- **Per-mode `health_weights`** wired into `parlay_health()` — 12-leg HIGH_RISK tickets no longer auto-punished for being 12 legs.
- **Pin validation** (`services/parlay/pins_and_alternates.validate_pin`): returns PIN_ACCEPTED / PIN_CONFLICT + reason codes (`NOT_CANONICAL`, `NO_REAL_ODDS`, `BELOW_LOCK_FLOOR`, `DEPENDENCY_CONFLICT`, …). `pin_report` in every response.
- **Alternate ranking**: replaces "rank by Lock" — now ranks by dependency safety → mode eligibility → diversification → survival impact → Lock (tiebreak).
- **Regenerate determinism**: `refresh_nonce=0` → byte-identical fingerprints. `refresh_nonce>0` → controlled diversity.
- **Saved parlay wager freeze v2** verified across MLB/NFL/Soccer/Tennis.
- 43 regression tests pass (`tests/test_rollover_immutability_and_parlay30.py`, `tests/test_parlay30_universal_closure.py`).
- Memo: `memory/parlay30_universal_closure_2026_06_21.md`. PRODUCTION PUBLISHED: NO.
