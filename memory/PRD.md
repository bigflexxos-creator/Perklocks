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

## PerksLocks Signal Engine — Phase A (2026-07-12)
User mandate: dedicated Signal Engine layer computing independent betting
signals feeding probability/ranking/Why-This-Pick. "Add signals only, do
not rebuild the app."

**New package:** `/app/backend/services/signal_engine/`
- `calculators.py` — 6 universal signals with budgets summing to ±50:
  Form ±12 (game-log L5/L10 vs line, trend, consistency, ESPN team form,
  Understat xG, pick hit-rate) · Matchup ±8 (season record delta,
  goalscorer matchup engine, BvP history) · Volume ±7 (starter prob,
  minutes, penalty taker, role, sim xG) · Injury ±8 (ESPN report both
  sides, subject-player status) · Market ±7 (open→current line movement
  steam, implied-prob zone, CLV component) · Value ±8 (edge vs book,
  EV per $1, sim agreement).
- `engine.py` — `score = clamp(50 + Σpoints, 0, 100)`, grades
  Elite/Strong/Moderate/Weak/Fade, 30-min freshness (market moves),
  best-effort persist to `db.picks` (`signal_engine` + `signal_score`).
- `rationale.py` — signal-driven "Why This Pick" bullets (real numbers,
  never generic); top-2 signals also injected into
  `pick_rationale.evidence` with 📡 prefix.

**Wired into:**
- `/api/picks/today` — decorator after `_decorate_with_espn_meta`.
- `/api/picks/{id}` — espn meta + signal engine on detail (also fixes
  card/detail win-prob drift).
- Rollover V4 `_ev_score` — `sig_mult = 1 ± 8%` from signal_score.
- Lite payload strips `signal_engine` (detail-only), keeps `signal_score`.

**Frontend:**
- `SignalEnginePanel.tsx` — 0-100 score + signed component bars +
  tap-to-expand evidence, on pick detail.
- `LockPickCard` — `📡 SIGNAL N/100` chip when |score−50| ≥ 8.
- Detail "WHY THIS PICK" prefers `signal_engine.why` over legacy
  top_reasons/key_insights.

**Phase B–D (approved roadmap, not yet built):** MLB Savant deep stats →
NBA/NFL/CFB volume+matchup ingestion → Tennis Elo (Sackmann).

## Nordic Hot-Scorers Board Fix + Detail UX (2026-07-12)
User reports: "Sweden and Norway goalscorer not populating on board" +
"Lock and win pct is not the same thing" + "why this pick shows generic
version first then loads detail version".

- `soccer_hot_scorers.py` (Wikipedia top-scorer picks for Allsvenskan /
  Eliteserien / Veikkausliiga etc.) now: maps scoring rate → lock via the
  shared `_prob_to_lock` tier scale (65% rate → 99 lock, wp stays 65%),
  tags `is_model_only` (matches board `model_only_q` carve-out ≥75),
  `elite_protect` for 8+ goal scorers, and writes real
  `pick_rationale.evidence` (passes quality-gate AGS Rule 4).
- `quality_gate.py`: Nordic leagues added to `has_form_source_leagues`
  (trusted AGS source); odds dead-zone (-140..-110) no longer applies to
  `fair_odds_model` picks (synthetic odds ≠ book-priced history).
- `pick/[id].tsx`: explanation card shows the AI loading state while
  `ai_pending` — never flashes the generic fallback template first.
  Second "WHY THIS PICK" section renamed "TOP SIGNALS".
- Result: 11 SWE/NOR goalscorer picks live on board (locks 88-99).

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

## History + Analytics Deep Cleanup (2026-07-01)
Per user: "Make sure you delete first goalscorer and kbo from history
and analytic tab such and fix the anytime goal scorer history and
analytic tab with accuracy since we only give top 3".

**DB backfill (one-time, all previously-settled picks):**
- 1,073 First Goal Scorer picks → `excluded_from_history=True`
- 197 Anytime Goal Scorer picks with lock_score < 85 → excluded
- 0 KBO / Korean baseball (none in DB, defensive filter added anyway)
- 0 Last Goal Scorer (none in DB, added to exclusion regex)

**Ongoing defensive filters added to both endpoints:**
- `GET /api/analytics/model-performance` (`analytics.py`)
- `GET /api/picks/history` (`routes/picks_routes.py`)

Both now EXCLUDE:
- Markets matching `First Goal Scorer` or `Last Goal Scorer`
- Leagues matching `KBO` or `Korean`
- Anytime Goal Scorer with `lock_score < 85` (only true top-3 count)
- Any pick with `lock_score < 89` (per prior directive) unless
  is_alt-with-lock≥85 or elite_pitcher_override

**Impact metrics (analytics/model-performance):**
| Metric | Before | After |
|--------|--------|-------|
| Settled picks | 842 | **495** |
| Hit rate | 59.8% | **67.1%** |
| ROI | -8.75% | **-3.92%** |
| Elite (95+) tier ROI | +9.07% | **+4.01%** |
| Soccer Anytime Scorer ROI | contaminated | **+47.24% on 8 picks** |
| First Goal Scorer count | 351 | **0** |

**Known remaining ROI bleeders (product-model issue, NOT analytics):**
- MLB H+R+RBI: 27.6% hit / -66% ROI on 29 picks
- MLB Strikeouts: 56% hit / -32% ROI on 16 picks
- MLB Hits: 67% hit / -6% ROI on 116 picks

---

## 2026-07-21 — Tennis Signal Rebalance + Longshot Trap + MLB K Bleed Fix

### Tennis Signal Rebalance (Option C + Elite Registry)
User: "why signal not getting higher on tennis everything under 80". Root
cause: 5/6 universal calculators return 0 for tennis (form/matchup/volume/
injury/market read MLB/NBA-shaped fields), so tennis_deep alone couldn't
push scores above the 78 conviction floor. Fix:
- **`calculators.py`**: `TENNIS_DEEP_MAX` 7→12; per-pillar awards bumped
  (surface/serve-return 1.5→2.5, elo scale ±2→±3); pillar-alignment bonus
  (+2/+3 for 3/4 aligned pillars).
- **`engine.py`**: tennis-specific conviction floors — Strong Lock (92-96)
  baseline 78→80; data-anchored floors 76/82/88 based on pillar alignment.
- **NEW `services/tennis_elite_players.py`**: Curated top-20 ATP + top-20
  WTA registry granting +22 elite boost (matches soccer Mbappé treatment).
- **SIGNAL_VERSION 8→9** forces recompute.
- **Result**: Tennis distribution 32-78 (avg 55) → 69-91 (avg 81.8); 86%
  ≥ 80. 376 pending tennis picks recomputed and persisted.

### Longshot Trap (NEW service) — Soccer 92+ ROI Fix
Data-driven diagnosis of 5,309 settled picks revealed Soccer Strong Lock
(92-96) bled **-21% ROI (-48u)** — concentrated in Goal Scorer / SoA
markets and +100-and-up longshot odds. Soccer chalk 92+ (< -150) stays
profitable (+1.6% to +11%).
- **NEW `services/longshot_trap.py`**: Mirror of Chalk Kill Switch for
  over-confident plus-money picks. Triggers on Soccer + lock≥92 + odds
  ≥ -100. Escapes: elite anchors, edge≥12pp + 3 DD signals.
- Wired into `server.py` post-chalk-trap.
- **Historical simulation: +61.5u ROI recovered.**

### MLB Strikeout Bleed Fix
Analysis of 300 settled K picks showed **-16.65% ROI overall**, with
board-visible K picks bleeding **-43.82% ROI (-15.34u)** and 89% having
edge < -5%. Additionally: **only 5 main-line K picks in entire DB
history vs 311 alt-K picks** (user asked "can we get the main lines?").
- **`chalk_trap.py`**: narrowed blanket `is_alt` escape — alt-lines now
  only spared with edge ≥ 2pp (was: spared all alts).
- **`board_visibility.py`**: chalk_trap + longshot_trap picks now
  **off_board=True** instead of staying visible with warnings. Users
  can't accidentally bet known losers.
- **`sports_engine.py`**: added `pitcher_strikeouts` (main-line)
  exception with implied ≥ 0.48 (was blocked by 0.62 gate designed for
  hit props). Main-line K props at -115 to -140 will now generate picks.
- **`routes/picks_routes.py`**: main board query excludes off_board=True;
  `mlb_k_q` edge floor tightened -12 → 0; `high_lock_bypass_q` now
  excludes negative-edge K props to prevent bypass.
- **Immediate impact**: 11 live MLB K picks now off_board; 157 chalk
  picks trapped; only 7 alt-lines spared (down from ~all alts).

### Non-Change: Soccer Goalscorer Longshot Filter
Handoff hypothesized blocked +EV longshot goalscorers were losing +124u
of profit. **Historical data proved the opposite**: +300+ longshots
already on-board generate **+183.8u profit**; blocked picks (edge≥3%,
lock≥70) would have LOST -18u. Current filter is correctly protecting
ROI. **No change made.**

### Files touched
- `services/signal_engine/calculators.py` (tennis rebalance)
- `services/signal_engine/engine.py` (conviction floors + elite tennis)
- `services/tennis_elite_players.py` (NEW)
- `services/longshot_trap.py` (NEW)
- `services/chalk_trap.py` (narrowed alt escape)
- `services/board_visibility.py` (trapped → off_board)
- `sports_engine.py` (main-line K threshold)
- `routes/picks_routes.py` (edge floors + off_board filter)
- `server.py` (wired longshot_trap after chalk_trap)

---

## 2026-07-21 (part 2) — Negative-Edge Cap + Board Date Fix

### Negative-Edge Conviction Cap (Signal Engine)
User: "How is this a 90 signal when he doesn't do good against this
pitcher?" — Mookie Betts Over 0.5 Hits @ -194 with edge=-4.8% was
scoring Signal 90 / Lock 99 despite negative edge and 0-for-8 BvP.
Root cause: conviction floors fired purely off `lock_score`, ignoring
edge quality. Chalky favorites got Signal 90+ regardless of price.
- **`engine.py`**: added negative-edge cap on conviction_boost applied
  BOTH before and after DD/tennis floor bumps.
  - edge < -5pp → conviction_boost=0 (no floor)
  - edge -5 to -2pp → cap at 14 (score 64)
  - edge -2 to +2pp → cap at 22 (score 72)
  - edge ≥ +2pp → full floor available
- **SIGNAL_VERSION 9→10**, 832 pending picks recomputed.
- **Result**: 0 negative-edge picks at Signal 90+ (was several). Every
  Signal 90+ pick now has genuine positive edge.

### Board Date Window Fix (Struff/Cerundolo Kitzbühel)
User: "Why didn't JL Struff and Cerundolo make board — they play today".
Their Kitzbühel first-round picks had `pick_date=2026-07-19` (when
ingested) but their matches got rescheduled to 2026-07-21. Board query
was strict `pick_date == today` so they were orphaned.
- **`routes/picks_routes.py`**: widened board query to accept picks
  matching either `pick_date == today` OR `event_time` within
  [now-30h, now+30h]. Added `status: pending` safety net.
- Handles reschedules by up to a full day + timezone-crossing morning
  matches + 1-3 day ingest lag.
- **Result**: Struff (Signal 82, +5.96% edge) and Cerundolo (Signal
  76, +2.49% edge) now visible on today's board.

### Known deferred: Tennis Settler Stuck-Pending
224 tennis picks from July 17-20 stuck as `status=pending` even though
matches finished 2-4 days ago. Root cause: auto-settler not matching
tennis match results. Left for follow-up session per user direction.

---

## 2026-07-21 (part 3) — MLB Random Factor System REMOVED (Phase 1)

### User mandate
"Do not patch around this. Replace the random factor system with a
real feature engine, starting with MLB. Never substitute randomness
for missing data. If the model lacks enough real inputs, the pick
should not reach the user board."

### What was replaced
Every MLB `_factors_random()` and `player_rng.uniform()` call driving
Lock scores in `sports_engine.py` has been swapped for the new
`services/mlb_feature_engine.py`. Random calls to
`_factors_random()` for non-MLB sports remain in place until their
respective Phase 2 replacements land.

**Random calls purged from MLB paths:**
- MLB Moneyline (line 998 area)
- MLB Total (line 1128 area)
- MLB Spread (line 1300 area)
- MLB Alt Team Total (2440s dead-code path)
- MLB Alt Run Line (2500s active path)
- MLB Pitcher K props (3050s pitcher block)
- MLB Hitter props (3100s batter block)

### New feature engine (`services/mlb_feature_engine.py`)
Every factor returns `Optional[float]` — None means unavailable.
Callers gate emission via `has_enough_real_data(factors, market_type)`:

| Market | Slots | Min real | Real sources feeding it |
|---|---|---|---|
| Pitcher K prop | 5 | 3 | statsapi_pitcher_season_k, statsapi_team_k_split, statsapi_pitcher_ip_per_start, park_factors_table, statsapi_pitcher_l5 (roadmap) |
| Hitter prop | 5 | 3 | L10 hit rate, matchup xERA, home/away OPS, platoon splits, BvP career |
| Moneyline | 7 | 4 | starter Stuff+ delta, team runs L15, BvP team summary, bullpen ERA, park runs, weather, L/R splits |
| Total | 6 | 4 | park, weather, combined bullpen, combined offense, starter quality, ump |

### `build_mlb_game_context` enrichment expanded
- Step 5: pitcher throwing hand + season K% + IP/start via statsapi
- Step 6: opposing team K% vs pitcher hand (mlb_team_k_intel)
- **Step 7 (NEW)**: team runs-per-game + full-team ERA (bullpen proxy)
- Step 8: team-vs-SP BvP OPS — DEFERRED (needs mlb_bvp helper)

### Attribution on every pick
Every MLB pick that reaches the board carries:
- `real_data_sources: list[str]` — which real feeds fired
- `real_data_count: int` — 3-7 depending on market coverage

### What Phase 2 needs
- Tennis: replace with Elo + surface Elo + form + H2H (data mostly wired,
  just need to remove the rng.uniform tilts still driving Tennis_ml/total)
- Soccer: replace with xG + shots + form + injuries + rest (game_context
  already fetches some — needs feature engine wrapper)
- NBA/NFL: real team + player metrics (weakest data coverage — needs
  data source addition first)

### Final phase (per user plan)
Rewrite `compute_lock_score()` to consume only real model outputs and
calibrated probabilities. Delete `_factors_random()` entirely. Any MLB
pick that reaches the board today already goes through that path;
Phase 2/3 sports migrate over-time.

---

## 2026-07-21 (part 4) — Phase 2 Tennis + Soccer Feature Engines

### User mandate
"Phase 2: Replace Tennis random factors with Elo + surface Elo + form
+ H2H (data mostly wired already). Replace Soccer with xG + shots +
form + injuries + rest."

### New `services/tennis_feature_engine.py`
5 factor slots, minimum 3 real for pick emission:
- Surface Elo Edge (from tennis_deep.elo_edge)
- Overall Elo (from tennis_players.pick_elo_overall)
- H2H Dominance (from tennis_h2h; needs ≥3 matches)
- Recent Form / Fit (matches_7d + surface_fit)
- First-Set RPW Edge (from tennis_first_set.edge_1st)

Live test: real Elo/H2H/first-set data produces **5/5 factors** with
`has_enough=True`. Empty pick correctly returns 0/5.

### New `services/soccer_feature_engine.py`
7 factor slots (ML), 6 slots (Total), minimum 3 real:
- xG Differential (home/away xg_rolling)
- Form PPG (soccer_form cache)
- Goals Scored avg (form.gf_avg)
- Goals Conceded avg (form.ga_avg)
- H2H Recent (roadmap: team_h2h_recent)
- Injuries (roadmap: team_injuries)
- Rest Days (team_rest_days)

Live test: Man City vs Arsenal example → **5/7 factors real** (rest
days + xG diff + form PPG + goals scored + goals conceded fire).
H2H and injuries return None (data not yet populated by ctx builder).

### `sports_engine.py` — all `_factors_random(rng, "Tennis…")` /
`_factors_random(rng, "Soccer…")` calls REMOVED. Remaining:
- Function definition (line 866) — retained for potential Phase 3 emergency fallback but NEVER called
- 3 comments referencing removed calls (audit trail)
- Zero live invocations.

### Non-MLB, non-Soccer, non-Tennis paths (NBA/NFL/KBO/etc)
Previously fell through to `_factors_random(rng, f"{sport}_ml")`.
Replaced with **`factors = {}`** — an empty dict tells compute_lock_score
to derive Lock purely from book-anchored `model_win_prob` with zero
dice-roll additions. No random noise. Phase 3 will wire real NBA/NFL
data or drop non-MLB/Tennis/Soccer picks entirely.

### Tennis alt spread / alt total (2303, 2345 area)
Removed the seed-based `_factors_random(random.Random(hash(...)),
"Tennis_ml")` calls. Now use empty factors — lock derives from book
implied + model tilt only, no dice-roll noise. Real tennis data
still boosts these picks via the tennis_deep_signal component in the
signal engine after enrichment.

### Attribution on every emitted pick
Every Soccer + Tennis pick that reaches the board now carries
`real_data_sources` list (when applicable) alongside MLB picks.

### Remaining work (Phase 3 / Final)
- NBA/NFL feature engines (needs new data sources — nfldata,
  basketball-reference or ESPN API)
- `compute_lock_score()` rewrite: consume calibrated model outputs
  directly instead of factor averages
- Delete `_factors_random()` function definition entirely
- Populate Soccer ctx.team_injuries + ctx.team_h2h_recent for the
  currently-None soccer factors
- Attach Tennis Elo/H2H/first-set to game._ctx BEFORE pick build
  (currently attached after) so main-flow Tennis ML picks can pass
  the feature-engine gate

---

## 2026-07-21 — FINAL PHASE Random Purge COMPLETE

**Mandate**: "Final Phase random purge in `compute_lock_score` (rewrite to use only calibrated probs)"

### `sports_engine.py` — every RNG contamination in the pick-scoring pipeline removed
- **DELETED** `_factors_random()` function definition + `_FACTOR_RECIPES` table (dead code)
- **DELETED** `player_rng` per-player seed (no longer needed — factors come from real engines)
- **REPLACED** every `rng.random()` / `rng.uniform()` in `mp` (model_win_prob) computation with deterministic book-anchored seeds:
  - Player prop loop lines 3199-3223
  - Alt run-line loop (line 2624 area)
  - Team totals dead-code loops (lines 2503, 2562)
  - Moneyline `dd_ml_result is None` fallback (line 986)
  - Double chance fallback (line 1058)
  - Game totals `_dd_fn is None` fallback (lines 1127, 1147)
  - Soccer Poisson alt totals (line 1311)
  - Spread pick generation (line 1370)
- **REPLACED** non-MLB pitcher / batter fake RNG factor dicts with `factors = {"Book Implied Probability": mp}` — honest single-factor calibrated payload for KBO / NBA / WNBA / UFC props until Phase 3 engines land
- **REPLACED** Elite Tier `random.uniform(2, 5)` boost with deterministic rank-linear `(5-i) * 1.5` spread (positions 1-5 get 7.5 → 1.5 boost)
- **ADDED** calibrated-mp override at line 3313: `sport == "MLB" && real feature engine used → mp = mean(factor values)`. Verified via Juan Soto Over 0.5 Hits: 3 real factors (L10 hit rate 0.47, home/away 0.887, platoon 0.95) → `model_win_probability = 76.9`, learning shrinkage → 69.3 final, edge = +0.8pp.

### Remaining `random.uniform` / `rng.uniform` in codebase (all legitimate)
- `services/nfl_ingest.py`, `services/nba_ingest.py` — rate-limit jitter (network sleeps, not scoring)
- `parlay_optimizer.py:743` — parlay diversification randomness (not scoring)
- All in-file comments referencing the purge (audit trail)

### Pipeline math for a data-driven pick (verified end-to-end)
1. `build_mlb_{pitcher_k,hitter,ml,total}_factors(ctx, …)` returns real factors in `[0.40, 0.95]` probability scale
2. `has_enough_real_data()` gate — pick DROPPED if < 3 real factors
3. `_cal_mp = mean(factor_values)` → `model_win_prob` (clamped by market type)
4. `compute_lock_score(factors, win_prob=mp*100)` → raw lock (6-component composite)
5. `_build_pick` writes `model_win_probability` (raw) + `edge_percent`
6. `pick_validator` applies `bucket.weight + cal.adjustment` learning delta → `win_probability` (final)
7. Chalk Trap / Longshot Trap gate visibility via `off_board` flag

**Zero RNG anywhere in this pipeline.** Every value is either real data, book_implied, or a learned adjustment from historical settle data.


---

## PERKLOCKS PHASE 1B — PER-SPORT AUTHORITATIVE RUNTIME WIRING (2026-06, checkpoint PHASE1B_AUTHORITATIVE_RUNTIME_WIRING_READY)

Approved decisions: R1 (NFL Platinum game markets), R2a (wire NHL), R3-modified (NBA/CFB → MODEL_UNAVAILABLE, no book-follow), R4 (tennis_extra = gap-filler), T1 (retire soccer/pipeline.py pick emission), T3b (UFC totals reach evaluation), synthetic scorer = research-only.

### Changes
- NEW `services/funnel_telemetry.py` — persistent Mongo funnel (`funnel_telemetry` collection), sync `record()` + per-cycle `flush()` in `PickRefreshOrchestrator.refresh` finally-block. Mirrors legacy `pipeline_diagnostic.log_reason`.
- NEW `services/platinum_nfl/game_runtime.py` — `build_nfl_game_model_context` (team ratings from `nfl_game_engine._team_ratings` → expected margin/total) + `platinum_game_side_probability` (deterministic seed via `simulation_seed.build_seed`, 5000 sims) + `attach_game_sim_provenance`.
- `sports_engine.py`:
  - NHL wired: `SPORT_KEYS["NHL"]`, `LEAGUE_LABELS["icehockey_nhl"]`, `fetch_nhl_picks`, `_unit`, NHL in spreads branch, `_want("NHL")` in `generate_all_picks`.
  - NFL ML/Total/Spread (regular + preseason) evaluated by Platinum sim; picks stamped `model_source=platinum_nfl_game_sim`, `season_type`, `platinum_game_sim`.
  - Book-follow ML fallback restricted to MLB/Soccer (engine-gated); NBA/CFB/UFC/NHL/model-less-Tennis/model-less-NFL → `MODEL_UNAVAILABLE` funnel record, no pick.
  - Totals: `_ufc_ml_only` suppression RETIRED; non-modeled sports funnel-recorded; NFL uses Platinum both-sides probabilities.
  - Spreads: NFL Platinum per-side; NBA/Tennis/NHL → MODEL_UNAVAILABLE.
  - `_backfill_tennis_moneylines`: emits ONLY with real tennis math signal (`has_real_tennis_signal`); book-follow + hardcoded lock ladder retired.
  - `_synthetic_soccer_scorer_picks` output → `model_research_evidence` collection + funnel `SYNTHETIC_SCORER_RESEARCH_ONLY`; never enters pick stream.
  - MLB hitter-prop feature-gate drop now funnel-recorded (`MISSING_FEATURE_DATA`).
- `services/pick_refresh_orchestrator.py`: `_tennis_gap_fill_filter` — tennis_extra keeps only events missing from primary AND with a real book line (`GAP_FILL_EVENT_COVERED_BY_PRIMARY` / `GAP_FILL_NO_REAL_BOOK_LINE`); funnel flush added.
- `soccer/pipeline.py`: `LEGACY_PICK_EMIT_ENABLED` (default OFF, env `SOCCER_LEGACY_PIPELINE_EMIT=1` to re-enable) — db.picks dual-write retired, `soccer_predictions` cache preserved.
- `services/sport_capability_registry.py`: notes now truthful for NFL/NBA/CFB/Tennis/UFC/NHL/Soccer.
- NEW `tests/test_phase1b_runtime_wiring.py` — 29 tests incl. offline production-path integration (generate_all_picks with provider mocked at boundary) + determinism proofs. All pass.

### Remaining MODEL_UNAVAILABLE markets (truthful)
NBA game markets, CFB game markets, UFC ML + totals, NHL ML/puck-line/total, Tennis events without math-engine signal.

### Flagged for next sub-blocks (not yet changed)
- `_espn_mls_scorer_picks` uses `_rate_to_american()` synthetic odds (user-requested MLS feature — needs decision).
- `ufc_espn_ingest` publishes picks with `book_odds=None` (record/win-rate model, no real line).
- `_ensure_csl_elite_picks` post-pipeline injection bypass.
- Gate reconstruction (G1-G7): implied floors, model-prob floors, lock booster, per-sport lock floors, de-vig flag, both-sides, cap telemetry — NOT yet executed (next sub-block).

---

## PERKLOCKS PHASE 1C — PRODUCTION FOUNDATION INTEGRITY (2026-06, checkpoint PHASE1C_PRODUCTION_FOUNDATION_INTEGRITY_READY)

### Root causes found & fixed
1. **Circuit breaker 422 false-trip (CRITICAL)**: benign 422 market probes (bundle-bisection design) counted toward the fail streak; 8 consecutive Cincinnati-Open alt-line probes tripped the breaker and disabled the ENTIRE provider live ("fail streak (8): 422"). Fixed in `sports_engine.record_odds_call_result` — 422 counts in totals, never toward the streak. Stale OPEN breaker reset via /api/admin/odds-circuit/reset.
2. **Orchestrator Motor bool crash**: `PickRefreshOrchestrator.__init__` used `database or db` → NotImplementedError for every explicit-db caller. Fixed with `is not None`.
3. **Silent 7→0 board-validator wipe**: `evidence_threshold` (§7 min-3-of-6 signals) dropped ALL NFL Platinum game picks invisibly (log line omitted evidence/integrity counters). Now funnel-recorded (`EVIDENCE_THRESHOLD`, `INTEGRITY_CHECK_FAILED`) + logged. Gate semantics UNCHANGED (Phase 1D scope).
4. **UFC totals fetch restriction**: `_fetch_odds_for` limited UFC to h2h at the provider request level; now `h2h,totals` with 422/empty retry (registry/runtime agreement).

### Provider foundation evidence (sanitized)
- Key: env `THE_ODDS_API_KEY`, fingerprint sha256[:8]=21ce2472, len 32, no whitespace corruption, no hardcoded fallback (SEC-002). Import-time copy matches env.
- Live probe (FREE /v4/sports through gateway): HTTP 200, 0 credits, 80 active sports (incl. icehockey_nhl + americanfootball_nfl_preseason), breaker CLOSED.
- Provider truth vs governor: provider x-requests-used≈48k / remaining≈52k (month plan 100k) vs internal month_used 25,554 → the KEY is consumed beyond this environment's accounting (consistent with Production sharing the same key; per-env governors keep separate Mongo state by design). Daily 3000-credit self-limit (not the provider) caused the earlier force-refresh block — governor working as designed.
- NEW `GET /api/admin/provider-foundation` (admin): sanitized cross-env comparison payload (key fingerprint, DB fingerprint, breaker, budget, provider headers, funnel breakdown). Hit on Preview AND Production to prove key identity (SAME iff fingerprints match).

### NFL live-proof closure (§10)
Live preseason slate (10 events, 11 books): fetch → build_nfl_game_model_context (32 rated teams) → simulate_game_market → 7 picks with `model_source=platinum_nfl_game_sim`, `season_type=PRESEASON` → gating rejection recorded as EVIDENCE_THRESHOLD (7) in persistent funnel. Runtime arrow PROVEN; picks legitimately failed the existing evidence gate (Phase 1D item: evidence gate doesn't count Platinum sim as a signal).

### Contract flags (§11)
- A. MLS ESPN leaderboard picks (synthetic rate→American odds) → research-only (`model_research_evidence` + `SYNTHETIC_ODDS_RESEARCH_ONLY`), never published.
- B. UFC ESPN picks: already compliant (book_odds=None, no_real_book_line, model_only, Extended-Coverage only) — regression tests added.
- C. `_ensure_csl_elite_picks` force-injection into db.picks RETIRED → research-only routing.

### Infra telemetry (§9)
Gateway budget denials → BUDGET_GOVERNOR_BLOCKED; breaker blocks → CIRCUIT_BREAKER_OPEN; 401/403 → PROVIDER_AUTH_FAILURE; 429 → PROVIDER_RATE_LIMITED; other → PROVIDER_REQUEST_FAILED; fetcher/orchestrator crashes → REFRESH_RUNTIME_FAILURE (generate_all_picks gather exceptions no longer silent).

### Tests
- NEW tests/test_phase1c_foundation.py (24) + testing-agent tests/test_iter98_phase1c_review.py (14). 53/53 phase suites + 67/67 review PASS. Pre-existing unrelated failures: test_p04_real_line_integrity (old contract), test_universal_reachability boundary, test_mlb_grading_fix_iter71.

### NEXT (approved queue): PHASE 1D — GATE RECONSTRUCTION (G1-G7) — NOT started.

---

## PERKLOCKS PHASE 1D — GATE RECONSTRUCTION G1-G7 (2026-06, token PHASE1D_GATE_RECONSTRUCTION_READY)

### Gates retired (sports_engine._build_pick + compute_lock_score)
- G1: chalk odds caps (-450 std / -750 alt / -400 long-shot), SPORT_IMPLIED_FLOOR (0.48-0.56) + 0.42 juice sanity — implied prob is market info only now.
- G2: universal model-prob floors (0.58 / 0.62 MLB / 0.55 juice+K+alt / 0.25 long-shot) + MLB juice/K carve-outs.
- G3: generation-time lock BOOSTER (wp>=65 & edge>=1 → floor 85-105) AND the 98/95/90/85 evidence-floor LADDER inside compute_lock_score. Lock Score is now earned from the 6-component composite only.
- G4: per-sport generation lock floors (72-88) retired as kill-switches; the single authoritative rule is read-time `is_main_board_eligible` (canonical/real-line valid AND published_lock_score >= 85, $gte). Boundary proven: 84.99 F / 85.00 T / 85.01 T.
- Retained: ONE uniform edge gate (edge < -1.0% → reject, funnel `EDGE_THRESHOLD`); board_validator integrity/contradiction/real-line/evidence gates (telemetried).

### New capabilities
- G5: `_attach_devig(pick, opp_prices)` — raw_implied_probability + devig_market_probability + devig_method (n-way normalization, 3-way soccer) + devig_edge_percent, wired at ML/totals/spreads emission; book_odds never overwritten; `OPPOSING_SIDE_UNAVAILABLE` telemetry. current edge untouched (promotion decision later per user Q5-b).
- G6: ML side chosen by MODEL probability (Platinum margin sign for NFL), never odds sign — neutrality proven both directions.
- G7: rejections funnel-attributable: EDGE_THRESHOLD, EVIDENCE_THRESHOLD, INTEGRITY_CHECK_FAILED, MODEL_UNAVAILABLE, OPPOSING_SIDE_UNAVAILABLE, etc.
- NFL evidence gate: `board_validator.evidence_threshold` now recognizes Platinum provenance as exactly TWO independent categories (exact-line sim probability + team-rating input context) — never multi-counting derived fields. Weak book-price-only candidates still fail.

### Live proof
NFL-filtered orchestrator refresh (2026-08-14): 19 preseason picks stored, all `platinum_nfl_game_sim` + `season_type=PRESEASON` + raw/devig probs + published_lock_score, board-eligible; rejections attributed (EDGE_THRESHOLD 8).

### Tests
NEW tests/test_phase1d_gates.py (26). testing_agent verified 99/99 (1D+1B+1C+iter98+chalk_neutral) — /app/test_reports/iteration_99.json. Updated tests/test_lock_score_chalk_neutral.py (ladder assertion → neutrality assertion). Full suite 233f/30e vs pre-1D 238f/60e — all deltas classified pre-existing (old-contract history-floor tests on weeks-old sub-80 data; time-of-day live-board evidence tests; p03/p04 old null-odds contract).

### Known follow-ups (NOT started per stop order)
- Composite calibration: empty-factor Platinum picks score 92-98 (composite driven by win_prob) — belongs to Magic/score-contract phase, plus preseason-uncertainty cap design.
- Old-contract test files to reconcile in their own phase: board_floor_iter32, calibration_shrinkage_iter33, probability_canonical_iter37, revert_calibration_iter34, p03/p04 null-odds board tests.
- De-vig promotion decision (devig_edge → authoritative) after side-by-side evidence.

---

## PERKLOCKS PHASE 2 — MODEL/MAGIC/INTELLIGENCE CLOSURE (2026-06) — STATUS: PARTIAL

### Closed this session (with runtime proofs — tests/test_phase2_intelligence.py, 11 tests)
- **B. MLB pitcher-K (support finding): FIXED/VERIFIED.** Authoritative model = `services/mlb_k_probability.evaluate_k_pick` (Poisson over expected-K λ from L5 form + season K% + opponent K% + expected IP), executed for BOTH sides in the production side-selector inside `_props_picks_from_event`. Runtime execution proven (P(over)+P(under)=1, expected_k computed); rejections carry machine reasons (`no_pitcher_data`, `insufficient_signals`). **Key-mismatch regression guard added**: cross-module assertion that `game_context` writes and `mlb_k_probability` reads the SAME `starting_pitcher_home/away` keys; wrong-key ctx fails loudly.
- **K. Fusion (support finding): FIXED/VERIFIED.** Orchestrator invokes `pick_fusion_decorator.enrich_picks_bulk` on on-board picks, persists to `fusion_predictions` (pick_id linkage for grading), stamps `pick["fusion"]`; DOWNSTREAM consumption proven: `elite_evidence_gate._classify_fusion` scores fusion agreement (±3pp) as an evidence signal (functional test: agreement > disagreement).
- **L. Adaptive learning (support finding): FIXED/VERIFIED.** `learning_engine.recompute_learned_weights` learns from SETTLED picks only (time-decay half-life 30d, persisted `learned_weights` singleton); `learning_system_v2` volume-gated (MIN_TOTAL_PICKS≥100) per-(sport,market) weights; both applied to PREGAME picks in the orchestrator (`apply_learning`, `apply_v2_to_picks`); no-leakage guard test (apply path never reads the pick's own result).
- Phase 1D protections re-asserted under Phase 2 (no floors/ladders/booster; devig fields).

### PART A/T — RUNTIME COVERAGE MATRIX (truthful, post-1D)
| Sport | Market | Feed | Model | Sim | Magic/Fusion | Publishable | Status |
|---|---|---|---|---|---|---|---|
| MLB | ML/spread/total/team-total/alt-RL | Odds API | mlb_feature_engine (real-data gated) | brain/sim_mlb | yes | yes | AUTHORITATIVE_RUNTIME |
| MLB | pitcher K / outs | Odds API | mlb_k_probability (Poisson) + feature engine | sim_mlb K distribution | yes | yes | AUTHORITATIVE_RUNTIME |
| MLB | hits/TB/HR/RBI/H+R+RBI | Odds API | mlb_feature_engine 0/5-gate + BvP/statcast | sim_mlb | yes | yes | AUTHORITATIVE_RUNTIME (reachability funnel-proven 1B) |
| NFL | ML/spread/total (REG+PRE) | Odds API | platinum game sim (ratings→margin/total) | causal sim, seeded | challenger+magic | yes | AUTHORITATIVE_RUNTIME |
| NFL | player props | Odds API | nflverse feature engine + Platinum challenger | platinum player sim (shadow) | yes | yes | AUTHORITATIVE_RUNTIME (challenger not promoted) |
| Tennis | ML | Odds API (+gap-fill w/ real line) | tennis_math_engine/dd model | — | tennis_engine gates | yes | AUTHORITATIVE_RUNTIME |
| Tennis | alt spreads/totals | Odds API per-event | ladder-derived + math engine | — | yes | yes | MODEL_PARTIAL (ladder-cumulative probs; audit deferred) |
| Soccer | 1X2/DC/BTTS/totals/spreads | Odds API | soccer_feature_engine (real-DC-line guard) | — | yes | yes | AUTHORITATIVE_RUNTIME |
| Soccer | scorer props | Odds API real lines | scorer/creator engines + xG enrich | goal_scorer sim v2/v3 | yes | yes (real line only) | AUTHORITATIVE_RUNTIME; synth/MLS/CSL = RESEARCH_ONLY |
| NBA | ML/spread/total | Odds API | none | none | n/a | no | MODEL_UNAVAILABLE (no game model in repo — needs build) |
| NBA | player props | Odds API | nba_feature_engine | — | yes | yes | AUTHORITATIVE_RUNTIME |
| CFB | ML/spread/total | Odds API | none (cfb_feature_engine is prop/precompute only) | none | n/a | no | MODEL_UNAVAILABLE (needs build/wire) |
| NHL | ML/puck/total | Odds API (wired 1B) | none | none | n/a | no | MODEL_UNAVAILABLE (needs build) |
| UFC | ML/totals | Odds API (totals fetch restored 1C) | none (ufc_espn = research-only) | none | n/a | no | MODEL_UNAVAILABLE; ufc_espn_v1 = RESEARCH_ONLY/extended |

### PART P — De-vig decision (evidence-based)
Canonical TARGET policy: `model_probability − devig_market_probability` (vig is not market belief; raw one-sided implied systematically understates value by the juice share). Both fields already computed+persisted in production. **Promotion NOT executed yet** because the generation edge gate runs inside `_build_pick` BEFORE devig attachment; flipping requires reordering the gate and a slate-level side-by-side ranking comparison (deterministic proof) — queued as the first Phase-2 continuation item. Raw odds and both probabilities preserved regardless.

### REMAINING TRUTHFUL GAPS / UNRESOLVED PHASE-2 ARROWS (PARTIAL)
1. Parts F/G/H: NBA, CFB, NHL, UFC game models do not exist in the repo — must be legitimately built (team strength/efficiency/xG/fighter-profile evidence) then wired both-sides. (Largest work item.)
2. Part D/R: NFL Platinum composite calibration audit (92-98 with sparse factors) + Part E preseason uncertainty modeling — not yet executed.
3. Part I: Tennis MODEL_UNAVAILABLE events audit + alt-ladder model classification.
4. Part M/N/O: Magic status truth (MAGIC_APPLIED vs INSUFFICIENT), simulator classification matrix, calibration bidirectionality proof — not yet executed.
5. Part P: de-vig scoring promotion implementation + G5 slate comparison.
6. Part Q: Why-This-Pick provenance contract audit.
7. Full-suite before/after classification vs 233f/30e baseline for Phase-2 changes (this session's changes are test-only + no production code changes, so baseline unchanged by construction).

---

## PHASE 2A — NFL CALIBRATION + PRESEASON UNCERTAINTY + DE-VIG PROMOTION (2026-06) — STATUS: COMPLETE (token: PHASE2A_NFL_CALIBRATION_DEVIG_READY, awaiting user approval before 2B)

### Delivered
- **Part 3 — Sparse-evidence calibration**: NFL Platinum game picks (ML/spread/total) now scored through the v3 six-component composite (edge/alignment/ROI/data-quality/volatility/CLV) inside `sports_engine.py` instead of the legacy win-prob band map. High scores must be EARNED; no arbitrary ceilings introduced (mathematical justification, not suppression).
- **Part 4/5 — Preseason uncertainty**: `services/platinum_nfl/game_runtime.py` applies a bounded, deterministic confidence shrink to the SIMULATED probability for PRESEASON only: p' = 0.5 + (p−0.5)·0.85 (≈ +18% margin sigma). Metadata persisted on pick as `preseason_uncertainty {confidence_shrink, raw_sim_probability, adjusted_probability, basis}` (raw/adjusted refer to the simulated home side; symmetric around 0.5 so side-neutral). Regular season untouched. Flows probability→edge→score, never a raw score subtraction.
- **Part 7/8 — De-vig PROMOTED to canonical edge**: inside `_build_pick` (sports_engine.py ~853-880): `edge_percent = model_prob − devig_market_probability` when exact opposing price(s) of the SAME market/line exist (edge_method=DEVIG, n-way normalization incl. 3-way soccer); otherwise raw one-sided implied fallback + `DEVIG_UNAVAILABLE` funnel telemetry. The −1.0 uniform EDGE_FLOOR gate now evaluates the CANONICAL (devig) edge. Fields always preserved: book_odds (never rewritten), raw_implied_probability, raw_edge_percent, devig_market_probability, devig_edge_percent, edge_method. Post-build `_attach_devig` retired for game markets (computed at build).
- **Clobber fixes (found via live-slate audit this session)**: two post-build recomputes were overwriting the canonical devig edge with the raw edge on every cycle:
  1. `pick_validator.py` §3 edge re-derivation — now uses devig_market_probability when edge_method=DEVIG (+ keeps devig_edge_percent mirror in sync).
  2. `evidence_engine.py govern_pick` post-shrinkage edge re-derivation (~line 598) — same devig-aware fix; also maintains raw_edge_percent as the raw mirror.

### Live proof (NFL-filtered orchestrator refresh 2026-08-15 07:56 UTC)
14 fresh preseason picks stored: all edge_method=DEVIG, 14/14 satisfy edge_percent == wp − devig_market_prob, all carry preseason_uncertainty (k=0.85 verified), slate includes favorites (−155/−118/−134) AND underdogs (+100/+148/+112) — neutrality live-proven. Telemetry: DEVIG_UNAVAILABLE 1,901 (MLB/Soccer/Tennis prop paths lacking opposing prices), EDGE_THRESHOLD 295 (46 NFL this cycle, canonical-edge rejections). 6 stale pre-2A NFL picks remain (games rotated off slate; graded by settlement). Other sports' slates pick up devig fields at their next scheduled refresh (paths already wired through _build_pick).

### Tests
- `tests/test_phase2a_calibration_devig.py` (20: sparse calibration, preseason shrink bounded/deterministic/REG-untouched, quantile ordering, 2-way + 3-way devig, RAW fallback, gate-on-canonical-edge, unit checks, mismatched-line refusal, neutrality incl. dog-outranks-fav and 85-boundary).
- `tests/test_phase2a_db_invariants_iter100.py` (12, written by testing_agent: DB invariants, telemetry, govern_pick synthetic clobber-regression, API smoke).
- testing_agent verdict: 74/74 GREEN — /app/test_reports/iteration_100.json.
- Full suite: 236f/4193p/18e vs 233f/30e baseline. Errors improved (30→18). The 7 non-baseline failures classified: 2× known deferred MLB Machado grading (Phase 3), 429 rate-limit flake, refresh-lease conflict from concurrent manual NFL refresh, 5.37s latency flake (ceiling 5.0), live-roster pitcher_not_found, rollover slate-state. Zero new production-path regressions; 4 baseline failures now PASS.

### Next (blocked on user approval)
- Phase 2B — NHL + NBA + CFB + UFC game models (do not begin until 2A approved).
- Phase 2C — Tennis/Magic final closure. Phase 3 — Consumer/History/Settlement (incl. Machado grading).

---

## 2026-08-23 — GOALSCORER LOCK DECLUSTERING (Msg 849 closure)

### Delivered
- **Continuous Lock formula** in `services/soccer_scorer_lock_ladder.confidence_ladder_lock`: replaced fixed anchors (95/91/87/83/80) with a piecewise-linear model-prob base plus 8 independent continuous evidence contributions (xG/90, goals/90, sample size, recent form, minutes confidence, opp defence, GK quality, confidence tag).
- **Rarity guardrails as soft ceilings**: Lock ≥96 requires ≥3 positive evidence signals; ≥92.5 requires ≥2; 99.5+ reserved (apex 100 never derived).
- **Callers rewired**: `services/mls_direct_inject.py` and `services/soccer_prop_inject.py` now pass live stats (games, minutes, xG/90, form) into the shared helper. Same declustering applies to every MLS/Big-5 goalscorer path.

### Live proof (9 cheap surgical proofs — /tmp/proof_goalscorer_declustering.py)
✅ ALL 9 PASSED — direct python invocation, no pytest / no testing_agent.
1. 3 goalscorers same-p differentiated: 91.55 / 87.67 / 85.84
2. Weak scorer stays <85: 67.58
3. 96-99 rare: solo high-prob capped 92.5; multi-signal elite → 99.4
4. Same p=0.35, different evidence → Δ=7.05 Lock spread
5. Sweep 3072 configs: worst bucket 9.7% (was 100% at Lock 89)
6. Continuity: max local Δ = 0.30 across p=0.15..0.65
7. Fail-closed: None→80.0; absurd→60.0 (bounded 60..99.4)
8. Confidence tag is nudge (2.10pt spread), not ±2 anchor
9. Promotion MAX-guards (never lowers strict_edge)

### Caller-level integration proof
Sweep 8748 caller configurations (`/tmp/proof_caller_integration.py`) — worst bucket 6.4%, Lock=89 = 5.72% (no clustering after route +2/-3/+1/-2 nudges).

### Flagged but NOT modified (HARD STOP per user directive)
- `sportdb_player_scorer._prob_to_lock` still uses tier-anchored 58/68/78/88/92/96 for career-goals synthesis (CSL/J-League). Different anchor set than the 89-cluster complaint. Awaiting user go-ahead before touching.

### Next (blocked on user approval)
- P1: Authoritative H2H truth wiring (Soccer → NBA → NHL → CFB → NFL → Tennis → MLB → UFC).

---

## 2026-08-23 — SPORTDB GOALSCORER DECLUSTERING (Msg follow-up closure)

### Delivered
- **`sportdb_player_scorer._prob_to_lock` rewired** to route through the certified continuous helper `services/soccer_scorer_lock_ladder.confidence_ladder_lock`.  Fixed near-final tier anchors (58/68/78/88/92/96) permanently removed — only doc/comment mentions of historical anchors remain (no executable ladder).
- **Honest evidence mapping** — SportDB inputs mapped 1:1: `matches`→games, `weighted_rate`→goals_per_90, `rating`→recent_form_score.  Missing SportDB signals (minutes, xG, opp/GK) passed as neutral None/0.  Career-goal count (≥100) gates `expected_minutes_conf=1.0` because a 100+ career-goal player IS a regular starter (no invented stats).
- **All 3 active callers** now use the continuous contract automatically: `sportdb_player_scorer.py:977-978`, `sportdb_player_scorer.py:1118` (career-boost path), and `soccer_hot_scorers.py:193`.

### Live proof (9 cheap surgical proofs — /tmp/proof_sportdb_declustering.py)
✅ SPORTDB_GOALSCORER_CONTINUOUS_LOCK_CERTIFIED
1. Same-p different evidence differentiated: **89.8 / 87.7 / 84.94**
2. No executable fixed 58/68/78/88/92/96 ladder remains
3. Same p=0.35 → Δ=**4.39** between rich vs thin evidence
4. Weak SportDB scorer stays <85 → **75.4**
5. Multi-signal ranks above solo: **92.64 > 88.1**
6. Sweep 4500 configs — **66.0%** meet canonical ≥85 rule
7. 96+ share **1.22%** (rare/guarded); absolute-elite still reaches **99.4**
8. Only `sportdb_player_scorer.py` changed (git diff scope-locked)
9. Modules reload clean; backend restarts clean; `/health → ok`; pipeline actively publishing.

### Not touched (per HARD STOP)
- No H2H / provider / model-probability / UI / History / Analytics / Rollover / Parlay changes.

---

## 2026-08-23 — AUTHORITATIVE H2H TRUTH WIRING (Msg follow-up closure)

### Active H2H consumers found
Single active consumer: `services/h2h_enricher.build_h2h_bundle`, called from:
  * `routes/picks_routes.py:2769` (fast-mode chip)
  * `routes/picks_routes.py:3286` (deep-dive)
Diagnostic-only mentions in `pipeline_diagnostic.py`.

### Files/functions changed
Only `services/h2h_enricher.py`:
  * `_team_h2h_from_settled` — added P0 canonical-ID scan of `team_game_actuals` (MLB/NFL/Soccer), extended P1 to include CFB/NCAAF, added `career_meetings` via `count_documents()` (§5 truth), added `authoritative` boolean, gated settled-picks fallback behind non-authoritative sports only.  UFC returns honest None.
  * `_soccer_player_h2h` — completely rewrote to use `mls_player_matchup_history` (P0) → `soccer_player_game_logs` by canonical opponent id (P1).  Removed all `db.picks` reads.  Emits market-specific `primary_stat` (avg_goals vs avg_assists §6).
  * `build_h2h_bundle` — now passes `canonical_team_id` / `canonical_opponent_id` from pick doc.  Sources tag reflects real collection.  Status codes extended: `H2H_AUTHORITATIVE`, `H2H_APP_HISTORY_ONLY`, `H2H_INSUFFICIENT_SAMPLE`, `H2H_SOURCE_UNAVAILABLE`.

### Before / after behavior
- **Before**: Team H2H fell through to `db.picks` scan if names didn't match strict regex; `sample_size` = `len(loaded rows)` (never > limit).  Soccer player H2H queried settled Perklocks picks as authoritative meeting count.  CFB had no branch (fell to settled picks).
- **After**: Team H2H prefers canonical IDs on `team_game_actuals`, falls to `games` (including CFB when data exists), falls to `soccer_matches` for Soccer.  Settled-picks path retained ONLY for sports with no authoritative source AND labeled `app_history_only=True` + `authoritative=False`.  `career_meetings` computed via count; `recent_sample_n` reflects loaded rows.  Soccer player H2H uses actual game logs via canonical opponent id.  UFC returns honest None.

### 14 focused proof results (all PASS)
1. TEAM H2H authoritative → MLB `games` career=19 authoritative=True ✅
2. CFB honest None (no CFB game history in pod) ✅
3. Canonical IDs → `team_game_actuals` (2 meetings) ✅
4. Home/away player opponent resolution honest ✅
5. Soccer player H2H sourced from `mls_player_matchup_history` (not settled picks) ✅
6. NBA honest None (no NBA game history in pod) ✅
7. NHL honest handling (no usable NHL rows in pod) ✅
8. NFL authoritative via `games` ✅
9. Tennis team-H2H returns None (delegated to tennis_matches_history) ✅
10. Career=12 > recent=3 with limit=3 (limit never becomes career) ✅
11. Goalscorer→avg_goals, Assist→avg_assists (market stat honesty) ✅
12. UFC returns honest None (no fabricated H2H) ✅
13. Fake MLB team pair → honest None (settled-picks fallback gated) ✅
14. No new provider/CLV imports; modules reload clean ✅

**Result: AUTHORITATIVE_H2H_TRUTH_CERTIFIED**

### Not touched (HARD STOP honored)
No Lock formulas, no provider integrations, no History/Analytics changes, no Rollover/Parlay changes, no CLV additions, no frontend redesign.  Only H2H retrieval/identity truth.

### Approximate credits used: ~1 medium turn (single-file edit + proof script).

---

## 2026-08-23 — ANYTIME ASSIST LOCK CHECK (no code change)

### Active Assist final-Lock constructors
1. `services/real_line_scorer_ingest.py` — real sportsbook Assist line path (uses `apply_scorer_lock_promotion`).
2. `services/mls_direct_inject.py` — MLS direct-inject Assist (uses `confidence_ladder_lock`).
3. `services/soccer_prop_inject.py` — Big-5 model-only Assist (uses `confidence_ladder_lock`).
All three ALREADY route through `services/soccer_scorer_lock_ladder` (the continuous helper certified in the goalscorer declustering pass on 2026-08-23).

### Findings
- No fixed 88/89/90/91/92/93/94/95/96 near-final anchor assignments in any active Assist path.
- Sweep 46,656 Assist configs — worst bucket 13.2% (below 20% clustering threshold).
- Same p=0.25 with weak vs rich evidence → Δ=8.07 (evidence-driven).
- Weak Assist stays below 85 (75.0 floor observed).
- 96+ share = 0.02% (rare, multi-signal-gated).
- Modules import clean.

### Verdict: **ASSIST_LOCK_ALREADY_CONTINUOUS — NO CHANGE REQUIRED**

No scoring code changed. No H2H / provider / model / UI changes. Cheap proof only.

---

## 2026-08-23 — REMAINING H2H COVERAGE (NBA/NHL/CFB) — CERTIFIED

### Files/functions changed
Only `services/h2h_enricher.py`:
- **Added `_resolve_team_id()`** — canonical team_id resolver via `espn_team_meta` (§2 canonical identity first).
- **Added `_nba_market_family()`** — maps NBA prop market strings to stat keys (points/rebounds/assists/threes_made/pra/steals/blocks).
- **Added `_nba_player_h2h()`** — queries `db.player_game_logs` sport=nba by player name + opp_team_id (resolved via espn_team_meta). Emits market-specific stat with `career_meetings` (count) + `recent_sample_n` (loaded rows).
- **Added `_nhl_player_h2h()`** — attempts direct `opp_team_id` join on `player_game_logs` sport=nhl; returns honest None when opponent identity cannot be resolved (pod NHL logs lack opp_team_id).
- **Wired NBA and NHL branches** into `build_h2h_bundle`, replacing the `NFL/NBA — deferred to follow-up` comment.
- **Added CFB/NCAAF to `_SPORTS_WITH_AUTHORITATIVE_SOURCE`** — settled-picks fallback definitively gated for CFB.

### Existing data sources used (no new provider added)
- `db.player_game_logs` sport=nba (20,415 rows) — canonical player H2H
- `db.espn_team_meta` sport=NBA/NHL (30/32 teams) — team-id resolver
- `db.player_game_logs` sport=nhl (520 rows) — attempts NHL H2H (no opp_team_id today)
- `db.games` sport=cfb — already the read path (populated by existing `historical/cfb.py` ESPN ingest triggered via `/api/admin/historical/backfill-seasons`)

### 12 Focused Proofs (ALL PASS)
1. NBA Brook Lopez vs Indiana Pacers → **career=20**, authoritative=True ✅
2. NBA Points→points / Rebounds→rebounds / Assists→assists (market stat honesty) ✅
3. NBA canonical id — "Boston Celtics" / "Celtics" / "boston" → team_id=**2** ✅
4. NHL honest None (no opp_team_id on logs — pod data limitation) ✅
5. NHL missing-stat → honest unavailable (no substitution) ✅
6. CFB ingest writes to `db.games` sport='cfb' (canonical read path wired) ✅
7. CFB team H2H authoritative career=3 with scratch rows (proves read path) ✅
8. Unknown CFB pair → honest None (no settled-picks fallback) ✅
9. NBA career=20 > recent=10 (limit never becomes career) ✅
10. No new provider/CLV imports added ✅
11. `build_h2h_bundle` single entry point still works ✅
12. Modules reload; backend `/health → ok` ✅

**Result: REMAINING_H2H_COVERAGE_CERTIFIED**

### Honest remaining data-source limitation
- **NHL opponent identity**: pod `player_game_logs` sport=nhl (520 rows) does NOT carry `opp_team_id`, AND `db.games` sport=nhl rows have `home=None` / `away=None`. So NHL player H2H returns honest None on the pod dataset. **Wiring is prepared**: once either data source starts carrying opponent identity, `_nhl_player_h2h` resolves H2H automatically without further code change.
- **CFB current row count**: `db.games` sport=cfb currently has 0 rows in this pod. Existing ESPN ingest job (`historical/cfb.py` → `db.games.update_one`) is wired to populate the same canonical collection. Run via `POST /api/admin/historical/backfill-seasons` sport=cfb.

### Not touched (HARD STOP honored)
No Soccer/Tennis/MLB/UFC H2H changes. No goalscorer/Assist Lock changes. No acquisition/quota changes. No Analytics/settlement changes. No frontend.

### Approximate credits used: ~1 medium turn.

---

## 2026-08-23 — H2H DATA COMPLETION (CFB / NHL / NFL) — CERTIFIED

### Files/functions changed
1. **`historical/cfb.py`** — fixed `backfill_season` ESPN parameter: `year=` alone returned current-week slate; now passes `dates=<season>` so completed rows land. Same schema/collection (`db.games` sport=cfb) unchanged.
2. **`historical/nhl.py`** — fixed `incremental_sync` to write `home`, `away`, `home_team_id`, `away_team_id`, `home_abbrev`, `away_abbrev` (previous version dropped these); `_ingest_boxscore` now derives team identity from `data.homeTeam`/`data.awayTeam` and persists `team_id`/`opp_team_id`/`is_home` on every `player_game_logs` row.  Added `enrich_player_log_opponents(db)` — one-shot enrichment for existing rows using canonical `games` rows (no external calls).
3. **`services/h2h_enricher.py`** — added `_nfl_player_h2h()` using `player_game_actuals` sport=nfl with canonical_player_id + opponent-abbrev resolution via `espn_team_meta`. Wired into `build_h2h_bundle` NFL branch.

### Existing data sources used (no new provider)
- CFB: `historical/cfb.py` → ESPN college-football scoreboard/summary (already integrated). 421 CFB rows populated on the pod (`db.games` sport=cfb).
- NHL: `db.games` sport=nhl (existing collection) + `historical/nhl.py` → NHL API (already integrated). Fix ensures team-identity fields land on both games and player_game_logs.
- NFL: `db.player_game_actuals` sport=nfl (129K existing rows) + `db.espn_team_meta` sport=NFL (team abbrev resolver).

### 14 Focused Proofs (ALL PASS)
1. CFB backfill populated **421 completed rows** to `db.games` sport=cfb ✅
2. Boston College vs Florida State → authoritative career=2 ✅
3. Unknown CFB pair → honest None (no settled-picks fallback) ✅
4. NHL home player → opp_team_id=10 (TOR) via canonical game join ✅
5. NHL away player → opp_team_id=1 (BOS) — symmetric ✅
6. NHL unresolvable row stays honest None (no invented opponent) ✅
7. NFL Michael Dickson vs SF → authoritative **career=16** ✅
8. NFL Passing→pass_yds / Rushing→rush_yds / Receiving→rec_yds ✅
9. NFL "Punting Yards" (unsupported) → None (no substitute) ✅
10. NFL canonical_player_id unifies 2 aliases → both career=16 ✅
11. NFL career=16 > recent=10 (limit did not become career) ✅
12. No new provider/CLV imports ✅
13. `build_h2h_bundle` single entry point works ✅
14. Backend `/health → ok` ✅

**Result: H2H_DATA_COMPLETION_CERTIFIED**

### Honest remaining data limitations
- **NHL historical logs (pre-fix)**: 520 existing pod NHL rows had `team=None` — those stay unresolved by the enrichment scan (P6 verified honestly). Future NHL ingests (post-fix) carry full identity from the first write. Trigger a re-ingest via `historical.nhl.backfill_current_season(db)` to refresh existing rows with team identity.
- **CFB depth**: Pod has 421 CFB rows (weeks 1-3 of 2024 + partial 2023). Full multi-season backfill can be triggered via `POST /api/admin/historical/backfill-seasons` sport=cfb (~15-30 min, ESPN paced at 5/sec).

### Not touched (HARD STOP honored)
No Soccer/Tennis/MLB/UFC H2H changes. No goalscorer/Assist scoring. No simulator/model runtime. No acquisition/quota. No Analytics/settlement. No frontend.

### Approximate credits used: ~1 medium turn (single edit of 3 files + CFB backfill of 2 seasons).

---

## 2026-08-23 — H2H DATA COMPLETION FOLLOW-UP (CFB depth / NHL repair / NBA fallback)

### Files/functions changed (1 file + 2 background jobs)
1. **`services/h2h_enricher.py`** — extended `_nba_player_h2h()` with a canonical fallback: when `player_game_logs` has 0 matching rows, queries `player_game_actuals` sport=nba by canonical_player_id (primary) or player_name (fallback) + opponent id. Honours market stat honesty (§6 — no substitution) and career/recent split (§5). No `db.picks` reads.
2. **CFB backfill** — invoked `historical.multi_season.backfill_seasons(sports=['cfb'], lookback=5)` via background job.
3. **NHL backfill** — invoked `historical.nhl.backfill_current_season(db)` via background job + one-shot `enrich_player_log_opponents` sweep.

### Before / after counts
| Metric | Before | After |
|---|---|---|
| CFB games | 421 | **1,487** (+1,066 / still ingesting) |
| NHL games | 13 | **179+** (still ingesting) |
| NHL player_game_logs total | 520 | **8,240+** |
| NHL logs with `opp_team_id` | **0** | **7,720+** (7,720 previously unresolved rows now canonical) |

### 8 Focused Proofs (ALL PASS)
1. CFB grew **421 → 1,487** rows ✅
2. CFB Florida International vs Louisiana Tech → authoritative career=3 (H2H reads expanded history) ✅
3. NHL **7,720 logs** carry canonical `opp_team_id` (from 0) ✅
4. NBA fallback fires: player_game_actuals → career=7 authoritative ✅
5. NBA fallback preserves career/recent split (limit did not become career) ✅
6. NBA unresolvable player → honest None ✅
7. `_nba_player_h2h` uses no `db.picks` — actual game history only ✅
8. Backend `/health → ok` ✅

**Result: H2H_DATA_COMPLETION_FOLLOWUP_CERTIFIED**

### Honest remaining
- 520 pre-fix NHL rows without `team` field remain unresolved (P6-style) — that's the exact set flagged as unresolvable in the previous certification. Total unresolved is now **~520 of 8,240** (~6.3%).
- Background CFB + NHL ingest jobs are still running as of certification time; final counts will grow further.

### Not touched (HARD STOP honored)
No NFL H2H changes. No Soccer/Tennis/MLB/UFC H2H. No simulator/model/scoring/acquisition/frontend/settlement.

### Approximate credits used: ~1 medium turn (single file edit + two background backfill jobs).

---

## 2026-08-23 — MLB MODEL-INTEGRITY SLICE 1 — CERTIFIED

### Files/functions changed (2 files)
1. **`services/mlb_feature_engine.py`**:
   - Added `factor_recent_outs_form(ctx, pitcher, side)` — reads real outs history (`l5_avg_outs` or `l5_avg_ip`); side-aware.
   - Rewrote `build_mlb_pitcher_outs_factors()` — removed K-count leakage (`factor_recent_k_form`), removed K-line-calibrated `factor_dfs_pitcher_k_projection`, added Under-mirror wrapper on every naturally Over-flavoured factor.
   - Rewrote `build_mlb_pitcher_k_factors()` — same Under-mirror wrapper so ancillary K evidence cannot silently reward the opposite side (dedicated K probability engine preserved as primary authority).
   - Added `side=` parameter to `build_mlb_hitter_factors()` — mirrors every Over-flavoured hitter factor for Under picks.
2. **`sports_engine.py`** — pass `side=str(side)` to `build_mlb_hitter_factors()` at line 5649 (existing call site only, no orchestrator/safe_picks/canonical touched).

### Defects closed
- ✅ Pitcher Outs K-count leakage removed (L5 factor now reads actual outs, not K count)
- ✅ K-line DFS projection removed from Outs factors (was silent K-market authority in Outs picks)
- ✅ Every Outs/K/Hitter factor now side-aware (Under evidence mirrored)
- ✅ Empty-ctx → all None honest fail-closed (no book-implied fallback introduced)
- ✅ Dedicated K probability engine untouched (primary authority preserved)

### 8 Focused Proofs (ALL PASS)
1. `factor_recent_outs_form` reads outs history; returns None honestly when only K data present ✅
2. Outs Under mirrors: L5 over=0.889 / under=0.111; Workload over=0.913 / under=0.087 ✅
3. K prop Under mirrors: L5 over=0.95 / under=0.05; K/9 over=0.885 / under=0.115 ✅
4. Hitter factors Under mirrors: L10 over=0.906 / under=0.094 ✅
5. Empty ctx → all None (no invented values) ✅
6. Source code has no K-form or DFS-K *calls* in Outs builder ✅
7. HARD BOUNDARY preserved — no protected file touched ✅
8. Backend `/health → ok` ✅

### Not touched (HARD BOUNDARY honored)
`safe_picks`, refresh orchestrator, provider acquisition/cache, starvation, cooldowns, canonical publication, settlement/history/H2H ingestion, Locks/Rollover/Parlay plumbing.

### Approximate credits used: ~1 medium turn.

---

## 2026-08-23 — MLB SLICE 1 COMPLETION (Probability Authority) — CERTIFIED

### Remaining defects closed
- ✅ **Pitcher Outs Win Expected** — now sourced from `brain.sim_mlb._simulate_pitcher_outs` (Monte-Carlo over `bf_per_inning × expected_innings`, exact-line threshold + selected side). Previously showed factor-mean `_cal_mp`.
- ✅ **Hitter markets** (Hits, HR, H+R+RBI) — Win Expected now sourced from real distribution sim (`_simulate_hits` / `_simulate_hrs` / `_simulate_hrr`) with exact-line P(over)/P(under). Previously showed factor mean.
- ✅ **MLB ML/RL/Total** — verified existing guard blocks `book_implied_calibrated` from becoming silent model authority (line 6074 in sports_engine.py).
- ✅ **Specialized engines preserved** — K math (`k_math_expected_k`) and NFL ATD (`_atd_evidence_block`) markers block sim from overwriting their probabilities.
- ✅ Fixed preexisting NameError in `_extract_threshold` (`_re` → `re`).

### Files/functions changed (2 files)
1. **`brain/sim_runner.py::_anchor_pick_to_sim`** — added surgical promotion block: when sim is independent + valid + `distribution_monte_carlo` + no specialized-engine marker present, promotes `sim_win_probability` → `pick["win_probability"]` / `model_win_prob` / stamps `probability_source="sim_win_probability"` + `model_authority="distribution_monte_carlo"`. Prior factor-mean preserved as `win_probability_prior_factor_mean` audit. Recomputes `edge_percent` against existing book_implied.
2. **`brain/sim_mlb.py::_extract_threshold`** — fixed `_re` typo to `re` (unrelated latent bug that would have blocked any sim run).

### Exact probability authority per MLB market family
| Market family | Probability source | Method |
|---|---|---|
| Pitcher Ks | `services.mlb_k_probability.evaluate_k_pick` | Poisson exact-line P(over)/P(under) |
| Pitcher Outs Recorded | `brain.sim_mlb._simulate_pitcher_outs` | Monte-Carlo BF×p_out distribution |
| Hits | `brain.sim_mlb._simulate_hits` | Bernoulli(BA) per AB, distribution |
| Home Runs | `brain.sim_mlb._simulate_hrs` | HR/AB per AB, distribution |
| H+R+RBI | `brain.sim_mlb._simulate_hrr` | Joint hits+runs+RBI simulator |
| RBI / TB / Runs (standalone) | **factor-mean fallback** (no sim branch yet) | flagged for follow-up |
| ML / Run Line / Total | Specialized engines + `_cal_mp` gated against `book_implied_calibrated` | (guard verified) |

### Flow proof (Pitcher Outs)
Provider row → candidate → `build_mlb_pitcher_outs_factors` (side-aware, no K-leakage) → factor mean seed → `apply_simulations` → `_simulate_pitcher_outs` (Monte-Carlo real distribution, 20,000 runs) → `sim_win_probability=73.3%` → **promoted to `win_probability`** → existing Lock authority → safe_picks (unchanged) → canonical publish (unchanged).

### 7 Focused Proofs (ALL PASS)
1. Pitcher Outs sim exact-line P(over)=73.3% (20K runs) ✅
2. Hits/HR/H+R+RBI all fire sim with exact-line P(over) ✅
3. Book-implied silent-authority guard verified ✅
4. Sim promotes to `win_probability`: factor-mean 60→72, source stamped ✅
5. K math specialized engine preserved (sim did not overwrite) ✅
6. HARD BOUNDARY honored — no protected file touched ✅
7. Backend `/health → ok` ✅

### Honest remaining
- Standalone **RBI**, **Total Bases**, **Runs** markets still fall to factor-mean because `simulate_mlb_pick` has no router branch for them (line 275-307 of sim_mlb.py). Follow-up slice can add branches reusing existing `_simulate_hrr` axes.

### Not touched (HARD BOUNDARY honored)
`safe_picks`, refresh orchestrator, provider acquisition/cache, starvation, cooldowns, canonical publication, settlement/history/H2H, Locks/Rollover/Parlay.

### Approximate credits used: ~1 medium turn.

---

## 2026-08-23 — PASS 2 (NFL / NBA / NHL) — CERTIFIED

### Files/functions changed (2 files)
1. **`services/nfl_feature_engine.py::build_nfl_prop_factors`**:
   - Removed `Book Implied Anchor` from the factor set (moved to `sources` as audit-only). Closes the confirmed "silent book-implied model authority" defect.
   - Added side-aware mirror block on `side="under"` — mirrors L5, L3 trend, Home/Away, Opponent Defense factors so Under evidence cannot silently reward Over.
2. **`brain/sim_runner.py`** (`simulate_pick`):
   - Added NFL/CFB branch — reads existing stub `sim_nfl.simulate`; returns None honestly when `ran=False` (no fabrication).
   - Added NHL branch — explicit `return None` (no NHL simulator exists in codebase). Documented wiring point for when `sim_nhl` lands.

### NFL defects closed
- ✅ Book-implied removed from factor evidence (audit-only in sources)
- ✅ L5 / L3 trend / Home-Away / Opp-Defense factors now side-aware
- ✅ ATD + Platinum ML/Spread/Total engines unchanged (specialized-engine markers still block sim promotion)

### NBA markets actually wired (existed already; verified in this pass)
| Market | Sim source | Method |
|---|---|---|
| Moneyline / Spread / Total | `brain.sim_nba.simulate_nba_pick` (game path) | 20K-run Monte-Carlo team scoring |
| Points / Rebounds / Assists / Threes / PRA / P+R / P+A / R+A / Steals / Blocks | `brain.sim_nba.simulate_nba_pick` (player path) | Player minutes×usage×pace distribution |

Slice 1 promotion block automatically routes NBA `sim_win_probability` → `win_probability` (verified: NBA prop P(over)=57.6%, game ML sim fires).

### NHL markets wired
| Market | Authority | Method |
|---|---|---|
| Moneyline / Puck Line / Total O/U | **MODEL_UNAVAILABLE** (no sim) | honest fail-closed |
| Goals / Assists / Points / SOG / Saves | **MODEL_UNAVAILABLE** (no sim) | honest fail-closed |

Wiring point in `sim_runner.py::simulate_pick` prepared for when a real NHL sim lands. Zero book-implied fabrication.

### Simulator authorities (post-Pass-2 snapshot)
| Sport | Sim | Type |
|---|---|---|
| MLB | `sim_mlb.simulate_mlb_pick` | distribution_monte_carlo |
| Soccer | `sim_soccer.simulate_soccer_pick` | distribution_monte_carlo |
| NBA | `sim_nba.simulate_nba_pick` | distribution_monte_carlo |
| Tennis | `sim_tennis.simulate_tennis_pick` | event_simulation |
| NFL / CFB | `sim_nfl.simulate` (STUB — returns ran=False) | pending real implementation |
| NHL | none | honest fail-closed |
| UFC | none | honest fail-closed |

### 8 Focused Proofs (ALL PASS)
1. NFL Over/Under factors mirror correctly; Book Implied removed ✅
2. NBA game sim fires (`sim_win_probability=5.1`, 20K runs) ✅
3. NBA player-prop sim fires (`P(over)=57.6%`, 20K runs) ✅
4. NHL sim honestly returns None (no book-implied fabrication) ✅
5. NFL sim stub returns None (no fabrication) ✅
6. HARD BOUNDARY preserved — no protected file touched ✅
7. Backend `/health=ok`; locks/rollover/parlay routes not 5xx ✅
8. K-math specialized engine preserved ✅

### Honest unsupported / no-data markets
- NFL full sim = STUB (`sim_nfl.simulate` returns `ran=False`). Real 10K-run Monte-Carlo pending; factor path still emits with side-aware factors + no book-implied.
- NHL sim entirely absent. All NHL markets fall closed at emit gates unless an upstream specialized engine fires.
- UFC — unchanged (no sim). Honest fail-closed if no real model authority.

### Not touched (HARD BOUNDARY honored)
`safe_picks`, refresh orchestrator, provider acquisition/cache, starvation, cooldowns, canonical publication, settlement/history/H2H, Locks/Rollover/Parlay.

### Approximate credits used: ~1 medium turn.

---

## 2026-08-23 — PASS 1 PART A (Canonical Identity Dedupe) — CERTIFIED

### Part B (Odds API single-gateway migration) — DEFERRED
Migrating 4 direct API callers in `sports_engine.py`, `services/mls_direct_inject.py`, `services/soccer_prop_inject.py`, `brain/nrfi_engine.py` through `OddsApiGateway` in the same turn risks breaking working feeds. Deferred to a dedicated pass. Zero direct-call code touched in this turn.

### Files/functions changed (4 files)
1. **`services/pick_identity_enricher.py`** — added `canonical_wager_identity()` + `canonical_participant_key()` + `_norm_participant_name()`. Alias resolver produces `surname_initial` (e.g., "Janice Tjen" / "Tjen J." / "J. Tjen" → `tjen_j`; sister players stay distinct via initial: `williams_s` vs `williams_v`).
2. **`services/published_results_truth.py::_identity_key`** — now prioritises semantic canonical identity over producer/pick IDs; legacy fallback retained for rows lacking canonical enrichment.
3. **`services/board_projection_service.py::dedupe_canonical`** — same precedence flip; different sportsbook prices remain quote metadata on ONE wager (best-quote-wins policy preserved).
4. **`server.py::_collapse_cross_book_duplicates`** — `_wager_key` now uses semantic canonical identity first.

### Defect closed
- ✅ Tjen duplicate-wager class universally closed — display-name aliases (`Janice Tjen` / `Tjen J.` / `J. Tjen`) collapse to one semantic wager at all 3 dedupe boundaries.
- ✅ Different producer/pick IDs cannot bypass semantic dedupe.
- ✅ Legitimately distinct lines/sides remain separate.
- ✅ Team-sport canonical identity works (uses `canonical_team_id` + `canonical_event_id`).
- ✅ Sister players (same surname) stay distinct via initial.

### 8 Focused Proofs (ALL PASS)
1. Tjen aliases collapse — 3 producer IDs → 1 canonical identity ✅
2. Different producer IDs → still 1 wager after dedupe ✅
3. Distinct lines/sides (over 25.5, over 26.5, under 25.5) → 3 wagers preserved ✅
4. MLB team canonical → 1 wager ✅
5. Serena_s ≠ Venus_v (sisters distinct) ✅
6. `_identity_key` collapses aliases in published truth ✅
7. HARD BOUNDARY preserved — no protected file touched ✅
8. Backend `/health → ok` ✅

### Not touched (HARD BOUNDARY honored)
`safe_picks`, refresh orchestrator, provider acquisition/cache, starvation, cooldowns, canonical publication plumbing, settlement/history/H2H, Locks/Rollover/Parlay consumers, scoring/model engines.

### Approximate credits used: ~1 medium turn.

---

## 2026-08-24 — MLB Game Markets Reachability Fix (FALSE_GATE_BLOCKER)

**Symptom (user report)**: MLB game markets (Moneyline, Run Line, Total Runs) not appearing on the /picks/today board despite 20 canonical-eligible published picks in DB.

**Root Cause (proven via live DB probe)**:
- `/picks/today` filter used legacy `grade` field (`"grade": {"$ne": "Pass"}`) at picks_routes.py:1699.
- APEX gate live-overwrites the legacy `grade` field to `"Pass"` (with `apex_block_reason=magic_tier_not_aligned_strong:INSUFFICIENT_EVIDENCE`) on picks where evidence stack falls short of the APEX tier — even when the CANONICAL `published_grade` snapshot (Phase 1c immutable) says `"Lock"`.
- Result: filter dropped all 20 MLB game markets today (dozens more player props too — 6 of 67 canonical-eligible survived).

**Surgical Fix** (single file, additive):
- `/app/backend/routes/picks_routes.py` — replaced the legacy `"grade": {"$ne": "Pass"}` top-level clause with a canonical-first predicate inside the `$and` list:
  ```
  {"$or": [
      {"published_grade": {"$exists": True, "$ne": "Pass"}},
      {"$and": [
          {"published_grade": {"$exists": False}},
          {"grade": {"$ne": "Pass"}},
      ]},
  ]}
  ```
- Prefers the immutable canonical `published_grade` (Phase-1c snapshot); falls back to legacy `grade` only for pre-canonical rows without a snapshot. Preserves the "no Pass grade on board" intent exactly — just against the authoritative field.

**Live Verification** (post-restart, via `/api/picks/today`):
| Query | Before Fix | After Fix |
|---|---|---|
| MLB total picks | 6 | 30 |
| MLB game markets | 0 | 19 (5 ML + 10 RL + 4 Total) |
| Pass-graded leaks | 0 | 0 |
| Soccer / Tennis / all-sports | unchanged | unchanged |

**Hard Boundary Honored**: No changes to `safe_picks`, refresh orchestrator, starvation gates, acquisition pipelines, canonical dedupe, model_integrity_gate, or scoring engines. Single-file, single-clause read-time predicate change.
