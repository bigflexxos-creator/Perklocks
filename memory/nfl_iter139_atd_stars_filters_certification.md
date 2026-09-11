# NFL Continuous Surgical Build — Iter 139
### ATD Tab · Alt-Ladder Filter Pills · Star Player Watchlist

Certification token: `NFL_ITER139_UX_TRIPLE_BUILD_CERTIFIED`

## 1. Files / functions changed

**Backend (surgical, additive):**
- `routes/picks_routes.py::picks_today`
  - Added `stars_only: Optional[bool] = False` query param.
  - New READ-time filter block **after canonical wager dedupe** that
    narrows NFL responses to picks matching the curated
    `elite_players.ELITE_PLAYERS["NFL"]` roster via
    `elite_players.find_elite_player()`.  Uses canonical identity
    fields (`canonical_player_name`, `elite_player_name`,
    `player_name`, `selection`, `market`) with accent-insensitive
    whole-word matching.  **Never mutates any scoring field.**
  - Applied the SAME star filter to the eligibility-union rescue
    path (the wiring gap that let 583 non-star canonically-eligible
    picks bypass the primary filter).

**Backend (read-only additive endpoint — was already there):**
- `routes/nfl_routes.py::nfl_atd_by_game` (unchanged; existing
  endpoint at `/api/nfl/atd/by-game` already emits `book_odds`,
  `implied_probability`, `lock_score`, canonical `pick_id`,
  `provenance=canonical_publication`).

**Frontend:**
- `frontend/app/(tabs)/atd.tsx` — rewritten:
  - MLB HR-style **toggle** between `🔥 Top 5 Today` and `📋 By Game`.
  - Top 5 uses `/nfl/atd/leaderboard` (widened defaults to surface
    honest slate size — `limit=60, min_probability=0.10, opp=low`).
  - By Game uses `/nfl/atd/by-game` (top 5 per game).
  - New **`ATD -168` chip** surfaces real sportsbook ATD odds.
  - Defensive guards on `weighted_touches_recent` /
    `weighted_tds_recent` (present on leaderboard, absent on
    by-game endpoint) — no runtime crash on toggle.
- `frontend/src/lib/api.ts`
  - New `nflAtdByGame()` client + `NFLAtdByGameResponse` /
    `NFLAtdByGameGroup` types.
  - `NFLAtdPick` extended with optional `book_odds`,
    `implied_probability`, `lock_score`, `position`.
  - `PickFilters.starsOnly` optional field.
  - `picksToday()` forwards `stars_only=true` to the backend when
    `filters.starsOnly` is set.
- `frontend/src/components/SportFilterBar.tsx`
  - New `⭐ STARS` pill (NFL only, `market-pill-nfl-stars` testID).
  - Composes with market pills (STARS + PASS YDS, STARS + REC YDS,
    etc.).  Active state renders as `⭐ STARS ✓`.

**Tests:**
- NEW `tests/test_nfl_star_watchlist.py` — 6 tests covering:
  - Filter reduces slate to a strict subset.
  - No non-star name leakage.
  - **Scoring fields never mutated** (identity comparison across
    plain vs stars responses over every overlapping pick).
  - STARS + PASS_YDS composition (no cross-family leakage).
  - STARS + REC_YDS composition (no cross-family leakage).
  - Iter 138 non-regression (no raw `· ALT LOCK` in star response).

## 2. ATD Dedicated-Tab implementation
The ATD screen lives at `app/(tabs)/atd.tsx`, reached via the
🏈 ATD chip in the NFL filter row (already present).  The screen
is a **dedicated route**, not a Player-Props filter pill — same
UX pattern as the MLB `/hr` (`app/(tabs)/hr.tsx`) route.

```
NFL Locks board
  ↓  tap "🏈 ATD" pill (SportFilterBar)
router.push("/atd")
  ↓
app/(tabs)/atd.tsx
  ├── 🔥 Top 5 Today  → /api/nfl/atd/leaderboard
  └── 📋 By Game       → /api/nfl/atd/by-game
```

## 3. ATD Top 5 runtime proof (canonical publication, no fabrication)
```
GET /api/nfl/atd/leaderboard?limit=60&min_probability=0.10&min_opportunity_rating=low
  mode = canonical_publication
  picks_len = 4    total_candidates = 4
  #1 Derrick Henry     BAL vs IND  td_p=0.678  ATD -210  LS=77.2
  #2 Jonathan Taylor   IND vs BAL  td_p=0.663  ATD -195  LS=77.2
  #3 Bijan Robinson    ATL vs PIT  td_p=0.653  ATD -168  LS=82.5
  #4 Ashton Jeanty     LV  vs MIA  td_p=0.626  ATD -165  LS=71.2
```
Only 4 qualifying candidates exist on today's slate — the screen
correctly renders 4 rows and does NOT invent a 5th.

## 4. ATD By Game runtime proof (3 distinct matchups)
```
GET /api/nfl/atd/by-game?top_n_per_game=5&min_probability=0.05&min_opportunity_rating=low
  games_count=3   picks_returned=4
  [Atlanta Falcons @ Pittsburgh Steelers]
      Bijan Robinson   td_p=0.653  ATD -168
  [Baltimore Ravens @ Indianapolis Colts]
      Derrick Henry    td_p=0.678  ATD -210
      Jonathan Taylor  td_p=0.663  ATD -195
  [Miami Dolphins @ Las Vegas Raiders]
      Ashton Jeanty    td_p=0.626  ATD -165
```
Canonical identity survives — same td_probability, same ATD odds,
same LS values as Top 5.  One player = one score across views.

## 5. Filter-pill runtime counts (canonical NFL board, no cross-family leakage)
```
GET /api/picks/today?sport=NFL&market=<token>&lite=true

  passing_yards        → 97 picks  · sample: Patrick Mahomes 175+ Passing Yards · LS=95.2 · -475
  rushing_yards        → 238 picks · sample: Kenneth Walker III 30+ Rushing Yards · LS=94.7 · -900
  receiving_yards      → 410 picks · sample: Rashee Rice 30+ Receiving Yards · LS=95.0 · -650
  player_receptions    → 137 picks · sample: Rashee Rice 4+ Receptions · LS=95.8 · -580
```
Every pick returned by a family filter carries the correct
canonical `market` family regex (`_MARKET_REGEX["passing_yards"]`
etc.) — no leakage.  Milestone label format preserved (Iter 138).

## 6. Star Player Watchlist runtime proofs (visibility-only)
```
GET /api/picks/today?sport=NFL&stars_only=true&lite=true
  →  359 picks   (down from 908 non-filtered)
  →  ZERO non-star names leaked (checked: George Holani, Woody Marks,
     Dontayvion Wicks, Kayshon Boutte, Alec Pierce, Colston Loveland,
     Malik Davis, Tyler Warren)

Sample watchlist rows (canonical identity + real odds preserved):
  Rashee Rice 4+ Receptions          LS=95.8  wp=89.52  odds=-580
  Patrick Mahomes 175+ Passing Yards LS=95.2  wp=88.05  odds=-475
  Rashee Rice 30+ Receiving Yards    LS=95.0  wp=87.50  odds=-650
  Kenneth Walker III 30+ Rushing Yds LS=94.7  wp=87.43  odds=-900
  Travis Kelce 15+ Receiving Yards   LS=94.7  wp=87.38  odds=-930
```

Watchlist composes with market pills:
```
STARS + passing_yards    → 75 picks  (all star QBs)
STARS + receiving_yards  → 109 picks (Rice, Kelce, Engram, …)
```

## 7. Canonical identity proof (Watchlist ↔ full slate)
Every watchlist pick's `id` matches the exact same `id` in the
unfiltered NFL response.  Filter never creates a new canonical
identity, never renames, never dedupes differently — it only
narrows the outgoing list.

## 8. Real sportsbook odds / line provenance proof
All Star-Watchlist rows carry `book_odds` populated (verified
above).  All 4 ATD Top-5 rows carry real ATD odds (-210, -195,
-168, -165).  No synthetic odds, no synthetic lines, no
manufactured candidates.

## 9. Score-before / score-after proof (Watchlist NEVER boosts)
```
359 star picks compared against the same canonical ids in the
unfiltered response over 5 scoring fields (lock_score,
win_probability, book_odds, edge_percent, grade):

    mismatches = 0
```
Zero score mutation — enforced by
`test_nfl_star_watchlist.test_star_filter_never_mutates_scores`.

## 10. Iter 138 non-regression proof
```
GET /api/picks/today?sport=NFL&lite=true
  nfl total=908
  milestone-form labels=801
  raw " · ALT LOCK" labels=0     ← Iter 138 contract preserved
```

## 11. Settlement-anchor non-regression proof
Watchlist rows carry the raw `line` field intact:
```
sample: Rashee Rice 4+ Receptions  →  line=3.5  (settlement anchor)
                                       market="Rashee Rice 4+ Receptions" (display)
```
359/359 star picks carry a non-null `line`.

## 12. Preview / Web / Native parity
All three UX additions route through the SAME canonical API
surface (`/api/picks/today`, `/api/nfl/atd/leaderboard`,
`/api/nfl/atd/by-game`) used by the existing NFL board.  There
is no separate ranking source, no client-side score mutation,
no preview/native fork.  Screenshots captured on the Web preview
show:
- NFL board with `🏈 ATD` and `⭐ STARS` pills in the market row.
- Terry McLaurin 25+ Receiving Yards @ LS 96 (milestone label,
  Iter 138 preserved).
- `⭐ STARS ✓` gold-active state after tap.
- ATD Top 5 screen with real ATD -210 / -195 / -168 / -165 odds.
- ATD By Game screen with 3 matchups grouped correctly.

## 13. Tests run
```
pytest tests/test_nfl_star_watchlist.py \
       tests/test_nfl_alt_label_projection.py \
       tests/test_nfl_alt_ladder_truth.py \
       tests/test_iter137_nfl_alt_surgical_closure.py \
       tests/test_nfl_atd_leaderboard_routing_fix.py \
       tests/test_nfl_alt_ladder_full_emission.py

  → 49 passed · 1 xfailed · 1 xpassed · 1 warning
```
- Star Watchlist: 6 new tests, all pass
- Iter 138 alt-label projection: 14 tests, all pass
- Alt-ladder truth: 14 tests, all pass
- ATD leaderboard routing: 8 tests, all pass
- Iter 137 closure: 6 pass, 1 xfail (correct — cap intentionally
  removed), 1 xpass (test is now trivially true)
- Alt-ladder full emission: 1 pass

## Guardrails observed
- ❌ MLB not touched.
- ❌ NFL scoring engine not touched.
- ❌ sports_engine not refactored.
- ❌ Settlement anchors not modified.
- ❌ No fake candidates, no fake odds, no fake lines.
- ❌ No popularity boost.
- ❌ No `_prop_market_label` change.
- ❌ Iter 138 unchanged; regression proof above.
- ✅ Additive UX/read-path surgical build — canonical truth intact.

**CERTIFICATION: `NFL_ITER139_UX_TRIPLE_BUILD_CERTIFIED`**
