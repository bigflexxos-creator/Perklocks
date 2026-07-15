# DATA GAP AUDIT — Current vs Professional-Grade Model Inputs
**Date:** 2026-07-14 · **Scope:** MLB, Soccer, Tennis, NFL, UFC (NBA held on user request)

---

## 0. Executive Summary

Your engine is **inputs-rich on cheap public data** (ESPN, MLB Stats API, Understat, Open-Meteo)
and **inputs-poor on the two categories that move win rates the most**: (a) **Statcast/tracking
data** and (b) **market microstructure**. Historical ROI tells the story — favs are hitting
71.6% but paying −4.7u/100 because the model can't discriminate between "72% chalk that closes
at −220" and "72% chalk that closes at −180" (i.e., you're consistently on the wrong side of
the closing line). Longshots (+500+) are your only positive-ROI band (+44.9u/100), which is
exactly what happens when a model has directional accuracy but poor probability calibration.

The **five ingestions that will move your accuracy needle the most** (in priority order):

1. **Closing-line + steam / no-vig fair odds** (all sports) — kills negative-EV chalk grinding.
2. **Statcast batter-quality (xwOBA, barrel%, EV)** + **Stuff+ / pitch grades** (MLB).
3. **xG / xGA per-shot with venue+opp adjustments + PPDA + xT** (Soccer, from FBref/StatsBomb).
4. **Serve/return granular splits (1st serve %, break-point saved/converted, hold%/break%)**
   pulled from Tennis Abstract or Jeff Sackmann's github (Tennis).
5. **Snap-count / route participation / target share** (NFL from PFR or nflfastR).

---

## 1. Per-Sport Feature Inventory (what the model READS today)

### 1a. MLB

| Bucket | Field | Source | Signal Engine consumer |
|---|---|---|---|
| **Batter** | Season AVG, OBP | MLB Stats API | mlb_hitter_intel → form_signal |
| Batter | vs LHP / vs RHP splits | MLB Stats API | mlb_hitter_intel |
| Batter | Last-15 HR count, PA | MLB Stats API gameLog | mlb_hr_intel |
| Batter | ISO, barrel% (best-effort) | Statcast hidden endpoint | mlb_hr_intel |
| Batter | BvP career (H, AB, AVG) | MLB Stats API | matchup_signal.bvp_history |
| **Pitcher** | Season K/9, ERA, HR/9 | MLB Stats API | mlb_hitter_intel |
| Pitcher | Hand (L/R) for platoon | MLB Stats API | mlb_hitter_intel |
| Pitcher | Same-day starter map | MLB Stats API schedule | mlb_matchup_resolver |
| **Park** | Park HR / hits / runs factor (30 teams, hard-coded) | Baseball Savant averages baked in | mlb_deep_signal |
| **Weather** | Temp, wind speed/dir, humidity, precip | Open-Meteo (free) | mlb_hr_intel |
| **Market** | American odds, implied % | The Odds API | market_signal, value_signal |
| Market | Odds at pick (opening) | Internal snapshot | market_signal (line move) |
| Market | Model win prob (v1/v2/sim) | probability_engine ensemble | value_signal |
| **Injury** | Team injury tiers (out/dbt/qst) | ESPN injuries | injury_signal |
| **Form** | Last-5 / Last-10 game logs | historical.lookup | form_signal |
| **Signal Engine deep** | park × market family alignment | mlb_deep.enrich_mlb_pick | mlb_deep_signal (±5) |

### 1b. Soccer

| Bucket | Field | Source | Signal Engine consumer |
|---|---|---|---|
| **Player attack** | Goals, xG, xA, minutes, npxG | Understat (Top-5 leagues) | goal_scorer_engine_v2 |
| Player attack | Shots, key passes, penalties taken | Understat | goal_scorer_engine_v2 |
| Player attack | Anytime scorer probability | goal_scorer_engine_v2 computed | scorer_streak |
| Player attack | Goals/xG ratio → HOT/COLD tag | Understat | form_signal, soccer_deep_signal |
| **Team offense/defense** | Team xG, xGA (season) | Understat | soccer_deep_signal.xg_diff |
| Team | xG Difference component 0-100 | factors table (internal) | soccer_deep_signal |
| Team | Home advantage component 0-100 | factors table + league-tier list | soccer_deep_signal |
| **Player usage** | Penalty taker flag, starter probability | national_team_squads + soccer_ingest | volume_signal |
| Player usage | Expected minutes, role tag | goal_scorer_engine_v2 | volume_signal |
| **Fallback player data** | Goals in season for lower leagues | FotMob (only if Understat misses) | soccer_ingest.py |
| Fallback | Top-scorer list per league | Wikipedia scraper (`wiki_top_scorers`) | scorer_bundles |
| **Market** | Odds, implied %, book_odds movement | The Odds API | market_signal |
| **Injury** | ESPN injuries | ESPN | injury_signal |
| **Form (team)** | Last-5 team W-L-D delta | espn_signal_engine | form_signal |
| **Simulator** | Monte Carlo win probability | sim_engine | value_signal |
| **Signal Engine deep** | xG regression (G/xG > 1.35 or < 0.70) | soccer_deep.enrich_soccer_pick | soccer_deep_signal (±5) |

### 1c. Tennis

| Bucket | Field | Source | Signal Engine consumer |
|---|---|---|---|
| **Elo** | Overall Elo, per-surface Elo (Hard/Clay/Grass) | `tennis_players` DB (internal, seeded from Tennis Abstract) | tennis_deep_signal.elo_edge |
| **Player recent load** | Matches in last 7d | `tennis_players.matches_7d` | tennis_deep_signal fatigue |
| **Surface fit** | Composite 0-100 (implied % + tour tier hash) | tennis_engine._surface_score | tennis_deep_signal.surface_fit |
| Surface fit | Serve/return dominance composite 0-100 | tennis_engine._serve_return_score | tennis_deep_signal.serve_return |
| **Motivation** | Tier-based (Slam > Masters > ATP 500 > ATP 250 > ITF) | tennis_engine._motivation_score | tennis_deep_signal.motivation |
| **Variance** | Player-tier + market variance penalty | tennis_engine._variance_penalty | tennis_deep_signal.variance |
| **99-lock eligibility** | Tour-tier + implied % gate | tennis_engine | tennis_deep_signal (bonus) |
| **Market** | Odds, implied %, line moves | The Odds API + Tennis Explorer scrape (fallback) | market_signal |
| **Tournament** | Tour, event, surface | tennis_extra scraper | context |

### 1d. NFL (light — off-season)

| Bucket | Field | Source | Consumer |
|---|---|---|---|
| Player | Season game log, targets, rushes | ESPN team roster + player stats | nfl_atd_engine, nfl_ingest |
| Player | Red-zone touches heuristic | derived from game logs | nfl_atd_engine._td_outlier_check |
| Team | Season record | Wikipedia scrape | matchup_signal (record) |
| Team | Injury report | ESPN | injury_signal |
| Market | Odds | The Odds API | market_signal, value_signal |
| Rationale | Rules-based rationale | nfl_rationale.py | frontend blurb |

### 1e. UFC

| Bucket | Field | Source | Consumer |
|---|---|---|---|
| Fighter | Record, ESPN athlete stats | ESPN | espn_signal_engine.form + record |
| Fight | Card + weight class metadata | ufc_espn_ingest | ingestion |
| Market | Odds | The Odds API | market_signal |
| Injury | ESPN (rare for MMA) | ESPN | injury_signal |

> UFC has essentially **no fight-quant inputs** — no significant strike accuracy, TD defense,
> reach differential, layoff. It's running on odds + season record only.

---

## 2. Professional-Grade Benchmark — What Winning Bettors / Sportsbooks Actually Use

### 2a. MLB (bookmaker parity target)

Inputs that empirically improve out-of-sample AUC for MLB props (source: Baseball Prospectus,
Baseball Savant published papers, Fangraphs pitcher-projection models):

| Feature | Impact | Currently in your model? |
|---|---|---|
| **xwOBA, xBA, xSLG** (batter quality decoupled from luck) | High | ❌ |
| **Barrel%, hard-hit%, exit velocity, launch angle** | High | Partial (barrel% best-effort) |
| **Pitcher Stuff+, Location+, Pitching+** (Eno Sarris / Fangraphs) | High | ❌ |
| **Pitch mix + release point + spin rate** | Medium-High | ❌ |
| **Catcher framing runs** (2-3 K per game per pitcher over avg framer) | Medium | ❌ |
| **Umpire zone tendency + K% behind the plate** | Medium | ❌ |
| **Batter recent-15 xwOBA vs pitcher-type (velo band × pitch mix)** | High | ❌ |
| **Bullpen usage / rest state (pitcher fatigue days back)** | High | ❌ |
| **Batting order + top-of-order PA projections** | Medium | ❌ (assumes order) |
| **Home/road platoon + day/night splits** | Low-Medium | Partial (LHP/RHP only) |
| Park HR/hits/runs factors | Medium | ✅ (static) |
| Weather (temp, wind vector to plate) | Medium (HR) | ✅ |
| **Weather wind projected onto ballpark azimuth** (wind_out_to_CF vs raw wind_deg) | Medium | ❌ (stored raw only) |
| **Vegas total + implied run env for the game** | High (game-tot picks) | Partial |
| **Closing-line delta / no-vig fair odds** | Very High (ROI) | ❌ |
| **Steam moves / sharp-side indicator** | High | ❌ |
| Injuries | Medium | ✅ |
| BvP small-sample | Low (overweighted historically) | ✅ (already weighted low) |

### 2b. Soccer (pro squad-model parity target)

| Feature | Impact | Currently in your model? |
|---|---|---|
| **xG / xGA per shot with venue+opp adjustment (FBref, StatsBomb Free)** | Very High | Partial (Understat basic xG) |
| **npxG, npxGA (non-penalty)** | High | ✅ |
| **PPDA (pressing intensity), high turnover regains** | Medium-High | ❌ |
| **xT (Expected Threat) — chain-of-passes value** | High | ❌ |
| **Set-piece xG (corners, free kicks, throw-ins)** | Medium | ❌ (heuristic prior only) |
| **Player-level xA, key passes, progressive passes** | Medium | ✅ (Understat) |
| **Shot creation actions (SCA) + goal creation actions (GCA)** | Medium | ❌ |
| **Defensive metrics: tackles won, interceptions, aerial %** | Medium | ❌ |
| **GK-specific: post-shot xG saved, GSAA** | Medium (BTTS/UO markets) | ❌ |
| **Formation matchup (4-2-3-1 vs 3-4-3 etc.)** | Low-Medium | ❌ |
| **Referee card/foul tendency** | Medium (card markets) | ❌ |
| **Travel distance + rest days** | Medium | ❌ |
| **Elo (club, per-competition)** | High | ❌ (only team factor table) |
| **Market: line movement in last 6h vs sharp books (Pinnacle, Betfair steam)** | Very High | Partial (own book movement only) |
| **Closing-line value tracking** | Very High | Partial (component computed, not persisted-cleanly) |
| Injuries (weighted by minutes/xG) | High | Partial (ESPN counts only, not weighted) |
| Understat HOT/COLD flag | Medium | ✅ |

### 2c. Tennis (pro parity)

| Feature | Impact | Currently in your model? |
|---|---|---|
| **1st serve %, 1st serve won %, 2nd serve won %** | Very High | ❌ (composite proxy only) |
| **Break point saved % (BPS%) + break point converted %** | Very High | ❌ |
| **Return points won % (per surface)** | High | ❌ |
| **Hold % / Break %** | High | ❌ |
| **Elo per surface** (Sackmann's) | Very High | ✅ (surface Elo in tennis_players DB) |
| **Head-to-head (career + surface-specific)** | Medium | ❌ |
| **Recent match load (7d + 14d) + travel** | Medium | Partial (7d only) |
| **Retirement rate / injury history** | Medium (avoids busted books) | ❌ |
| **Best-of-3 vs Best-of-5 form gap** | Medium (Slams only) | ❌ |
| **Indoor vs outdoor court** | Low-Medium | ❌ |
| **Altitude venue (Bogotá, Kitzbuhel)** | Medium | ❌ |
| **First-set win% conditional on match win** | Medium (in-match derivatives) | ❌ |
| **Sportsbook market movement + no-vig line** | Very High | ❌ |
| Tour tier motivation | Medium | ✅ |

### 2d. NFL (pro parity — off-season minimum for return)

| Feature | Impact | Now? |
|---|---|---|
| **Snap count %, route participation %** | Very High | ❌ |
| **Target share, air-yard share, aDOT** | Very High | ❌ |
| **Red-zone / goal-line touch share** | Very High | Partial (heuristic) |
| **YAC / EPA per touch (nflfastR)** | High | ❌ |
| **QB pressure rate, sack rate, deep-ball rate** | High | ❌ |
| **Vegas game total, spread, implied team total** | Very High | Partial |
| **Weather (wind, temp, precip)** | High (outdoor games) | ❌ |
| **O-line / D-line grades (PFF-like)** | High | ❌ |
| **Coach tendencies (pass rate over expected, PROE)** | Medium | ❌ |
| **Injury status weighted by usage share** | High | Partial |

### 2e. UFC (pro parity)

| Feature | Impact | Now? |
|---|---|---|
| **Significant strikes landed / absorbed per min** | Very High | ❌ |
| **Takedown accuracy / defense** | Very High | ❌ |
| **Reach differential, height, stance matchup** | High | ❌ |
| **Cardio decay (round-by-round output)** | High | ❌ |
| **Layoff duration, camp changes** | Medium | ❌ |
| **Weight-cut history (missed weight, hydration issues)** | Medium | ❌ |
| **Style matchup (grappler vs striker etc.)** | Medium | ❌ |
| **Market movement + no-vig** | Very High | ❌ |

---

## 3. Prioritized Ingestion Roadmap

Priority is on **expected accuracy lift** measured against your 30-day historical loss patterns
(chalk-hemorrhage on −110 to −200, breakeven big-dogs, profitable long-shots). The single
biggest hole is **market microstructure** — you're consistently on the wrong side of the
closing line, which is a market problem, not a sport problem. Fixing #1 alone will convert
your 71.6% chalk band from −4.7u/100 to positive-EV.

### Phase 0 — Cross-sport (BIGGEST LIFT, do first)

| Rank | Feature | Source | Ease | Est. accuracy lift |
|---|---|---|---|---|
| 0.1 | **Closing-line snapshot at match start + no-vig fair odds** | Snap `book_odds` at T-5min into `closing_odds` field; compute no-vig via own devig math on both sides | Easy (schema already has `closing_odds_snapshotter.py`) | +2-4% ROI on all sports |
| 0.2 | **Pinnacle / Betfair line as sharp anchor + steam detector** | The Odds API supports Pinnacle as a bookmaker; store `sharp_odds` alongside `book_odds` and compute delta | Easy | +1-3% ROI |
| 0.3 | **CLV tracking dashboard** (per pick: did we beat the close?) | Uses 0.1 output; already have `lock_components.clv` component computed but not persisted-across-days | Easy | Massive learning signal for weekly model retunes |

### Phase 1 — MLB (highest impact, sport-specific)

| Rank | Feature | Source | Ease | Lift |
|---|---|---|---|---|
| 1.1 | **Statcast xwOBA, xBA, xSLG, barrel%, EV, launch angle** (batter + pitcher) | Baseball Savant CSV endpoints (`https://baseballsavant.mlb.com/statcast_search/csv`) — free, no auth | Medium (CSV parsing, daily job) | +3-5% AUC on hitter Overs |
| 1.2 | **Fangraphs Stuff+ / Location+ / Pitching+ pitcher grades** | Fangraphs public leaders CSV (`https://www.fangraphs.com/leaders.aspx?...`) — HTML scrape | Medium | +3-5% AUC on pitcher K/ER |
| 1.3 | **Bullpen fatigue: pitcher days-rest + pitches-thrown-in-last-3d** | MLB Stats API (already have gameLog) — derived field | Easy | +2% on team totals / late-game overs |
| 1.4 | **Umpire behind-plate K% + BB tendency** | UEFL public data (`http://www.uefl.net/`) or Baseball Prospectus umpire spreadsheet | Medium (scrape + weekly refresh) | +2-3% on pitcher K props |
| 1.5 | **Batting order projection (top-3 = ~4.6 PA, bottom-3 = ~3.6 PA)** | MLB Stats API `/game/{pk}/boxscore` publishes probable lineups ~2h pre-game | Easy | +2% on all volume-driven props |
| 1.6 | **Wind projected onto ballpark azimuth** (wind_out_to_CF component) | Already have wind_deg + Statcast has park orientation table | Easy (add azimuth column to stadium map) | +1-2% on HR/total-bases |
| 1.7 | **Catcher framing runs** | Baseball Prospectus / Statcast public tables | Medium (rare-refresh, yearly) | +1-2% on pitcher K |

### Phase 2 — Soccer

| Rank | Feature | Source | Ease | Lift |
|---|---|---|---|---|
| 2.1 | **FBref per-90 xG/xGA/PPDA/xT for both team + opponent** | FBref (StatsBomb-powered) — HTML scrape, ~500ms per league | Medium | +3-5% on team markets |
| 2.2 | **Elo per club per competition** | ClubElo.com API (free, JSON) | Easy | +3-4% on H2H / +2% on match total |
| 2.3 | **Set-piece xG share of total xG** | FBref `stats/{team}_set-piece.csv` | Medium | +2% on corner/set-piece markets |
| 2.4 | **Rest days + travel distance** | Compute from own fixture list (kickoff timestamps + venue coords) | Easy | +1-2% on team form |
| 2.5 | **Sharp book anchor (Pinnacle) for soccer moneylines** | The Odds API — already available, just wire the "sharpest of any book" logic | Easy | +1-3% ROI on H2H |
| 2.6 | **Referee card/foul tendency (top-5 leagues)** | FBref referee page (per-league scrape) | Medium | Enables card markets (currently 0 picks) |

### Phase 3 — Tennis

| Rank | Feature | Source | Ease | Lift |
|---|---|---|---|---|
| 3.1 | **Jeff Sackmann per-match serve/return CSVs** | GitHub `JeffSackmann/tennis_atp` + `tennis_wta` (weekly refresh, MIT-licensed) | Easy (git-based ingest) | +4-6% on tennis moneylines |
| 3.2 | **Career + surface-specific H2H** | Sackmann matches CSV filtered by both player IDs | Easy | +2% |
| 3.3 | **Retirement / walkover rate per player last 12 months** | Sackmann matches CSV, filter `RET`/`W-O` | Easy | +1-2%, avoids blown props |
| 3.4 | **Indoor/outdoor + altitude flags per tournament** | Static table (~60 events) | Easy | +1-2% on serve-dominant markets |
| 3.5 | **No-vig fair odds** (already covered by Phase 0.1) | — | — | — |

### Phase 4 — NFL (queue for pre-season)

| Rank | Feature | Source | Ease | Lift |
|---|---|---|---|---|
| 4.1 | **nflfastR play-by-play → snap %, route %, target share, aDOT** | Python nflfastr (parquet on GitHub) — batch-load weekly | Medium (batch) | +5-7% on ATD / receiving props |
| 4.2 | **Vegas team totals (implied points per team)** | Derive from spread + total (already have both) | Easy | +2-3% on QB pass yards, RB rushing |
| 4.3 | **Weather at outdoor venues** | Open-Meteo (already integrated for MLB — reuse) | Easy | +1-3% on totals / pass yards |
| 4.4 | **PFR o-line / d-line grades** | pro-football-reference weekly HTML scrape | Medium | +2% |

### Phase 5 — UFC (build from scratch)

| Rank | Feature | Source | Ease | Lift |
|---|---|---|---|---|
| 5.1 | **UFCStats.com fighter career stats** (SLpM, StrAcc, StrDef, TDAcc, TDDef, SubAvg) | HTML scrape (`http://ufcstats.com/statistics/fighters?...`) | Medium | +6-10% (starting from ~zero data) |
| 5.2 | **Reach differential + stance matchup** | UFCStats fighter page | Easy | +2% |
| 5.3 | **Layoff duration + camp changes** | UFCStats + Sherdog (camp changes rarely tracked) | Medium | +1-2% |

---

## 4. What NOT to build (low-value distractions)

- ❌ **Additional player-form APIs beyond what we have.** Understat + MLB Stats API + ESPN cover 90% of usable form data; a 5th form source just adds noise.
- ❌ **BvP small-sample overweighting.** You already correctly cap at ≥8 AB and weight low; more BvP fields won't help.
- ❌ **"Sentiment / news" scrapers.** Twitter injury reports beat you to it and add noise.
- ❌ **Player-tracking cameras / SportVU-equivalent for soccer.** Not licensable at your price point and marginal lift over FBref.

---

## 5. Recommended sequencing (fastest → highest ROI)

**Week 1 (Phase 0):** Closing-line snapshot + no-vig + Pinnacle anchor. Zero new data sources — everything is already in The Odds API or in memory. Expected ROI lift: +2-4% across all sports.  ✅ **COMPLETE 2026-07-14**
   • `services/devig.py` — no-vig two-way/three-way math + `devig_pick` mutator.
   • `pick_enrichment.py` — attaches `no_vig_implied_pct`/`book_hold_pct` to every new pick.
   • `routes/picks_routes.py` — on-read devig backfill for `/api/picks/today` so existing picks also benefit.
   • `signal_engine/calculators.py::value_signal` — grades edge against fair market when available (kills the chalk-hemorrhage over-edge).
   • `signal_engine/calculators.py::market_signal` — reads `sharp_vs_median_pp` for Pinnacle steam detection.
   • `closing_line_snapshotter.py` — captures Pinnacle price alongside the median close, persists `sharp_closing_odds` + `sharp_vs_median_pp`.
   • `routes/analytics_routes.py::/analytics/clv` — new CLV dashboard endpoint with per-band Beat-Close %, ROI, and average CLV in implied-probability points.
   • `tests/test_devig.py` — 24 unit tests, all passing.

**Week 2 (Phase 1 top 3):** Statcast xwOBA/barrel%, Fangraphs Stuff+, bullpen fatigue. Baseball is still in-season and highest pick volume.
   • Phase 1.3 + 1.5 (bullpen fatigue + batting order) ✅ **COMPLETE 2026-07-14**
     - `services/mlb_usage.py` — MLB Stats API-backed batching for lineup + probable pitcher + gameLog fatigue calculation.
     - Attaches `batting_order`, `expected_pa`, `pitcher_days_rest`, `pitcher_pitches_3d`, `pitcher_fatigue_flag`.
     - `volume_signal` rewrites: top-3 hitters +1.8, bottom-third −1.5, bench −4.0. Fresh pitcher +1.2 / gassed −2.5 on Over K props.
     - 23 unit tests passing.
   • Phase 1.1 (Statcast xwOBA / barrel% / EV) ✅ **COMPLETE 2026-07-14**
     - `services/mlb_statcast.py` — Baseball Savant CSV ingester (expected-statistics + statcast batted-ball). 422 batters + 486 pitchers cached for 2026 season.
     - Daily 24h refresh loop scheduled from server startup.
     - Attaches `statcast_batter` (xBA/xwOBA/barrel%/EV) and `statcast_pitcher` (xwOBA-against/xERA).
     - `mlb_deep_signal` budget bumped to ±7. Reads Statcast for xBA regression signal (unlucky = Over buy), barrel% for HR/TotalBases boost, pitcher xwOBA-against for K/IP/ER modulation.
   • Phase 1.4 (Umpire K% zone) ✅ **COMPLETE 2026-07-14**
     - `services/mlb_umpire.py` — 40 active MLB umpires seeded with 2024-2025 K% deltas vs league avg.
     - Fetches plate umpire from MLB Stats API boxscore.
     - `volume_signal` awards ±1.8 pts on pitcher K props based on ump zone.
   • Phase 1.2 (Fangraphs Stuff+) — user held off, may pick up later.

**Testing status:** 130/131 tests passing (iteration 73 GREEN). Iter71 MLB grading fix intact.

**Week 3 (Phase 2 — Soccer):** ✅ **COMPLETE 2026-07-14** — Multi-source fallback ingest replacing the previously-blocked ClubElo/FBref/538 chain.

   • `services/soccer/` package — modular multi-provider architecture with per-document source tracking.
   • Sources implemented:
     - **football-data.co.uk** (CSV, no key) — 22,230 matches with 99.87% closing-odds coverage across 20 main leagues (EPL, La Liga, Bundesliga, Serie A, Ligue 1, Championship, Segunda, Serie B, Ligue 2, Eredivisie, Primeira, SPL, SD1, Belgian, Turkish, Greek, Conference, English L1/L2, La Liga 2) + 17 extra leagues via BRA/ARG/SWE/NOR/USA/MEX/CHN/DEN/SUI/AUT/POL/IRL/FIN/ROU/JPN/RUS CSVs.
     - **Football-Data.org** (free tier, API key `FOOTBALL_DATA_ORG_KEY` in env) — 212 standings + 60 fixtures + 212 teams across 12 competitions. Rate-limit-safe (6.5s inter-request delay + 429 retry).
     - **TheSportsDB** (free, no key) — 456 team metadata records (name, stadium, founded, badge, website).
     - **OpenLigaDB** (free, no key) — Bundesliga 1/2/3 authoritative data (306 matches per season).
     - **ESPN** — already wired via existing `services/espn_signal_engine.py`.
   • **Fallback orchestrator** (`services/soccer/fallback.py`) — provider priority per capability with cache-first reads; if all providers fail, returns cached data even if stale (never leaves callers empty).
   • **Provider trust ranking** — merge logic prefers higher-trust source on conflict (football-data.co.uk > football-data.org > openligadb > thesportsdb) but never overwrites a non-None field with None.
   • **Daily background refresh** scheduled from server startup.
   • **Diagnostic endpoints** (admin-only):
     - `GET /api/admin/soccer/status` — per-source counts, coverage %, per-league breakdown, last-run timestamps.
     - `POST /api/admin/soccer/refresh?seasons=...` — manual refresh trigger.
     - `GET /api/admin/soccer/team/{name}?days=N` — recent-form snapshot with closing odds per match.
   • **Testing:** 24 unit tests (`test_soccer_sources.py`), 62 tests total across Phase 0/1/2 all passing.

**Week 4:** ✅ **COMPLETE 2026-07-14** — Phase 3 (Tennis) + Soccer signal wiring.

   • **Phase 3 — Tennis Sackmann-format ingest** (`services/tennis/`):
     - Source: Tennismylife/TML-Database GitHub CSVs (Sackmann's original repos removed 2025).
     - 6,071 ATP/WTA matches ingested from 2023-2024 with full serve/return stats.
     - 699 players aggregated into `tennis_player_stats` with 52-week rolling: `first_serve_pct`, `first_serve_won_pct`, `second_serve_won_pct`, `hold_pct`, `break_saved_pct`, `ace_pct`, `df_pct`, `retirement_rate_pct` — per-player AND per-surface (Hard/Clay/Grass/Carpet).
     - **Name normalizer** — handles both "Firstname Lastname" (Sackmann format) and "Lastname F." (Tennis Explorer scraper format).
     - `services/tennis/fallback.py::get_player_stats/get_h2h/get_recent_matches` — public lookups.
     - Weekly refresh loop scheduled from server startup.
     - **Wired into `tennis_deep_signal`** (budget bumped to ±7):
       * ±2 pts on first-serve-won differential (≥3pp gap)
       * ±1.5 pts on break-saved differential (≥5pp gap)
       * −1.2 pts penalty on retirement rate ≥8%
       * ±1.5 pts on career H2H edge (≥3 matches)

   • **Phase 2b — Soccer recent-form signal wiring:**
     - On-read enricher in `/api/picks/today` populates `pick["soccer_form"]` from cached `soccer_matches` (last-10 W/D/L per team, GF/GA averages, form string).
     - **Wired into `soccer_deep_signal`**:
       * ±2 pts on point-differential from last 10 (home vs away form gap ≥5 pts)
       * +1.5 pts on Over Goals when combined recent averages ≥3.2 gpm
       * +1.5 pts on Under Goals when combined recent averages ≤2.0 gpm
     - Signal now fires even for picks WITHOUT Understat coverage (previously dark for lower-tier leagues).

**Coverage status verified live:**
   - Tennis: 15/63 picks today show real ATP-tour Sackmann stats (rest are lower-tier ITF/challenger not in Sackmann's dataset).
   - Soccer: real form data for teams in cached leagues (EPL, Bundesliga, La Liga, Sweden, etc.); minor gap on Mexican Liga MX (not in our extra-leagues map — easy add if you want it).

**Week 3 (Phase 3 + Phase 2 top 3):** Sackmann tennis CSVs, ClubElo, FBref xG/PPDA. Tennis is high-volume on your board (132 picks today), soccer is highest overall (334).

**Week 4 (Phase 2 finish + Phase 0 dashboard):** Set-piece xG, referee tendencies, CLV dashboard.

**Off-season pause:** NFL/UFC ingestion in September and October respectively.

---

**File status:** written 2026-07-14. Update whenever a Phase completes so the roadmap reflects
reality.

---

## 2026-07-15 update — Phase 1.2 + Coverage Expansion (Iter 75)

### Phase 1.2 — Stuff+/Location+/Pitching+ (MLB)
- **Source**: Baseball Savant `pitch-arsenal-stats` (Fangraphs' own site is behind
  an interactive Cloudflare challenge that server-side scrapers cannot bypass;
  we compute a Stuff+/Location+ *analog* from Baseball Savant's per-pitch data —
  calibrated to Fangraphs' actual mean=100/SD=10 distribution).
- **Module**: `/app/backend/services/mlb_stuff_plus.py`
- **Storage**: `mlb_stuff_plus_players` — one doc per (player_id, year) with
  `stuff_plus`, `location_plus`, `pitching_plus`, weighted `whiff_pct`/`k_pct`,
  and per-pitch arsenal breakdown.
- **Refresh loop**: daily, wired into `server.py` alongside Statcast.
- **On-read enrichment**: `routes/picks_routes.py` attaches `stuff_plus` block
  to every MLB pitcher prop (K, Outs Recorded, ER, Hits Allowed).
- **Signal wiring**: `services/signal_engine/calculators.py` adds ±2 pts to
  pitcher K/IP Overs for elite/weak Stuff+, and ±1.5 pts to ER props for
  Location+ command signals.

### Coverage expansion — Mexican Liga MX
- **Canonical league code** `LigaMX` (added to `services/soccer/models.py`).
- **Sources**: football-data.co.uk extra-leagues `MEX.csv` (historical + odds)
  and TheSportsDB league id `4350` (metadata + fixtures).
- Both league name aliases ("Mexico Liga MX", "Mexican Liga MX", "Mexican
  Primera División") resolve to `LigaMX`.

### Coverage expansion — ITF/Challenger tennis
- **Source**: `stats.tennismylife.org/data/` — the maintainer's extended mirror
  that hosts ATP Challenger main draws + ATP Tour qualifying (NOT in the
  GitHub TML-Database repo).
- **New source module**: `/app/backend/services/tennis/sources/tml_stats.py`
  - `fetch_challenger_year(year)` — challenger main draw
  - `fetch_atp_quali_year(year)` — ATP Tour qualifying rounds
- **Wiring**: `services/tennis/fallback.py`'s `refresh_tennis_history` now
  ingests all three sources (main tour + challenger + qualifying), roughly
  doubling match volume and unlocking rolling stats for the ~300 players
  who cycle between tour and challenger levels.
- Circuit disambiguation via new `circuit` field on each match doc:
  `"challenger" | "atp_quali" | "atp"` (implicit).
- **Note**: WTA challenger + ITF Futures are NOT publicly mirrored anywhere
  we could find — WTA Sackmann repo was also deleted and no alternative
  fork ships lower-tier WTA data. Deferred.

### Test coverage
- `tests/test_mlb_stuff_plus.py` — 14 tests (scale math, aggregation weighting,
  pitcher-market extraction).
- `tests/test_coverage_expansion_iter75.py` — 8 tests (Liga MX canonical
  codes across 3 sources, tml_stats URL format + parsing).
- Existing test suite still green (85+ tests).

---

## 2026-07-15 Iter 76 — Phase 4 + Phase 5 Advanced Analytics

### Phase 4 — NFL nflfastR / nflverse (pre-season prep)
- **Module**: `/app/backend/services/nfl_nflfastr.py`
- **Source**: nflverse GitHub Releases (public CDN, no auth)
  - `snap_counts/snap_counts_YYYY.parquet` — per-game offensive/ST snap %
  - `player_stats/player_stats_season.parquet` — season-aggregated stats
- **Signals captured**: snap %, target share, air yards share, WOPR (weighted
  opportunity), aDOT (avg depth of target), YPRR (yards per route run, estimated),
  receiving_epa, rushing_epa, carries, passing yards/TDs.
- **Live verification**: 2024 season fetched successfully — 659 snap-count docs +
  570 season-stat docs.
- **Wiring**: daily refresh (weekly cadence, ~2 MB per parquet), on-read
  enrichment via `enrich_picks_with_nfl_usage_bulk`, signal engine nudges in
  `services/signal_engine/calculators.py::volume_signal` (±1.5 pts on
  target-share, ±1.5 pts on snap %, +1.2 pts on high WOPR, +0.8 pts on aDOT).

### Phase 5a — Kelly staking calculator
- **Function**: `analytics.kelly_stake(prob, odds, bankroll, fraction=0.25, max_stake_pct=0.05)`
- **Endpoints**: `GET /api/analytics/kelly` + `GET /api/analytics/kelly/for-pick`
- **Default**: ¼-Kelly with 5% max-stake safety cap. Supports probability as
  0..1 or 0..100. `for-pick` uses `no_vig_pct` when available.

### Phase 5b — CLV tracking UI
- **New Lab tab**: Analytics (with CLV / Kelly / Steam sub-sections).
- **Data source**: existing `/api/analytics/clv` endpoint.
- **Rendering**: `frontend/app/(tabs)/lab.tsx::CLVSection` — 4-cell headline
  (N / Win% / ROI-per-100u / Beat-Close %) + by-odds-band breakdown.

### Phase 5c — Steam detection
- **Module**: `/app/backend/steam_detector.py`
- **Data source**: existing `pick_line_history` collection (populated by
  `closing_line_snapshotter.line_observer_loop` every ~5 min for pending picks).
- **Detection**: implied-probability delta ≥3.0pp (~5¢) inside 5-minute
  rolling window → tag pick with `steam` block (direction, magnitude_pp,
  american_delta, observations).
- **Endpoint**: `GET /api/analytics/steam?hours=6&direction=toward&limit=50`
- **Background loop**: sweeps every 60s.

### Test coverage
- `tests/test_iter76_phase4_5.py` — 16 unit tests (Kelly math, steam math,
  NFL classifiers).
- `tests/test_iter76_live_integration.py` — 12 live-DB integration tests
  covering nflverse fetch, endpoint contracts, signal-engine nudges,
  enrichment behavior. All 28 tests green.

### Dependencies
- `pyarrow==25.0.0` added to `requirements.txt` for parquet ingest.

---

## 2026-07-15 Iter 77 — Tennis Calibration (Phase 3c)

### The "everyone scores 92" problem
Before: every tennis pick showed lock_score 91-92 regardless of quality. Root
cause was two-fold:
1. `tennis_engine.compute_components` computed surface_fit / serve_return by
   scaling the book's implied probability — so a 75% favorite always got
   surface=90 whether they were Alcaraz or an ITF Futures player.
2. `tennis_engine.apply_tennis_engine` mapped composite confidence (60-100)
   into a lock range of just 85-95 (10pt span), then took `max(original, v2)`
   — so a market-based 92 pick always kept 92 regardless of evidence.

### Fixes landed
- **NEW**: `/app/backend/services/tennis_calibration.py`
  - Computes surface-specific league averages (Hard/Clay/Grass) from
    `tennis_player_stats` (populated by Sackmann ingester).
  - `get_calibrated_serve_return(db, player, surface)` and
    `get_calibrated_surface_fit(db, player, surface)` return z-score-normalized
    0-100 grades.
  - Fuzzy-name matcher: "Rublev A." → "Andrey Rublev", "Alcaraz C." → "Carlos
    Alcaraz", etc.
  - Small-sample regression: <20 matches → blend toward league mean (50).
- **UPDATED**: `tennis_engine.compute_components` accepts optional
  `calibrated_surface_fit` / `calibrated_serve_return` overrides. Blends 70%
  calibrated / 30% heuristic. Unknown-player fallback: 40% heuristic / 60% =40
  penalty (so ITF players NOT in Sackmann can't game the score).
- **UPDATED**: `apply_tennis_engine` is now `async` (needs db handle).
- **UPDATED**: `apply_tennis_engine`'s lock-score mapping widened from 85-95
  → 75-96 (21pt span); blends 65% v2 / 35% original so calibrated evidence
  actually differentiates picks.
- **NEW**: daily `refresh_league_averages` background loop in `server.py`.

### Verified live results
Lock spread went from 1.3pt → 14.9pt across 19 distinct values. Top picks
now score 92 with real evidence (Zaar/Zimmerman surface=99.6, serve=94.5).
Bottom doubles picks correctly demoted to 77.3 "Pass" (Frantzen/Haase
surface=64, serve=64). Super-elite picks with edge + Elo + surface + market
alignment can still cross into 99 territory via the `is_99_lock_eligible` gate.

### Test coverage
- `tests/test_tennis_calibration.py` — 9 unit tests (z-score math + fallback).
- All 85 prior tests still green.
