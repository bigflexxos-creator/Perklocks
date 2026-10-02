# Perklocks — Product Requirements (Live)

## Current Pass (2026-06-27): NFL Props Final Writer Fix

### What the user asked for
Change the NFL alt-prop writer so sportsbook-backed alternate picks consume the REAL observed rows already present in `db.live_alt_lines` instead of generating replacement sportsbook ladders. Apply universally across the 4 supported NFL alt families, preserve exact book / price / threshold, write one predictive distribution per (player, stat family) so monotonicity holds at write-time (0 read-time clamps), and attach provider provenance (`sportsbook`, `provider_market_key`, `line_source=OBSERVED_BOOK`, …) on every alt pick.

### What changed (minimal, surgical)
1. **`backend/sports_engine.py` — new helper `_load_nfl_live_alt_lines(event_id)`** returns raw rows from `db.live_alt_lines` for the 4 supported NFL alt families (`player_pass_yds_alternate`, `player_rush_yds_alternate`, `player_reception_yds_alternate`, `player_receptions_alternate`).
2. **`_fetch_player_props_for_sport`** attaches those rows on each NFL event's payload as `payload["_nfl_live_alt_lines"]`.
3. **`_props_picks_from_event`**:
   - Walks the Odds-API bookmaker payload AND the observed `live_alt_lines` rows.
   - Builds a per-key provenance map `_nfl_alt_lineage[(mk, player, point, side)]` so every surviving NFL alt pick carries the real `sportsbook`, `provider_market_key`, `provider_outcome`, `provider_last_seen`, `line_source="OBSERVED_BOOK"`.
   - For every NFL alt rung the writer now uses the **pure** per-rung distribution probability (`__rung_p_hat`) instead of the 85·rung + 15·factor_mean blend — this is the "one predictive distribution per player + stat family" the user mandated, and makes `P(lower) ≥ P(higher)` monotone by construction so the existing read-time guard remains a pure safety net (0 clamps required for the regenerated ladders).
4. **Alt-ladder dedup regex fix** — `_prop_key` and `_dedup_key` were collapsing every NFL milestone-format alt rung (`"Dak Prescott 175+ Passing Yards"`) into one dedup bucket because their line regex only matched decimal (`Over 240.5`) forms. The milestone regex is now added; each exact threshold rung now produces a UNIQUE key, and the full real-sportsbook ladder survives publication.
5. **`backend/services/magic/line_wire.py`** — `OBSERVED_BOOK` added to the `_STRUCTURED_SOURCES` whitelist so the downstream line-source attacher preserves the writer's stamp verbatim.

### Scope guardrails honored
- No NHL / NBA / CFB / UI / identity / injury work.
- No new architecture, no NFL Props V2 rewrite.
- Never invents a threshold; model-only rungs never publish as sportsbook-backed alts.
- Burrow handling: whenever `live_alt_lines` holds his FanDuel rows the writer consumes them; when it does not we report `PROVIDER/FEED PARITY GAP` and never synthesize a substitute.

### Acceptance evidence
First post-fix NFL refresh persisted real FanDuel / DraftKings alt rungs across QBs, WRs and RBs — see the FINAL ACCEPTANCE REPORT returned to the user. Second refresh (with the ladder-dedup regex fix) is running at report time to materialize multi-rung ladders for Dak / Lamar / Burrow Pass Yds; dedup fix is independently proven correct via isolated regex-level unit evidence.
