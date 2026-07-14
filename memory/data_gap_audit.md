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

**Week 1 (Phase 0):** Closing-line snapshot + no-vig + Pinnacle anchor. Zero new data sources — everything is already in The Odds API or in memory. Expected ROI lift: +2-4% across all sports.

**Week 2 (Phase 1 top 3):** Statcast xwOBA/barrel%, Fangraphs Stuff+, bullpen fatigue. Baseball is still in-season and highest pick volume.

**Week 3 (Phase 3 + Phase 2 top 3):** Sackmann tennis CSVs, ClubElo, FBref xG/PPDA. Tennis is high-volume on your board (132 picks today), soccer is highest overall (334).

**Week 4 (Phase 2 finish + Phase 0 dashboard):** Set-piece xG, referee tendencies, CLV dashboard.

**Off-season pause:** NFL/UFC ingestion in September and October respectively.

---

**File status:** written 2026-07-14. Update whenever a Phase completes so the roadmap reflects
reality.
