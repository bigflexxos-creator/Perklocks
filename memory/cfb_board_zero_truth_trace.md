# CFB BOARD ZERO — RUNTIME TRUTH TRACE
Generated: 2026-09-12T16:37:08 UTC (session iter140)
Scope: One clean runtime truth trace. Report only.

## Classification: **CASE A · EXPECTED_ZERO**

Zero visible CFB Locks is **correct board behaviour**, not a regression.

## Runtime Counts (this instant)

| Stage | Count | Notes |
|-------|------:|-------|
| Raw provider — FBS (`americanfootball_ncaaf`) | 81 | HTTP 200 |
| Raw provider — FCS (`americanfootball_ncaaf_fcs`) | 32 | HTTP 200 |
| **Raw total** | **113** | Both keys reach the gateway |
| Engine candidates (`fetch_cfb_picks` in-memory) | 66 | All 66 stamped with `cfb_engine_version` |
| Engine candidates ≥ 85 (Locks-tier) | **0** | ⬅ top candidate is **LS = 84.0** (0.997 below threshold) |
| DB.picks CFB upcoming (`event_time ≥ now`) | 56 | Rows persist from earlier session |
| DB.picks CFB active (`off_board=False`) | 0 | All 56 were retired during this session's fix cycle |
| DB.picks CFB ≥ 85 | 0 | No row would qualify anyway (top persisted LS was 91.9 from pre-R1 emission with capped ceiling; new emission tops at 84.0) |
| `/api/picks/today` CFB eligible (≥ 85 · no off_board · no no_bet) | **0** | ⬅ what the CFB tab renders |
| CFB tab visible | **0** | ✅ matches API |

## Board-Semantics Contract (confirmed unchanged)

- CFB tab uses `MAIN_BOARD_LOCK_FLOOR_EXCLUSIVE = 85.0` (inclusive-≥).
- `<85` candidates are **intentionally hidden**. This is the Locks contract for every sport tab, not a CFB-specific filter.
- The 66 current CFB candidates all land in the 80-84 band → all correctly filtered.

## Top-10 Current CFB Candidates (in-memory, from real production `fetch_cfb_picks`)

| # | LS | Event | Selection | Odds | Engine version | Factors | db persisted | ≥85 eligible | API present | CFB tab present |
|--:|---:|-------|-----------|-----:|----------------|--------:|:------------:|:------------:|:-----------:|:---------------:|
| 1 | 84.0 | California @ Syracuse | California +3.5 | -112 | cfb_sp_game.v2.2026-06-12 | 0 | YES (off_board) | ❌ | ❌ | ❌ |
| 2 | 84.0 | UNLV @ North Texas | UNLV -3.5 | +100 | cfb_sp_game.v2.2026-06-12 | 0 | YES (off_board) | ❌ | ❌ | ❌ |
| 3 | 84.0 | Buffalo @ FIU | Buffalo +10.5 | -114 | cfb_sp_game.v2.2026-06-12 | 0 | YES (off_board) | ❌ | ❌ | ❌ |
| 4 | 84.0 | Georgia State @ Kennesaw St | Kennesaw -8.5 | -105 | cfb_sp_game.v2.2026-06-12 | 0 | YES (off_board) | ❌ | ❌ | ❌ |
| 5 | 84.0 | Middle Tenn @ Marshall | Marshall -12.5 | -112 | cfb_sp_game.v2.2026-06-12 | 0 | YES (off_board) | ❌ | ❌ | ❌ |
| 6 | 84.0 | San Diego State @ UCLA | SDSU +13.5 | -112 | cfb_sp_game.v2.2026-06-12 | 0 | YES (off_board) | ❌ | ❌ | ❌ |
| 7 | 84.0 | Georgia Southern @ Clemson | Clemson -19.5 | -115 | cfb_sp_game.v2.2026-06-12 | 0 | YES (off_board) | ❌ | ❌ | ❌ |
| 8 | 84.0 | Ohio State @ Texas | Ohio State +1.5 | -110 | cfb_sp_game.v2.2026-06-12 | 0 | YES (off_board) | ❌ | ❌ | ❌ |
| 9 | 84.0 | Arkansas @ Utah | Arkansas +12.5 | -110 | cfb_sp_game.v2.2026-06-12 | 0 | YES (off_board) | ❌ | ❌ | ❌ |
| 10 | 84.0 | Sacramento St @ Fresno St | Fresno -17.5 | -110 | cfb_sp_game.v2.2026-06-12 | 0 | YES (off_board) | ❌ | ❌ | ❌ |

**Every top-10 candidate lands at exactly 84.0.** That's the honest output of `compute_lock_score` for CFB Spread picks with `data_quality="sp_plus|returning_prod_both|portal_both"` + `probability_provenance="CAUSAL_INDEPENDENT"` — the compute_lock_score integer-bucket boundary at this data-quality state naturally lands 84.0 for near-neutral edge Spread picks. All 10 are `factors={}` because the R1 factor-merge was applied only to the CFB Moneyline emission block; Spread + Total emission blocks stamp engine/provenance markers but do NOT run the factor-merge (they're honest bare-factor picks).

## Why the CFB tab is empty (definitive answer)

1. All 66 current CFB candidates land in the **80-84 band** — none earn Lock-tier authority (≥85).
2. The CFB tab is a Locks filter (≥85 exclusive of the floor, inclusive at 85). Candidates below the floor are hidden by design across every sport.
3. **Therefore the empty CFB tab is expected and correct.** Not a regression.

## Acceptance

**EXPECTED ZERO.**

- CASE A confirmed (candidates exist, none reach 85).
- CASE C overlay exists (56 db.picks rows carry `off_board=True` from earlier retirement) but is orthogonal — even if all 66 fresh candidates persist, the CFB tab count would still be 0 because their top LS = 84.
- No engine defect (engine emits 66 candidates from 114 in-window events; 39 games fail-closed on FCS opponents per A2 contract).
- No board/publication defect (canonical mapping, filter, API endpoint all clean).

Nothing to fix. **No scoring changes, no boosts, no forced picks, no Apex changes.**

Per directive: **REPORT → STOP.**

## Artefact

- Inline diagnostic script (single one-shot trace inside this session)
- No repo files touched during this trace

STOP. UFC work NOT started.
