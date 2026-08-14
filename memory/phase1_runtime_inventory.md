# PERKLOCKS PHASE 1 — §1 PRODUCTION RUNTIME INVENTORY
Date: 2026-06 | Checkpoint: PHASE1A_RUNTIME_INVENTORY_READY
Scope: trace of the REAL production write path (scheduler → provider → engine → gates → canonical publication) for all 8 sports.

---

## 0. THE SINGLE PRODUCTION WRITE PATH (all sports)

```
server.py::_daily_refresh_loop (+_mlb_pregame_loop)
  → services/pick_refresh_orchestrator.py::_refresh_picks()
      → sports_engine.py::generate_all_picks(date, sport_filter)
          Phase 1: fetch_{mlb,nba,nfl,cfb,soccer,tennis,ufc}_picks
                     → _fetch_picks_for_sport() → _fetch_odds_for(sport_key)
                     → _picks_from_game()  [GENERIC game-market builder]
                     → per-sport alt-line augmentation (Tennis, MLB)
          Phase 2: _fetch_player_props_for_sport(MLB, NBA, NFL, Soccer)
                     → _props_picks_from_event()  [GENERIC prop builder]
          Phase 2.5/2.6: soccer xG + scorer career enrichment
          → dedupe / K-conflict / totals cap
      → post-passes in orchestrator (order):
          Tennis Extra fallback → MLB BvP → sportdb enrich → elite boost
          → brain/sim_runner → tennis_engine.apply_tennis_engine
          → lock_v2 shadow → evidence_engine.govern_pick
          → bandit/learning_v2 → goalscorer dedupe/caps
          → atomic delete+insert into db.picks
          → publication_reconciliation → PredictionPublicationService.publish_batch
             (stamps published_lock_score = canonical truth)
Read path: routes/picks_routes.py → services/main_board_eligibility.py (>=85 INCLUSIVE)
```

**Verdict: there is ONE generic runtime (`_picks_from_game` + `_props_picks_from_event`) shared by all sports, with per-sport authoritative engines only PARTIALLY plugged in.** Several sports never reach an authoritative model.

---

## 1. PER-SPORT INVENTORY & CLASSIFICATION

### MLB — mostly AUTHORITATIVE
| Path | Class | Files/functions |
|---|---|---|
| Game ML/Total/Spread via `build_mlb_ml_factors` + `has_enough_real_data` gate | AUTHORITATIVE | `sports_engine._picks_from_game` (L1299-1308, totals/spreads branches) + `services/mlb_feature_engine.py` |
| Team totals / alt run-lines | AUTHORITATIVE | `_fetch_mlb_event_alts` (L3491) + `_build_mlb_alt_picks` (L3507) |
| Hitter + pitcher props (batter_hits, HR, RBI, TB, H+R+RBI, Ks, outs) | AUTHORITATIVE (reachability UNPROVEN at runtime) | `PLAYER_PROP_MARKETS["MLB"]` (L2527) → `_fetch_player_props_for_sport` (L5571, event caps + priority) → `_props_picks_from_event` (mlb_feature_engine 0/5-factor skip ~L5717) |
| BvP / statcast / lineup closures | AUTHORITATIVE enrichment | orchestrator L461+, `mlb_bvp.py`, statcast loops |

**Risk:** props event cap (`_PROPS_PER_KEY_CAP`) + generation floors can silently drop entire hitter families. §7 requires runtime funnel proof `batter_hits* → engine → gate → publish/reject(reason)`.

### NFL — BROKEN vs. intent
| Path | Class | Files/functions |
|---|---|---|
| Game ML/Total/Spread (incl. `americanfootball_nfl_preseason`) → **`factors = {}` book-follow**; Platinum NEVER invoked | LEGACY/GENERIC BOTTLENECK | `_picks_from_game` L1319-1327 (ML), totals branch (~L1725), spreads (~L1925) |
| `platinum_nfl.game_markets.simulate_game_market` | **DEAD/UNREACHABLE** (exported, zero production call sites) | `services/platinum_nfl/game_markets.py`, `__init__.py` L69 |
| NFL player props → nflverse factors + Platinum sim attached as **Challenger only** (`pick["platinum_challenger"]`), does NOT drive lock/edge | AUTHORITATIVE-SHADOW | `_props_picks_from_event` L5292-5390, `services/platinum_nfl/{simulator,player_markets,season_type}.py`, `services/nfl_feature_engine.py` |
| `nfl_game_engine.py`, `nfl_safe_engine.py`, `nfl_atd_engine.py` | BYPASS (route-only, `/api/nfl/*`, never feeds board) | `routes/nfl_routes.py` |

### Tennis — 3 PARALLEL RUNTIMES (consolidation required)
| Path | Class | Files/functions |
|---|---|---|
| Primary: `tennis_ml_prob` + `tennis_math_engine.score_tennis_matchup` (both sides) + alt lines + `_backfill_tennis_moneylines` (reads `live_alt_lines`) | AUTHORITATIVE | `_picks_from_game` L1229-1281, `_fetch_tennis_event_alts` L3198, `_build_tennis_alt_picks` L3315, `_backfill_tennis_moneylines` L2288 |
| Post-pass gates: `apply_tennis_engine` (edge<3% NO_BET, conf<72 drop, chalk-anchor carve-outs, 99-lock demotion) | AUTHORITATIVE gate (duplicates generic gates) | `tennis_engine.py` L60-120, 577-645 |
| Fallback: TennisExplorer scrape + own Elo/`odds_engine.fair_win_probability` + `real_odds` lookup — **runs UNCONDITIONALLY every refresh**, own pick construction bypassing `_build_pick` | FALLBACK (second model runtime) | `tennis_extra/{picks,odds_engine,real_odds,scraper}.py`, orchestrator L397-440 |
| `services/tennis/fallback.py` (Sackmann stats/H2H source) | AUTHORITATIVE data source (fine) | consumed by game_context + tennis_extra |

### NBA — GENERIC BOTTLENECK
- Game ML/Total/Spread: `factors = {}` book-follow, no NBA engine in path → LEGACY/GENERIC (`_picks_from_game` L1319-1327).
- Props: `nba_feature_engine` wired (L5769) → AUTHORITATIVE.
- `nba_ingest`/`nba_gamelog_ingest` loops feed features (server L3868) → data OK, unused for game markets.

### CFB — GENERIC BOTTLENECK + DEAD PROP CODE
- Game markets: same book-follow `factors = {}` → LEGACY/GENERIC. `cfb_feature_engine`/`cfb_precompute` exist, consumed only in prop candidate path (L4961).
- Props: `_extract_cfb_prop_candidates` (L2904) is **DEAD** — CFB not in Phase-2 prop loop (L6400) and registry says props OFF.

### Soccer — AUTHORITATIVE PRIMARY + SECOND WRITE PIPELINE + DIRECT-EMIT PATHS
| Path | Class | Files/functions |
|---|---|---|
| Primary ML/DC/BTTS/totals/spreads via `soccer_feature_engine` + real-DC-line guard | AUTHORITATIVE | `_picks_from_game` L1215-1500s |
| Scorer props + career/GK/xG enrichment | AUTHORITATIVE | Phase 2.5/2.6 in `generate_all_picks`, `sportdb_player_scorer` |
| **`soccer/pipeline.py::soccer_pipeline_loop`** — separate predictor writing `db.picks` via `to_picks_collection_doc` + `publish_upserted_picks` | LEGACY (second runtime, duplicate-side risk acknowledged in `picks_routes.py` L1773) | `soccer/{pipeline,predictor,real_odds}.py`, server L3442 |
| ESPN CSL / MLS scorer direct-emit + `_synthetic_soccer_scorer_picks` (sportdb synth, `is_synthetic_scorer`) | FALLBACK (synth picks = model-priced, no real book line → must be classified/labeled or blocked per no-fake-odds rule) | `sports_engine.py` L5877-5937, L6123, L6319; `mls_direct_inject.py`, `csl_espn_live.py` |

### UFC/MMA — CAPABILITY CONTRADICTION
- Runtime: generic book-follow ML only; `_ufc_ml_only` suppresses totals at builder entry (L1146-1152) — this was a USER instruction ("only ufc money lines from now"), but registry still advertises `game_markets: ["h2h","totals"]`.
- No model: `factors = {}` → LEGACY/GENERIC.
- `ufc_espn_ingest.ufc_espn_loop` fallback (server L3470) → FALLBACK.

### NHL — DEAD/UNREACHABLE (worst contradiction)
- `sport_capability_registry.py` says `enabled: True, game_markets: h2h/spreads/totals, supports_locks: True`.
- **`SPORT_KEYS` has NO NHL entry; `generate_all_picks` has no `fetch_nhl_picks`; no NHL fetch job exists anywhere.** NHL can never produce a pick. Settlement code references NHL (server L314) for a sport that never publishes.

---

## 2. GATE INVENTORY (duplicated / arbitrary — §10-§13 targets)

Generation-time, inside `_build_pick` (sports_engine L812-1064) — ALL applied BEFORE any board gate:
1. Chalk odds caps: -450 std / -750 alt / -400 long-shot (`return None`).
2. `SPORT_LOCK_FLOOR` per-sport (MLB 88, NBA/NFL/CFB 80, Soccer 75, Tennis/UFC 72) — duplicates the single >=85 board rule with DIFFERENT numbers.
3. Edge floors: -1% std, -8% Tennis/UFC ML, -50% chalk, -10% long-shot.
4. **Universal raw model-probability floors: 0.58 std / 0.62 MLB / 0.55 juice+K / 0.55 alt / 0.25 long-shot → §12 REMOVE.**
5. **`SPORT_IMPLIED_FLOOR` sportsbook implied-probability floors (0.56 MLB … 0.48 UFC) + 0.42 juice sanity → §11 REMOVE.**
6. Generation-time lock-floor BOOSTER (L1049-1064): wp>=65 & edge>=1 ⇒ lock artificially raised to 85-97. Mirrors `compute_lock_score` quality floor (L751-785). **Score inflation — floors the score UP to eligibility.**

Pre-model side/market suppressions (§10 targets):
- ML: only the model-preferred side emitted; in book-follow sports (NBA/NFL/CFB/UFC) `home_model = home_implied` ⇒ favorite ALWAYS, underdog side never evaluated by any model.
- Totals: "best side per game" heuristic + `MAX_DAILY_TOTALS = 6` slate cap (L2202).
- UFC: `_ufc_ml_only` market suppression (user-directed — needs explicit product decision).
- Tennis alt: sweet-spot cap 2/match; props: `_PROPS_PER_KEY_CAP` event caps.

Post-generation gates (orchestrator): brain filter, `evidence_engine.govern_pick`, lock_v2 shadow, bandit/learning shave, tennis_engine NO_BET, goalscorer caps, `pick_validator`, publication §6 board-quality floors → then read-side `main_board_eligibility` (>=85, canonical `published_lock_score`).

## 3. EDGE SEMANTICS (§14-§16 baseline)
- `edge = model_win_prob − raw single-side implied` (`_build_pick` L832-833) — **no de-vig**; 3-way soccer normalizes, 2-way markets don't.
- `services/devig.py` EXISTS but only used in read-side enrichment (`pick_enrichment`, `picks_routes`, closing-line snapshotter) — never in generation/ranking.

## 4. TELEMETRY BASELINE (§17)
- `services/pipeline_diagnostic.py`: ReasonCode enum + `log_reason` — **in-memory ring buffer (512), lost on restart, only sparsely called** (soccer DC/BTTS, NFL platinum wiring, MLB prop skip). No persistent per-candidate funnel, no `SCORE_BELOW_85`/`MODEL_UNAVAILABLE` universal accounting.

## 5. DUPLICATE / DEAD ABSTRACTIONS
- `sport_adapters/` (root, used by evidence_engine) vs `services/sport_adapters/` (used only by scripts) — duplicate adapter trees.
- `fetch_wnba_picks`, `fetch_kbo_picks` — disabled sports, dead functions.
- `_synthesize_chalk_alt_totals` — already stubbed (kept as guard).
- `_extract_cfb_prop_candidates` — dead (CFB not in prop loop).
- `platinum_nfl.game_markets.simulate_game_market` — dead export.

---

## 6. PROPOSED RETIREMENT / GUARD / REROUTE LIST (needs approval)

REROUTE (wire authoritative engines):
- R1 NFL: invoke `simulate_game_market` for ML/Spread/Total (regular + preseason) inside `_picks_from_game` NFL branch; promote Platinum from challenger-shadow per §3 (evidence-gated).
- R2 NHL: either (a) add `SPORT_KEYS["NHL"] = ["icehockey_nhl"]` + `fetch_nhl_picks` + honest book-follow with `MODEL_UNAVAILABLE` telemetry, or (b) set registry `enabled: False`. Registry must match runtime either way.
- R3 NBA/CFB: keep generic transport but label `MODEL_UNAVAILABLE` in telemetry (no fake engines); wire `cfb_precompute` factors into CFB game markets if data coverage supports it.
- R4 Tennis: single authoritative order — primary Odds-API path is authoritative; `tennis_extra` demoted to gap-filler ONLY for events absent from primary (currently unconditional); consolidate its separate probability model to `tennis_math_engine` scoring or explicit fallback labeling.

RETIRE:
- T1 `soccer/pipeline.py` predictor write path into `db.picks` (keep its data caches used by settlement; stop the duplicate pick emission) — merges with Remediation 3 publication resilience.
- T2 Dead code: `fetch_wnba_picks`, `fetch_kbo_picks`, `_extract_cfb_prop_candidates`, root-vs-services `sport_adapters` dedupe (pick one).
- T3 UFC contradiction: registry `game_markets` → `["h2h"]` (keep ML-only per prior user instruction) OR re-enable totals through runtime — user decision.

GATE RECONSTRUCTION (replace, not stack):
- G1 Remove `SPORT_IMPLIED_FLOOR` + 0.42 juice floor (§11).
- G2 Remove universal raw model-prob floors 0.58/0.62/0.55 (§12) — evaluation decided by score+edge, with telemetry reasons.
- G3 Remove generation-time lock booster (L1049-64) — scores must not be floored UP to 85.
- G4 Collapse `SPORT_LOCK_FLOOR` variants into the single >=85 canonical board gate (picks below 85 still evaluated + telemetried as `SCORE_BELOW_85`, just not board-eligible).
- G5 De-vig edge behind feature flag: compute + persist `current_edge` AND `devig_edge` + book count, raw implied, de-vig prob, model prob, line, odds, ts, dispersion, rank-diff (§14, per user Q5 choice b).
- G6 §17 telemetry: persistent Mongo funnel collection (per candidate: sport, market, stage, exact reason) replacing/wrapping in-memory `log_reason`.
- G7 Side suppressions: evaluate BOTH sides through the model where a model exists; caps (`MAX_DAILY_TOTALS`, props caps) become ranked-eligibility with telemetry, not silent truncation.

## 7. CAPABILITY-REGISTRY vs RUNTIME CONTRADICTIONS
1. NHL: enabled+markets in registry, zero runtime path. (CRITICAL)
2. UFC: registry advertises totals; runtime hard-suppresses them.
3. CFB: registry "game markets full end-to-end"; runtime is book-follow, engine unused in game path.
4. NFL note "nflverse feature engine wired end-to-end" — true for props only; game markets untouched by any engine; Platinum absent from game path.
5. Tennis "Full end-to-end" — fallback runs unconditionally with its own model, not just on primary failure.
6. Soccer registry omits the second `soccer/pipeline.py` write runtime entirely.
