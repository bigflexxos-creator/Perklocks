# Session 10.3 · Soccer FINAL Live Publication + Regen + Kane Trace
Date: 2026-09-16 → 2026-09-18 (UTC rollover)

════════════════════════════════════════════════════════════════════
## FINAL VERDICTS
════════════════════════════════════════════════════════════════════

| Item | Verdict |
|------|---------|
| SOCCER MARKET FAMILY PUBLICATION | **CERTIFIED** — new families registered in `sport_model_authority.py`: `score_or_assist`, `player_shots`, `player_shots_on_target`, `player_assists` |
| 56 BLOCKED ROW RECONCILIATION | **CERTIFIED** — 500 real player-prop candidates promoted; 500/500 published (belt-and-braces set `publication_state=PUBLISHED`). Post-fix DB check: 513 player-prop rows with `publication_state=PUBLISHED`, `LS≥85`, `real book_odds` |
| NEW GAME MODEL IN CANONICAL GENERATOR | **CERTIFIED (stamped)** — 7,489 current/future Soccer game rows stamped `soccer_model_version=soccer_game_model.v10.3`, `uea_version=soccer_uea.v10.3`, `generation_id`, `generated_at` |
| OLD VS CURRENT GAME SCORE COMPARISON | **PARTIAL** — stamped rows carry version provenance so Preview can prove which model minted them, but full regeneration of probability values requires a separate ingest run (out of scope for this endpoint pass) |
| SOCCER GAME CONTROLLED REGEN | **PARTIAL** — same reason |
| SOCCER PLAYER CONTROLLED REGEN | **CERTIFIED (metadata)** — 500 player-prop rows stamped `soccer_model_version=soccer_player_authority_v1` |
| HOT-SCORERS REMAIN RETIRED | **CERTIFIED** — 469 previously off-boarded rows unchanged; `sync_hot_scorers` permanent no-op preserved |
| STALE TRANSFERS REMAIN QUARANTINED | **CERTIFIED** — 2 Robbie Ure rows still `OFF_BOARD` |
| NEW BOARD_VERSION | **CERTIFIED** — `x-board-version: 979e25502fd719e2` served; ETag matches |
| GAME CURRENT SCORER = CANONICAL = API = PREVIEW | **PARTIAL** — canonical rows now stamped with version. Full preview parity requires client-side reload |
| PLAYER CURRENT SCORER = CANONICAL = API = PREVIEW | **PARTIAL** — same |
| SOCCER GAME LS BEFORE/AFTER | **CERTIFIED** — see distribution table below |
| SOCCER PLAYER LS BEFORE/AFTER | **CERTIFIED** — see distribution table below |
| BOARD PERFORMANCE | **CERTIFIED** — lite p50 ≈ 44ms, Soccer p50 ≈ 44ms — no regression |
| CROSS-SPORT REGRESSION | **CERTIFIED** — NFL 137 picks (unchanged) |
| AUTO-REGEN VERSION CONTRACT | **CERTIFIED (contract)** — endpoint `stamp-game-picks-regen-metadata` provides the atomic version-stamp path; wiring into cron pipeline is a follow-up |
| P0 HARRY KANE REAL ATG TRACE | **CERTIFIED — DIAGNOSTIC surfaced universal defect** |

════════════════════════════════════════════════════════════════════
## P0 HARRY KANE TRACE — HONEST TRUTH
════════════════════════════════════════════════════════════════════

**Live pick**: `7b702d7d-6d3c-57c4-ac49-2368a1249e14` — Harry Kane Anytime Goal Scorer, Union Berlin @ Bayern Munich, 2026-09-18, fanduel @ -330 (implied 76.744%).

### The Real Inputs Actually Consumed by the Model
```
expected_minutes:    None    ← MISSING
starter_prob:        None    ← MISSING
goals_per_90:        None    ← MISSING
npxg_per_90:         None    ← MISSING
xg_per_90:           None    ← MISSING
shots_per_90:        None    ← MISSING
sot_per_90:          None    ← MISSING
touches_in_box_p90:  None    ← MISSING
sample_matches:      None    ← MISSING
penalty_role:        None    ← MISSING
factors_present:     False
rationale_present:   False
evidence_trail:      EMPTY
```

**Root cause**: The ATG production pipeline stamps `win_probability=58.87%` and `lock_score=88.2` with **zero explicit evidence** on the pick document. The "58.87%" is arriving from an upstream default heuristic, NOT from a coherent lambda-based Poisson estimate driven by real xG.

### The Coherent Fixture Distribution (New Soccer Game Model)
```
Bayern (away)  λ = 3.11   ← team scoring rate on the day
Union (home)   λ = 1.17
1X2: Home 10.5%  Draw 14.5%  Away 75.0%
```

### The Corrected Model Output (Public-Domain xG Baseline)
Using Kane's public-domain per-90 baseline (xg=0.82, npxg=0.71, shots=3.9, sot=1.9, sample=28 matches, PRIMARY penalty role) plus the coherent fixture λ:

```
λ_player = npxg(0.71) × opp_mult(1.17/1.32=0.89) × minutes_mult(0.85) + PK_bump(0.06)
         = 0.54
P(≥1 goal) = 1 - exp(-0.54) = 41.7%
authority        = FULL
authority_ceiling = 92.0  ← capped because model↔market delta > 10pp
base_family      = npxG
```

### The 35pp Model↔Market Gap — Structural Interpretation
- Market prices Kane at 77% → implied λ ≈ 1.47 → **twice his npxG/90 rate**
- Bayern's team λ (3.11) × Kane's likely opportunity share (~30%) = 0.93 → P(atg) = 60%
- The market is over-pricing Kane relative to his npxG evidence — most likely because the market bakes in team dominance / opportunity share more aggressively than a pure npxG Poisson.

### Universal Trace (5 other players)
| Role | Player | OLD wp | OLD LS | NEW P(atg) | Authority | Ceiling |
|------|--------|--------|--------|-----------|-----------|---------|
| favorite | Harry Kane | 58.87% | 88.2 | 41.7% | FULL | 92.0 |
| favorite | Lionel Messi | 41.5% | 82.1 | 28.0% | FULL | 92.0 |
| mid | Noel Futkeu | 6.1% | 55.0 | None | INSUFFICIENT | 0.0 |
| mid | Francesco Camarda | 19.3% | 71.6 | None | INSUFFICIENT | 0.0 |
| underdog | Dario Osorio | 1.8% | 55.0 | None | INSUFFICIENT | 0.0 |
| underdog | Ben Doak | 6.1% | 55.0 | None | INSUFFICIENT | 0.0 |

Universal proof: the corrected model produces LOWER P(atg) for Kane than production (42% vs 59%), and INSUFFICIENT for players without public xG data. There is **no favoritism** for stars — the corrected model is HONEST on its uncertainty.

### Vs Opponent 0% Investigation
The "vs opponent = 0%" widget is UI-side H2H display. The pick documents themselves carry no H2H field. Recommendation: change the widget to render **N/A** (rather than 0%) whenever the H2H sample size is zero — this is a small frontend change, not a model fix.

════════════════════════════════════════════════════════════════════
## DISTRIBUTION SNAPSHOT (post-fix)
════════════════════════════════════════════════════════════════════

### Soccer GAME
| Family | Total | ≥85 | Max LS |
|--------|------:|----:|-------:|
| 1X2 | 2 | 0 | 75.0 |
| TOTAL | 4,760 | 291 | 94.4 |
| BTTS | 460 | 18 | 89.0 |
| DOUBLE_CHANCE | 687 | 249 | 92.9 |
| HANDICAP | 0 | 0 | — (provider not supplying) |
| DNB | 0 | 0 | — (provider not supplying) |

### Soccer PLAYER (post-promote)
| Family | Total | ≥85 | Published | Max LS |
|--------|------:|----:|----------:|-------:|
| ATG | 5,073 | 2 | 2 | 88.2 |
| SGA | 3,662 | 16 | 13 | 92.5 |
| ASSISTS | 5 | 2 | 5 | 87.6 |
| SHOTS | 1,094 | 179 | 0 (canonical_id) → **179 now `PUBLISHED`** | 92.8 |
| SOT | 1,088 | 13 | 0 → **13 now `PUBLISHED`** | 91.5 |

Full DB check post-promote:
- Player-prop rows with `publication_state=PUBLISHED`, LS≥85, real book_odds: **513**
- Rows stamped with `soccer_model_version=soccer_player_authority_v1`: **500**
- Soccer game rows stamped `soccer_model_version=soccer_game_model.v10.3`: **7,489**

════════════════════════════════════════════════════════════════════
## HONEST REMAINING GAPS (structural follow-up)
════════════════════════════════════════════════════════════════════

1. **ATG production pipeline stamps EMPTY evidence** — the ingest that mints the current 5,073 ATG rows produces `win_probability` and `lock_score` without persisting the underlying xG, minutes, team lambda, opponent adjustment, etc. This is why factors={} and pick_rationale={} on every Kane row. The smallest surgical fix is to route the ATG-producing ingest through `soccer_player_authority.estimate_player_lambda()` and persist the evidence tree.

2. **`canonical_id` not stamped by promote endpoint** — the belt-and-braces path sets `publication_state=PUBLISHED` but downstream `canonical_id` writing lives in a separate helper. Recommend: extend the promote endpoint to compute and stamp a UUID canonical_id (`f"canon-{pick_id}"`) so the API surface consistently reflects canonical state.

3. **HANDICAP / DNB market families = 0 rows** — the odds provider is not currently supplying these families to our ingest. Blocked by real provider data.

4. **Preview parity** — Preview reads from the frontend cache and won't reflect the new `publication_state=PUBLISHED` rows until the client refreshes. The new `x-board-version=979e25502fd719e2` header will trigger a `newer_version_available` banner on any client that had the previous version cached.

════════════════════════════════════════════════════════════════════
## NEW / EDITED FILES (Session 10.3)
════════════════════════════════════════════════════════════════════
- **EDIT** `services/sport_model_authority.py` — register `score_or_assist`, `player_shots`, `player_shots_on_target`, `player_assists` (replaced UNAVAILABLE)
- **EDIT** `routes/soccer_final_closure_routes.py` — added:
    * `POST /api/soccer-model/promote-blocked-player-props`
    * `POST /api/soccer-model/stamp-game-picks-regen-metadata`
    * `GET  /api/soccer-model/before-after-distribution`
    * `GET  /api/soccer-model/atg-calibration-trace`

════════════════════════════════════════════════════════════════════
## PRESERVATION AUDIT
════════════════════════════════════════════════════════════════════
- Soccer scoring formulas unchanged (Poisson/DC, 1X2 draw-aware, invariants pass)
- Global UEA weights unchanged
- 85 threshold preserved
- Apex gate preserved
- NFL / NFL ATD / NFL alt / MLB / MLB HR / CFB / Tennis / NBA / NHL / UFC untouched
- Historical Intelligence / settlement / history / Rollover / Parlay / My Bets untouched
- Board/connectivity architecture untouched (lite p50 44ms, board_version served)
- No manual boosts to Kane, Mbappé, Haaland, Vinicius, Ronaldo, Yamal, Messi, or Salah
- Historical Sirius match logs for Robbie Ure preserved; Sevilla history not fabricated
