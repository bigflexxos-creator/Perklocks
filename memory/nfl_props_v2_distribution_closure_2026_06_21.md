# NFL Player Props 2.0 · P0 Distribution Closure — 2026-06-21

## Blockers Closed

### P0-A — NFL Distributions Now Materialize

Previously `distributions_by_market = {}` because
`player_history.service.get_player_history` returned no L20 slice for
free-form NFL market strings.

**Fix**: `services/nfl_props_v2/nfl_stat_mapping.py` (NEW) resolves free-form
sportsbook market strings onto the canonical stat keys stored in
`db.player_game_actuals`:

* `Matthew Stafford Over 261.5 Player Pass Yds` → `pass_yds`
* `Matthew Stafford Over 1.5 Player Pass Tds` → `pass_tds`
* `Matthew Stafford Over 34.5 Player Pass Attempts` → `attempts`
* `Kyren Williams Over 54.5 Player Rush Yds - Alternative` → `rush_yds`
* `Kyren Williams Over 1.5 Player Receptions` → `receptions`
* `Malik Nabers Over 50.5 Player Reception Yds` → `rec_yds`
* `Bijan Robinson Anytime TD Scorer` → `anytime_td` (sum of `rush_tds`+`rec_tds`)

`engine.build_distribution` now reads directly from
`db.player_game_actuals` (sport=`"nfl"`, 132K docs) — the existing
authoritative NFL history PerkLocks already stores.  No competing
history source created.

**As-of safety**: query includes `event_time < as_of` where `as_of` is
the current game's `event_time`.  Proven on Bijan Robinson: 2024-09-01
as-of returns rookie-year distribution (n=17 μ=57.4 Q25=34 Q75=75);
2026-09-22 as-of returns current distribution (n=20 μ=91.6 Q25=70
Q75=104).  No future leakage.

**Small-sample tolerance**: `sample_size` reflects the truth — 20 for
Stafford/Kyren/Bijan, 19 for Nabers.  No player suppressed because
N<20.

### P0-B — Weather Auto-Penalty Removed

`_weather_confidence_multiplier` now returns exactly `1.0` when:
* `weather.status == UNAVAILABLE`
* `weather.is_indoor == True`
* `roof_status contains "closed" or "dome"`

Confidence is only reduced when REAL trustworthy weather DATA suggests
degraded conditions (`wind ≥ 20 mph`, `gust ≥ 30 mph`, `precip_prob ≥ 0.7`).
Missing enrichment ≠ penalty.

Confidence breakdown across the current slate is now
`{game_context: 1.0, weather: 1.0, availability: 1.0, availability_prob: 1.0}`
→ **CONFIDENCE = 1.0** for every player.  Zero suppression from missing
weather feed.

Injury `PARTIAL` (feed up but player not listed) is now neutral (1.0)
per spec — PARTIAL is honest, not a penalty.  Explicit designations
still map to availability probabilities (OUT→0, DOUBTFUL→0.25,
QUESTIONABLE→0.65, LIMITED→0.85, FULL/PROBABLE→0.95) as priors.

## Files Changed

* `backend/services/nfl_props_v2/nfl_stat_mapping.py` — **NEW** — canonical
  market→actuals-key mapper + `actuals_value(actuals, market_key)` helper.
* `backend/services/nfl_props_v2/engine.py` — `build_distribution`
  rewritten to query `player_game_actuals` with as-of safety +
  small-sample tolerance; `_weather_confidence_multiplier` added;
  `evaluate_player_across_markets` now passes `as_of = game.event_time`
  and applies neutral confidence multipliers for
  UNAVAILABLE/PARTIAL/indoor.
* `backend/tests/test_nfl_props_v2.py` — added 12 tests for P0-B
  closure (weather neutrality) + NFL market mapping + anytime_td
  composite.

## Real Acceptance Proof (Canonical Parity Preview, pick_date 2026-09-22)

**Stafford (QB, LAR → NYG · GAME_CTX margin=13.5 total=47.3 · WEATHER neutral · AVAIL neutral · CONF=1.0)**

| Real Line                                    | n  | Q25  | Median | Q75  | Odds | p(hit) | Floor | Edge     |
|----------------------------------------------|----|------|--------|------|------|--------|-------|----------|
| Over 1.5 Pass TDs                            | 20 | 2.0  | 3.0    | 3.0  | -152 | 0.850  | +0.5  | +24.7 pp |
| Over 261.5 Pass Yds                          | 20 | 245.0| 280.0  | 304.0| -115 | 0.600  | -16.5 | +6.5 pp  |
| Over 34.5 Pass Attempts                      | 20 | 32.0 | 35.0   | 40.0 | -109 | 0.550  | -2.5  | +2.9 pp  |
| Over 0.5 Rush Yds                            | 20 | -1.0 | 0.0    | 1.0  | +128 | 0.350  | -1.5  | -8.9 pp  |

**SAFEST BET**: `175+ Passing Yards @ -550 → p=0.950, floor=+70 yds` (line sits 70 yds below Q25)
**BEST VALUE**: `Over 1.5 Pass TDs @ -152 → p=0.850, edge=+24.7 pp`

**Kyren Williams (RB, LAR → NYG · CONF=1.0)**

| Real Line                                    | n  | Q25  | Median | Q75  | Odds | p(hit) | Floor | Edge     |
|----------------------------------------------|----|------|--------|------|------|--------|-------|----------|
| Over 54.5 Rush Yds - Alternative             | 20 | 60.0 | 72.0   | 84.0 | -125 | 0.800  | +5.5  | +24.4 pp |
| Over 1.5 Receptions - Alternative            | 20 | 1.0  | 2.0    | 3.0  | -150 | 0.700  | -0.5  | +10.0 pp |
| Over 12.5 Reception Yds                      | 20 | 10.0 | 15.0   | 21.0 | -115 | 0.600  | -2.5  | +6.5 pp  |
| Over 2.5 Receptions - Alternative            | 20 | 1.0  | 2.0    | 3.0  | +195 | 0.300  | -1.5  | -3.9 pp  |

**SAFEST BET**: `Over 44.5 Rush Yds - Alt @ -250 → p=0.950, floor=+15.5`
**BEST VALUE**: `Over 49.5 Rush Yds - Alt @ -160 → p=0.900, edge=+28.5 pp`

**Bijan Robinson (RB, GB → ATL · CONF=1.0)**

| Real Line                                    | n  | Q25  | Median | Q75   | Odds | p(hit) | Floor | Edge     |
|----------------------------------------------|----|------|--------|-------|------|--------|-------|----------|
| Over 65.5 Rush Yds - Alternative             | 20 | 70.0 | 86.0   | 104.0 | -185 | 0.750  | +4.5  | +10.1 pp |
| Over 75.5 Rush Yds - Alternative             | 20 | 70.0 | 86.0   | 104.0 | -111 | 0.600  | -5.5  | +7.4 pp  |
| Over 85.5 Rush Yds - Alternative             | 20 | 70.0 | 86.0   | 104.0 | +115 | 0.500  | -15.5 | +3.5 pp  |
| Over 95.5 Rush Yds - Alternative             | 20 | 70.0 | 86.0   | 104.0 | +151 | 0.300  | -25.5 | -9.8 pp  |

**SAFEST BET**: `Over 65.5 Rush Yds - Alt @ -185 → p=0.750, floor=+4.5`
**BEST VALUE**: `Over 65.5 Rush Yds - Alt @ -185 → p=0.750, edge=+10.1 pp` (same line — safest also has best edge)

**Malik Nabers (WR, LAR → NYG · CONF=1.0)** — CANARY equivalent to
the "40+ receiving" opportunity type

| Real Line                                    | n  | Q25  | Median | Q75  | Odds  | p(hit) | Floor | Edge     |
|----------------------------------------------|----|------|--------|------|-------|--------|-------|----------|
| Over 50.5 Reception Yds - Alternative        | 19 | 59.0 | 69.0   | 82.0 | -185  | 0.789  | +8.5  | +14.0 pp |
| Over 60.5 Reception Yds - Alternative        | 19 | 59.0 | 69.0   | 82.0 | -112  | 0.737  | -1.5  | +20.9 pp |
| Over 70.5 Reception Yds - Alternative        | 19 | 59.0 | 69.0   | 82.0 | +118  | 0.474  | -11.5 | +1.5 pp  |
| Over 80.5 Reception Yds - Alternative        | 19 | 59.0 | 69.0   | 82.0 | +157  | 0.263  | -21.5 | -12.6 pp |
| Over 90.5 Reception Yds - Alternative        | 19 | 59.0 | 69.0   | 82.0 | +210  | 0.211  | -31.5 | -11.2 pp |

**SAFEST BET**: `3+ Receptions @ -1000 → p=0.895, floor=+2.0`
**BEST VALUE**: `Over 4.5 Receptions @ -144 → p=0.842, edge=+25.2 pp`

TE was not sampled — the current slate contains no TE with elite_player_name
in the top-position-bucket picks scanned (`by_position: {QB:1, RB:2,
WR:1, TE:0}`).  Not a defect — the coverage snapshot honestly reports
what the slate contains.

## Canary Requirement — Discovery Demonstrated

The engine, without any hardcoded name, discovered:

* **Malik Nabers** SAFEST BET = `3+ Receptions @ -1000, p=0.895, floor=+2`.
  This is precisely the class of high-floor opportunity the spec
  requested — a low alt threshold sitting comfortably below the
  player's Q25 (59 rec yds) with a distribution mean of 77.6.
* **Kyren Williams** BEST VALUE = `Over 49.5 Rush Yds @ -160,
  p=0.900, edge=+28.5 pp` — legitimate 90 % hit rate on a rush-yards
  alt.
* Would produce equivalent discoveries for Amon-Ra St. Brown / Davante
  Adams the moment they appear on the current slate with real lines.

## 98 / 99 / APEX Reachability

The engine outputs raw `hit_probability_monotonic` up to 0.95 under
the current L20 window (the empirical max for these players' current
distributions).  Any 98/99 award is downstream of this pipeline
(Lock scorer + Magic + Apex gate consume `hit_probability_monotonic`,
`floor_distance`, `edge_pp`, plus `confidence`).  This pass introduces
no cap and no bonus — the highest legitimate probability observed on
today's slate is **0.950** for Stafford `175+ Passing Yards` and Kyren
`44.5+ Rush Yds` (both real sportsbook lines, both with substantial
positive floor distance).  With the downstream Lock scorer's current
weights, that translates through the normal path; nothing in this
pipeline manufactures a 98/99 fixture.

## Focused Tests

`backend/tests/test_nfl_props_v2.py` — **31 pass · 0 fail** (was 19; +12
new tests for the P0/P0-B closure).

Combined cumulative suite (NFL Props 2.0 + Rollover + Parlay 3.0 +
Universal Closure): **74 pass · 0 fail**.

## PRODUCTION PUBLISHED: NO
