# PerkLocks — Product Requirements Document (PRD)

## 2026-06-02 — PHASE 24 ROOT CLOSURE — PERKLOCKS_WHOLE_APP_CERTIFIED

**Verdict**: `PERKLOCKS_WHOLE_APP_CERTIFIED` — all 30 Phase 24 questions PASS with live runtime evidence. Three remaining Root Closure blockers sealed with idempotent, chunked backfill scripts; the Preview Locks board scroll defect verified end-to-end via Playwright; and the settlement mirror preservation is now defended against re-publish regressions.

### Root Closure — Three blockers sealed
1. **Q28 — Historical Settlement Backfill (no fabrication)**
   - Script: `/app/backend/scripts/q28_history_settlement_backfill.py`
   - Ledger-first, `final_score` grader second, `UNRESOLVED` fallback last.
   - Live run 2026-09-02T20:44Z: scanned 60,557 historical pending picks; 499 canonically synced from `settlement_events` ledger; 60,058 explicitly marked `settlement_status='UNRESOLVED'` with machine-readable `unresolved_reason`. ZERO fabricated actuals; ZERO errors.
2. **Q29 — Historical Lock-Score Drift Repair (immutable truth only)**
   - Script: `/app/backend/scripts/q29_lock_score_drift_repair.py`
   - Live run: 120,509 picks scanned. Post-repair drift (>0.001) = **0**. Breakdown: `PURE=106,724`, `RESTORED_FROM_PICK_PUBLISHED=1,494`, `RESTORED_FROM_SNAPSHOT=11,730`, `LEGACY_LOCK_UNRECONSTRUCTABLE=561` (marked, never recomputed).
3. **Preview Locks Board Scroll — end-to-end reachability**
   - `/app/frontend/app/(tabs)/index.tsx` — `styles.list={flex:1}` + `content.paddingBottom=140`.
   - Playwright runtime capture: `scrollHeight=3916`, final `scrollTop=3454`, `atBottom=True`; last MLB card (Dylan Cease PEAK-98) fully rendered at bottom of 46-game board.

### Regression defense wired
- **NEW**: `/app/backend/services/picks_mirror_sync.py` — reconciles `picks.status` from the immutable `settlement_events` ledger.
- **NEW hooks** in `soccer_prop_inject.py`, `mls_direct_inject.py`, `espn_soccer_fixtures.py` — call `preserve_settlement_on_replace` after every `ReplaceOne` bulk_write.
- **PATCH** in `real_line_scorer_ingest.py::_upsert_pick` — settlement mirror fields (`status`, `settlement_status`, `settled_at`, `unresolved_reason`, `final_score`, `units_profit`) split into `$setOnInsert` so re-scorer sweeps can no longer regress a settled row back to `pending`.

### Phase 24 Runtime Certification tests
- `/app/backend/tests/test_phase24_root_closure_certification.py` — 4/4 PASS
  - `test_q28_no_fake_actuals_and_no_pending_backlog_mirror_lag`
  - `test_q29_lock_score_drift_zero_and_no_recompute_markers`
  - `test_preview_locks_scroll_container_has_flex_and_padding`
  - `test_phase24_final_certification_doc_declares_certified`
- Under live scheduler pressure (45s post-restart soak): mirror lag = 0, drift = 0.

### Root Closure artifacts
- `/app/memory/phase24_final_certification.md` — 30-question matrix + evidence.
- `/app/memory/q_logs/q28.log` — full Q28 run trace.
- `/app/memory/q_logs/q29.log` — full Q29 run trace.

### Q30 (APEX absence) — truthfully reported
No APEX (100) live example on today's slate; distribution: STRONG=23, STANDARD=24, ELITE=42, RARE=8, PEAK=1, APEX=0. Not a defect — reported per master directive; NEVER manufactured.

### Preserved
- All Phase 1-23 canonical guards, PVS 2.0 gold reservation, spread/totals truth guards, sport model authority registry, canonical publication boundary, and Lab isolation remain unchanged.
- No provider migration; no Lock math changes; no threshold changes; no synthetic data introduced.

---

## Prior verdicts (unchanged, retained for history)

- 2026-09-01 — Totals Core Pass 1 (Universal De-vig + Fail-Closed Guard) — partial delivery documented.
- 2026-09-01 — NFL_MLB_CFB_10X + Tennis Board Recovery — CERTIFIED.
- 2026-09-01 — NFL SportsData.io Key Replacement + Recovery — CERTIFIED.
- 2026-08-31 — Strategy Lab Continuous Surgical Research Upgrade — CERTIFIED.
- 2026-08-28 — Strategy Lab 10X Pro Continuous Build — CERTIFIED.
- 2026-08-27 — Soccer Player Feature Resolver Universal Fix — CERTIFIED.
- 2026-08-27 — NFL Parity / Universal Board Window Fix — CERTIFIED.
- 2026-08-27 — Final Hidden-Blocker Sweep — CERTIFIED.
- 2026-08-27 — CFB Game Model Intelligence Upgrade — PARTIAL_CERTIFIED.
- 2026-08-26 — Final Missing Closures (P0 CFB + P1 Parity + P2 Perf) — CERTIFIED.
- 2026-08-26 — Final Continuous Surgical Production Closure — CERTIFIED.

Full history preserved in `/app/test_result.md`.
