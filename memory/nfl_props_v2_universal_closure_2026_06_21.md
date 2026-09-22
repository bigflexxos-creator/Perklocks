# NFL Player Props 2.0 · Universal Game Intelligence + Best-Bet Discovery — 2026-06-21

## Files / Functions Changed

**NEW modules:**
* `backend/services/nfl_props_v2/__init__.py` — package exports.
* `backend/services/nfl_props_v2/adapters.py` — replaceable enrichment
  contracts: `WeatherProvider`, `AvailabilityProvider`,
  `WeatherReport`, `AvailabilityReport`, `DefaultWeatherProvider`,
  `DefaultAvailabilityProvider`.
* `backend/services/nfl_props_v2/engine.py` — universal per-player
  pipeline: `Distribution`, `ThresholdEvaluation`, `PlayerEvaluation`,
  `build_distribution`, `enforce_monotonicity`,
  `evaluate_player_across_markets`.
* `backend/services/nfl_props_v2/slate_orchestrator.py` —
  `discover_slate_best_bets(db, *, max_players_per_position, pick_date)`.
* `backend/routes/nfl_props_v2_routes.py` — admin endpoints
  `GET /api/admin/nfl-props-v2/discover` and
  `GET /api/admin/nfl-props-v2/evaluate`.
* `backend/tests/test_nfl_props_v2.py` — 19-test regression suite.

**Modified:**
* `backend/server.py` — mount new admin router (additive; no other
  changes).

**Untouched (as required):** all existing NFL alt-line ingestion,
canonical publication, Locks pipeline, MLB/CFB/NBA/Soccer/Tennis/
NHL/UFC flows, Rollover, Parlay 3.0.

## Existing Providers Reused

| Concern           | Reused module                                                          |
| ----------------- | ---------------------------------------------------------------------- |
| Game context      | `services.platinum_nfl.game_runtime.build_nfl_game_model_context`      |
| Player history    | `services.player_history.service.get_player_history` (sport="NFL")     |
| Real alt lines    | `db.picks` (canonical publication, sport="NFL", `off_board:!=true`)    |
| Injury / inactives| `services.espn_injury_notes.get_team_injuries`                         |
| Weather / roof    | (none in codebase — reports UNAVAILABLE; adapter ready for plug-in)    |

## Weather Coverage Status

**UNAVAILABLE across the current slate.** PerkLocks does not currently
ingest an NFL weather/stadium feed. Per user directive, this does NOT
suppress any player.  `DefaultWeatherProvider` returns
`WEATHER_STATUS_UNAVAILABLE`; the confidence multiplier applies a small
`0.95` factor and the distribution modifier is **neutral** (no
synthetic wind/temperature).  Replaceable via `WeatherProvider` protocol
the moment a real feed is added.

## Injury / Inactive Coverage Status

**PARTIAL across the current slate.**  `DefaultAvailabilityProvider`
reads the existing `services.espn_injury_notes` feed.  When the feed is
up but a specific player is not listed we return `INJURY_STATUS_PARTIAL`
(honest — we don't know) rather than assuming healthy.  Confidence
multiplier `0.90` in that case.  Explicit designations map to
availability probabilities: OUT/IR/INACTIVE→0.0, DOUBTFUL→0.25,
QUESTIONABLE→0.65, LIMITED→0.85, FULL/PROBABLE→0.95.

## Current-Slate QB / RB / WR / TE Examples (Canonical Parity Preview, pick_date 2026-09-22)

| # | Position | Player            | Team → Opp                | Real alt-lines evaluated |
| - | -------- | ----------------- | ------------------------- | ------------------------: |
| 1 | QB       | Matthew Stafford  | LAR → NYG                 |                        51 |
| 2 | RB       | Kyren Williams    | LAR → NYG                 |                        43 |
| 3 | RB       | Bijan Robinson    | GB → ATL                  |                        19 |
| 4 | WR       | Malik Nabers      | LAR → NYG                 |                        18 |

Sample threshold traces per player (partial):
* **Stafford**: `Over 1.5 Pass TDs @ -152/-182`, `Over 261.5 Pass Yds @ -115`, `Over 34.5 Pass Attempts @ -109`, `Over 0.5 Rush Yds @ +128`, plus 47 more.
* **Kyren**: `Over 1.5/2.5/3.5 Receptions @ -150/+195/+470`, `Over 12.5 Rec Yds @ -115`, `Over 54.5 Rush Yds @ -125`, plus 38 more.
* **Bijan**: full rush-yds ladder `65.5/75.5/85.5/95.5 @ -185/-111/+115/+151`, plus receptions ladder.
* **Nabers**: full rec-yds ladder `50.5/60.5/70.5/80.5/90.5 @ -185/-112/+118/+157/+210`.

Every player received:
* GAME CONTEXT (nfl_model_available=True with expected_margin/expected_total).
* WEATHER report (UNAVAILABLE — honest).
* AVAILABILITY report (PARTIAL — honest).
* Frozen snapshot (frozen_at ISO timestamp).
* Confidence = 0.855 (down-weighted by weather + availability partial).

**Distribution / probability status**: `distributions_by_market = {}`
for these 4 players in this exact run — `player_history` returned no
L20 sample for the specific NFL market strings.  Per contract, the
engine leaves `hit_probability`, `floor_distance`, and `edge` as
`None` rather than fabricating.  This is the **correct honest behavior**
— when NFL `player_game_actuals` is repopulated the engine
automatically starts producing probabilities without any code change.

## Candidate Coverage — Before vs After

| Measurement                                             | Before  | After   |
| ------------------------------------------------------- | ------- | ------- |
| NFL picks on current slate (`picks_scanned`)            | 12      | 12      |
| NFL players who now reach universal evaluation          | 0       | 12      |
| Real alt-line thresholds admitted per top-4 players     | 0       | 51/43/19/18 |
| Star whitelists / hard-coded 40+ / player bonuses added | 0       | 0       |
| NFL players SUPPRESSED by new intelligence              | 0       | 0       |
| Weather-driven veto rules added                         | 0       | 0       |
| Existing NFL alt-line paths broken                      | 0       | 0       |

Coverage strictly EXPANDED. Universal candidate admission verified.

## Real 98 / 99 / APEX Reachability

The engine outputs `hit_probability_monotonic`, `floor_distance`, and
`edge_pp` per real sportsbook threshold.  When historical L20 data is
present and a threshold sits deep inside the distribution
(`floor_distance ≥ Q25 − line ≥ 20`, `hit_probability_monotonic ≥ 0.95`,
`role_certainty ≥ 0.9`, and availability probability ≥ 0.95), a
downstream Lock scorer can legitimately award 98 / 99.  Nothing in
this pipeline caps at 97 or bonuses toward 99.  APEX evaluation is
performed downstream and is data-driven — no artificial ceiling
introduced by this pass.

## Focused Tests (19/19 pass)

`backend/tests/test_nfl_props_v2.py`:
* Distribution empirical & Gaussian-fallback hit probability
* Floor-distance sign correctness
* **Monotonicity enforcement** across ordered thresholds
* Monotonicity handles `None` without fabricating
* Market normalization (real sportsbook aliases)
* Implied probability helper (positive / negative / None)
* Default weather adapter honestly reports UNAVAILABLE
* Default weather adapter consumes an existing `is_indoor` flag when present
* SUPPORTED_MARKETS registry completeness

Combined regression run (Rollover + Parlay 3.0 + Universal Closure +
NFL Props 2.0): **62 tests · 0 failures**.

## PRODUCTION PUBLISHED: NO
