# PERKLOCKS — Product Requirements & Session Log

## PRODUCT REQUIREMENTS (immutable)
- Maintain immutable canonical truth (`PublishedPickContract`)
- Zero mock data. Never fabricate sportsbook lines or synthesize odds
- Preserve EXPO parity
- Real runtime sportsbook data must flow completely to the UI
- Historical team must NEVER override current roster membership

## SESSION LOG — 2026-09-07 (MAIN 41)

### Goal
Close NFL prop pipeline: current-team identity + regular props + ATD, then
prove end-to-end on the live board.

### Changes Applied
1. **`services/nfl_feature_engine.py`** — new
   `resolve_nfl_current_team_for_player()` reads from `db.players`
   (ESPN roster, refreshed daily) with `updated_at desc` freshness + name
   variants (Jr./Sr./II). Falls back to `nfl_player_weekly` for GSIS.
   Distinguishes `current_team` vs `historical_team`.
   Adds per-invocation memo cache so 382-candidate events don't hit DB
   1500× per event.
2. **`sports_engine.py`** — after `_build_pick` for NFL, attaches
   `player_team`, `canonical_team_id`, `canonical_player_id`,
   `player_team_name`, `position`, and `identity_class`
   (AUTHORITATIVE / PROVISIONAL).
3. **`nfl_atd_engine.py`** — `_player_profile_from_weekly` now returns
   `current_team` (from most-recent 2026 row if present else latest);
   `predict_player_atd` propagates `current_team` + `historical_team`.
   Fixes stale-team regression class (Etienne → JAX ghosts).
4. **`routes/nfl_routes.py`** — `/api/nfl/atd/leaderboard` now sources
   from PUBLISHED canonical ATD picks first (`db.picks` where
   `market ~ /Anytime|1st TD|First TD/`), falls back to legacy
   historical ranking with `mode: "research_only"` and every pick
   tagged `provenance: "historical_ranking"` so the UI can distinguish.
5. **`routes/picks_routes.py`** — NFL-specific 168-hour board horizon
   (was 72h). Other sports remain at 72h.

Frontend:
6. **`frontend/src/components/SportFilterBar.tsx`** — Added
   `🏈 ATD` chip for NFL (mirrors MLB `🚀 HR` chip). Routes to `/atd`.

### Verified Via Direct-Call Funnel Audit (5 NFL events)
- Raw ATD outcomes: 148
- ATD engine accepted: 75
- Passed edge floor: 36
- Lock ≥85: 2 (both AUTHORITATIVE with player_team resolved)
- Team resolution: 100% on NE @ SEA event (31/31)
- Model probabilities individualized (McCaffrey 65.6%, Purdy 22.7%,
  Woody Marks 53.3%, Ferguson 15.3%). No compression.
- Sportsbook edges natural (-22% to +22%). No compression.
- Legitimate rejection reasons: unresolved_player_identity,
  current_team_unresolved (undrafted/preseason additions),
  no_recent_red_zone_path.

### OUTSTANDING — NEEDS NEXT SESSION
- **Direct-call emission proven; running orchestrator not persisting NFL
  props to DB despite `Props fetch NFL: selecting 16` firing every ~5min.**
- Symptom: 0 NFL prop picks written to DB across many refresh cycles.
- Likely root cause: `_fetch_player_props_for_sport("NFL")` returns 0 picks
  even though `_props_picks_from_event` in-process produces valid picks.
- Investigation pending: race between concurrent refresh triggers /
  bad_market_registry duplicate-key exceptions / event-loop cancellation.
- Verified NOT the cause: identity gate, team resolution, edge threshold,
  lock threshold, `_game_ctx` UnboundLocalError (NBA-only), horizon.

### Test Credentials
`demo@lockscore.ai` / `demo123` (admin)

### API Endpoints Confirmed
- `GET /api/mlb/hr-slate` → 200, 36 picks ✅
- `GET /api/nfl/atd/leaderboard?limit=25` → 200, currently mode=research_only
  (falls back because 0 canonical ATD picks in DB yet)
- `GET /api/picks/today?sport=NFL` → 4 game-level picks (all real, no props)

### Frontend Verified
- MLB HR chip → `/hr` → 36 picks rendering
- NFL ATD chip → `/atd` → picks rendering (research_only mode)
- NFL tab → market chips include "🏈 ATD" alongside standard prop chips
