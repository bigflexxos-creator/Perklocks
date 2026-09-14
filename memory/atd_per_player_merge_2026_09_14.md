# NFL ATD — Per-Player Merge & Depth Fix
**Date:** 2026-09-14 18:02 UTC (rev 2 of the by-game universe fix)
**Scope:** `services/nfl_atd_universe.py` + `routes/nfl_routes.py`
**Untouched:** ATD model, NFL alt props, UEA weights, all other sports.

---

## Second Defect (from user screenshot review)

Rev 1 gated on-demand ATD scoring at the **event** level:
`if event has ANY canonical row → treat event as covered → skip`.
That broke depth for events that had 1 canonical publication:
DET @ BUF, CAR @ ATL, NO @ BAL all showed only the single canonical
player.

## Rev 2 — Per-Player Merge

New selector contract (`services/nfl_atd_universe.py`):

```
for each event with fresh provider Anytime-TD rows:
    canonical_player_names_lower = {name for name in canonical rows of this event}
    missing = {provider_players} - canonical_player_names_lower
    if missing:
        run build_nfl_game_context(prop_candidates=missing)
        emit engine rows for missing players (rejects dropped)
```

Dedupe helper `dedupe_atd_candidates` — canonical always wins;
key is `player_id` when present, else lower-cased `player_name`.

Grouping key in `/atd/by-game` switched from `canonical_event_id`
→ `event` (name) so canonical rows (which persist
`canonical_event_id = event_name`) and on-demand rows (which carry
the Odds API hex `event_id`) coalesce under the same matchup card.

## Post-Fix Proof — Event Completeness Table

| Matchup | Provider | Canonical | Missing (fed to engine) |
|---|--:|--:|--:|
| Carolina Panthers @ Atlanta Falcons | 4 | 1 (Bijan) | 3 |
| Denver Broncos @ Kansas City Chiefs | 29 | 0 | 29 |
| Detroit Lions @ Buffalo Bills | 7 | 1 (Gibbs) | 6 |
| New Orleans Saints @ Baltimore Ravens | 4 | 1 (Henry) | 3 |

## Rendered By-Game Depth (LIVE)

```
DEN @ KC  (14 candidates)   1. RJ Harvey · 2. Dobbins · 3. Rashee Rice · 4. Sutton · 5. K. Walker III · 6. Bo Nix · 7. Waddle · 8. Worthy
DET @ BUF (7 candidates)    1. Jahmyr Gibbs (canonical) · 2. Amon-Ra St. Brown · 3. Josh Allen · 4. James Cook · 5. Jameson Williams · 6. Sam LaPorta · 7. DJ Moore
CAR @ ATL (4 candidates)    1. Bijan Robinson (canonical) · 2. Drake London · 3. Tet McMillan · 4. Chuba Hubbard
NO  @ BAL (4 candidates)    1. Derrick Henry (canonical) · 2. Travis Etienne Jr. · 3. Chris Olave · 4. Lamar Jackson
```

- `games_count = 4`, `candidates_total = 29` (was 3 · 3 pre-rev-1, 4 · 17 post-rev-1).
- Canonical rows always rank first per game (highest td_prob).
- No duplicate player cards (dedupe verified).
- Frontend header now reads **`4g · 18 picks`** (with default `top_n_per_game=5` on FE).

## Whole-Slate Top-15 (leaderboard proof)

`total_candidates = 29`. Top-3 remain the canonical Elite Locks
(Gibbs 0.8227, Henry 0.7734, Bijan 0.6804); ranks 3-15 blend in
on-demand rows sorted purely by td_probability.

## Acceptance Row Set

```
ATD EVENT COMPLETENESS CHECK      — CERTIFIED
ATD PARTIAL-EVENT BACKFILL        — CERTIFIED
ATD CANONICAL+ON-DEMAND MERGE     — CERTIFIED
ATD PLAYER DEDUPE                 — CERTIFIED
ATD BY-GAME MULTI-CANDIDATE DEPTH — CERTIFIED
```

## Files Changed

- `~ /app/backend/services/nfl_atd_universe.py` — signature switched
  from `canonical_event_ids: set[str]` → `canonical_by_event:
  dict[event → {player_ids, player_names}]`; per-player missing set
  computed and only unpublished players fed to the engine; new
  `dedupe_atd_candidates(canonical, on_demand)` helper.
- `~ /app/backend/routes/nfl_routes.py` — both endpoints build a
  per-event coverage map from their canonical scan, invoke the
  expander, then merge via `dedupe_atd_candidates`.  By-Game
  grouping key switched from `canonical_event_id` → `event` (name).

## Guarantees Reaffirmed

- ATD model (`nfl_atd_engine.py`) — untouched
- NFL alt-line contract — untouched
- UEA weights / adapters — untouched
- MLB / Soccer / Tennis / NBA / NHL / UFC — untouched
- Main board admission (`/api/picks/today`) — untouched
- Canonical publication service — untouched (it still emits its own
  ATD picks on its next tick; the expander is a runtime lift, not
  a replacement, and canonical always wins the dedupe collision)
