# LockScore AI — Product Requirements

## Overview
LockScore AI is an AI-powered sports betting intelligence mobile app (React Native / Expo). It aggregates daily fixtures across MLB, NFL, NBA, Soccer, and Tennis, computes a proprietary **Lock Score (0–100)** per market, and presents the highest-EV opportunities. The platform never claims guaranteed winners — only probabilities, edges, and confidence.

## Core Features (v1)
1. **Email/password auth (JWT)** — register, login, persistent session via expo-secure-store
2. **Daily Lock Picks** — sorted by Lock Score ≥85, sport-filtered (All/MLB/NFL/NBA/Soccer/Tennis)
   - Lock Score, Win Probability, Implied Probability, Edge %, Confidence, Book Odds
   - Grades: Elite Lock (95–100), Strong Lock (90–94), Good Bet (85–89), Pass (<85)
3. **Rollover tab** — single best bet of the day, ranked across the entire board by composite (`lock_score + edge_percent × 1.5`)
4. **Bet Killer** — picks below 85 Lock Score, red warning aesthetic, AI-generated "Why To Avoid"
5. **Pick Detail** — AI explanation (Claude Sonnet 4.5), factor breakdown bars, key insights bullets
6. **Profile** — board stats summary, by-sport breakdown, sign-out
7. **Daily auto-refresh** — background task refreshes picks at 06:00 UTC every day; manual refresh available

## Tech Stack
- **Backend**: FastAPI + MongoDB (motor) + bcrypt + PyJWT
- **AI**: Claude Sonnet 4.5 via Emergent Universal Key (`emergentintegrations`)
- **Sports data**: API-Sports.io (live MLB/NBA/NFL/Soccer/Tennis fixtures, key `APISPORTS_KEY`)
- **Frontend**: React Native Expo Router, dark theme

## Lock Score Engine
Each pick is scored via weighted factor matrix per sport:
- **MLB**: H2H 20% / Recent Form 15% / Splits 25% / Pitcher Weakness 20% / Defense 10% / Weather 10%
- **NBA**: Usage / Minutes / Pace / Defense vs Position / Form / Splits / Back-to-Back
- **NFL**: Snap Share / Target Share / EPA Allowed / Pressure / DVOA / Weather
- **Soccer**: xG / xGA / Form 20% / H2H / Home Advantage / Injuries / Defense
- **Tennis**: Surface 25% / Form 20% / H2H 15% / Hold% 15% / Break% 15% / Fatigue 10%

Score formula: `50 + avg_factor × 40 + peak_factor × 10` → clamped 55–99. Top 5 picks per refresh boosted to Elite tier.

## API Endpoints
- `POST /api/auth/register`, `POST /api/auth/login`, `GET /api/auth/me`
- `GET /api/picks/today?sport=` (Lock ≥85)
- `GET /api/picks/all?sport=` (entire board)
- `GET /api/picks/bet-killer?sport=` (Lock <85)
- `GET /api/picks/rollover` (single best EV pick)
- `GET /api/picks/{id}` (with on-demand AI explanation)
- `POST /api/picks/refresh` (force-regenerate today's board)
- `GET /api/stats/summary`

## Smart Business Enhancement
- Daily auto-refresh keeps users coming back every morning
- Rollover tab creates a "pick of the day" habit-forming hook
- Future hooks: Pro tier (Stripe) for full-board access, push notifications at refresh time, parlay builder

## Disclaimers
- All picks are probabilistic. App never displays "guaranteed winner" language.
- "Pass" tier explicitly recommends NOT betting that market.

## Historical Sports Intelligence Engine (June 2026)
Low-cost historical memory layer feeding the Lock Engine — uses FREE APIs only:
- **MLB**: statsapi.mlb.com (no key)
- **NBA**: balldontlie.io (free tier)
- **NFL**: ESPN hidden API (no key)
- **NHL**: api-web.nhle.com (no key)
- **Soccer**: football-data.org (free tier, 10 req/min, 6.5s pacing)

MongoDB collections: `players`, `games`, `player_game_logs`, `season_totals`, `team_form`.

### Admin endpoints
- `POST /api/admin/historical/backfill` — body `{sports, mode, days?}`
- `GET /api/admin/historical/status`
- `GET /api/admin/historical/player-form?sport=&name=&market=`

### Lock Engine integration
Runs ALONGSIDE `elite_players.py` / `auto_elite.py` (per user spec — never replaces).
Each player-prop pick is enriched with `player_form` data and a soft ±1.5 nudge
based on hot/cold trend + consistency (min 3 logged games to react).

## Goalscorer Matchup Engine v3 (2026-06-30)
User mandate: "PerkLocks goalscorer engine feels like it is choosing players from
historical averages instead of evaluating the actual match."

**New modules:**
- `/app/backend/goalscorer_matchup.py` — matchup-first scoring engine
- `/app/backend/national_team_squads.py` — curated 26-man matchday squads

**Weights (per user spec):**
- Matchup     = 35%
- Opportunity = 30%
- Form        = 20%
- Historical  = 15%

**Confidence penalties:** bench risk · expected minutes <60 · market disagreement
· missing data · not in national squad · recent injury · short rest.

**Explainability fields surfaced on every soccer goalscorer pick:**
`matchup_score`, `matchup_grade` (A+..F), `starter_probability`,
`expected_minutes`, `role`, `penalty_taker`, `xG_form`, `market_rank`,
`why_this_pick[]`, `why_not_this_pick[]`.

**Filter behaviour:** picks with confidence < 0.45 OR score < 28 OR player
not in announced national team squad are DROPPED (unless `elite_protect`
flag is set).

**Wired into:** `/app/backend/routes/picks_routes.py` after
`_dedupe_goalscorer_per_event` — runs on every `/api/picks/today` request.

**UI:** `/app/frontend/src/components/LockPickCard.tsx` renders a grade chip
(green/yellow/red) plus starter %, expected minutes, role, PK flag, xG/90,
and the bullet-point "why" / "why-not" reasons inside the "Why this pick?"
panel.

**Smoke-tested outcomes (Sweden @ France slate):**
- Mbappé: A grade, 83.5, with full why-this-pick bullets
- Ivan Toney: DROPPED via squad gate (not in England's announced squad)
- Lukaku (Belgium away vs Senegal): F grade 32.6 — properly demoted

## MLB Home-Run Tab (2026-06-30)
New tab in the mobile app surfacing the 3–5 highest-conviction HR hitters per
MLB game with full matchup context.

**Endpoint:** `GET /api/mlb/hr-slate` (auth required)

**Engine modules:**
- `/app/backend/services/mlb_hr_intel.py` — HR probability + scoring
- `/app/backend/routes/mlb_hr_routes.py` — FastAPI route + 25-min cache

**Scoring factors (multiplicative on LEAGUE_HR_PER_PA = 3.3%):**
- **Park HR factor** — Statcast 2023-25 averaged. Coors 1.27, Yankee 1.22,
  Citizens Bank 1.12, Petco 0.86, Oracle 0.78, etc.
- **Pitcher HR/9** — from MLB Stats API season stats; clamps 0.55..1.85.
- **Batter power profile** — ISO + barrel% + HR/PA blend (50%/30%/20%).
- **Recent form** — last 15 games HR rate; HOT (≥0.30/g) = +20%.
- **Wind** — Open-Meteo (free, no key) projected onto stadium HR axis
  (home→CF compass bearing). Out-to-CF wind boosts up to +25%.
- **Temperature** — every 10°F above 70°F = +3% carry, capped ±10%.
- **Roof** — closed_dome zeros out all weather effects.
- **Platoon (LHP/RHP)** — +8% opposite-hand boost, −5% same-hand penalty.
- **H2H BvP** — shrinkage blend when ≥6 PA (data feed wired in v2).

**Confidence floor:** hr_score ≥ 45 to surface (≈10% individual HR probability).

**Frontend:**
- `/app/frontend/app/(tabs)/hr.tsx` — new tab "HR" (baseball icon, between
  Rollover and Parlay)
- Per-game card: away @ home, venue, park factor, wind/temp/roof chips,
  both probable SPs with HR/9, then up to 5 batter rows
- Each batter row: grade chip (A+..C+ color-coded), HR%, score, season HR,
  last-15 form, and 4–6 bullet-point why-this-pick rationale

**Verified on 2026-06-30 slate:**
- Kyle Schwarber vs Bubba Chandler @ Citizens Bank Park — A+ 99.2, 33.1% HR
  (HR-friendly park, HOT 6 HR last 15G, wind out to CF 10 mph, 90°F)
- Pete Alonso vs HR-prone pitcher — A+ 85.4
- 15 games / 73 picks / 10.5s build time (subsequent calls < 100ms cached)
