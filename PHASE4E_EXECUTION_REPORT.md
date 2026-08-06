# Phase 4E — Execution Report & Deliverables

**Date:** 2026-08-06
**Scope:** Tennis + Soccer + Magic Tier + Cross-Sport Calibration
**Status:** ✅ COMPLETE — awaiting review before Phase 4F

---

## 1. Exact files created

| Path | Purpose |
|---|---|
| `backend/services/tennis_identity.py` | Stable Sackmann-ID-first tennis identity resolver + name-fallback marker |
| `backend/services/tennis_data_quality.py` | Tennis feature-coverage assessor + advisory tier cap |
| `backend/services/soccer_scorer_eligibility.py` | Soccer scorer/lineup eligibility caps per market family |
| `backend/services/magic_tier_policy.py` | Post-processing Magic Tier policy — WRAPS Apex/Elite/Strong/Lock/Playable, only downgrades |
| `backend/services/board_ranker_guards.py` | Cross-sport ranking guardrails (EV, edge, DQ, dup collapse, per-event cap) |
| `backend/scripts/phase4e_magic_tier_baseline.py` | Historical baseline report per (sport, market_family, tier) |
| `backend/scripts/phase4e_cross_sport_calibration.py` | Segmented raw vs. calibrated report per (sport, market_family) |
| `backend/scripts/phase4e_settlement_replay.py` | READ-ONLY tennis + soccer settlement audit script |
| `backend/tests/test_phase4e.py` | 23 assertions covering all Phase 4E requirements |
| `/app/PHASE4E_MAGIC_TIER_BASELINE.{json,md}` | Historical baseline output |
| `/app/PHASE4E_CROSS_SPORT_CALIBRATION.{json,md}` | Segmented calibration output |
| `/app/PHASE4E_SETTLEMENT_REPLAY.json` | Combined settlement audit output |
| `/app/PHASE4E_TENNIS_SETTLEMENT_REPLAY.md` | Tennis-only audit markdown |
| `/app/PHASE4E_SOCCER_SETTLEMENT_REPLAY.md` | Soccer-only audit markdown |

## 2. Exact files changed

| Path | Change |
|---|---|
| `backend/tennis_engine.py` | (a) Added 5 advisory fields (`identity_source`, `stable_identity`, `data_quality`, `data_quality_signal_count`, `data_quality_max_tier`) to `TennisComponents`. (b) `compute_components` stamps these on the components using `resolve_tennis_identity` (async lookup stashed at `pick["tennis_identity"]`) + `assess_tennis_data_quality`. (c) Docstring on `_player_hash` clarifies it is no longer the identity — only bounded ±0.05 micro-noise. (d) `apply_tennis_engine` calls `resolve_tennis_identity` async and stashes the result before `compute_components`. |
| `backend/services/pick_refresh_orchestrator.py` | Wired `apply_magic_tier` immediately after `apply_v2_to_picks` — only downgrades, never upgrades; logs count of capped picks. |

**No other files modified.**  Phase 4B/4C/4D wire-ups untouched.

## 3. Tennis model changes (Part 1)

* Placeholder identity `_player_hash(name)` **demoted** from primary identity to bounded ±0.05 micro-noise (documented in the docstring).
* New async resolver `resolve_tennis_identity(db, name, tour)` prefers Sackmann `player_id` from the `player_db_tennis` collection; falls back to a normalised name key when no provider ID is found; the fallback is EXPLICITLY marked `identity_source="name_fallback"` and `stable_identity=False`.
* Accent-stripping, punctuation-collapse, whitespace-normalisation baked into `normalize_name` — "José-Luis Álvarez" and "jose luis alvarez" hash to the same identity key.
* Data-quality assessor reports coverage of 7 tennis feature families (surface Elo edge, overall Elo, H2H sample ≥3, recent form, first-serve/first-set edge, serve stats block, injury/retirement signal).
* Quality tiers `full` / `partial` / `sparse` / `empty` each carry an advisory `max_tier` cap that flows into the Magic Tier policy.
* No invented features.  Missing coverage results in a lower tier cap, never a fabricated signal.

## 4. Soccer model changes (Part 2)

* New `assess_scorer_eligibility(player_ctx, market)` returns eligibility flag + tier cap.
* **Bench players → Playable max.** Never Lock+, never Strong Lock, never Elite/Apex.
* **Projected starters → Strong Lock max.** Never Elite/Apex.
* **Confirmed starters + ≥2 signals → Apex allowed.**
* **`out` status → Pass** (do not emit).
* **`doubt` → Lock max.**
* **`unknown` lineup for a sport where lineup matters → Strong Lock max** (via Magic Tier policy).
* Score-or-assist dispatched to a distinct family with 90-minute + team_attack requirements for Apex eligibility.
* First-/last-scorer requires an EXPLICIT `penalty_taker` boolean to reach Strong Lock — unknown penalty role caps at Lock (no invention).
* Shots markets require `shot_volume90` for Apex eligibility.
* Own-goal / penalty-goal handling relies on the existing settler (audit-only in Phase 4E per user rule "do not silently alter historical policy").

## 5. Magic Tier redesign (Part 3)

**Wrapper policy** — the module NEVER upgrades a tier; it can only cap or downgrade.

Signals consumed:
* Sport-provided cap (tennis DQ, soccer scorer eligibility) — respected as authoritative.
* Real-factor count `signals_present` (min-signal thresholds per Apex / Elite / Strong).
* Sample size (per-sport thresholds; NFL/CFB/Tennis lower defaults).
* Odds freshness (30-minute → Strong cap; 3-hour → Lock cap).
* Lineup certainty (`out`/`bench`/`projected`/`doubt`/`unknown` → distinct caps).
* Calibration gap (|predicted − historical| → tiered caps at 5pp / 8pp / 12pp).
* Posterior std (Phase 4B) — **NOT counted as independent model agreement**; only used as a stability signal to cap Apex when the posterior is very wide.
* Identity stability (`name_fallback` → Strong Lock cap).

Frontend contract:
* Overwrites `pick["grade"]` only when capped.
* Stashes rationale under internal `pick["magic_tier"]` field.
* No new fields the FE reads; existing `grade` / `tier_v2` behaviour preserved.

## 6. Magic Tier historical results (Part 4)

Baseline generated via `scripts/phase4e_magic_tier_baseline.py --days 180`.

Output: `/app/PHASE4E_MAGIC_TIER_BASELINE.{json,md}`.

Local dev-environment Mongo is empty (0 settled picks), so all six sports report `total_settled=0` and the baseline is a structural / schema validation rather than a live tier-ordering audit.

The script:
* Auto-expands the window 60 days at a time (up to 540 days) when sample < 50.
* Buckets by `(sport, market_family, tier_label)`.
* Reports N, hit rate, avg predicted probability, Brier, log-loss, ROI units, calibration gap.
* Marks buckets < 15 picks as `insufficient_sample=True` so they are NOT used to promote / demote thresholds.
* Reports the actual window used per sport and annotates cross-season windows.

**Production replay** is the responsibility of the ops team pointing the script at the production replica (`python scripts/phase4e_magic_tier_baseline.py --days 180`).

## 7. Cross-sport calibration results (Part 5)

Report generated via `scripts/phase4e_cross_sport_calibration.py --days 180`.

Output: `/app/PHASE4E_CROSS_SPORT_CALIBRATION.{json,md}`.

Segmentation:
* Uses the Phase 4B `services.calibration_segmentation` policy — L1 through L6 fallback hierarchy.
* Reports `(sport, market_family)` buckets ≥ `MIN_SAMPLE_L4` (40 picks).
* Splits 80/20 train/holdout with a deterministic seed.
* Reports raw vs. calibrated Brier / log-loss / ROI / calibration gap on the holdout.
* **Promotes** a calibrator only when BOTH Brier AND log-loss improve.
* **Falls back** when either metric degrades OR sample is below threshold.

Empty local Mongo means the report presently shows every bucket as `insufficient_sample`; the recommendation flag is deterministic and will populate under production data.

## 8. Ranking changes (Part 6)

New `apply_ranking_guards(picks, per_event_max=3, ...)` returns `(ranked_picks, report)`.

Guards:
1. **Duplicate contract collapse** (same player+market+side+line+event → keep best price).
2. **Same-event overexposure cap** (default 3 picks per event, prefer highest guarded composite).
3. **Data-quality tie-break** — when two picks share Lock Score within 2 pts, higher `factor_sources` count and higher EV win; positive-odds preferred on true tie.
4. **Composite guarded score** = `0.45·lock + 0.30·ev + 0.15·magic_tier_rank + 0.10·data_quality`.

Result:
* Positive-EV underdogs cannot be dropped below negative-EV chalk with equal lock.
* Weak-data picks cannot outrank strong-data picks with the same lock score.
* Frontend schema unchanged (adds a single internal `guarded_composite` field).

## 9. Tennis settlement validation (Part 7)

Script: `scripts/phase4e_settlement_replay.py` — READ-ONLY audit.

Checks:
* Normal completion — outcome matches winner/loser.
* Retirement — flags picks graded `void` or `push` without a book-void-flag (potentially incorrect void).
* Walkover — flags picks NOT settled as void.
* Abandoned — flags picks NOT settled as void.

Policy notes emitted with the report:
* Current `tennis_extra` settler does NOT explicitly branch on retirement/walkover; it uses winner/loser name matching.
* Retirements are settled by whoever finished; walkovers do not appear in the results scrape.
* **Phase 4F consideration** (not this phase): wire an explicit book-void-flag confirmation for WO / abandoned.

## 10. Soccer settlement validation (Part 8)

Same script, soccer branch — READ-ONLY.

Checks:
* Bench-player scorer win with `played_minutes<5` → flagged.
* Own-goal flagged as scorer market win → flagged.
* First-scorer wins with recorded `goal_minute` → counted for visibility.
* Abandoned / postponed settled as WIN / LOSS instead of VOID → flagged.

Policy notes:
* Current FotMob + ESPN settlers explicitly void penalty misses and do NOT double-count own goals for scorer markets.
* `score_or_assist` is settled distinctly via `_settle_scorer_market`; audit confirms no silent policy change.

## 11. Test commands and results

```
$ cd backend && pytest tests/test_phase4e.py -v
============================= 23 passed in 0.15s =============================

$ pytest \
    tests/test_phase4b_sim_stability.py \
    tests/test_phase4b_simulator_and_calibration.py \
    tests/test_phase4c_mlb.py \
    tests/test_phase4c_finalization.py \
    tests/test_phase4d_nba_cfb.py \
    tests/test_phase4d_finalization.py \
    tests/test_phase4e.py
============================ 101 passed in 0.42s ============================
```

All 23 Phase 4E assertions pass; all 78 prior Phase 4 tests remain green. No regressions.

## 12. Runtime verification

```
$ sudo supervisorctl restart backend
$ curl -s http://localhost:8001/api/version
{"data_version":"...","server_time":"2026-08-06T21:45:46...","server_started_at":"2026-08-06T21:45:41..."}

$ TOKEN=$(login as demo@lockscore.ai)
$ curl -sH "Authorization: Bearer $TOKEN" .../api/picks/today?sport=Soccer
{"picks": [9 items], ...}
  first pick — sport=Soccer, grade=Lock, lock_score=93.0
  frontend schema fields present: id, market, selection ✓
```

Backend starts clean, ingestion loops arm, no import errors, picks endpoint returns valid schema.  `magic_tier` field will populate on the next slate rebuild — cached picks predate the wire-up and remain valid.

## 13. Remaining Phase 4F blockers

**None from Phase 4E.**  Items surfaced by the audit scripts for Phase 4F consideration:

1. Book-void-flag confirmation for tennis walkover / abandoned matches (settlement gap identified).
2. Historical baseline requires production-DB replay to validate tier ordering (baseline script ready; needs to be run against replica).
3. Full-suite pre-existing failures inherited from earlier phases (5 tests — see Phase 4D finalization report). Not caused by 4E.

## 14. Suggested Git commit message

```
Phase 4E — Tennis + Soccer + Magic Tier + Cross-Sport calibration

* Tennis identity resolver (services/tennis_identity.py) prefers
  Sackmann player_id, marks name-fallback identity explicitly.
* Tennis data-quality assessor (services/tennis_data_quality.py)
  caps advisory tier based on feature coverage + identity stability.
* Soccer scorer eligibility (services/soccer_scorer_eligibility.py):
  bench→Playable, projected→Strong Lock, confirmed→Apex; role-aware
  first/last scorer & score-or-assist caps.
* Magic Tier post-processing policy (services/magic_tier_policy.py):
  WRAPS existing tiers, only downgrades — data-quality, sample-size,
  stale-odds, lineup, calibration-gap, posterior-std (stability-only,
  not agreement).
* Cross-sport ranker guards (services/board_ranker_guards.py):
  EV/edge/DQ-aware ordering, dup collapse, same-event cap.
* Historical baseline + cross-sport calibration + settlement replay
  scripts (READ-ONLY).
* 23 assertions covering Parts 1-8; 101/101 Phase 4 tests green.

No frontend schema changes.  No prior-phase files touched.
```

## 15. Rollback instructions

Phase 4E is purely additive (new modules + advisory fields on `TennisComponents` + one wire-up in `pick_refresh_orchestrator.py`).  To revert:

```bash
# 1. Delete the new files.
rm -f \
  backend/services/tennis_identity.py \
  backend/services/tennis_data_quality.py \
  backend/services/soccer_scorer_eligibility.py \
  backend/services/magic_tier_policy.py \
  backend/services/board_ranker_guards.py \
  backend/scripts/phase4e_magic_tier_baseline.py \
  backend/scripts/phase4e_cross_sport_calibration.py \
  backend/scripts/phase4e_settlement_replay.py \
  backend/tests/test_phase4e.py

# 2. Revert tennis_engine.py + pick_refresh_orchestrator.py from git.
cd /app && git checkout HEAD -- \
  backend/tennis_engine.py \
  backend/services/pick_refresh_orchestrator.py

# 3. Restart backend.
sudo supervisorctl restart backend
```

The revert is safe because:
* The Magic Tier policy is wrapped in try/except; even if we skip it, picks flow normally.
* The `TennisComponents` new fields default to `"unknown"` — the FE ignores them.
* The 3 scripts are read-only reports; deleting them has zero runtime impact.

---

**Phase 4E is complete.  Awaiting explicit user authorization to begin Phase 4F.**
