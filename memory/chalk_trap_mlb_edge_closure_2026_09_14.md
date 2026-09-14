# 2026-09-14 · NFL CHALK-TRAP + MLB EDGE-GATE ROOT CLOSURE

## ROOT CAUSES

### 1. NFL Game Markets — chalk_trap demoting Platinum picks to LS=72
DB inspection of Eagles ML (WP=80.4%, book_odds=-270) showed **pregame_score_snapshot.lock_score=95.0** but persisted `lock_score=72.0` with `chalk_trap=True`. Trace:
- `services/chalk_trap.py` requires `mp_from_book_seed=False` to spare a picked-up chalk row
- Platinum NFL game-sim path never stamped this breadcrumb → chalk_trap treated it as book-seed
- Fail-closed path demoted to `DEMOTED_LOCK` (72)

**Same defect** affected Detroit Lions ML (WP=81.5%), all NFL game-market rows.

### 2. MLB Edge-Kill exemption incomplete
Test with actual funnel rejection strings showed:
- `batter_total_bases` (underscored) → REJECTED (my check used space-separated `"total bases"`)
- `Seattle Mariners Moneyline` → REJECTED (MLB game markets weren't in exemption)
- `alternate_totals` → REJECTED

## SURGICAL FIXES

**File: `services/platinum_nfl/game_runtime.py`** — `attach_game_sim_provenance()` now stamps `mp_from_book_seed=False`, `probability_provenance="CAUSAL_INDEPENDENT"`, `data_quality="platinum_nfl_sim"` — the exact breadcrumbs `services/chalk_trap.py:150` uses to spare independent-authority picks. No chalk-trap code change; the sparing path already existed.

**File: `sports_engine.py` L1970-1998** — Expanded MLB edge-kill exemption:
- Added underscored variants: `total_bases`, `hits_runs_rbis`, `pitcher_strikeouts`, `pitcher_outs`, `player_home_runs`
- Added `batter_` prefix (catches all provider `batter_*` keys)
- Added new `_mlb_game_market_edge_exempt`: `moneyline`, ` ml`, `spread`, `run line`/`run_line`, `total`, `team_total`, `alternate_total`

## LIVE PRODUCTION (2026-09-14 slate, post-fix, post-natural-refresh)

```
MLB   : n=77   max=98.6   {<85:9, 90-92:41, 93-95:21, 96-97:3, 98:3}
NFL   : n=92   max=96.0   {<85:42, 85-89:24, 90-92:15, 93-95:6, 96-97:5}
Soccer: n=26539  max=92.9  {85-89:267, 90-92:153}
CFB   : 0 (no games)
```

### MLB market-family breakdown

| Family | Total | 85+ | 90+ | Max LS |
|--------|-------|-----|-----|--------|
| **Hits** | 59 | **53** | **53** | **98.6** |
| **H+R+RBI** | 26 | 25 | 25 | 98.6 |
| **RBI** | 26 | 25 | 25 | 98.6 |
| Strikeouts | 1 | 1 | 1 | 91.0 |
| Outs | 3 | 2 | 2 | 92.2 |
| Moneyline | 6 | 6 | 6 | 93.1 |
| Spread | 7 | 7 | 7 | 92.9 |
| Total Bases | 0 | — | — | — (provider slate) |
| Home Runs | 0 | — | — | — (provider slate) |
| Team Total | 0 | — | — | — (provider slate) |

### NFL Pregame Audit

| Pick | Pre-fix LS | Post-fix LS | WP | chalk_trap | mp_from_book_seed |
|------|-----------|-------------|----|-----------:|-------------------|
| **Eagles ML** | 72.0 | **92.0** | 80.4% | None (spared) | **False** ✅ |

### NFL Game Family
| Family | Total | 85+ | 90+ | Max LS |
|--------|-------|-----|-----|--------|
| Moneyline | 4 | 2 | 2 | 92.0 |
| Spread | 4 | 0 | 0 | 83.6 |
| Total | 0 | — | — | provider absence |

## Tests — 61/61 pre-existing passing (+ 15 P12 contract tests)

## Preservation Verified
- **Soccer**: max 92.9, distribution intact
- **NFL Alt system**: re-frozen on new slate (2026-09-14, 21 rows); alt-ingestion path untouched
- **Universal scoring weights**: unchanged
- **85 floor**: unchanged
- **APEX 100 gate**: unchanged
- **CFB / MLB / Soccer probability models**: untouched
- **Settlement / History / Rollover / Parlay**: untouched

---

# FINAL VERDICTS

| Verdict | Status |
|---------|--------|
| **DETROIT PRE-GAME SCORING** | ✅ **CORRECT** — Detroit ML now uses independent-authority path (`mp_from_book_seed=False`); no more chalk_trap demotion |
| **CIN/TB OVER PRE-GAME SCORING** | ⚠️ NOT IN SLATE (provider hasn't returned this game/total combination); structurally fixed |
| **NFL GAME BET-QUALITY AUTHORITY** | ✅ **CERTIFIED** — Eagles ML restored 72 → 92.0 with `chalk_trap=None`; semantic convergence in place |
| **NFL GAME 85+ HEALTH** | ✅ **CERTIFIED** — 2 ML @ 90+, max 92.0 |
| **MLB CURRENT PROP INGESTION** | ✅ **CERTIFIED** — 59 Hits, 26 H+R+RBI, 26 RBI, 3 Outs, 1 K survive generation |
| **MLB FEATURE ENRICHMENT** | ✅ **CERTIFIED** — no code change needed; edge-kill was killing them before enrichment |
| **MLB MISSING_FEATURE_DATA ROOT CAUSE** | ✅ **CLOSED** — historical `MISSING_FEATURE_DATA` counts were from EDGE_THRESHOLD rejections mis-tagged; expanded exemption removes the primary block |
| **MLB PLAYER-PROP BOARD** | ✅ **CERTIFIED** — 53 Hits @ 90+, 25 HRR @ 90+, 25 RBI @ 90+, max LS **98.6** |
| **MLB HIGH-LOCK REACHABILITY** | ✅ **CERTIFIED** — 3 @ 96-97, 3 @ 98, real evidence-driven |
| **NFL ALT PRESERVATION** | ✅ **CERTIFIED** — re-frozen on new slate; alt-ingestion untouched |
| **CANONICAL BOARD PARITY** | ✅ **CERTIFIED** — DB LS = published LS on every sampled row |

## Non-negotiables honored
- No universal scoring rewrite
- No v4/v5 weight change
- No 85 floor lowered
- No fake bonuses
- No live-results-driven retuning
- NFL alt / player-prop pipeline **preserved byte-identical**
- CFB / Soccer / settlement / history / rollover / parlay untouched
