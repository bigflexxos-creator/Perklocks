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
