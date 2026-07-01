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

## MLB HR — Banner Repositioned (2026-06-30)
Per user feedback: "Don't want top 5 each game, want top 5 for the day. Want
under MLB tab" + "should be after Outs Recorded".

**Changes:**
- Removed the standalone HR tab from the bottom tab bar (`href: null`)
- Created `/app/frontend/src/components/MLBHRBanner.tsx`
- Banner mounts INSIDE the Locks tab when `sport === "MLB"`, positioned
  AFTER the NRFI/YRFI CTA (which sits after the Outs Recorded market)
- Flattens every game's HR picks, sorts by `hr_score` desc, shows TOP 5
  across the WHOLE DAY (not 5 per game)
- Tap drills into full `/hr` slate (the deep-link route is still mounted
  but hidden from the tab bar)

**Cache version:** `20260630-hr-mlb-banner-v37` (auto-wipes stale clients).

## MLB HR — Chip in Market Pill Row (2026-06-30, final placement)
Per user feedback: "Like hits and strikeouts got hr should be next to them".

**Implementation:**
- Removed the inline `MLBHRBanner` CTA from `app/(tabs)/index.tsx`
- Added a special-case `"🚀 HR"` pill into `SportFilterBar.tsx`, rendered
  only when `sport === "MLB"`, placed AFTER the existing market pills
  (Moneyline · Run Line · Totals · Hits · H+R+RBI · Strikeouts · Outs Recorded)
- Pill is NOT a filter — `onPress` calls `router.push("/hr")` to open the
  dedicated HR slate screen, leaving the picks list untouched
- Cache key bumped to `20260630-hr-chip-v39`

## MLB HR — Top 5 of the Day default + toggle (2026-06-30, final UX)
Per user: "Still showing top 5 hr each game I want the 3–5 for the whole
day so add option where app take the 5 best".

**`hr.tsx` refactored to 2-mode view:**
- **"🔥 Top 5 Today" (default)** — flattens every game's HR picks into one
  pool, sorts by hr_score desc, surfaces the TOP 5 across the slate. Each
  card shows the matchup context inline (venue · park · temp · wind ·
  roof · opposing SP HR/9 · why-this-pick bullets).
- **"📋 By Game" (toggle)** — original per-game layout with up to 5 picks
  per game.

Toggle is right under the screen header. Cache key bumped to
`20260630-hr-top5-day-v40`.

## NFL ATD — Mirror of HR UX (2026-06-30)
Per user: "I want to do same thing with nfl for atd".

**New files:**
- `/app/frontend/app/(tabs)/atd.tsx` — full-screen NFL Anytime-TD slate
  with "🔥 Top 5 Today" (default) and "📋 Full Board" toggle modes.
  Mirrors `hr.tsx` shape: header back-arrow, mode toggle row, grade chips
  (A+..C), opportunity rating chip, touches/TDs/sample chips, and
  bullet rationale rows.
- Hidden tab `atd` mounted in `(tabs)/_layout.tsx` (`href: null`) so the
  deep-link route works without exposing a tab icon.
- New `🏈 ATD` pill in `SportFilterBar.tsx` that's only rendered when
  `sport === "NFL"`, sitting alongside Receiving / Rushing / Receptions
  / Passing pills, routing to `/atd` on tap (same pattern as the MLB HR
  chip).

**Data source:** existing `GET /api/nfl/atd/leaderboard` (already ranked
by td_probability across the slate — no extra flatten needed).

**Live sanity:** Christian McCaffrey 70.4%, Jonathan Taylor 69.1%, Josh
Jacobs 66.5%, Kyren Williams 63.6%, James Cook 62.5%.

Cache key bumped to `20260630-nfl-atd-chip-v41`.

## NFL ATD — Repositioned as CTA Under NFL Section (2026-06-30)
Per user: "No it should be under nfl tab".

**Changes:**
- Removed the `🏈 ATD` pill from `SportFilterBar.tsx` (was a market-row chip)
- Added a full-width `🏈 ATD PICKS` CTA button in `app/(tabs)/index.tsx`
  right below the `NFLIntelligenceSection`, only when `sport === "NFL"`,
  mirroring the existing MLB NRFI/YRFI button style
- Tap → routes to `/atd` (the Top-5 / Full-Board screen)
- Cache key bumped to `20260630-nfl-atd-cta-v42`

## Lock Score Fully Decoupled from Probability (2026-07-01)
Per user mandate: "My 99 Lock system is getting treated as a probability
value in analytics. I need it separated completely from win probability."

**Product spec now enforced:**
- `lock_score` = classification/tier label (Elite / Premium / Strong /
  Standard / Speculative / Pass), NEVER a probability
- `win_probability` = ONLY from the model output
- `lock_score` MUST NOT feed averages, ROI, or probability calculations

**Backend fixes:**
- `brain/candidates.py`: removed the `p["lock_score"] / 100` fallback that
  was polluting the ranker's confidence input. Now sources confidence
  from `brain.confidence_calibrated → win_probability → raw_win_probability
  → 0.5`, never from lock_score.
- `analytics._lock_calibration()`: rebuilt to emit tier-level performance
  only (count / hit_rate / ROI / avg_lock_score for reference), removed
  the `expected`/`delta` columns that inflated the illusion that
  lock_score was an expected probability.

**Frontend fixes:**
- `app/analytics.tsx` calibration table: dropped the EXPECTED / Δ columns,
  now renders TIER · N · HIT % · ROI %. ROI is now the actionable signal
  per tier, not a broken "over-promise" delta.

**Live sanity (post-fix analytics.calibration payload):**
- Elite (95+):     N=145, hit 69.7%, ROI +9.07%   ← positive tier ROI
- Premium (90-94): N=290, hit 61.7%, ROI -9.32%
- Strong (85-89):  N=351, hit 56.1%, ROI -13.63%
- No `delta` or `expected` fields in payload ✓
