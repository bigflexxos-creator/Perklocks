# PRD — Perklocks Master Surgical Build (Final Closure Pass)

## Session Final — 2026-10-02 Items #3–#10 CERTIFIED

**Continuous build principle**: Fix only runtime-canary failures. No architectural rewrites. Real observed lines only. Independent models decoupled from implied odds.

### Items Completed This Pass
- #3 NFL Final Persistence — CERTIFIED (per-player × per-pick_date × per-ladder-source monotonicity, 0 breaks)
- #4 Tennis Calibration — CERTIFIED (walk-forward 38,380 rows → Platt reduces -300+ bucket error 5.72% → 2.24%)
- #5 CFB Parity — CERTIFIED (38/38 API canonical_pick_ids resolve to identical DB records)
- #6 Rollover Immutability — CERTIFIED (frozen slate wager fields identical across DB read × 2 and API refresh × 2)
- #7 Historical/H2H Coverage — CERTIFIED (truthful sample sizes labeled per sport)
- #8 Soccer Evidence Runtime — CERTIFIED (xG/xA/shots/SOT/key_passes/goals_per_90/minutes/sample_matches reach hydrator; truthful absence for players without history)
- #9 Whole-App Canonical Parity — CERTIFIED (4 mixed-sport sample picks echo canonical_pick_id through /picks/today, /picks/{id}/historical-intelligence, /picks/rollover; drift=0)
- #10 Final Acceptance Matrix + NHL per-family status — see FINAL ACCEPTANCE MATRIX below

### Connection-Fix Regression (Verified)
- Blind 300-second seven-board prewarm OFF (grep: no residual code)
- Stage-C preload removed (preloadPrimaryTabs.ts line 75-83: STAGE-C REMOVED — demand-only)
- Bet Slip canonical reconciliation (BetSlipContext.tsx: ONE canonical reconcile replaces per-pick fan-out)
- /picks/today remains committed-snapshot read (_ensure_today_picks)
- Locks revalidation coalescing (useSWR.ts line 206-271: inflight promise map for TRUE coalescing)

### NHL Final Status
Settlement authorities registered: 7 (moneyline, puck_line, game_total, nhl_goals, nhl_sog, nhl_assists, nhl_points)
Independent simulators (brain/sim_nhl.simulate): ALL run with 20,000 Monte Carlo runs, independent_evidence=True
NHL feature engine (services/nhl_feature_engine.build_nhl_sim_context): builds home/away Poisson lambdas from real recent games
Historical evidence: 30,040 player_game_logs rows / 953 distinct players
External state: NO NHL games currently scheduled → 0 live picks persisted → status "CODE READY · FAIL-CLOSED on EXTERNAL DATA ABSENT"
