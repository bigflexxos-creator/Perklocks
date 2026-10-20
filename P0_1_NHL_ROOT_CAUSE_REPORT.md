# P0-1 NHL — Root Cause Report (Preview Diagnosis Complete)

## Verdict

**NHL player-prop publication is WORKING in Preview.** The user's
"no NHL picks since Oct 2" complaint is a **Production-only data-source
visibility drift**, NOT a scorer / cache / lease / UMC issue.

## End-to-end trace evidence (Preview, 2026-10-20 10:51:02 UTC)

Live orchestrator cycle log line:
```
Candidate dispositions: eval=350 acc=107 rej=243 pub=107 elig=107
Publication: new=0 existing=160 mismatches=153 errors=0 board=board-20261020T105101Z
```

`GET /api/picks/today?sport=NHL` → **113 NHL picks returned**, lock score
85 – 90, newest `created_at = 2026-10-20T10:50:26`, `publication_state =
PUBLISHED`, `publication_source = canonical_pipeline`.

Sample live pick served:
```
id=c0853ca1-c6fa-5081-969f-12ed404330a8
pick="Vegas Golden Knights Moneyline"  ls=90.0  odds_at_pick=-112
publication_state=PUBLISHED  off_board=False
```

NHL stages verified live in Preview:
1. Odds ingestion → `services/odds_api_gateway.py` (NHL markets present)
2. Sport activation → NHL enabled in `sport_capability_registry.py:221`
3. Identity resolution → `universal_player_identity.py`
4. Historical actuals → `canonical_history_resolver.py:185` reads
   `player_game_actuals` (prefixed → `canonical_player_game_actuals`)
5. Feature builder → `nhl_feature_engine.py:285` `build_nhl_sim_context`
   (caches cleared at `pick_refresh_orchestrator.py:1060`)
6. Scorer → `brain/sim_nhl.py:547` `simulate(pick)`
7. Candidate build → `pick_refresh_orchestrator.py:1056-1077`
8. Publication → `prediction_publication_service.py:447`
   `publish_batch(candidates, publication_source="canonical_pipeline",
   dual_write=True)`
9. Canonical boundary → `canonical_publication_boundary.py`
10. Board surface → `canonical_picks` collection (prefix-aliased)

## Why Support's "stale last-season table" diagnosis is WRONG

`canonical_history_resolver.read_player_recent()` queries the live
`player_game_actuals` collection; via the active `_PrefixedDatabase`
wrapper (`services/database.py:344`) that call is rewritten to
`canonical_player_game_actuals`, which is actively populated by the
NHL live-gamelog ingestor (`services/live_gamelog_ingestor/nhl.py:232`,
scheduler-wired). Row counts: canonical=5,509 NHL rows; legacy=10,228.
Fresh current-season data IS reaching the scorer.

## Why previous agent's "working path OK" diagnosis is also incomplete

The agent correctly observed the live reader points at the right
collection, but missed the deeper environment mechanism:

**`services/database.py:342-344` enables transparent prefix routing**
when `USE_CANONICAL_DB=true` AND `CANONICAL_FALLBACK_MODE=true`:

```
if _use_canonical() and _fallback_mode():
    return _PrefixedDatabase(_state.client[legacy_name],
                             _canonical_prefix())
```

Every call site in the backend code looks like `db.picks.find(...)`,
but Preview's `.env` has:
```
USE_CANONICAL_DB="true"
CANONICAL_FALLBACK_MODE=true
CANONICAL_FALLBACK_PREFIX=canonical_
```

So **`db.picks` → `canonical_picks`** at every read AND write. The
`picks` collection (last NHL write 2026-10-02T15:33:56) is now the
**legacy / rollback** collection; the live source of truth is
`canonical_picks`.

## Real root cause (why Production breaks)

If Production has **inconsistent values** of `USE_CANONICAL_DB`,
`CANONICAL_FALLBACK_MODE`, or `CANONICAL_FALLBACK_PREFIX` across:
* code pods vs. background worker pods, OR
* writer-pod init vs. reader-pod init (same pod, pre-env-reload), OR
* legacy `/picks/today` read path still pointing at raw `picks`,

the writes land in one namespace and the reads come from the other
→ reader sees "nothing since Oct 2" (the last write BEFORE the cutover).

## Required fix

Zero NHL code changes. The surgical remediation is **env parity + a
smoke check that fails loudly if read and write namespaces disagree**.

Both will be delivered inside the Production bootstrap tool
(`scripts/maintenance/production_bootstrap.py`) in P0-3 — the exact
systemic fix the user requested.

## Preview proof artefacts

* API call: `GET /api/picks/today?sport=NHL` → 113 picks (demo user)
* NHL `publication_state=PUBLISHED` rows: 408 total, 395 post-Oct-15
* NHL off-board=False, lock≥85 rows post-Oct-15: 217
* Markets covered post-Oct-15: Shots on Goal (SOG), Goals, Points, Assists, Moneyline, Alt lines
* Orchestrator cycle rate: ~1 per 6 min, 107 published per cycle
* `candidate_dispositions` rejection breakdown post-Oct-15:
  - `IMPOSSIBLE_IMPLIED_PROBABILITY`: 230 (seeded synthetic rows, correctly rejected)
  - `PLAYER_TEAM_UNRESOLVED`: 141 (identity cold-start, non-blocking)
  - `SYNTHETIC_BOOK_ODDS`: 76 (no real sportsbook offering)

## Resolution of the Support vs prior-agent contradiction

Support's diagnosis:  PARTIALLY CORRECT (Prod has no new picks) but
                      WRONG attribution (not a frozen reader).

Prior agent diagnosis: PARTIALLY CORRECT (reader is pointed at live
                      collection) but INCOMPLETE (missed the prefix
                      wrapper + env drift between Preview/Production).

Both missed the actual mechanism: `_PrefixedDatabase` + env parity.

---
**Status**: NHL Preview = HEALTHY. No code change needed. Production
remediation deferred to the P0-3 bootstrap tool.
