# NBA ROOT-CAUSE EVIDENCE REPORT

**Date (UTC):** 2026-10-20
**Scope:** NBA pipeline — Preview env — pick_date=2026-10-20
**Change log:** ZERO code changes made. Diagnostic only.
**Database:** `lockscore_db` (local Mongo, `MONGO_URL=mongodb://localhost:27017`)

---

## EXECUTIVE SUMMARY

* 434 NBA candidates reached `canonical_picks` today, 234 with LockScore ≥ 85.
* **ZERO** published to final `lockscore_db.picks` (NBA, today).
* **ZERO** candidates generated for 5 of the 8 required families (MONEYLINE, POINT_SPREAD, GAME_TOTAL, PRA, THREES).
* Three distinct rejections fire on EVERY candidate in the three families that *did* generate (POINTS, REBOUNDS, ASSISTS):
  1. `MODEL_LINE_NOT_REAL_OFFERING` — authority registry says NBA is MODEL_UNAVAILABLE.
  2. `UMC_CAPABILITY_STATE_MODEL_UNAVAILABLE` — Universal Market Contract says every NBA family is MODEL_UNAVAILABLE.
  3. `PLAYER_TEAM_UNRESOLVED` — identity gate cannot prove the player is on either team of the event.

Classification mix (my conclusion, per family-rule):
* Rejections #1 and #2: **E — INTENTIONAL SAFETY GUARD WORKING CORRECTLY** *as literally coded today*, but the architectural comment that justifies the UNAVAILABLE state is **STALE**: the "zero NBA rows" premise it cites no longer holds (29,481 player-game actuals and 649 players with team-mapping are now ingested). So the guard is still *technically* correct per registry, but the registry is **D — valid NBA evidence not being recognized by validation (via a stale registry)**.
* Rejection #3: **D — VALID NBA EVIDENCE REJECTED BY WRONG VALIDATION**. Team-mapping data for every NBA player is available in `canonical_players` AND stamped on the pick in `player_recent_rows[0].team`, but the identity gate reads only `pick.player_team / canonical_team_id / team / …` which are not populated by the NBA pick-build path.
* Zero game markets (ML/SPREAD/TOTAL): **C — MODEL NOT WIRED**. Real `h2h / spreads / totals` NBA odds are in the provider cache (`basketball_nba`), but no NBA game-model path (sim_nba generates `moneyline` but no NBA game-market producer feeds it with real pick candidates).
* Zero PRA / THREES: Mix of C and D — provider cache contains `player_threes_alternate` quotes, but no PRA candidates/THREES candidates reach `canonical_picks` → the NBA pick-build path is not emitting them (route gap).

**None of the above is fixable by weakening a safety guard.** All five classes have a legitimate upstream fix.

---

## 0 · OBSERVED FACTS (RAW DB TRUTH)

### 0.1 Provider / odds cache (last cache cycle for today)

| Provider market | Events bookmakers x markets observed |
|---|---|
| `h2h`                      | 210 |
| `spreads`                  | 224 |
| `totals`                   | 224 |
| `alternate_spreads`        | 9   |
| `alternate_totals`         | 6   |
| `player_points_alternate`  | 28  |
| `player_rebounds_alternate`| 28  |
| `player_assists_alternate` | 28  |
| `player_threes_alternate`  | 28  |

**Event list:** 46 `basketball_nba` events in cache (first commence_time `2026-10-20T19:00Z`).
**Cache docs for `sport_key="basketball_nba"`:** 32 (1 bulk_odds + 31 generic). All freshly refreshed earlier today (per `canonical_candidate_dispositions` NBA timestamps ~`2026-10-20 11:49–11:52 UTC`).
**Standard-line fetches (`h2h`, `spreads`, `totals`):** only **1 cache doc each** → the pipeline fetched the slate-wide bulk once; it did NOT fetch per-event standard lines for individual game-market scoring.
**Alt-line fetches:** 30 cache docs each for `alternate_spreads`, `alternate_totals`, `player_*_alternate` → the per-event alt scan DID run for the player-prop markets and for alt spreads/totals, but no game-market scorer consumed them.

### 0.2 Historical / evidence data

| Collection | NBA (uppercase) | nba (lowercase) |
|---|---|---|
| `canonical_player_game_actuals` | 0 | **29,481** |
| `canonical_team_game_actuals`   | 0 | 0 |
| `canonical_player_game_logs`    | 0 | 0 |
| `canonical_games`               | 0 | 0 |
| `canonical_players`             | 0 | **649** (full {name,team,team_id,team_name} map) |
| `canonical_season_totals`       | 0 | 0 |
| `canonical_player_stats`        | 0 | 517 |
| `canonical_player_profiles_v2`  | 25 | 0 |
| `canonical_player_identities`   | 0 | 0 |

**Observation:** All ingested NBA documents use **lowercase `sport="nba"`**. The publication/evidence code paths filter by **uppercase `sport="NBA"`**. The pick itself carries `sport: "NBA"`. The upstream NBA ingestors (`live_gamelog_nba_v1`) write lowercase; the pick-build path writes uppercase. **Case mismatch is a real factor** downstream of the gate (not primary rejection cause, but blocks evidence lookups).

Sample `canonical_player_game_actuals` doc (sport='nba'):
```
{sport: 'nba', canonical_player_id: '1966', player_name: 'LeBron James',
 team: 'Philadelphia 76ers', opponent: 'Houston Rockets',
 canonical_team_id: 'Philadelphia 76ers', event_id: '401809241',
 event_time: '2025-12-26T01:00Z', season: null,
 actuals: {points: 18.0, rebounds: 2.0, assists: 5.0, steals: 2.0, blocks: 1.0, ...}}
```

Sample `canonical_players` doc (sport='nba'):
```
{sport: 'nba', player_id: '5142718', name: 'AJ Dybantsa',
 team: 'WSH', team_id: '27', team_name: 'Washington Wizards'}
```

### 0.3 Candidate funnel today (NBA, canonical_picks)

Total candidates: **434** — pick_date `2026-10-20` — all have `line_source: OBSERVED_BOOK` (real bookmaker, no fabrication).

Family breakdown:
| Family | Candidates | Standard | Alt | LS≥85 | Published |
|---|---|---|---|---|---|
| MONEYLINE          | 0   | 0   | 0  | 0   | 0 |
| POINT_SPREAD       | 0   | 0   | 0  | 0   | 0 |
| GAME_TOTAL         | 0   | 0   | 0  | 0   | 0 |
| PLAYER_POINTS      | 196 | 114 | 82 | 85  | 0 |
| PLAYER_REBOUNDS    | 161 | 75  | 86 | 100 | 0 |
| PLAYER_ASSISTS     | 77  | 34  | 43 | 49  | 0 |
| PLAYER_PRA         | 0   | 0   | 0  | 0   | 0 |
| PLAYER_THREES      | 0   | 0   | 0  | 0   | 0 |

For the three families that *did* generate: **every candidate** was rejected with the same three reasons.

### 0.4 Candidate_dispositions NBA (last 12 h)
```
('evaluated', None)    → 434
('rejected',  'no_bet')→ 434
```
Both stages fire on every candidate — pipeline is reaching the publication-boundary disposition step.

### 0.5 Simulator output on sample
Top-LS PLAYER_POINTS candidate (`Jrue Holiday Over 4.5 Points · ALT`):
```
simulator_name:    nba_simulator
simulator_version: 1.1.0
simulator_type:    distribution_monte_carlo
sim_win_probability:   96.5
model_win_probability: 92.3
sim_trust_state: SIM_EDGE
sim_signal:      neutral
input_quality:   FULL
independent_evidence: True        ← pick claims YES
real_data_count: 3
sample_games:    10
lock_score:      97.2
```

---

## 1 · PER-FAMILY DIAGNOSTIC MATRIX

### 1.a MONEYLINE
| Check | Value |
|---|---|
| sportsbook events discovered     | 46 (basketball_nba events in cache)       |
| standard lines available (`h2h`) | **YES**, 210 bookmaker-market rows in bulk_odds |
| real alt lines available         | n/a (ML has no alt)                       |
| odds present                     | YES                                       |
| historical actuals present       | YES (29,481 player + team-level logs via lowercase sport) |
| L5/L10/season evidence present   | YES (via player_recent_rows derivation available) |
| opponent/context evidence        | YES (opponent name in event + L10 contains opponent) |
| exact scorer/model called        | NONE — no NBA-ML producer feeds `_fetch_picks_for_sport("NBA")` game-market candidates into the engine |
| simulator attempt                | sim_nba.simulate_nba_pick HAS a moneyline branch (brain/sim_nba.py:183) — never reached because no candidate carries market="Moneyline" |
| probability produced             | NO                                        |
| model_version                    | n/a                                       |
| disposition                      | 0 candidates generated → not evaluated     |
| **first fail-closed gate**       | **Upstream route gap**: candidate never created. `_extract_nba_prop_candidates` is player-prop only; no symmetric `_extract_nba_game_candidates` exists for ML / SPREAD / TOTAL in `sports_engine.py`. |
| missing requirement              | A NBA game-market candidate-emitter that converts `h2h/spreads/totals` provider rows into pick dicts routed through `simulate_nba_pick`. |
| **Classification**               | **C — MODEL NOT IMPLEMENTED/WIRED** (sim exists, producer absent) |
| **Required legitimate fix**      | Wire an NBA game-market producer in `sports_engine.fetch_nba_picks` that:<br>  (1) iterates per-event `h2h/spreads/totals` quotes already in `canonical_odds_api_cache`,<br>  (2) builds candidate dicts (market string standardized to "Moneyline", "Spread -X.X", "Total Over X.X"),<br>  (3) stamps `provider_market_key`, `line_source=OBSERVED_BOOK`, player-less identity,<br>  (4) lets `simulate_nba_pick`'s `moneyline` / future `team_total` branches run. Then flip the UMC + authority (see §3). |

### 1.b POINT_SPREAD
| Check | Value |
|---|---|
| sportsbook events discovered | 46 |
| standard lines available (`spreads`) | **YES**, 224 bookmaker-market rows |
| real alt lines (`alternate_spreads`) | YES, 9 bookmaker-market rows + fresh 30 cache docs |
| odds / historical / L5/L10 / opponent | YES / YES / YES / YES |
| exact scorer | NONE — sim_nba has no spread branch (classifier returns "unknown" for "Spread -3.5") |
| simulator attempt | NONE |
| probability produced | NO |
| disposition | 0 candidates generated |
| **first fail-closed gate** | Upstream route gap (same as 1.a) + sim_nba `_classify_nba_market` returns "unknown" for spread markets. |
| missing requirement | NBA spread candidate-emitter + a `spread` classification in `brain/sim_nba._classify_nba_market` + a `spread` simulation branch (anchor sim via Normal(µ, σ=12) margin like the ML branch does today). |
| **Classification** | **C — MODEL NOT IMPLEMENTED/WIRED** |
| **Required legitimate fix** | Same producer as 1.a, plus 2 small additions in `brain/sim_nba.py`: (a) classifier `"spread" in m → "spread"`, (b) spread branch that reuses the ML Normal-margin sim parameterised by the given spread line. Same guard flip in §3. |

### 1.c GAME_TOTAL
| Check | Value |
|---|---|
| sportsbook events discovered | 46 |
| standard lines (`totals`) | **YES**, 224 |
| alt lines (`alternate_totals`) | YES, 6 + 30 cache docs |
| odds / historical / L5/L10 / opponent | YES / YES (team pace via season totals once ingested) / YES / YES |
| exact scorer | NONE — sim_nba has `team_total` branch but produces `return 1.0` for `_factor_adjustment` and never actually simulates distribution (brain/sim_nba.py:144). No producer feeds game-total candidates. |
| simulator attempt | NONE |
| probability produced | NO |
| disposition | 0 candidates generated |
| **first fail-closed gate** | Upstream route gap + no real game-total distribution simulator (current code treats team_total as a factor-only pass-through). |
| missing requirement | (1) NBA game-total candidate emitter; (2) In `brain/sim_nba.py` complete the `team_total`/`game_total` branch — sample λ or Normal(µ,σ) from team-pace priors in `canonical_team_game_actuals` (which must be ingested for lowercase `sport="nba"`); (3) Flip registry. |
| **Classification** | **C — MODEL NOT IMPLEMENTED/WIRED** (plus **B — missing team-game actuals for pace priors**) |
| **Required legitimate fix** | Add producer + implement the sim branch using team-pace priors from the existing `nba_gamelog_ingest`. If needed, add team-level actuals ingestion (not yet populated for NBA). |

### 1.d PLAYER_POINTS
| Check | Value |
|---|---|
| sportsbook events discovered | 46 |
| standard lines (`player_points`) | implied from pick: `provider_market_key="player_points"` + `player_points_alternate` surface alts. 196 picks produced; cache has 28 alt-market docs (standard not explicitly fetched — producer uses alt + implied-standard). |
| real alt lines (`player_points_alternate`) | **YES**, 28 bookmaker-market rows, 30 cache docs |
| odds / historical actuals / L5/L10 / opponent | YES / **YES** (29,481 docs under `sport='nba'`) / YES / YES |
| exact scorer | `brain.sim_nba.simulate_nba_pick`, category "points", **Normal** distribution. Confirmed ran: `simulator_name='nba_simulator' v1.1.0`, `simulator_type=distribution_monte_carlo`. |
| simulator attempt | **YES** for all 196 |
| probability produced | **YES** (sim_win_probability populated for all 196) |
| model_version | `sim_nba_v1.1.0` |
| disposition | all 196 → `evaluated` → `rejected (no_bet)` |
| **first fail-closed gate** | `services/canonical_publication_boundary.evaluate_publication` — Rule 5.5 (`sport_model_authority.is_unavailable("NBA", "player_points") → True`) → appends `MODEL_LINE_NOT_REAL_OFFERING`; Rule 5.6 (`universal_market_contract.get("NBA", NBA_POINTS).capability_state == MODEL_UNAVAILABLE`) → appends `UMC_CAPABILITY_STATE_MODEL_UNAVAILABLE`; Rule 6 (`player_event_identity_gate.evaluate_identity → PLAYER_TEAM_UNRESOLVED` because `pick.player_team / canonical_team_id / team / team_abbrev` all absent despite data existing in `canonical_players`). |
| exact input received at the gate | `pick.market_family=None`, `pick.canonical_market_family=None`, `pick.sport='NBA'`, `pick.player_team=None`, `pick.canonical_team_id=None`, `pick.player_name='Jrue Holiday'`, `pick.player_recent_rows[0].team='…'` (present but unread). |
| exact missing requirement | (a) The two registries claim MODEL_UNAVAILABLE while the pick now carries real sim output + 29,481 real actuals + L10 team-stamped recent rows; (b) the identity gate reads only direct `player_team`-style keys and never falls back to `player_recent_rows[0].team` or a lookup in `canonical_players`. |
| **Classification** | **D — VALID NBA MODEL OUTPUT REJECTED BY STALE REGISTRY + WRONG IDENTITY-READING** |
| **Required legitimate fix** | **(1) Enrichment:** before publication, stamp `pick.player_team` from `canonical_players{sport:'nba', name:player_name}.team_name` (or `player_recent_rows[0].team`). **(2) Market-family stamp:** set `canonical_market_family="player_points"` on build so the authority/UMC lookup is deterministic. **(3) Case normalization:** when NBA code reads actuals/players, filter `sport ∈ {"NBA","nba"}` (or lower() both sides). **(4) Registry flip (ONLY after sim_nba independence is validated):** change the NBA `player_points / player_rebounds / player_assists / nba_pra / player_threes` capability_state from `MODEL_UNAVAILABLE` to `ACTIVE` with `model_authority="brain_sim_nba"`, and change `AUTHORITY["NBA"]["player_points"]` from `UNAVAILABLE` to `"brain_sim_nba"`. **This flip must only happen when (a) sim_nba is reading `recent_rows` of ≥N real actuals, (b) `independent_evidence` is validated to mean "P(over) is not just the back-solve of model_wp", and (c) the architecture comment at `sport_model_authority.py:106` is updated with the new justification.** |

### 1.e PLAYER_REBOUNDS
Identical mechanism to §1.d. 161 candidates, 100 ≥ 85, same three reasons. Classification **D**. Same fix.

### 1.f PLAYER_ASSISTS
Identical mechanism to §1.d. 77 candidates, 49 ≥ 85, same three reasons. Classification **D**. Same fix.

### 1.g PLAYER_PRA
| Check | Value |
|---|---|
| sportsbook events discovered | 46 |
| standard / alt lines | provider cache has `player_points_rebounds_assists_alternate` market key available by Odds API catalog, but **the current NBA fetch does not request it** (only `player_points_alternate / rebounds / assists / threes` are requested — see cache markets list). |
| odds present | NO (not fetched) |
| historical actuals | YES — PRA computable from the 29,481 actuals rows |
| L5/L10 / opponent | YES |
| exact scorer | `brain.sim_nba.simulate_nba_pick` HAS a `pra` branch (sum of three Poissons/Normal), but it's never reached because candidates aren't built. |
| simulator attempt | NO |
| probability produced | NO |
| **first fail-closed gate** | Upstream route gap: PRA alt market not in the NBA markets requested from provider. |
| **Classification** | **A — MISSING REAL SPORTSBOOK INPUT (upstream acquisition)** |
| **Required legitimate fix** | Add `player_points_rebounds_assists`/`player_points_rebounds_assists_alternate` to the NBA markets requested in the Odds API acquisition path (same provider plan the user already pays for). |

### 1.h PLAYER_THREES
| Check | Value |
|---|---|
| sportsbook events discovered | 46 |
| standard / alt lines | **YES** — 28 `player_threes_alternate` bookmaker-market rows + 30 cache docs |
| odds present | YES |
| historical / L5/L10 / opponent | YES |
| exact scorer | `brain.sim_nba._classify_nba_market` returns `"threes"`, sim has a dedicated branch |
| simulator attempt | NONE (no candidate reaches it) |
| probability produced | NO |
| **first fail-closed gate** | Upstream route gap: cache contains alt three-point lines, but `_extract_nba_prop_candidates` / NBA pick-build path does not emit THREES candidates. |
| **Classification** | **C — MODEL NOT WIRED (producer gap)** |
| **Required legitimate fix** | Extend `_extract_nba_prop_candidates` to read `player_threes_alternate` offerings (same shape as the other three families) and emit candidates. Then the existing `threes` sim branch simulates them. |

---

## 2 · FULL FUNNEL COUNTS

Legend: `provider_events → provider_markets → standard_lines → alt_lines → normalized_rows → history_ready → model_attempts → probabilities_produced → candidates → ≥85 → publication_attempts → published`

| Family | prov_events | prov_markets | std_lines | alt_lines | norm_rows | hist_ready | model_attempts | prob_produced | candidates | ≥85 | pub_attempts | published |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| MONEYLINE          | 46 | 210 (`h2h`)            | 210 | 0  | 0   | YES* | 0 | 0 | **0** | 0 | 0 | 0 |
| POINT_SPREAD       | 46 | 224 (`spreads`)+9 alt  | 224 | 9  | 0   | YES* | 0 | 0 | **0** | 0 | 0 | 0 |
| GAME_TOTAL         | 46 | 224 (`totals`)+6 alt   | 224 | 6  | 0   | YES* | 0 | 0 | **0** | 0 | 0 | 0 |
| PLAYER_POINTS      | 46 | 28 (`_alternate`)      | n/a† | 196| 196 | YES* | 196 | 196 | **196** | 85 | 196 | **0** |
| PLAYER_REBOUNDS    | 46 | 28                      | n/a† | 161| 161 | YES* | 161 | 161 | **161** | 100 | 161 | **0** |
| PLAYER_ASSISTS     | 46 | 28                      | n/a† | 77 | 77  | YES* | 77 | 77 | **77** | 49 | 77 | **0** |
| PLAYER_PRA         | 46 | 0 (NOT REQUESTED)       | 0   | 0  | 0   | YES* | 0 | 0 | **0** | 0 | 0 | 0 |
| PLAYER_THREES      | 46 | 28 (`_alternate`)      | n/a† | 0  | 0   | YES* | 0 | 0 | **0** | 0 | 0 | 0 |
| **TOTAL**          | 46 | ~814 bookmaker-market pairs | — | — | 434 | — | 434 | 434 | **434** | 234 | 434 | **0** |

\* `history_ready = YES` requires reading NBA actuals via lowercase sport filter. Any code reading `sport="NBA"` sees 0.
† Standard-line counts for player props are via same provider market key family (standard + alternate share the same Odds API family).

---

## 3 · CRITICAL QUESTION ANSWERED

**"Explain exactly why producing NBA picks right now would require violating the safety constraints."**

It **would not — provided we fix the right things first**. Specifically:

* **The sportsbook inputs are present** for 6 of 8 families (ML, SPREAD, TOTAL, POINTS, REBOUNDS, ASSISTS, THREES). Only PRA is **A — MISSING REAL SPORTSBOOK INPUT** (not requested from Odds API).
* **The real historical evidence is present** — 29,481 NBA player-game actuals + 649 players with team mapping, all under lowercase `sport="nba"`. The system doesn't *see* them in the publication path because:
  - Validators query uppercase `sport="NBA"` (**D — wrong NBA-specific validation**).
  - The pick-build path never stamps `player_team` even though the data to resolve it is a single Mongo lookup away.
* **The model is implemented** for POINTS/REBOUNDS/ASSISTS/THREES/PRA (brain/sim_nba.py has all those branches, with Normal and Poisson distributions). But:
  - The producer only *emits candidates* for POINTS/REBOUNDS/ASSISTS. THREES has `_alternate` quotes in cache but no candidate emitter (**C**). PRA is not even requested from provider (**A**).
  - Game markets (ML/SPREAD/TOTAL) have provider data but no producer wiring and partial sim coverage (ML sim exists; SPREAD/TOTAL sim is stubbed) (**C**).
* **The two registries (`sport_model_authority.AUTHORITY['NBA']`, `universal_market_contract` NBA entries) explicitly claim `MODEL_UNAVAILABLE` for every NBA family.** The architectural comment that justifies this (`sport_model_authority.py:106–114`) is **STALE**:
  * It says "player_game_actuals/team_game_actuals carry zero NBA rows in this deployment" — this is NO LONGER TRUE for player actuals (29,481 rows for sport='nba'). It is still true for team actuals (0 rows for both cases).
  * It says sim_nba is "MODEL_CONDITIONED (research only)". This is correct for the architecture **as literally coded in sim_nba.py's docstring** (back-solve λ from model_wp). The today's picks are stamped `independent_evidence: True` with `sim_trust_state: SIM_EDGE`, implying the sim has partially broken out of pure model-conditioning when `recent_rows` is supplied — but this is a **product-decision**, not a mechanical fix. **Someone with authority must certify that sim_nba with recent_rows constitutes independent predictive evidence before flipping the registries.** Flipping without certification would be weakening the integrity contract.

**In plain English:** The pipeline would NOT be forced to manufacture lines, fake probabilities, lower the 85 threshold, or weaken evidence requirements to publish NBA picks. It needs:
1. Three legitimate upstream wiring fixes (producer gaps for ML/SPREAD/TOTAL/THREES, provider-markets gap for PRA).
2. Two correctness fixes (enrichment to stamp `player_team` from existing data; case normalization for the lowercase `nba`/uppercase `NBA` split).
3. One certified product decision (flip the two registries from MODEL_UNAVAILABLE to ACTIVE for sim_nba — ONLY after someone certifies sim_nba as independent, and the stale architectural comment is updated).

**I am not making any of these changes until authorized.** The report above is the full evidentiary basis.

---

## 4 · RECOMMENDED NEXT STEP (NO CHANGE YET)

The smallest **safe, legitimate** sequence that would unblock NBA WITHOUT weakening a single safety guard:

1. **STEP A — Case + enrichment fix (zero-authority change, strictly correctness):**
   - Stamp `pick.player_team` + `pick.canonical_team_id` from `canonical_players{sport:{$in:["NBA","nba"]}, name|canonical_name:player_name}` during NBA pick build.
   - Stamp `pick.canonical_market_family` deterministically from `provider_market_key` on build.
   - Where NBA code reads actuals/players, use `sport ∈ {"NBA","nba"}` (or `.lower()`).
   - **Effect:** `PLAYER_TEAM_UNRESOLVED` disappears for every player prop. `IMPOSSIBLE_IMPLIED_PROBABILITY` (1 instance) stays — that one row has a real edge-magnitude outlier and SHOULD be rejected.
   - **Still blocked by:** `MODEL_LINE_NOT_REAL_OFFERING` + `UMC_CAPABILITY_STATE_MODEL_UNAVAILABLE` (the registry pair).

2. **STEP B — Producer wiring (strictly adding real data flow):**
   - Add NBA game-market producer (ML/SPREAD/TOTAL) consuming `h2h/spreads/totals` cache into pick dicts.
   - Add NBA THREES producer path (same shape as points/rebounds/assists).
   - Add `player_points_rebounds_assists_alternate` to the NBA markets Odds API acquisition list.
   - Add two tiny sim_nba classifier/branch entries for `spread` and complete the `team_total` distribution.
   - **Effect:** All 8 families start generating candidates.
   - **Still blocked by:** the registry pair.

3. **STEP C — Registry flip (product decision, requires sign-off):**
   - Flip `sport_model_authority.AUTHORITY["NBA"][*]` from `UNAVAILABLE` → `"brain_sim_nba"` (new tag) for the families where sim_nba is proven independent.
   - Flip `universal_market_contract` NBA entries from `capability_state=MODEL_UNAVAILABLE` → `ACTIVE` with `model_authority="brain_sim_nba"`.
   - Update the stale architectural comment at `sport_model_authority.py:106–114` to reflect the new truth and the certification basis.
   - **This is the ONLY step that touches a safety guard.** It should be an explicit, logged, user-authorized action — not a side effect.

If any of A/B/C is not authorized, NBA remains fail-closed by design and the only honest report for today is: **0 NBA picks published — root cause = architectural MODEL_UNAVAILABLE state + enrichment gaps + producer gaps.**

---

**End of report. ZERO code changes made.**
