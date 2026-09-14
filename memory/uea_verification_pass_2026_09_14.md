# UEA + ATD + Soccer Shots/SOT — Continuous Verification Pass
**Run date:** 2026-09-14 17:23 UTC
**Backend build:** `2026.06.11-soccer-shots-sot-assists-wired-v49`
**Truth source:** `/api/picks/today` (authenticated demo user) + `/api/nfl/atd/*`
**Sample size:** 295 live wire picks · 3 canonical ATD candidates

---

## 1. Last-Release Certifications

### 1.1 UEA LIVE RELEASE — ✅ CERTIFIED
- 295/295 (100.0%) wire picks carry a full `evidence_authority` payload
  (`weighted_score`, `coverage`, `ceiling`, `tier_gate`, `peak_non_apex`,
  `strong_axes`, `contradictions`, `version=uea.v1.2026-06`).
- Every payload contains the 7-axis `components` block: `model_probability`,
  `prediction_reliability`, `history_threshold_support`, `matchup_role_support`,
  `independent_convergence`, `simulation_distribution_support`, `data_quality`.
- 90+ reachability confirmed live (all data-flowing sports):
  * MLB: 52 in 95-99 · 45 in 90-94 · 0 in 85-89 · 97 picks
  * NFL: 7 in 95-99 · 20 in 90-94 · 29 in 85-89 · 56 picks
  * Soccer: 0 in 95-99 · 1 in 90-94 · 140 in 85-89 · 141 picks
  * Tennis: 1 in 90-94 · 1 pick
- Coverage-based ceiling verified: a wire pick with only `model_probability`
  AVAILABLE reports `coverage=0.1429`, `ceiling=84.9`, `tier_gate=84.9` — UEA
  correctly refuses to certify 99-tier from a single axis.

### 1.2 7-AXIS PERSISTENCE REPLAY — ✅ CERTIFIED
- Persistence path unchanged (`pick_refresh_orchestrator.py`).
- Replayed a live top-scored wire pick (MLB), 100% of components round-tripped
  from DB → JSON → API with axis `status`/`score`/`detail` triple intact.

### 1.3 STANDARD -1000 CAP — ✅ CERTIFIED
- Zero standard-market picks with `american_odds < -1000` on the wire.
- Live odds distribution: min -930, max +1800, median -149.
- 7 picks below -500, all with a legitimate NFL alt marker.

### 1.4 NFL ALT EXEMPTION — ✅ CERTIFIED (code-verified)
- Admission code (`picks_routes.py:3382-3421`) exempts NFL alt via
  `is_alt=True` / `alt_line_provider` / threshold patterns
  (`3+ 4+ 5+ 15+ 25+ 35+ 100+ 125+ 150+ 175+ 200+ 225+ 250+ 275+ 300+`).
- Wire currently carries 29 NFL alt picks (mix of chalk & juice), most
  negative -930 (Kelce 15+ Rec Yds, LS 94.7). Data does not currently supply
  any alt priced worse than -1000, so the exemption is dormant but present.

### 1.5 STALE BUILD BANNER FIX — ✅ CERTIFIED
- Frontend `APP_DATA_VERSION = "2026.06.11-soccer-shots-sot-assists-wired-v49"`.
- Backend `/api/version.data_version = "2026.06.11-soccer-shots-sot-assists-wired-v49"`.
- Live UI screenshot: **no banner rendered** (login screen clean).

---

## 2. ATD Live Check

| Metric | Value |
|---|---|
| Canonical ATD universe | 3 |
| Top-5 endpoint count   | 3 |
| By-Game candidate count | 3 |
| By-Game games_count    | 3 |
| TOP5_IDS ⊆ BY_GAME_IDS | **TRUE** |
| BY_GAME – TOP5         | ∅ (universe ≤ 5) |

Candidate IDs (universe): `00-0039139` (Jahmyr Gibbs), `00-0032764`, `00-0038542`
— each grouped under its own matchup (Lions@Bills · Panthers@Falcons · Saints@Ravens).

- ATD TOP 5 — ✅ CERTIFIED (Anytime TD only; `market_filter="Anytime TD only"`
  in response `rules`).
- ATD BY GAME FULL-UNIVERSE — ✅ CERTIFIED (structural). Because current
  universe = 3 and NFL Week 1 is just starting, universe ≤ 5 today. The
  MLB-HR-style grouping code is exercised and returns the full universe;
  the "BY_GAME – TOP5 non-empty when universe>5" invariant is honored by
  code inspection (`nfl_routes.py` returns non-Top-5 rows once >5 exist).
- ATD FRONTEND MLB-HR-STYLE FLOW — ✅ CERTIFIED
  (`/app/frontend/app/(tabs)/atd.tsx`: parallel fires both endpoints,
  Top-5 sort by td_probability, By Game maps `byGame.games[]`).

---

## 3. Soccer SHOTS / SOT End-to-End Trace

### 3.1 Pipeline stages (each verified live in DB / code)

| Stage | Status | Evidence |
|---|---|---|
| Provider rows (`live_alt_lines`) | ✅ | Odds API alt-lines feed populating `book_odds`, per-book quotes |
| Normalized rows (`real_line_scorer_ingest.py`) | ✅ | 1,431 `player_shots` + 1,384 `player_shots_on_target` in `db.picks` |
| Identity resolved | ⚠ Partial | Shots/SOT next-72h: 447 RESOLVED · 176 UNRESOLVED · 12 AMBIGUOUS |
| Event resolved (fixture ⇄ matchup) | ✅ | `event`, `home_team`, `away_team` populated on 100% |
| Lineup / minutes evidence | ⚠ Projected only | `lineup_status = {"status":"projected"}` — no confirmed XI yet |
| Player history (shots/90, SOT/90) | ❌ **MISSING** | `compute_soccer_scorer_factors_sync()` returns empty → `PLAYER_FORM_NOT_FOUND` on 100% of LS≥85 shots/SOT |
| Opponent matchup allowance | ❌ **MISSING** | `resolve_soccer_player_matchup()` returns empty rows |
| Model probability | ✅ | Computed from `book_impl` fallback (line 326 of `real_line_scorer_ingest.py`) |
| UEA scoring | ✅ | 7 axes emitted, MISSING marked `status:MISSING, score:null` — never 0 |
| LS ≥ 85 | ✅ | 68 shots/SOT in window achieve LS≥85 from model_prob+book_impl |
| Off-board classification | ✅ (correct) | 100% of LS≥85 shots/SOT stamped `off_board=True` with `PLAYER_FORM_NOT_FOUND` |
| `publication_source` | ✅ | Stamped `real_line_alt_scorer_v1` on every row |
| `/api/picks/today` | ✅ correctly hidden | 0 shots/SOT surface because `off_board:{$ne:True}` filter (line 1748) drops them |
| Frontend rendered | ✅ correctly empty | Shots/SOT tabs surface empty state (no false zeros) |

### 3.2 Top current Shots/SOT candidates (unpublished, DB-visible)

| Player | Team → Opp | Line/Mkt | Book Odds | WP | LS | Off-Board Reason | UEA coverage |
|---|---|---|---:|---:|---:|---|---:|
| Lautaro Martinez | Inter → ? | Shots | -2500 | 96.15% | 92.2 | PLAYER_FORM_NOT_FOUND | 0.14 |
| Marcus Thuram    | Inter → ? | Shots | -2000 | 95.24% | 92.0 | PLAYER_FORM_NOT_FOUND | 0.14 |
| F.P. Esposito    | Inter → ? | Shots | -1500 | 93.75% | 91.8 | PLAYER_FORM_NOT_FOUND | 0.14 |
| Ange-Yoan Bonny  | Inter → ? | Shots | -1500 | 93.75% | 91.8 | PLAYER_FORM_NOT_FOUND | 0.14 |
| Lamine Yamal     | Barça → ? | SOT   | -1250 | 92.60% | 91.5 | PLAYER_FORM_NOT_FOUND | 0.14 |
| Kevin De Bruyne  | Napoli → ? | Shots | -900 | 90.00% | 91.0 | PLAYER_FORM_NOT_FOUND | 0.14 |
| Dodi Lukebakio   | Sevilla → ? | Shots | -400 | 80.00% | 85.5 | PLAYER_FORM_NOT_FOUND | 0.14 |
| Dani Olmo        | Barça → ? | SOT   | -390 | 79.59% | 85.3 | PLAYER_FORM_NOT_FOUND | 0.14 |

- **UEA payload for each**: `components.model_probability = AVAILABLE`;
  the other 6 axes are `status:MISSING, score:null` with detail
  `no_history / no_matchup / no_provenance / no_scoring_factors /
  no_simulation / no_dq_signal`. **No axis renders as 0%.**
- Ceiling on all rows = 84.9 (UEA tier_gate honored on evidence side; legacy
  LS math is a separate lift not a hard cap per contract).

### 3.3 Verdicts

- **SOCCER SHOTS PROVIDER→UI** — ✅ **CERTIFIED (wiring)** · ❌ **UNPUBLISHED (data coverage)**
  The full pipeline is wired end-to-end; publication is honestly gated
  by missing player-form data (`PLAYER_FORM_NOT_FOUND`). This is the
  correct behavior — the system is not hallucinating history to force
  a board slot.
- **SOCCER SOT PROVIDER→UI** — ✅ **CERTIFIED (wiring)** · ❌ **UNPUBLISHED (data coverage)** (same reason)
- **SOCCER PLAYER MISSING≠ZERO** — ✅ **CERTIFIED**
  All six missing UEA axes on every shots/SOT candidate emit
  `status:MISSING, score:null` — never 0, never negative.

---

## 4. Soccer Regression Check (Goalscorer / Score-or-Assist / Assists)

| Market | In 72h window | Board-eligible | Wire | Notes |
|---|---:|---:|---:|---|
| player_goal_scorer_anytime | 855 | 0 | 0 | LS<85 across the board (no chalky Erling Haaland tonight); wiring unchanged |
| player_to_score_or_assist  | 729 | 4 | 2 | Yamal LS 86.4 @ -350, Mbappé LS 86.1 @ -360 |
| player_assists             | 0   | 0 | 0 | No Odds API rows this cycle |
| player_shots               | 320 | 0 | 0 | All PLAYER_FORM_NOT_FOUND (see §3) |
| player_shots_on_target     | 315 | 0 | 0 | All PLAYER_FORM_NOT_FOUND (see §3) |

- Market mapping: ✅ present (`_MARKET_LABEL` in ingest)
- Expected minutes: ⚠ projected only (no lineups yet)
- xG/xA: routed through `soccer_scorer_bridge` when form_row exists — else MISSING
- History: MISSING → `PLAYER_HISTORY_NOT_FOUND` / `PLAYER_FORM_NOT_FOUND`
- Opponent evidence: routed via `resolve_soccer_player_matchup` — MISSING today
- UEA: emits 7-axis correctly, missing != zero
- Legacy cap: unchanged; no retune performed

**SOCCER PLAYER REGRESSION — ✅ PASS.**
No wiring defect proven. Data coverage is the current bottleneck, not the
model / cap / ingester.

---

## 5. CFB Enrichment
**Status: BLOCKED EXTERNALLY.** CFBD monthly call quota exhausted. Not retried.
EPA/PPA + Success Rate ingestion queued for next quota window.

---

## 6. Final Report Row Set

```
UEA LIVE RELEASE                    — CERTIFIED
7-AXIS PERSISTENCE REPLAY           — CERTIFIED
STANDARD -1000 CAP                  — CERTIFIED
NFL ALT EXEMPTION                   — CERTIFIED (code-verified; dormant today)
STALE BUILD BANNER FIX              — CERTIFIED
ATD TOP 5                           — CERTIFIED (universe=3)
ATD BY GAME FULL-UNIVERSE           — CERTIFIED (structural; universe≤5 today)
ATD FRONTEND MLB-HR-STYLE FLOW      — CERTIFIED
SOCCER SHOTS PROVIDER→UI            — WIRING CERTIFIED · UNPUBLISHED (data coverage)
SOCCER SOT PROVIDER→UI              — WIRING CERTIFIED · UNPUBLISHED (data coverage)
SOCCER PLAYER MISSING≠ZERO          — CERTIFIED
SOCCER PLAYER REGRESSION            — PASS
CFB ENRICHMENT                      — BLOCKED EXTERNALLY
```
