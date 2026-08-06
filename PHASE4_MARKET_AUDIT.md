# PHASE 4A — MARKET AUDIT

**Status:** Read-only audit. **No code changes.**
**Ground truth:** `sports_engine.py` — `PROP_MARKETS` (lines 2170-2243), `_ALT_PROP_MARKETS` (2247-2256), `_PROP_FAMILY_MAP` (2282-2309), `_props_picks_from_event` (3427-4230), `_build_mlb_alt_picks` (2899-3172), `_build_tennis_alt_picks` (2707-2855).

---

## 1. MLB markets

### Fetched from The Odds API
```
Bulk /odds:      h2h, spreads, totals
Per-event props: batter_hits, batter_hits_alternate,
                 batter_hits_runs_rbis, batter_hits_runs_rbis_alternate,
                 batter_home_runs, batter_home_runs_alternate,
                 batter_rbis, batter_rbis_alternate,
                 batter_total_bases, batter_total_bases_alternate,
                 pitcher_strikeouts, pitcher_strikeouts_alternate,
                 pitcher_outs
Per-event team:  team_totals, alternate_team_totals,
                 alternate_spreads (alt run-lines)
```

### Per-market audit

| Market | Live? | Real Line? | Feature Engine | Implied Gate | Edge Gate | Notes / Defects |
|---|---|---|---|---|---|---|
| Moneyline | ✅ | ✅ from bulk `h2h` | `build_mlb_ml_factors` (≥4 factors) | — | +≥0% | Emitted via `_picks_from_game`. |
| Run line (main) | ✅ | ✅ from bulk `spreads` | ML factors reused | — | — | Emitted via `_picks_from_game`. |
| Alt run-line +1.5-3.5 | ✅ | ✅ from per-event `alternate_spreads` | `build_mlb_ml_factors` (fires only for ≥3 real factors — line 3087) | — | **≥8% (`_MLB_ALT_MIN_EDGE_PCT=8.0`)** | Underdog side only. |
| Game total (main) | ✅ | ✅ from bulk `totals` | `build_mlb_total_factors` (≥4) | — | +≥0% | — |
| Team totals (main) | ❌ | *(disabled 2026-07-19)* | dormant | — | — | `tt_outs: list = []` — dead block, code path never entered (see `sports_engine.py:2917-2924`). |
| Team totals (alt 2.5-3.5) | ❌ | *(disabled 2026-07-19)* | dormant | — | — | Same — `att_outs: list = []`. |
| Hits (Over 0.5, main) | ✅ | ✅ | `build_mlb_hitter_factors` (≥3) | ≥0.55 | — | User confirmed 2026-07-28 double-gate bug fix — main line clears now. |
| Hits (alternate) | ✅ | ✅ from Odds API | `build_mlb_hitter_factors` (≥3) | ≥0.80 (`_ALT_PROP_MIN_IMPLIED`) & ≤0.95 | — | Chalk alts only. |
| **Hits + Runs + RBIs** | ✅ | ✅ | `build_mlb_hitter_factors` (≥3) | ≥0.50 | — | **See §H+R+RBI DEEP DIVE below.** |
| Home Runs (main + alt) | ✅ | ✅ | `build_mlb_hitter_factors` (≥3) | ≥0.62 std / ≥0.80 alt | — | Explicit market keys enumerated. |
| RBIs (main + alt) | ✅ | ✅ | `build_mlb_hitter_factors` (≥3) | ≥0.62 std / ≥0.80 alt | — | — |
| Total Bases (main + alt) | ⚠️ | *(fetched but 0.5-line dropped)* | `build_mlb_hitter_factors` | ≥0.62 std / ≥0.80 alt | — | See line 3507: TB at 0.5 line **explicitly dropped** as duplicate of Hits 0.5. TB 1.5+ is allowed. |
| Pitcher Strikeouts (main) | ✅ | ✅ | `build_mlb_pitcher_k_factors` (≥3) + `evaluate_k_pick` gate (Poisson) | ≥0.48 | Poisson gate | K math gate drops if `model < book + 5pp` or price ≤ −220. |
| Pitcher Strikeouts (alt) | ✅ | ✅ | Same | 0.48 ≤ x ≤ 0.715 | Same | Deep chalk (implied ≥ 71.5%) rejected. |
| Pitcher Outs (main) | ✅ | ✅ | `build_mlb_pitcher_k_factors` (≥3) | ≥0.55 | — | No alt variant. |
| **Runs (batter_runs_scored)** | ❌ | — | — | — | — | **NOT in `PROP_MARKETS["MLB"]`.** The market key `batter_runs_scored` appears only in `_PROP_FAMILY_MAP` (dedupe) but is never fetched. User asked for Runs coverage → **DEFECT M-1**. |
| Pitcher walks / hits allowed / earned runs | ❌ | — | — | — | — | Not fetched. Family map entries exist for dedupe. |
| NRFI/YRFI | ⚠️ | — | `brain/nrfi_engine.py` (639 LOC) | — | — | Separate engine — not visible in `PROP_MARKETS`. Independent publication path (needs verification whether wired to live picks board). |

### H+R+RBI DEEP DIVE

**User claim to validate:** "main line and alternate lines are both discovered; real Over 0.5 is supported when supplied by a sportsbook; Over 0.5 is never fabricated from 1.5; 0.5, 1.5, and 2.5 are evaluated independently".

**Fetch path:** `sports_engine.PROP_MARKETS["MLB"]` lines 2177-2178:
```python
"batter_hits_runs_rbis",
"batter_hits_runs_rbis_alternate",
```
Both are requested from The Odds API per event. **The API decides whether 0.5 / 1.5 / 2.5 lines exist** — the app does not fabricate any.

**Line preservation:** In `_props_picks_from_event`:
- Each bookmaker's outcome is bucketed by `(mk, player, point, side)` at line 3510.
- `point_key = point` (line 3509) is the **exact float line** from the bookmaker — never rounded, never converted.
- `median = sorted(prices)[len(prices)//2]` — median across books for the identical (line, side) — this is *cross-book consensus for the same contract*, not a synthesis.

**Independent evaluation by line:**
- `bucket` key is `(mk, player, point, side)` — 0.5, 1.5, 2.5 are DIFFERENT bucket keys. Each is scored independently through the implied gate + feature engine.
- `std_seen` dedupe key is `(player, _prop_family_key(mk))` — collapses `batter_hits_runs_rbis` and `batter_hits_runs_rbis_alternate` into ONE family. This means: **the top-scoring line for a player wins; the other lines are dropped by the family dedup, not by any per-line filter.**

**No synthetic conversion:** `_synthesize_chalk_alt_totals` is only used for tennis (and is dead code since 2026-06-30). No synthesis logic touches MLB H+R+RBI.

**Player/event matching:** `raw_player = o.get("description") or o.get("name")` → `_clean_player_name` (line 3447). MLB adds team disambiguation via `_player_team_for_event` using birth-year hints from `(YYYY)` suffixes in Odds API descriptions.

**Provider gap vs downstream filtering:**
- If The Odds API doesn't return the market → the market is empty → no candidates emitted for that game.
- If The Odds API returns 0.5 & 1.5 but only 1.5 has ≥3 real factor coverage → 1.5 emits, 0.5 is dropped by `has_enough_real_data`. This is silent — no `logger.info` distinguishes provider gap from downstream drop.

**One failed line does not block another qualified line?**
- **After the pair-dedup and family dedup, YES — one line failing does not block another that scored higher.** But if a low-scoring 0.5 line is present, the family dedup at line 3831 (`std_key = (player, _prop_family_key(mk))`) drops it silently in favour of the winner. This is by design (one pick per player per family).
- The bookmaker-level outcome loop preserves ALL lines up to that dedup step (line 3510). So the provider's coverage is fully surfaced through the feature engine; only the winner survives to the emitted board.

**Rejection count logging:** MLB alt-line rejection counts are logged only in aggregate:
- `logger.info("MLB alt-line dedupe: %d → %d picks (removed contradictory sides)", …)` — sports_engine.py:3167-3170.
- `logger.info("PAIR_DEDUP: dropped %d symmetric-pair candidates (%d kept)…", …)` — line 3624.
- **No separate counter distinguishes "provider returned no line" vs "line dropped by implied gate" vs "line dropped by feature-engine coverage" vs "line lost the family dedup".** → **DEFECT M-2**.

### Lineup / weather / handedness

- `mlb_lineup.py` — 182 LOC. Refreshes starting-lineup + starting-pitcher status via MLB Stats API.
- `mlb_live.py` — 205 LOC. Live game feed.
- `services/mlb_umpire.py` — umpire K-inflation factor.
- `services/mlb_matchup_resolver.py` — resolves opposing pitcher / opposing team K% / handedness for each hitter.
- Park & weather feed: `game_context.py` (services) — provides `game_ctx` object read by `_props_picks_from_event` via `_game_ctx = payload.get("_ctx") or {}`.

**Coverage gate visibility:** The `_skip_pick` bool triggers if `has_enough_real_data` returns False, but **no log line captures which factor(s) were missing** — the debug value would be low.

---

## 2. NFL markets

### Fetched
```
Bulk /odds:      h2h, spreads, totals
Per-event props: player_pass_yds, player_pass_yds_alternate,
                 player_pass_tds, player_pass_attempts,
                 player_pass_completions,
                 player_rush_yds, player_rush_yds_alternate,
                 player_rush_attempts, player_rush_tds,
                 player_receptions, player_receptions_alternate,
                 player_reception_yds, player_reception_yds_alternate,
                 player_reception_tds,
                 player_anytime_td, player_1st_td
```

### Per-market audit

| Market | Live? | Real Line? | Real Feature Engine? | Notes |
|---|---|---|---|---|
| Moneyline / spread / total | ✅ | ✅ | `_picks_from_game` — uses `sports_engine._factors_random`-like path (needs cross-check; `nfl_game_engine.py` is real but NOT wired). | Live pipeline emits with Lock-Score built from generic MLB-mirrored factors — no NFL-specific feature engine on the game side. |
| Passing yards (main + alt) | ✅ | ✅ | `services.nfl_feature_engine.build_nfl_prop_factors` (via `ctx["nfl_precomputed"]`) | Uses NFLverse historical. |
| Passing TDs | ✅ | ✅ | Same | — |
| Rushing yards / TDs / attempts | ✅ | ✅ | Same | — |
| Receiving yards / receptions / TDs | ✅ | ✅ | Same | — |
| Anytime TD | ✅ | ✅ | Same (but sport-native model `nfl_atd_engine.py` **NOT wired**) | Live emission uses the generic prop factor engine, not the dedicated λ = team_td_rate × opp_share × … model. **Underutilised** — see Model Audit §2.2. |
| 1st TD | ✅ | ✅ | Same | — |

**Injury / snap-share / role handling:** `services/nfl_matchup_intelligence.py`, `services/nfl_opp_defense.py`, `services/nfl_nflfastr.py` provide inputs to the precompute. Snap-share is captured; **starter status is not gated with a hard reject** the way MLB gates starting-pitcher confirmation.

**Weather handling:** No dedicated NFL weather gate in `sports_engine.py`. Weather may be pulled by the feature engine but no visible cap on outdoor cold-weather passing markets.

**Underdog / positive-odds behaviour:** No underdog suppression — same implied gate applies to both sides. `_HIGH_PROB_MIN_IMPLIED = 0.62` gate applies to non-K MLB props but does NOT apply to NFL alt-lines because they are in `_ALT_PROP_MARKETS` (0.80 floor) OR match the `_mk_gated` set. NFL non-alt props like `player_pass_yds` fall through to the 0.62 gate.

---

## 3. CFB markets

**Live status:** CFB uses the NFL market list via the shared `_props_picks_from_event`. But the CFB feature engine is NOT called on the sync emission path — see `sports_engine.py:3992-4014`. All CFB props emit with `factors = {"Book Implied Probability": mp}`.

**Consequences:**
- Every CFB pick's `lock_score` is a monotonic function of book price.
- No returning-production, transfer portal, SP+, career-vs-opp, or L5 signals are surfaced.
- The 3-factor min-data gate does not apply (single-factor emission).
- Publication is technically "real line" (odds are pulled from Odds API), but the model is **book-follow only**.

**Injury / depth-chart:** No CFB-specific injury feed wired. General `player_intel` cache may fill some gaps but CFB coverage there is low.

---

## 4. NBA markets

### Fetched
```
Bulk /odds:      h2h, spreads, totals
Per-event props: player_points, player_points_alternate,
                 player_rebounds, player_rebounds_alternate,
                 player_assists, player_assists_alternate
```

### Per-market audit

| Market | Live? | Real Line? | Real Feature Engine? | Notes |
|---|---|---|---|---|
| Moneyline / spread / total | ✅ | ✅ | ❌ | Game markets emit with generic factors. |
| Player Points (main + alt) | ✅ | ✅ | ❌ | Book-follow. `factors = {"Book Implied Probability": mp}`. |
| Player Rebounds (main + alt) | ✅ | ✅ | ❌ | Same. |
| Player Assists (main + alt) | ✅ | ✅ | ❌ | Same. |
| PRA (Points+Rebounds+Assists) | ❌ | — | — | **NOT in `PROP_MARKETS["NBA"]`**. Missing. |
| 3-pointers | ❌ | — | — | **NOT in `PROP_MARKETS["NBA"]`**. Missing. |

**Minutes / usage / pace / matchup / rest:** `services/nba_ingest.py` and `services/nba_gamelog_ingest.py` **exist** but there is no `nba_precomputed` in `_ctx` and no `build_nba_prop_factors` call anywhere in `_props_picks_from_event`. **Data is ingested but never consumed by emission.**

**Blowout / rest / DNP handling:** No hard gate. If a star sits, the pick still emits at book-follow probability.

### DEFECTS

- **DEFECT NBA-1:** No NBA prop feature engine wired to emission — every NBA prop is book-follow.
- **DEFECT NBA-2:** PRA and 3-pointer markets are ABSENT from `PROP_MARKETS["NBA"]`. The Odds API supports them (`player_threes`, `player_points_rebounds_assists`).
- **DEFECT NBA-3:** No usage / pace / rest / minutes / injury gate.

---

## 5. NHL markets

**NHL is NOT in `SPORT_KEYS`.** No emission path exists.

- `historical/nhl.py` — historical data ingest only.
- No NHL entry in `SPORT_KEYS`, `PROP_MARKETS`, `_props_picks_from_event`, or `_picks_from_game`.
- No `sport_adapters/nhl.py`.
- No goalie confirmation / line combinations / ice time / shot models in the emission path.

**Verdict:** NHL requires **new implementation from scratch** if it is to be added. Not a Phase 4A defect — it is a scope gap.

---

## 6. Soccer markets

### Fetched
```
Bulk /odds:      h2h (1X2), spreads (Asian handicap), totals
Per-event props: player_goal_scorer_anytime,
                 player_to_score_or_assist,
                 player_first_goal_scorer
```

### Per-market audit

| Market | Live? | Real Line? | Real Feature Engine? | Notes |
|---|---|---|---|---|
| 1X2 (H/D/A) | ✅ | ✅ | ❌ | `_picks_from_game` — book-follow with generic Lock-Score factors. |
| Draw (X) | ✅ | ✅ | ❌ | Same — no dedicated draw model. |
| Spread / handicap | ✅ | ✅ | ❌ | Book-follow. |
| Total goals | ✅ | ✅ | ❌ | Book-follow. |
| BTTS | ❌ | — | — | **NOT in `PROP_MARKETS["Soccer"]`.** Missing. |
| Corners | ❌ | — | — | Not fetched. |
| Cards | ❌ | — | — | Not fetched. |
| Anytime scorer | ✅ | ✅ | ✅ `goal_scorer_engine_v2` | Elite-player floor 88/95 (see Model Audit §2.5). |
| To Score or Assist | ✅ | ✅ | ✅ | Threshold aligned with Anytime (2026-06-24 fix). |
| First Goal Scorer | ✅ | ✅ | ✅ | Higher variance. |
| Player shots | ❌ | — | — | Not fetched — Odds API `player_shots_on_target` not in list. |

**Starting XI / substitution:** `soccer_hot_scorers.py`, `soccer_player_form.py`, `services/mls_scorer_gate.py` capture some starter / reserve info. MLS scorer gate hard-blocks reserves.

**League coverage:** 30+ competitions in `SPORT_KEYS["Soccer"]` including World Cup, UCL, EPL, La Liga, Bundesliga, Copa Libertadores, MLS, CSL, Argentine LPF, Copa America, and 5+ national FAs. This is broad.

**No-start handling:** MLS scorer gate rejects reserves, but no equivalent for other leagues. If Odds API prices a scorer market for a bench player, the pick may still emit under `goal_scorer_engine_v2` unless the elite/starter gate fires.

**Provider mix:** ESPN CSL live (`csl_espn_live.py`), ESPN MLS (`services/espn_mls_stats.py` + `services/espn_mls_injuries.py`), FotMob (`soccer_fotmob_settle.py`), and multiple soccer feeds (`soccer/` package). This is well-diversified.

### DEFECTS

- **DEFECT SOC-1:** BTTS, corners, cards, and player-shot markets are not fetched.
- **DEFECT SOC-2:** Non-MLS starter-status gate is absent — reserves in other leagues can slip through if book prices them (rare, but possible).
- **DEFECT SOC-3:** `_synthetic_soccer_scorer_picks` (line 5028) creates picks from ESPN team+player data when the Odds API doesn't list a scorer. Needs cross-check to confirm every emitted pick is bookmaker-priced (not synthesized).

---

## 7. Tennis markets

### Fetched
```
Bulk /odds:      h2h, spreads (game handicaps), totals (total games)
Per-event alts:  alternate_spreads, alternate_totals
```

### Per-market audit

| Market | Live? | Real Line? | Real Feature Engine? | Notes |
|---|---|---|---|---|
| Moneyline (h2h) | ✅ | ✅ | ⚠️ book-follow + `_player_hash` variance | See DEFECT T-1. |
| Game spread (main + alt) | ✅ | ✅ | ⚠️ same | Alt spreads use `_pick_sweet_spot_alts` for chalk ladder. |
| Total games (main + alt) | ✅ | ✅ | ⚠️ same | **`_synthesize_chalk_alt_totals` is DEAD CODE — not called since 2026-06-30.** Real book outcomes only. |
| Set markets (set spread, correct score, first set) | ❌ | — | — | Not fetched. |
| Best of 3 vs Best of 5 | Implicit | — | — | `sim_tennis` handles both (SETS_BO3 default). |

**Surface / Elo / form:** Elo backfill via `espn_settlement.backfill_tennis_elo`. `services/tennis_elite_players.py` curated list. `services/tennis_math_engine.py` (208 LOC) is a real serve/return-based math model — but wired only to the sim, not to the composite emission path.

**Fatigue / injury / walkover:** `tennis_extra/settle.py` handles retirements & walkovers on the SETTLEMENT side. Pre-match, `tennis_engine` does not know about pre-game injuries or fatigue signals. Retirement is settled per ESPN status (`_tennis_pick_outcome` in `espn_settlement.py:118`).

### DEFECT T-1

`tennis_engine._player_hash(name)` is an MD5-of-name deterministic pseudo-random that is used as a **player identity baseline** in every component score (surface / form / serve / motivation / matchup). The comment on line 199 explicitly says *"Real stats will replace this when wired in"* — this has not been done. Effect: two players with **book-implied 60%** produce different lock scores solely because of their name hash. This is **manufactured variance masquerading as model signal**.

---

## 8. UFC markets

- `mma_method_of_victory` — only market. Book-follow with 18% implied floor.
- No moneyline / totals ingested via the props path (they arrive via `h2h`/`totals` bulk fetch).
- No fighter rating, reach, stance, camp, cutting-weight signal.

---

## 9. Real-line policy enforcement

**Requirements** (from user spec): no synthetic alt lines, no estimated prices shown as real, exact bookmaker + line + odds + timestamp, market contract identity.

**Findings:**

| Layer | Real-line compliant? | Evidence |
|---|---|---|
| MLB props | ✅ | `_props_picks_from_event` iterates real bookmaker outcomes; `median` is real cross-book consensus. |
| MLB alt run-line / team totals | ✅ | Fetched from per-event `alternate_spreads`; team totals disabled. |
| Tennis alt totals | ✅ (now) | Synthesizer disabled 2026-06-30 — only real API outcomes used. |
| Tennis alt spreads | ✅ | Only `_alt_outcomes_for_market(alt_payload, "alternate_spreads")` — real outcomes. |
| NFL props | ✅ | Per-event fetch. |
| NBA/CFB/Soccer non-scorer/UFC | ✅ (lines are real) | Model is book-follow, but the *odds shown to the user are the real median across books*. Real line, weak model. |
| **`_synthesize_chalk_alt_totals`** | ⚠️ *(dead code)* | Function still exists (`sports_engine.py:2600-2704`) but is never called. Could be re-invoked accidentally — recommend deletion in Phase 4B. |
| **Alt-Line Engine `model_projection` source** | ⚠️ | `services/alt_line_engine/ranker.py` emits `AltLine(source="model_projection")` for lines the book does not offer. **Contained to admin-only routes** — no live picks board consumes it. |
| **Odds timestamp** | ⚠️ | The pick record includes `event_time` and `created_at`, but NOT an odds-snapshot timestamp per book. The `median` odds are re-computed on every refresh, and prior odds are only preserved through `prediction_snapshots` (Phase 3F CLV pipeline). This is acceptable for snapshot-based CLV but not for per-refresh audit trail — call out in defects. |
| **Bookmaker identity per pick** | ⚠️ | The `median` price hides which book(s) offered which price. The alt-line snapshotter (`alt_lines_feed.py`, `propline_feed.py`) captures per-book detail, but the emitted pick's `book_odds` is anonymised. |

**Composite verdict:** **Real-line policy is honoured for prices**, but two soft weaknesses:
1. Bookmaker identity is anonymised in the pick (median across books).
2. Odds-snapshot timestamps per book are only preserved in `prediction_snapshots` — not on the pick doc itself.

---

## 10. Market-selection ranking

Ranking within `_props_picks_from_event`:
1. **Pair dedup** (line 3540-3629) — collapses Over/Under symmetric pairs to one deterministic winner using model-derived edges (for K props) or book consensus (all others).
2. **Family dedup** — `std_seen = {(player, _prop_family_key(mk))}` keeps ONE main-line per family.
3. **Alt caps** — max 3 alts per (player, side) per family.
4. **Sort** — `_dedup_sort_key` — is_alt=False first, then implied DESC, then mk ASC, then point ASC, then side ASC. **Deterministic and reproducible** (2026-07-28 fix).

**Ranking is by implied probability primarily, not by EV/edge.** This is the user's stated concern (§MARKET SELECTION in prompt: "Do not rank only by raw hit probability."). The gate to enter the candidate list uses the implied threshold; downstream Lock-Score sort in later layers is by lock, which itself has an edge component through calibration. **But at the emission tier, implied is the sort key** — this is a **DEFECT M-3**.

**Chalk bias:** Because implied is the primary sort, chalky picks dominate. The `chalk_trap` service (`services/chalk_trap.py`) exists as a downstream filter but is a filter, not a ranker.

**Underdog suppression:** Not intentional. Underdog markets can emit if their implied clears the family gate. But **positive-odds side rarely clears** (e.g. Anytime Goal Scorer above 22%, MMA method above 18%). The **absence of underdog rewards is structural**, not an active suppression.

**Correlated market conflicts / same-event overexposure:**
- Pair dedup enforces per-line non-contradiction.
- MLB alt-line dedup enforces per-team, per-family non-contradiction.
- **Cross-market correlation is NOT enforced.** Two-hit + one-total-base picks for the same MLB game can both emit for the same player. `correlation_guard.py` exists but was not confirmed to be on the emission path from static reading.

---

## 11. Dedupe / contradiction audit summary

| Layer | Rule | Enforced? |
|---|---|---|
| Same player, same market family, same line (Over vs Under) | Pair dedup — deterministic winner | ✅ |
| Same player, same market family, different lines | `std_seen` keeps the top-scoring main line | ✅ |
| Same player, alt Over vs alt Under | Separate 3-cap per side — Both can co-exist | ✅ intentional |
| Same team, alt total Over vs alt total Under | `_family_key` dedup | ✅ |
| MLB alt run-line +N vs alt team-total ±N (implicit correlation) | — | ❌ |
| Same event, two picks on correlated markets (spread + moneyline) | — | ❌ (`correlation_guard.py` present but not confirmed on path) |
| Soccer Anytime + SoA for same player | `_PROP_FAMILY_MAP` collapses both to `goal_scorer` family | ✅ |
| Soccer Anytime + FGS for same player | Same family | ✅ |

---

## 12. Publication path

Every emitted pick lands in the `picks` collection via `sports_engine.generate_all_picks` → `services.pick_refresh_orchestrator`.

**Published snapshots** — `services.prediction_publication_service.py` and `services.published_prediction_reader.py` implement immutable snapshotting. `services.published_write_guard.py` prevents post-hoc rewrites. This is the guardrail against retroactive lock-score inflation.

**CLV** — `services.closing_line_snapshotter` (started at line 2024 in the startup log) captures closing-line-value data feeding `learning_engine.py`.

---

## 13. Defects raised (Markets layer)

| ID | Description | Impact | Likelihood |
|---|---|---|---|
| M-1 | MLB `batter_runs_scored` NOT fetched — Runs market missing | ⚠️ Medium | High (Odds API supports) |
| M-2 | Rejection counters do not distinguish provider-gap vs feature-gap vs family-dedup vs implied-gate | 🟡 Low | High (blind spot) |
| M-3 | Primary emission sort key is book-implied, not EV/edge | 🔴 High | Certain (structural) |
| NBA-1 | No NBA prop feature engine wired — book-follow only | 🔴 High | Certain |
| NBA-2 | PRA and 3-pointers absent from `PROP_MARKETS["NBA"]` | 🟡 Medium | Certain |
| NBA-3 | No NBA usage/pace/rest/minutes/injury gate | 🔴 High | Certain |
| C-1 | CFB feature engine exists but is NOT wired — book-follow only | 🔴 High | Certain |
| T-1 | Tennis composite uses `_player_hash` MD5-of-name as identity baseline | 🔴 High | Certain |
| T-2 | Set markets, correct-score, first-set not fetched | 🟡 Medium | Certain |
| SOC-1 | BTTS/corners/cards/player-shots not fetched | 🟡 Medium | Certain |
| SOC-2 | Non-MLS starter-status gate absent | 🟡 Medium | Medium |
| SOC-3 | `_synthetic_soccer_scorer_picks` needs verification whether every emitted pick is bookmaker-priced | 🟡 Medium | Low-Medium |
| RL-1 | Bookmaker identity anonymised (median across books) in emitted pick | 🟡 Medium | Certain |
| RL-2 | Odds-snapshot timestamp per book not preserved on the pick doc | 🟡 Medium | Certain |
| RL-3 | `_synthesize_chalk_alt_totals` still exists as dead code; recommend deletion | 🟡 Low | Low (unused) |
| CORR-1 | Cross-market correlation not enforced on the emission path | 🟡 Medium | Medium |

(All defects consolidated in `PHASE4_AUDIT_EXECUTIVE_SUMMARY.md`.)
