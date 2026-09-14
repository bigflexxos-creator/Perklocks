# NFL ATD — By-Game Full Canonical Universe Fix
**Date:** 2026-09-14 17:45 UTC
**Scope:** `/api/nfl/atd/leaderboard` + `/api/nfl/atd/by-game` (universe expansion only)
**Untouched:** ATD model, NFL alt props, UEA weights, all other sports.

---

## Root Defect (as tonight's DEN @ KC proved)

Both endpoints previously read their candidate universe ONLY from
`db.picks` rows carrying `atd_evidence.td_probability > 0`. When the
canonical publication cycle lagged the Odds API's Anytime-TD push,
entire games disappeared from BOTH the Top-5 leaderboard AND the
By-Game grouping — even though real provider ATD sportsbook rows
existed in `live_alt_lines` and the ATD engine produced legitimate
probabilities for those players.

Concrete pre-fix evidence:

| Signal | DEN @ KC (kickoff tonight 00:15 UTC) |
|---|---|
| Provider Anytime TD rows in `live_alt_lines` | **61** across **33 unique players** |
| Canonical `player_anytime_td` rows in `db.picks` | **0** |
| Rows on `/api/nfl/atd/leaderboard` | **0 from DEN@KC** |
| Rows on `/api/nfl/atd/by-game` | **DEN @ KC matchup ABSENT** |
| ATD engine direct call for DEN/KC candidates | 7/8 tested returned `td_probability` 0.20–0.48 (only Engram rejected: `no_recent_red_zone_path`) |

This proved the by-game *selector* logic was already correct (it
groups the SAME canonical universe as the leaderboard, not the
Top-5 output). The *universe* itself was truncated upstream.

## Surgical Fix

New shared expander module: `services/nfl_atd_universe.py`

```
async def expand_atd_universe_from_live_alt_lines(
    db, *, canonical_event_ids: set[str], min_probability: float,
) -> list[dict]:
    """For every fresh NFL event with real player_anytime_td rows in
    live_alt_lines that is NOT already covered by the canonical
    universe (dedupe by event id/name), run the SAME authoritative
    build_nfl_game_context pre-loader and emit real ATD engine
    leaderboard rows. Rejects are dropped; no fake scores."""
```

Wired into both endpoints AFTER the canonical `db.picks` scan and
BEFORE any grouping / whole-slate sort:

```
canonical.extend(await expand_atd_universe_from_live_alt_lines(
    db,
    canonical_event_ids=<events already present>,
    min_probability=<endpoint parameter>,
))
canonical.sort(key=(td_probability desc, confidence desc), reverse=True)
```

Every on-demand row is tagged `provenance="on_demand_atd_engine"`
and `publication_state="LIVE_ENGINE"` for downstream transparency.
Canonical publication rows keep `provenance="canonical_publication"`
unchanged.

Fail-open on any expander error — canonical publication path stays
authoritative if the expander throws.

## Post-Fix Live Proof

**Leaderboard (`/api/nfl/atd/leaderboard?limit=10&min_probability=0.10`)**

| # | Player | Matchup | TD Prob | Odds | Provenance |
|--:|---|---|--:|--:|---|
| 1 | Jahmyr Gibbs | Lions @ Bills | 0.8227 | -285 | canonical_publication |
| 2 | Derrick Henry | Saints @ Ravens | 0.7734 | -200 | canonical_publication |
| 3 | Bijan Robinson | Panthers @ Falcons | 0.6804 | -205 | canonical_publication |
| 4 | RJ Harvey | **DEN @ KC** | 0.5564 | +300 | on_demand_atd_engine |
| 5 | J.K. Dobbins | **DEN @ KC** | 0.4815 | +180 | on_demand_atd_engine |
| 6 | Rashee Rice | **DEN @ KC** | 0.4756 | +155 | on_demand_atd_engine |
| 7 | Courtland Sutton | **DEN @ KC** | 0.3681 | +235 | on_demand_atd_engine |
| 8 | Kenneth Walker III | **DEN @ KC** | 0.3448 | -125 | on_demand_atd_engine |
| 9 | Bo Nix | **DEN @ KC** | 0.2541 | +550 | on_demand_atd_engine |
| 10 | Jaylen Waddle | **DEN @ KC** | 0.2047 | +195 | on_demand_atd_engine |

`total_candidates = 17` (was 3 pre-fix).

**By-Game (`/api/nfl/atd/by-game?top_n_per_game=5&min_probability=0.10`)**

| Matchup | Candidates In Game | Top-1 |
|---|--:|---|
| **Denver Broncos @ Kansas City Chiefs** | **14** | RJ Harvey (0.5564, +300) |
| Detroit Lions @ Buffalo Bills | 1 | Jahmyr Gibbs (0.8227, -285) |
| Carolina Panthers @ Atlanta Falcons | 1 | Bijan Robinson (0.6804, -205) |
| New Orleans Saints @ Baltimore Ravens | 1 | Derrick Henry (0.7734, -200) |

`games_count = 4` (was 3 pre-fix). `candidates_total = 17`.

## Tonight's Required Proof — DEN @ KC Full Ladder

| Rank | Player | Team | TD Prob | Confidence | Opp Rating | Odds | Grade | Provenance |
|--:|---|---|--:|--:|---|--:|---|---|
| 1 | RJ Harvey | Denver Broncos | 0.5564 | 0.490 | high | +300 | B+ | on_demand_atd_engine |
| 2 | J.K. Dobbins | Denver Broncos | 0.4815 | 0.447 | high | +180 | B | on_demand_atd_engine |
| 3 | Rashee Rice | Kansas City Chiefs | 0.4756 | 0.426 | med | +155 | B | on_demand_atd_engine |
| 4 | Courtland Sutton | Denver Broncos | 0.3681 | 0.268 | med | +235 | C | on_demand_atd_engine |
| 5 | Kenneth Walker III | Kansas City Chiefs | 0.3448 | 0.293 | high | -125 | C | on_demand_atd_engine |
| 6 | Bo Nix | Denver Broncos | 0.2541 | 0.254 | med | +550 | C | on_demand_atd_engine |
| 7 | Jaylen Waddle | Denver Broncos | 0.2047 | 0.092 | med | +195 | C | on_demand_atd_engine |
| 8 | Xavier Worthy | Kansas City Chiefs | 0.1969 | 0.101 | med | +275 | C | on_demand_atd_engine |
| 9 | Travis Kelce | Kansas City Chiefs | 0.1963 | 0.133 | med | +200 | C | on_demand_atd_engine |
| 10 | Troy Franklin | Denver Broncos | 0.1947 | 0.092 | low | +1100 | C | on_demand_atd_engine |
| 11 | Lil'Jordan Humphrey | Denver Broncos | 0.1520 | 0.033 | low | +1200 | C | on_demand_atd_engine |
| 12 | Pat Bryant | Denver Broncos | 0.1493 | 0.057 | med | +380 | C | on_demand_atd_engine |
| 13 | Nate Adkins | Denver Broncos | 0.1315 | 0.009 | low | +1800 | C | on_demand_atd_engine |
| 14 | Patrick Mahomes | Kansas City Chiefs | 0.1278 | 0.000 | low | +700 | C | on_demand_atd_engine |

- Provider Anytime TD candidate count: **33** (Odds API bookmakers for DEN @ KC).
- Modeled candidate count (ATD engine returned probability): **31**
  (2 rejects: `no_recent_red_zone_path`, `unresolved_player_identity`).
- Eligible candidate count (td_prob ≥ 0.10, non-DST filter): **14**.
- Board eligibility for `/api/picks/today`: independent — this endpoint feeds the ATD screen only, not the Locks board (main-board admission is unchanged).

## Frontend Render Verification (screenshot captured at 17:45 UTC)

The ATD screen’s **By Game** tab now renders **“Denver Broncos @ Kansas City Chiefs”** as its own group, with RJ Harvey (B+ 56%), J.K. Dobbins (B 48%), Rashee Rice (B 48%) shown beneath the matchup — exact same order and values as the API returned. Header summary line reads **`4g · 8 picks`** confirming the by-game universe now includes DEN @ KC alongside the three canonical publications.

## Invariants Verified

- **TOP5_IDS ⊆ BY_GAME_IDS**: TRUE
- **BY_GAME – TOP5** (universe > 5): 3 additional DEN@KC players present in By-Game only (Sutton, Walker, Rice)
- **Canonical rows unchanged**: Gibbs / Bijan / Henry retain `provenance=canonical_publication`
- **NFL alt props on `/api/picks/today`**: 29 alt rows (unchanged from pre-fix)
- **UEA 7-axis payloads**: unchanged
- **Pytest**: `tests/test_universal_evidence_authority.py` — 36/36 passing

## What Was NOT Changed

- ATD model (`nfl_atd_engine.py`) — untouched
- UEA weights / adapters — untouched
- NFL alt-line contract — untouched
- MLB / Soccer / Tennis / NBA / NHL / UFC — untouched
- Main board admission gates (`/api/picks/today` filters) — untouched
- Canonical publication service — untouched (it will still emit its
  own Anytime-TD picks on its next scheduled tick; the expander is
  a runtime lift, not a replacement)

## Files Changed

- `+ /app/backend/services/nfl_atd_universe.py` (new, 240 LOC)
- `~ /app/backend/routes/nfl_routes.py` — wired the expander into
  `nfl_atd_leaderboard` and `nfl_atd_by_game`. Both endpoints now
  concat + re-sort on-demand rows into the canonical list.
