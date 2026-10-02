# Perklocks — Product Requirements (Live)

## Latest Pass (2026-06-28): PERKLOCKS Master Surgical Fix Build

### Preservation guarantees honored
- No rebuild of PublishedPickContract, canonical identity, Probability Authority, model/calibrator registries, settlement authority, H2H foundation, NFL alt writer, Soccer provider acquisition, CFB 168h weekly, Rollover immutability.
- No CLV reintroduction, no synthetic sportsbook lines, no book-implied → model-probability leak, no artificial 90-99 boosts.

### P0-A — NHL Live-Season Production Wiring
- `sports_engine.py`: added `NHL` to `prop_sports` so Odds API player-prop acquisition runs; added `PLAYER_PROP_MARKETS["NHL"]` with the 4 canonical families (goals / SOG / assists / points) × main+alternate.
- `services/sport_capability_registry.py`: NHL `production_status` upgraded from `INTENTIONALLY_DEFERRED` → `LIVE_SEASON_WIRED`, prop_markets populated, per-family `market_status` all set to `MODEL_UNAVAILABLE` (fail-closed preserved).
- `services/historical_intelligence.py`: registered minimal `NHLPlayerHistoricalAdapter` + `NHLTeamHistoricalAdapter` + NHL branch in `resolve_market_family`.
- Downstream MODEL_UNAVAILABLE gating is still in place → NHL picks only publish when a legitimate independent evidence/model path is added.

### P0-B — Connection / Performance Root Closure
- `services/board_snapshot_prewarm.py`: 300s blind 7-board regeneration retired. Default `BOARD_SNAPSHOT_PREWARM_SEC=0` → periodic loop OFF. Startup prewarm still fires once; opt-in loop enforced floor at 900 s so misconfiguration can't restore the storm.
- `frontend/src/contexts/MLBLiveContext.tsx`: `/api/mlb/live` 60-second polling is now DEMAND-DRIVEN. Store tracks active `useMLBLive(event)` subscribers; polling only runs while subscribers > 0 AND tab visible.
- `frontend/src/contexts/BetSlipContext.tsx`: eliminated the N+1 `api.pickDetail(id)` fan-out on hydration. Single canonical `api.picksToday()` reconciles slip items; cached slip is preserved on network failure (last-known-good).
- `frontend/src/lib/preloadPrimaryTabs.ts`: Stage-C deleted. My Bets 5-way burst + Profile warm no longer fire blindly at startup; both surfaces lazy-load on first navigation.
- Locks revalidation: already centralized via `load()` with 1.5 s dedupe, `AbortController` cancellation of prior in-flight, and canonical-epoch consumer registration — PRESERVED.
- `/api/picks/today`: route handler is pure committed-snapshot read (no inline healing) — PRESERVED.

### P0-C — NFL Alt Writer: Freeze Code, Finish Runtime
- `sports_engine.py`: FROZEN. All accepted systems preserved — `_load_nfl_live_alt_lines`, `_nfl_alt_lineage`, pure `__rung_p_hat` for NFL alt mp, OBSERVED_BOOK provenance stamping, `_prop_key`/`_dedup_key` milestone regex fix.
- `services/magic/line_wire.py`: `OBSERVED_BOOK` added to `_STRUCTURED_SOURCES` so the writer's provenance stamp survives downstream line-wire attachment.
- Runtime: a fresh NFL refresh is in flight at submission (prior writer code already stored real FanDuel/DraftKings alt rungs across QBs/WRs/RBs for 2026-09-23; ladder-regex fix will materialize multi-rung ladders for Dak/Lamar/Burrow on next completed refresh).

### P0-D — Tennis ML Calibration Fix
- `services/tennis_math_engine.py`: `score_tennis_matchup` no longer falls back to `base_wp = home_implied` when Elo data is missing. Removed probability-authority double count. No surface Elo → FAIL CLOSED (returns None) so the pick never publishes with a book-implied-anchored "model" probability.

### P1 touchpoints
- P1-F Historical / H2H: NHL adapter + resolve_market_family branch added (empty-but-valid payload until NHL game-log ingestion lands).
- P1-D Rollover immutability: `freeze_official_slate` already enforces first-writer-wins via unique index — PRESERVED, no change.
- P1-E Settlement: `UniversalMarketContract` already carries NHL MONEYLINE / puck_line / GAME_TOTAL / NHL_GOALS / NHL_SOG / NHL_ASSISTS / NHL_POINTS — PRESERVED, no change.

### Items intentionally not touched this pass (preserved as working)
- CFB production/Expo sync — no concrete drift reported; existing canonical-epoch consumer path on Locks already revalidates on epoch change.
- Soccer player model enrichment — provider acquisition already closed per prior sign-off; no new enrichment keys added without validated provenance.
- Dynamic provider discovery — current `_API_SPORTS_KEYS` catalog in `odds_provider.py` is populated at startup from The Odds API `/sports` endpoint; no evidence of a missed active tournament this cycle.
