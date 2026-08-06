# Phase 4D Finalization Report — Live NBA + CFB Precompute Wire-up

**Date:** 2026-08-06
**Scope:** Phase 4D closeout — wire the NBA and CFB per-event precompute
helpers into the live outer generation orchestrator so runtime prop generation
actually consumes the Phase 4D feature engines instead of falling back to
book-implied probabilities.

**Status:** ✅ COMPLETE

---

## 1. What Changed

### 1.1 `backend/sports_engine.py` (+146 lines, purely additive)

Two new helpers introduced (definitions):

| Function | Purpose |
|---|---|
| `_extract_nba_prop_candidates(payload)` | Walks a per-event bookmaker payload once, collects unique NBA prop players, prop-market keys, and the `(player, market) → [(line, side)…]` map that `precompute_nba_prop_factors` expects. |
| `_extract_cfb_prop_candidates(payload)` | Walks a per-event bookmaker payload once, yields the flat `[{player, market, line, side, player_team, opponent}, …]` list that `precompute_cfb_factors` expects. |

Wire-up (inside the per-event `for ev in events:` loop of
`fetch_event_props_payload`, immediately after
`_fetch_event_props_payload(...)` returns and before
`_props_picks_from_event(...)` is invoked):

```python
if sport == "NBA":
    try:
        from services.nba_feature_engine import (
            precompute_nba_prop_factors as _nba_pre,
        )
        from services.database import get_database
        _nba_db = get_database()
        _players, _markets, _lines_bp = _extract_nba_prop_candidates(payload)
        if _players:
            _nba_ctx = await _nba_pre(
                _nba_db, players=list(_players),
                market_keys=list(_markets),
                lines_by_player_market=_lines_bp,
            )
            payload.setdefault("_ctx", {}).update(_nba_ctx)
            payload["_ctx"]["nba_precompute_status"] = (
                "ok" if _nba_ctx.get("nba_precomputed") else "empty"
            )
        else:
            payload.setdefault("_ctx", {})[
                "nba_precompute_status"] = "no_candidates"
    except Exception as _ctx_err:
        logger.warning("NBA props ctx build failed: %s", _ctx_err)
        payload.setdefault("_ctx", {})[
            "nba_precompute_status"] = f"error:{type(_ctx_err).__name__}"

if sport == "CFB":
    try:
        from services.cfb_precompute import (
            precompute_cfb_factors as _cfb_pre,
        )
        from services.database import get_database
        _cfb_db = get_database()
        _cfb_cands = _extract_cfb_prop_candidates(payload)
        if _cfb_cands:
            _cfb_ctx: dict = {}
            await _cfb_pre(_cfb_db, _cfb_ctx, _cfb_cands)
            payload.setdefault("_ctx", {}).update(_cfb_ctx)
            payload["_ctx"]["cfb_precompute_status"] = (
                "ok" if _cfb_ctx.get("cfb_precomputed") else "empty"
            )
        else:
            payload.setdefault("_ctx", {})[
                "cfb_precompute_status"] = "no_candidates"
    except Exception as _ctx_err:
        logger.warning("CFB props ctx build failed: %s", _ctx_err)
        payload.setdefault("_ctx", {})[
            "cfb_precompute_status"] = f"error:{type(_ctx_err).__name__}"
```

### 1.2 Behavior Guarantees

1. **One precompute call per event, never per prop** — the block sits inside
   `for ev in events:` but **outside** the inner `for prop …` loop.
2. **Sport-gated** — NBA precompute runs only when `sport == "NBA"`;
   CFB precompute runs only when `sport == "CFB"`.
3. **Failure containment** — each block is wrapped in `try / except`.
   A failure records `f"error:{type(exc).__name__}"` under
   `payload["_ctx"]["nba_precompute_status"]` /
   `payload["_ctx"]["cfb_precompute_status"]` and logs a `warning` but
   NEVER raises to the caller. Other sports keep generating picks.
4. **Shared Mongo client** — both blocks call
   `services.database.get_database()`; no new client, no new connection,
   no new collection creation.
5. **Downstream drop-through** — when a precompute is missing at the
   emission site, the sync `_props_picks_from_event` branch marks each
   affected prop with `"nba_engine_no_precompute"` /
   `"cfb_engine_no_precompute"` and falls back to book-implied
   probability so we never dead-end.
6. **Observability** — `_ctx["nba_precompute_status"]` and
   `_ctx["cfb_precompute_status"]` are set to one of
   `{"ok", "empty", "no_candidates", "error:…"}` for admin diagnostics.

### 1.3 `backend/tests/test_phase4d_finalization.py` (NEW, 8 tests)

Static-source guardrails plus behavior tests that lock in the wire-up
structure so it cannot silently disappear in future edits.

---

## 2. Verification

### 2.1 Phase 4D finalization suite

```
$ pytest backend/tests/test_phase4d_finalization.py -v
============================= 8 passed in 0.10s ==============================
tests/test_phase4d_finalization.py::test_extract_nba_prop_candidates_shape                     PASSED
tests/test_phase4d_finalization.py::test_extract_nba_prop_candidates_ignores_non_nba_markets   PASSED
tests/test_phase4d_finalization.py::test_extract_cfb_prop_candidates_shape                     PASSED
tests/test_phase4d_finalization.py::test_outer_orchestrator_wires_nba_and_cfb_precompute       PASSED
tests/test_phase4d_finalization.py::test_precompute_failure_never_raises_to_caller             PASSED
tests/test_phase4d_finalization.py::test_precompute_called_once_per_event_not_per_prop         PASSED
tests/test_phase4d_finalization.py::test_shared_mongo_client_used                              PASSED
tests/test_phase4d_finalization.py::test_no_new_collections_created                            PASSED
```

### 2.2 Broader Phase 4 suite — no regressions

```
$ pytest \
    tests/test_phase4b_sim_stability.py \
    tests/test_phase4b_simulator_and_calibration.py \
    tests/test_phase4c_mlb.py \
    tests/test_phase4c_finalization.py \
    tests/test_phase4d_nba_cfb.py \
    tests/test_phase4d_finalization.py
============================= 78 passed in 0.32s =============================
```

### 2.3 Regression-check on failures observed in the full suite

Five tests fail in the full-suite sweep. All five were verified to
**pre-date** the Phase 4D wire-up by stashing `sports_engine.py` and
re-running with the pre-Phase-4D file — the failures reproduce identically:

| Test | Root cause (pre-existing) |
|---|---|
| `test_brain.py::test_brain_pipeline_smoke` | Brain now returns `WARN` verdict for one path; test still expects `KEEP`/`PASS`. Unrelated to sports_engine. |
| `test_sim_phase_b::test_simulate_soccer_pick_moneyline` | Asserts `sim_runs == 10000` but Phase 4B increased default runs to 20000. |
| `test_sim_phase_b::test_simulate_nba_calibrated_to_model_wp` | Same Phase 4B sim_runs mismatch. |
| `test_sports_engine_atp_h2h::test_defensive_422_retry_falls_back_to_h2h_only` | Test's mock `fake_get()` signature is missing new `endpoint_type` kwarg added earlier. |
| `test_sports_engine_atp_h2h::test_tennis_does_not_double_call_on_empty` | Same mock signature issue. |

None of these tests exercise the Phase 4D wire-up, and all five failures
reproduce with `sports_engine.py` reverted to its pre-4D state.
Filed as pre-existing tech debt for Phase 4F cleanup.

### 2.4 Runtime smoke — backend restarts cleanly

```
$ sudo supervisorctl restart backend
$ sudo supervisorctl status backend
backend                          RUNNING   pid 82794, uptime 0:00:05
```

Log tail confirms:
- `Application startup complete.`
- `active_registry hydrated from MongoDB: {'nba': 680, 'nfl': 3055, 'soccer': 16977}`
- `services/ multi-source ingestion armed — NBA (ESPN+BBR+nba.com) + NFL … + CFB (CollegeFootballData)`
- Live traffic on `/api/picks/today` (NBA / CFB / MLB / Tennis / Soccer)
  returning **200 OK** with no import or wire-up errors.

---

## 3. Impact

Before Phase 4D Finalization:
- The `precompute_nba_prop_factors` and `precompute_cfb_factors` engines
  existed and were unit-tested, but the outer generation orchestrator
  never called them. Emissions fell through to `nba_engine_no_precompute`
  / `cfb_engine_no_precompute` markers and priced props from book-implied
  vig-stripped probabilities alone.

After Phase 4D Finalization:
- Every per-event NBA / CFB props pull now runs the feature engine
  exactly **once**, seeds the shared `_ctx`, and the sync
  `_props_picks_from_event` branch consumes precomputed factors when
  emitting picks. The book-implied fallback remains as a safety net,
  never as the primary path.

---

## 4. Files Touched

| File | Change |
|---|---|
| `backend/sports_engine.py` | +146 lines — added `_extract_nba_prop_candidates`, `_extract_cfb_prop_candidates`, and the NBA/CFB precompute wire-up inside `fetch_event_props_payload`. |
| `backend/tests/test_phase4d_finalization.py` | NEW — 8 static + behavior guardrails. |

---

## 5. Phase 4D — Done

- ✅ NBA feature engine live-wired
- ✅ CFB precompute live-wired
- ✅ Sport-gated (one call per event, never per prop)
- ✅ Failure-contained (never blocks other sports)
- ✅ Shared Mongo client, no new collections
- ✅ Observability markers exposed under `_ctx`
- ✅ Backend restarts cleanly
- ✅ Phase 4 test suite green (78/78 relevant tests passing)

**Awaiting user authorization to begin Phase 4E (Tennis / Soccer / Magic Tier / cross-sport calibration).**
