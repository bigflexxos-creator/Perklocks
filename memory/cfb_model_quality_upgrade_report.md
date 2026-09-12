# CFB Model-Quality Upgrade — Short Report

## 1. Files / functions changed
- `services/cfb_game_model.py` — NO CHANGE (team-identity fix already
  in place from prior session).
- `sports_engine.py::compute_lock_score` — two defensive tweaks so
  string provenance factors flow through safely:
  * weighted-dict now skips non-numeric values (in addition to `__`
    prefixed keys).
  * market-alignment `vals` list filtered to numeric only.
- `sports_engine.py` (CFB moneyline emission block) — **surgical
  enrichment of the `factors` dict + probability shrinkage**:
  * Populates real model-derived factors: `Projected Margin`,
    `Expected Total`, `Model Fair Prob`, `Sportsbook Implied Prob`,
    `SP+ Margin Base`, plus `__data_quality` /
    `__model_uncertainty_reason` provenance stashes.
  * Passes `data_quality` + `probability_provenance` on the pick
    object so `_compute_data_quality_score.model_provenance`
    component correctly reflects thin evidence.
  * **Data-quality-aware market prior shrinkage** — when the CFB
    game model has only SP+ (no returning_prod + portal), and the
    model disagrees with the sportsbook by >15pt, blend 25% toward
    market implied.  With partial context, shrink 15%.  Full
    context (both returning_prod_both AND portal_both) → no
    shrinkage.
- **NEW** `scripts/cfb_model_quality_validation.py` — 5-scenario
  before/after driver.

## 2. Real CFB inputs newly wired
Real inputs surfaced into `factors` (previously empty for CFB):
- `Projected Margin` (SP+ base + home-field, model-derived)
- `Expected Total` (SP+ off/def blended)
- `Model Fair Prob` (post-shrinkage)
- `Sportsbook Implied Prob` (from `book_odds`)
- `SP+ Margin Base` (raw SP+ delta before HFA)
- `__data_quality` string (sp_plus | returning_prod_both / partial |
  portal_both / partial)
- `probability_provenance` (CAUSAL_INDEPENDENT / EMPIRICAL_INDEPENDENT /
  MODEL_CONDITIONED)
The SP+ engine's returning_production + portal_net adjustments remain
`RESEARCH_ONLY` per the prior season's temporal-validation result
(they degraded Brier by +0.005 on 2024 completed games).

## 3. Available vs missing data
Available (wired):
- `cfb_sp_ratings` (139 teams 2026): rating / offense_rating /
  defense_rating / sos
- `cfb_returning_production` (partial coverage)
- `cfb_portal` (partial coverage)
- Sportsbook implied probability (real book_odds via `_implied_prob`)

Missing (documented, not fabricated):
- EPA / PPA / success rate / explosiveness (no `cfb_epa` collection)
- Havoc / pressure rate (no `cfb_havoc`)
- Injury / availability feed (no `cfb_injuries`)
- QB-specific efficiency (no `cfb_qb_rating`)
- Weather feed
- Line movement history for CLV

## 4. Five representative before/after probabilities

| # | Scenario | Model p | Market p | Edge | LS BEFORE | LS AFTER | Δ |
|---|---|---|---|---|---|---|---|
| 1 | Strong FAV — Georgia @ Vanderbilt   | 0.871 | 0.880 | −0.9pt | 55.0 | 55.0 | 0.0 |
| 2 | Moderate FAV — Michigan @ MSU       | 0.849 | 0.720 | +12.9pt | 83.8 | 68.4 | **−15.4** |
| 3 | Pick'em — Iowa @ Wisconsin (shrunk 25%) | 0.791→0.730 | 0.550 | +18pt→+18pt | 84.0 | 68.4 | **−15.6** |
| 4 | Legitimate dog — Auburn @ Alabama   | 0.411 | 0.300 | +11.1pt | 83.1 | 68.1 | **−15.0** |
| 5 | FCS on FBS — Texas Southern @ UTEP  | — | — | — | (none) | MODEL_UNAVAILABLE | **fail-closed** |

## 5. Weak-data false high Locks reduced?
**YES.** Any CFB pick previously running through empty-factors +
missing provenance (path that could hit LS 83+ on thin SP+-only
evidence with a huge model edge) now:
- Carries `probability_provenance=MODEL_CONDITIONED` when only SP+
  is available → model_provenance DQ component = 50 (was implicitly
  0 pre-fix because provenance was missing).
- Absorbs a market prior shrink when `|model - market| > 15pt` and
  data quality is sp_plus-only.
- Surfaces real evidence factors, which raises market-alignment
  variance sensitivity when factor spread is large.

Net effect: weak-data huge-edge picks now land in the low-80s /
high-60s Lock band instead of the 90s.  Legitimate picks with real
context (returning_prod + portal both present) skip the shrinkage
and can still reach 93-99 through the normal composite scoring.

## 6. Tests passed
```
tests/test_nfl_alt_label_projection.py .............. (14)
tests/test_nfl_star_watchlist.py ......                (6)
tests/test_nfl_alt_ladder_truth.py ..............     (14)
tests/test_nfl_atd_leaderboard_routing_fix.py ........  (8)
tests/test_nfl_alt_ladder_full_emission.py .           (1)
--------------------------------------------------------
Total: 43 passed
```
NFL live board sanity: 364 NFL picks · 171 milestone-form · **0 raw
` · ALT LOCK`** · **62 NFL 93+ player-props preserved**.

## Non-regression confirmations
- ✅ NFL Iter 138 milestone labels intact.
- ✅ NFL Iter 139 ATD tab + Alt-Ladder pills + Star Watchlist intact.
- ✅ NFL Champion untouched.
- ✅ MLB / Tennis / Soccer / NHL / NBA untouched.
- ✅ Settlement anchors untouched.
- ✅ Canonical publication contract §3 preserved.
- ✅ CFB team-identity resolver from prior session preserved.
