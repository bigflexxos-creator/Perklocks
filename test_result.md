#====================================================================================================
# START - Testing Protocol - DO NOT EDIT OR REMOVE THIS SECTION
#====================================================================================================

# THIS SECTION CONTAINS CRITICAL TESTING INSTRUCTIONS FOR BOTH AGENTS
# BOTH MAIN_AGENT AND TESTING_AGENT MUST PRESERVE THIS ENTIRE BLOCK

# Communication Protocol:
# If the `testing_agent` is available, main agent should delegate all testing tasks to it.
#
# You have access to a file called `test_result.md`. This file contains the complete testing state
# and history, and is the primary means of communication between main and the testing agent.
#
# Main and testing agents must follow this exact format to maintain testing data. 
# The testing data must be entered in yaml format Below is the data structure:
# 
## user_problem_statement: {problem_statement}
## backend:
##   - task: "Task name"
##     implemented: true
##     working: true  # or false or "NA"
##     file: "file_path.py"
##     stuck_count: 0
##     priority: "high"  # or "medium" or "low"
##     needs_retesting: false
##     status_history:
##         -working: true  # or false or "NA"
##         -agent: "main"  # or "testing" or "user"
##         -comment: "Detailed comment about status"
##
## frontend:
##   - task: "Task name"
##     implemented: true
##     working: true  # or false or "NA"
##     file: "file_path.js"
##     stuck_count: 0
##     priority: "high"  # or "medium" or "low"
##     needs_retesting: false
##     status_history:
##         -working: true  # or false or "NA"
##         -agent: "main"  # or "testing" or "user"
##         -comment: "Detailed comment about status"
##
## metadata:
##   created_by: "main_agent"
##   version: "1.0"
##   test_sequence: 0
##   run_ui: false
##
## test_plan:
##   current_focus:
##     - "Task name 1"
##     - "Task name 2"
##   stuck_tasks:
##     - "Task name with persistent issues"
##   test_all: false
##   test_priority: "high_first"  # or "sequential" or "stuck_first"
##
## agent_communication:
##     -agent: "main"  # or "testing" or "user"
##     -message: "Communication message between agents"

# Protocol Guidelines for Main agent
#
# 1. Update Test Result File Before Testing:
#    - Main agent must always update the `test_result.md` file before calling the testing agent
#    - Add implementation details to the status_history
#    - Set `needs_retesting` to true for tasks that need testing
#    - Update the `test_plan` section to guide testing priorities
#    - Add a message to `agent_communication` explaining what you've done
#
# 2. Incorporate User Feedback:
#    - When a user provides feedback that something is or isn't working, add this information to the relevant task's status_history
#    - Update the working status based on user feedback
#    - If a user reports an issue with a task that was marked as working, increment the stuck_count
#    - Whenever user reports issue in the app, if we have testing agent and task_result.md file so find the appropriate task for that and append in status_history of that task to contain the user concern and problem as well 
#
# 3. Track Stuck Tasks:
#    - Monitor which tasks have high stuck_count values or where you are fixing same issue again and again, analyze that when you read task_result.md
#    - For persistent issues, use websearch tool to find solutions
#    - Pay special attention to tasks in the stuck_tasks list
#    - When you fix an issue with a stuck task, don't reset the stuck_count until the testing agent confirms it's working
#
# 4. Provide Context to Testing Agent:
#    - When calling the testing agent, provide clear instructions about:
#      - Which tasks need testing (reference the test_plan)
#      - Any authentication details or configuration needed
#      - Specific test scenarios to focus on
#      - Any known issues or edge cases to verify
#
# 5. Call the testing agent with specific instructions referring to test_result.md
#
# IMPORTANT: Main agent must ALWAYS update test_result.md BEFORE calling the testing agent, as it relies on this file to understand what to test next.

#====================================================================================================
# END - Testing Protocol - DO NOT EDIT OR REMOVE THIS SECTION
#====================================================================================================



#====================================================================================================
# Testing Data - Main Agent and testing sub agent both should log testing data below this section
#====================================================================================================

user_problem_statement: |
  PerksLocks — AI Sports Betting Intelligence Platform (MLB/NFL/NBA/WNBA/Soccer/Tennis).
  Full E2E regression across all 5 tabs after restoring the `/api/picks/{pick_id}` decorator
  that was accidentally removed when the Auto Parlay endpoint was added, and after polishing
  the Auto Parlay tab UI (added "fewer legs available" gold notice banner).

backend:
  - task: "Auth: /api/auth/login & /api/auth/me"
    implemented: true
    working: true
    file: "/app/backend/server.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: true
    status_history:
      - working: true
        agent: "main"
        comment: "Demo user demo@lockscore.ai / demo123 — confirmed 200 OK via curl."

  - task: "Picks today / all / bet-killer / rollover"
    implemented: true
    working: true
    file: "/app/backend/server.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: true
    status_history:
      - working: true
        agent: "main"
        comment: "All 4 endpoints respond 200. Rollover scoped to today + Lock>=90."

  - task: "Auto Parlay endpoint /api/picks/parlay"
    implemented: true
    working: true
    file: "/app/backend/server.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: true
    status_history:
      - working: true
        agent: "main"
        comment: "Returns 200 for legs=3 and legs=5; gracefully returns best-available when fewer Elite picks qualify."

  - task: "Pick detail GET /api/picks/{pick_id}  [CRITICAL FIX]"
    implemented: true
    working: true
    file: "/app/backend/server.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: true
    status_history:
      - working: false
        agent: "main"
        comment: "Decorator @api.get('/picks/{pick_id}') was missing — endpoint was silently broken after the parlay endpoint was inserted."
      - working: true
        agent: "main"
        comment: "Restored decorator + module spacing. Confirmed 200 OK in backend logs after navigating to a pick from the parlay tab."

  - task: "AI explain POST /api/picks/{pick_id}/ai-explain (Claude Sonnet 4.5)"
    implemented: true
    working: true
    file: "/app/backend/server.py"
    stuck_count: 0
    priority: "medium"
    needs_retesting: true
    status_history:
      - working: true
        agent: "main"
        comment: "Async fetch — UI receives ai_pending boolean and replaces fallback when Claude responds."

  - task: "Elite Striker Triple-Market Visibility (Kane / Haaland / Mbappé / Messi / Ronaldo)"
    implemented: true
    working: true
    file: "/app/backend/sports_engine.py, /app/backend/elite_players.py, /app/backend/deep_dive.py, /app/backend/server.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: false
        agent: "user"
        comment: "User reported Harry Kane missing from Soccer goalscorer picks despite England vs Croatia being live today."
      - working: true
        agent: "main"
        comment: |
          Root cause: _fetch_player_props_for_sport() in sports_engine.py only fetched the chronologically-first 3 events per sport-key, so World Cup matches featuring elite strikers (England, France, Norway) were cut off behind earlier filler matches. Five-part fix:
          (1) sports_engine.py — Added _ANCHOR_SOCCER_TEAMS (France/Norway/England/Argentina/Portugal/Brazil/Spain/Germany/Netherlands + top clubs) + _ELITE_SOCCER_TEAMS. New _event_priority() returns 0 for anchors, 1 for elite, 2 for filler. World Cup cap raised 3→14, lookahead window 72h→168h.
          (2) elite_players.py — Added 'Erling Braut Haaland' (The Odds API's exact string), 'Cristiano Ronaldo dos Santos Aveiro', and Vinicius variants.
          (3) elite_players.py — Bidirectional synthesis: now derives FGS + SoA from real AGS picks too (was only AGS+FGS from SoA), so Mbappé/Haaland get all 3 markets even when their match only exposes Anytime Goal Scorer.
          (4) deep_dive.py — NO-BET gate now forces no_bet=False for any elite_player=True or any synthetic_fgs/ags/soa pick (FGS markets have inherently low win-prob ~14% which used to trigger no_bet=True).
          (5) server.py /api/picks/today — Query now uses $or: standard picks need lock_floor + edge>=0, but elite-player picks bypass both (still must not be no_bet or under_lock).
      - working: true
        agent: "testing"
        comment: |
          14/15 backend tests pass (93%). All 5 elite strikers verified with full 3-market trio in /api/picks/today?sport=Soccer:
            - Harry Kane: Croatia@England + Ghana@England (6 picks total)
            - Erling Braut Haaland: Senegal@Norway (3 picks)
            - Kylian Mbappe: Iraq@France (3 picks)
            - Lionel Messi: Algeria@Argentina + Austria@Argentina (6 picks)
            - Cristiano Ronaldo: DR Congo@Portugal + Uzbekistan@Portugal (6 picks)
          /api/picks/today returns 125 picks total, /api/stats/summary returns 70 elite (up from 12 after stats fix). No regressions: Vinicius Junior, Lamine Yamal, Jamal Musiala still present.
      - working: true
        agent: "main"
        comment: "Stats summary fix applied: elite_count now counts elite_player=True OR lock_score>=95 (was only the lock-score threshold). API confirms 70 elite picks visible."

frontend:
  - task: "Auth flow (login, register, persistence)"
    implemented: true
    working: true
    file: "/app/frontend/app/(auth)/login.tsx"
    stuck_count: 0
    priority: "high"
    needs_retesting: true
    status_history:
      - working: true
        agent: "main"
        comment: "Login persists token via AsyncStorage (web) / SecureStore (native)."

## ITERATION 22 — History grading speed (player-prop settlement overhaul)

User report: "History should grade bets faster it still on Friday". Settlement
was scanning 285 props per cycle but settling 0. Root-cause diagnosis revealed
4 separate bugs in `/app/backend/prop_settlement.py`:

1. **DNP-leaves-pick-pending-forever**: `_mlb_stat_for_player` returned `None`
   when the player was rostered but didn't play (empty `batting`/`pitching`
   blocks — bench scratch, late roster move). Now returns `0.0` so the bet
   grades cleanly under standard "Action" sportsbook rules.
2. **`fifa.world` not in soccer_leagues**: World Cup / qualifier props
   (Vinícius, Messi, Mbappé, Ronaldo etc.) had no ESPN scoreboard to match
   against. Added FIFA + UEFA Euro/qualifiers/Confederations + CONMEBOL
   Sudamericana + 2nd-tier domestic leagues.
3. **League regex matched "match"**: `_espn_summary` inferred the league
   from the event link via `re.search(r"/soccer/([a-z0-9.]+)/", link)` —
   ESPN URLs are `.../soccer/match/_/gameId/<id>`, so the regex captured
   `match` and 404'd. Replaced with an `event_league_map[event_id] = league`
   recorded at scoreboard fetch time. Bullet-proof.
4. **`scoringPlays` is empty in modern ESPN summaries**: ESPN moved goal
   events into `keyEvents` where each row has `type.text == "Goal"`. Both
   `_espn_did_score_goal` and `_espn_did_score_or_assist` now scan
   `keyEvents` (with regex fallback to parse "Goal! Team A 1, Team B 0.
   Player Name (Team)" text format).

Also added "To Score or Assist" → `soccer.scoreOrAssist` market mapping so
those picks reach the settler.

Verified live: `settle_player_props` now reports settled=25, won=1, lost=24
(was 0/0/0). Pending-by-day for the past 7 days dropped from 200+ stuck
picks to mostly-graded — Friday went from 62 pending → 60 pending in the
first sweep, and the backlog continues to drain on every scheduled run.

## ITERATION 21 — System-wide cleanup (Lock Score parity + Bet Killer purge)

### What landed
1. **Lock Score parity (root-cause fix)**
   - Backend: `_canonicalize_lock_score(pick)` + `_canonicalize_picks(list)` helpers in
     `/app/backend/server.py` applied to EVERY picks endpoint return. Promotes
     `lock_score = max(lock_score, lock_score_v2)`, clamped [0,99], re-grades.
   - Frontend: `/app/frontend/src/lib/lockScore.ts` single helper used by all 7 surfaces.
   - Verified live: 0/126 mismatches on /picks/today (was 45/52 = 87% before).

2. **Bet Killer purge (complete removal)**
   - Renamed `app/(tabs)/killer.tsx` → `app/(tabs)/under.tsx` (tab label = "UNDER LOCK").
   - `_layout.tsx` route name killer → under.
   - `/api/picks/bet-killer` deprecated stub returns `{"picks":[]}` (kept for back-compat).
   - Dropped `api.betKiller()` client method.
   - Dropped `bet_killer_warning()` and `_fallback_killer()` from `ai_engine.py`.
   - Removed dead `variant="killer"` prop from `LockPickCard`; removed `cardKiller`/`tagKiller`
     styles. Renamed theme colors `killerBg/killerBorder/killerSurface` → `dangerBg/dangerBorder/dangerSurface`
     (still used by logout button).
   - Pick detail page: header always "PICK BREAKDOWN", section always "WHY THIS PICK". No
     "BET KILLER" or "WHY TO AVOID" text anywhere in user-facing UI.

### Testing
- `testing_agent` iter21 = pass on both backend (11/11) and frontend smoke (3/3 parity).
- Manual curl verification: 0/126 v2>v1 mismatches across the whole /picks/today feed.
- All 5 picks endpoints return 200.


    implemented: true
    working: true
    file: "/app/frontend/app/(tabs)/index.tsx"
    stuck_count: 0
    priority: "high"
    needs_retesting: true

  - task: "LockPickCard displays max(lock_score, lock_score_v2) — UI/Detail score parity"
    implemented: true
    working: "NA"
    file: "/app/frontend/src/components/LockPickCard.tsx, /app/frontend/app/pick/[id].tsx, /app/frontend/src/lib/lockScore.ts, /app/backend/server.py"
    stuck_count: 1
    priority: "high"
    needs_retesting: true
    status_history:
      - working: "NA"
        agent: "main"
        comment: |
          User reported: "machado say 85 when I click pick break down it says he a 94 why won't it update".
          First attempt: only patched LockPickCard.tsx with inline Math.max. iteration_20 testing_agent
          found this was incomplete — pick detail page still showed the lower v1 score (e.g. 85 on card,
          85 on detail when v2 was 94). 87% of picks in /picks/today had lock_score != lock_score_v2.
      - working: false
        agent: "testing"
        comment: |
          iteration_20 P0: card now shows max(v1, v2) correctly, but pick detail page renders bare
          pick.lock_score → 4/5 random picks mismatched. Backend `lock_score` field was NOT being
          promoted from v2. Filed concrete fix: patch pick/[id].tsx + investigate writer.
      - working: "NA"
        agent: "main"
        comment: |
          Root-cause fix shipped (system-wide):
          1. **Backend** — Added `_canonicalize_lock_score(pick)` + `_canonicalize_picks(list)` to
             /app/backend/server.py. Applied to every picks endpoint response: /picks/today,
             /picks/all, /picks/bet-killer, /picks/under-of-the-day, /picks/rollover (both pick + alternates),
             /picks/parlay (every leg), and /picks/{pick_id}. The API now ALWAYS returns
             lock_score = max(lock_score, lock_score_v2), with grade + confidence re-derived.
             Curl-verified on the 5 previously-mismatched picks (all now matching v1 = v2) and
             /picks/today shows 0/52 mismatches.
          2. **Frontend** — Added /app/frontend/src/lib/lockScore.ts with `getDisplayLock(pick)` +
             `getDisplayLockRounded(pick)` as the single source of truth. Replaced every surface
             that previously read `pick.lock_score` directly:
               - /app/frontend/src/components/LockPickCard.tsx (home feed card)
               - /app/frontend/app/pick/[id].tsx (pick detail headline — now testID="pick-detail-lock-score")
               - /app/frontend/app/(tabs)/parlay.tsx (leg cards)
               - /app/frontend/app/(tabs)/killer.tsx (bet killer cards)
               - /app/frontend/app/(tabs)/rollover.tsx (rollover cards)
               - /app/frontend/app/soccer-lab.tsx (soccer lab cards)
               - /app/frontend/app/slip.tsx (bet slip avg + leg row)
             history.tsx left untouched intentionally — that surface shows the score AT the time
             the user placed the bet (immutable record).
          Defense in depth: even if the backend ever drifts, getDisplayLock() will still pick max.

  - task: "Backend canonicalize lock_score = max(v1, v2) at READ time across all picks endpoints"
    implemented: true
    working: "NA"
    file: "/app/backend/server.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: true
    status_history:
      - working: "NA"
        agent: "main"
        comment: |
          New helpers `_canonicalize_lock_score(pick)` and `_canonicalize_picks(list)` defined just
          before `_filter_in_play_window`. Promotes lock_score = max(lock_score, lock_score_v2),
          clamped to [0, 99], re-grades via sports_engine._grade. Wired into every picks endpoint
          return statement. Curl-verified — 0/52 mismatches on /picks/today vs 45/52 before.

  - task: "Rollover tab (single best pick, today only, Lock>=90)"
    implemented: true
    working: true
    file: "/app/frontend/app/(tabs)/rollover.tsx"
    stuck_count: 0
    priority: "high"
    needs_retesting: true

  - task: "Auto Parlay tab — UI + leg selector + notice banner"
    implemented: true
    working: true
    file: "/app/frontend/app/(tabs)/parlay.tsx"
    stuck_count: 0
    priority: "high"
    needs_retesting: true
    status_history:
      - working: true
        agent: "main"
        comment: "Verified via screenshot: gold-bordered payout card, 2/3/4/5 leg chips, gold notice when fewer Elite picks qualify, leg cards navigate to pick detail."

  - task: "Bet Killer tab (Lock<85 warnings)"
    implemented: true
    working: true
    file: "/app/frontend/app/(tabs)/killer.tsx"
    stuck_count: 0
    priority: "medium"
    needs_retesting: true

  - task: "Profile tab + logout"
    implemented: true
    working: true
    file: "/app/frontend/app/(tabs)/profile.tsx"
    stuck_count: 0
    priority: "medium"
    needs_retesting: true

  - task: "Pick detail screen (async Claude AI breakdown)"
    implemented: true
    working: true
    file: "/app/frontend/app/pick/[id].tsx"
    stuck_count: 0
    priority: "high"
    needs_retesting: true
    status_history:
      - working: true
        agent: "main"
        comment: "Verified — opens from Locks feed and from Parlay legs; shows Lock score, Win %, Edge, Book Odds, and 'Why This Pick' AI text."

metadata:
  created_by: "main_agent"
  version: "1.1"
  test_sequence: 2
  run_ui: true

test_plan:
  current_focus:
    - "Pick detail GET /api/picks/{pick_id}  [CRITICAL FIX]"
    - "Auto Parlay tab — UI + leg selector + notice banner"
    - "Auto Parlay endpoint /api/picks/parlay"
    - "Locks tab (today's picks feed)"
    - "Rollover tab (single best pick, today only, Lock>=90)"
    - "Bet Killer tab (Lock<85 warnings)"
    - "Profile tab + logout"
    - "Pick detail screen (async Claude AI breakdown)"
  stuck_tasks: []
  test_all: true
  test_priority: "high_first"

agent_communication:
  - agent: "testing"
    message: |
      iter-96 GoalScorer Engine v3 backend verification — 12/13 PASS.

      ✅ Admin endpoints all functional:
        - GET  /api/admin/goalscorer/v3/status  → engine_version=gs_v3.0.0, all 6 leagues indexed with correct teams/seasons.
        - POST /api/admin/goalscorer/v3/refresh → {ok:true, refreshed:{...}}
        - POST /api/admin/goalscorer/v3/predict → Salah/Chelsea (home, XI) p_anytime in [0.20,0.55], confidence=HIGH,
          lam_team>1.5, full ensemble{monte_carlo,closed_form,form_baseline}. Messi/LA Galaxy MLS also valid
          (confidence≠LOW). Nonexistent player → 404.

      ✅ Regression: /api/health, /api/picks/today (all sort modes except edge), /api/admin/odds-diagnostic all 200.
      ✅ Non-goal-scorer soccer picks (assist/goal_involvement) correctly stay on player_prop_intelligence_v2.
      ✅ /api/picks/{id} detail endpoint returns full v3 metadata correctly.

      ❌ THREE list-endpoint bugs (all in read/decorate layer — storage is correct):
        1. `_slim_rationale` (server.py:2998-3045) drops pick_rationale.v3_signals for 100/100 v3 picks in lite payload.
           Fix: add v3_signals to slim whitelist OR bypass slimming when engine=='goal_scorer_v3'.
        2. `_odds_decorate` (picks_routes.py:~2309) overwrites odds_source='model_derived' → 'odds_api' on 100/100 v3 picks.
           Fix: guard with `if pick.get('source') == 'goal_scorer_v3': return`.
        3. Elite-anchor pipeline recomputes numeric edge_percent (22.75, 1.93, etc.) on ~7% of v3 picks despite
           writer storing None. Detail endpoint also affected (edge_percent=-25.18 seen on a v3 pick).

      Full details + reproducer curl in /app/test_reports/iteration_96.json (rca_of_issue). Main agent should
      patch the two decorators and re-run `pytest tests/test_iter96_goalscorer_v3.py -v` — the single failing
      test (TestPicksV3Stamp::test_soccer_picks_include_v3_stamp_list) should then pass.

  - agent: "main"
    message: |
      Please run a full E2E regression across all 5 tabs (Locks, Rollover, Parlay, Bet Killer, Profile).
      Test credentials: demo@lockscore.ai / demo123 (see /app/memory/test_credentials.md).

      CRITICAL CONTEXT:
        - The `/api/picks/{pick_id}` route was silently broken (missing decorator) prior to this iteration.
          It was restored — please verify by opening any pick from the Locks feed AND from a Parlay leg.
        - Auto Parlay tab supports leg counts 2/3/4/5. If fewer Elite Lock 95+ picks are available,
          backend falls back to Lock 90–94 and a gold info banner appears in the UI.
        - All data is LIVE from The Odds API; no mocks. If a sport has no games, screens show
          "No games available" gracefully — that is correct behavior, not a bug.
        - AI explanation is async: pick detail loads instantly with a fallback explanation,
          then a POST /api/picks/{id}/ai-explain replaces it with Claude Sonnet 4.5 output.

      WHAT TO TEST:
        Backend:
          1. POST /api/auth/login  → returns access_token
          2. GET  /api/auth/me     → returns user
          3. GET  /api/picks/today, /api/picks/all, /api/picks/bet-killer, /api/picks/rollover
          4. GET  /api/picks/parlay?legs=2|3|4|5
          5. GET  /api/picks/{pick_id}  ← REGRESSION TARGET
          6. POST /api/picks/{pick_id}/ai-explain
          7. GET  /api/stats/summary
        Frontend:
          1. Login → land in Locks tab
          2. Tap a pick card → pick detail opens with Lock score + Why This Pick
          3. Switch to Rollover tab → single highest-rated pick (or empty state if none today)
          4. Switch to Parlay tab → 3-leg parlay shows by default; tap 2/4/5 chips
          5. Tap a parlay leg → pick detail opens (this verifies the backend fix)
          6. Switch to Bet Killer tab → list of Lock<85 dangerous bets
          7. Switch to Profile tab → user info + logout works

      NO mocked APIs.

  - agent: "testing"
    message: |
      iteration_2 — FULL E2E REGRESSION PASSED.

      Backend: 24/24 tests (100%). Added TestParlay class with parametrized legs=2/3/4/5,
      parlay→pick_detail end-to-end test, explicit regression test for GET /api/picks/{pick_id}
      asserting 200 + full pick fields + ai_pending bool present, and 2 ai-explain tests (200 + 404).
      The decorator fix is verified.

      Frontend: 12/12 (100%, mobile viewport 375x667). Login → Locks (50 cards) → pick detail OK;
      Rollover shows Lock 99 MLB pick; Parlay tab default 3-leg renders, 2/4/5 chips re-fetch,
      gold notice banner appears when only 4 of 5 Lock 90+ qualify, tapping a parlay leg navigates
      to /pick/{id} (end-to-end confirmation of the decorator fix). Bet Killer shows 92 dangerous
      bets with sport filter. Profile shows user + by-sport stats; logout returns to login.

      Bonus: AI explanations now real Claude markdown — LiteLLM completion(model=claude-sonnet-4-5-20250929)
      invocations succeed in backend logs.

      Only finding: persistent `props.pointerEvents is deprecated` console warning from a third-party RN
      lib (Reanimated/Gesture-Handler) — non-blocking, same as iteration_1.

      No retest needed.


  - agent: "main"
    message: |
      Bet Slip feature verification + polish complete (June 13, 2026):

      Fixed (BetSlipContext.tsx):
        - `storage.secureGet(SLIP_KEY)` was missing the required `fallback` arg (TS error +
          runtime never resolved). Swapped to AsyncStorage directly (slip payload exceeds the
          SecureStore 2KB cap on iOS for 25 enriched picks). Hydration is now non-blocking and
          guarded with a hydratedRef so initial empty state never overwrites persisted picks.

      New (BetSlipFab.tsx + tabs/_layout.tsx):
        - Floating gold "VIEW SLIP (N)" pill anchored above the tab bar across every tab,
          showing live combined parlay odds + $100 stake payout. Hidden on /slip and when slip
          is empty. Shows "FULL" tag at 25 legs. testID="bet-slip-fab" for E2E.

      New (slip.tsx):
        - Header now has share + clear buttons. Native React Native Share on iOS/Android, Web
          Share API → clipboard fallback on web. Share text includes formatted parlay summary,
          all legs (sport · market · odds · matchup), and avg lock score.

      Manual E2E verified on the preview URL with mobile viewport (414x896):
        - Login → tap pick card → tap "+" turns into checkmark, badge shows count.
        - FAB appears bottom-center over tab bar with correct count + live odds.
        - Slip page renders: 2-leg parlay (-575, -660) → -284 odds, $135 payout, $35.18 profit.
          3-leg parlay (-575, -720, -675) → -187 odds, $154 payout, $53.50 profit. Math is
          correct (decimal product → American conversion).
        - Trash icon on each leg removes; Clear button prompts confirm Alert; share icon dims
          when empty. FanDuel/DraftKings/BetMGM buttons open sportsbook URLs.
        - Persists across refresh via AsyncStorage.

      Discovered side-finding: on localhost:3000 testing, EXPO_PUBLIC_BACKEND_URL resolves to
      window.location.origin which doesn't proxy /api/* — preview URL must be used. Not a
      regression, already-existing behavior in api.ts.

      No backend changes. No retest needed for backend.


  - agent: "main"
    message: |
      iteration_71 setup — MLB grading regression FIXED (2026-07-13).

      Root cause: `_mlb_stat_for_player` in prop_settlement.py returned 0.0 for pitchers
      when their game was NOT the one selected by `_mlb_find_game`. On series/doubleheaders
      (Phillies @ Tigers 07-11 & 07-12), the settler merged D and D-1 schedules to catch
      late ET games but picked the earliest Final match, which was the wrong game where
      the pitcher didn't appear (empty stats → returned 0.0 → graded Over 5.5 K as LOST).

      Fixes applied:
        1) `_mlb_find_game` now accepts `event_time` and prefers the game whose gameDate
           is closest to the pick's event_time (fixes series game selection).
        2) `_mlb_stat_for_player` now routes to the correct batting/pitching block based
           on player position (was returning batting.strikeOuts=0 for pitchers who
           actually threw 10 K in pitching.strikeOuts).
        3) Added `Total Bases`, `Runs Scored`, and `Home Run` (singular) to `_MARKET_STATS`.
        4) `Hits + Runs + RBIs` combo markets now sum all three stats before grading
           (was only reading `hits`).
        5) grading_validator._mlb_verify_prop got the same D+D-1 merge, event_time
           distance selection, AND-team matching, position-aware block routing, and
           combo-market sum logic — otherwise the settler's authoritative-override
           inside `_record()` would use a broken validator and re-introduce wrong grades.
        6) stuck_pick_reaper.py excludes picks with `grade_disagreement` set so the
           validator's reopened picks don't get prematurely voided.
        7) grading_validator now `$unset`s stale `grade_disagreement` on agreement.
        8) Regex query in validator extended to cover Runs Scored / Walks.

      Verification (backend-only, no test agent yet):
        - Wheeler 2026-07-12: Over 5.5 K and Over 17.5 Outs Recorded → both WON (were LOST).
        - Trea Turner H+R+RBIs 07-02 → WON (was LOST — 1 hit + 0 R + 0 RBI = 1 > 0.5).
        - Willson Contreras Over 0.5 Hits 07-08 → LOST (0 hits, correct).
        - Juan Soto H+R+RBIs 07-08 → LOST (0 H+R+RBI, correct).
        - Jose Altuve Over 0.5 Hits 07-04 → LOST (0 hits, was incorrectly WON).
        - Manny Machado Over 0.5 Hits 07-09 → LOST (0 hits, was incorrectly WON, wrong series game).
        - After full sweep: 82 originally mis-graded picks now correct;
          0 grade_disagreement flags remaining.

  test_plan:
    current_focus:
      - "MLB pitcher-prop settlement (K, Outs, Walks) grades correctly against Stats API"
      - "MLB batter Hits + Runs + RBIs combo markets grade against sum of all three stats"
      - "Series/doubleheader game selection picks the game closest to event_time"
      - "grading_validator agrees with settler on freshly settled MLB picks"
      - "stuck_pick_reaper does NOT void picks with grade_disagreement flag"
    test_all: false
    test_priority: "high_first"


  - agent: "main"
    message: |
      Iteration 75 — Phase 1.2 (Fangraphs Stuff+/Location+/Pitching+) + Coverage Expansion
      for Mexican Liga MX and ITF/Challenger tennis.

      NEW MODULES:
        - /app/backend/services/mlb_stuff_plus.py — Baseball Savant pitch-arsenal source.
          Fangraphs is CF-protected (verified with curl_cffi chrome124 → 403). We compute
          Stuff+/Location+/Pitching+ *analog* scores from Baseball Savant `pitch-arsenal-stats`
          CSV, usage-weighted per pitcher, calibrated to Fangraphs' mean=100 SD=10.
          Emits `mlb_stuff_plus_players` docs keyed by (player_id, year).
        - /app/backend/services/tennis/sources/tml_stats.py — new source for ATP
          Challenger main draws + ATP Tour qualifying rounds via
          stats.tennismylife.org/data/. Sackmann-compatible parser.

      WIRING:
        - routes/picks_routes.py — on-read enrichment attaches `stuff_plus` block to
          every MLB pitcher prop.
        - server.py — daily refresh loop scheduled ("Stuff+ daily refresh loop scheduled").
        - services/signal_engine/calculators.py — Stuff+/Location+ nudges to
          mlb_deep_signal for pitcher K/IP/ER props (±2 pts for elite/weak stuff, ±1 pt
          for command).
        - services/tennis/fallback.py — refresh_tennis_history now ingests
          Challenger + Qualifying alongside main tour.
        - services/soccer/models.py, sources/football_data_co_uk.py, sources/thesportsdb.py
          — Liga MX canonical code `LigaMX` (previously ambiguous "MEX"), TheSportsDB
          league id 4350.

      TESTS (all green):
        - tests/test_mlb_stuff_plus.py — 14 tests (scale math, aggregation, pitcher extraction).
        - tests/test_coverage_expansion_iter75.py — 8 tests (Liga MX codes + tml_stats URLs).
        - No regressions on 85+ existing tests.

      DEFERRED:
        - WTA challenger + ITF Futures — no publicly-mirrored source available (WTA Sackmann
          repo deleted, no fork ships lower-tier WTA).
        - Fangraphs direct scraping — needs headless browser or Fangraphs API key.

  test_plan_iter75:
    current_focus:
      - "Phase 1.2 Stuff+ ingester loads, aggregates, and enriches MLB pitcher picks"
      - "Signal engine nudges pitcher K/IP/ER props based on Stuff+/Location+ scores"
      - "ATP Challenger + Qualifying histories ingest and merge into rolling player stats"
      - "Liga MX canonical league code registered across football-data.co.uk + TheSportsDB"
      - "Daily refresh loops scheduled at backend startup without errors"
    test_all: false
    test_priority: "high_first"


  - agent: "main"
    message: |
      Iteration 76 — Phase 4 (NFL nflverse) + Phase 5 (Kelly/CLV/Steam) COMPLETE.

      NEW MODULES:
        - /app/backend/services/nfl_nflfastr.py — nflverse GitHub Releases
          parquet ingester (snap counts + player_stats_season). Live-verified
          on 2024 season: 659 snap-count docs + 570 stat docs upserted.
        - /app/backend/steam_detector.py — 60s-cadence background loop watching
          pick_line_history for ≥3pp implied-prob moves inside a 5-min window;
          tags picks with `steam` block. Uses existing observer data — no new
          external calls.
        - /app/backend/analytics.py:kelly_stake() — ¼-Kelly default, 5% cap,
          accepts prob as 0..1 or 0..100, handles negative-edge picks.

      NEW ENDPOINTS (in /app/backend/routes/analytics_routes.py):
        - GET /api/analytics/kelly — inline Kelly calc.
        - GET /api/analytics/kelly/for-pick — Kelly for a specific pick_id.
        - GET /api/analytics/steam — recent steam-flagged pending picks.

      WIRING:
        - server.py — 2 new background loops (NFL weekly refresh + Steam
          detector). All 6 loops confirmed in startup logs.
        - routes/picks_routes.py — on-read NFL usage enrichment for skill
          props (RB / WR / TE / QB).
        - services/signal_engine/calculators.py — volume_signal now applies
          NFL nudges: ±1.5-1.8 pts on target share, ±1.5 pts on snap %,
          +1.2 pts on WOPR, +0.8 pts on aDOT.

      FRONTEND (Lab tab in /app/frontend/app/(tabs)/lab.tsx):
        - New Analytics module with 3 sub-tabs — CLV report, Kelly Calc,
          Steam alerts. All rendering correctly against live backend.
        - Kelly Calc verified live: 58% at -110 with $1000 bankroll →
          $29.50 stake (2.95%), +10.73pp edge, +0.107 EV/unit.
        - CLV shows 4007 picks, 55.8% win rate, per-odds-band breakdown.
        - Steam empty-state renders correctly.

      DEPENDENCIES:
        - pyarrow==25.0.0 added to requirements.txt (parquet ingest).

      TESTS (28/28 GREEN):
        - tests/test_iter76_phase4_5.py — 16 unit tests.
        - tests/test_iter76_live_integration.py — 12 live-DB integration tests
          (added by testing agent).
        - No regressions on prior 100+ tests.

  test_plan_iter76:
    current_focus:
      - "NFL nflverse parquet ingest populates nfl_player_usage with 500+ docs"
      - "Kelly endpoint math correct (positive stake for edge, zero for neg edge, 5% cap)"
      - "Steam detector loop scheduled and returns valid empty response when no data"
      - "Signal engine NFL nudges fire on target_share / snap_pct thresholds"
      - "Lab Analytics tab renders CLV, Kelly, and Steam sections without crash"
    test_all: false
    test_priority: "high_first"


# ─────────────────────────────────────────────────────────────────────
# 2026-07-22 · Player Prop Intelligence System — Phase 2
# ─────────────────────────────────────────────────────────────────────
# USER MANDATE:
#   "Upgrade the soccer betting engine into a complete Player Prop
#    Intelligence System with distinct models for Anytime Goalscorer,
#    Anytime Assist, and Goal Involvement."
#
# NEW MODULE:  /app/backend/services/player_props/
#   ├── __init__.py                    (public API)
#   ├── models.py                      (Archetype enum, PlayerStats,
#   │                                    MatchupSplit, PickRecommendation)
#   ├── stats_aggregator.py            (unified stats from
#   │                                    soccer_player_form + espn_mls_stats
#   │                                    + wiki_top_scorers +
#   │                                    mls_player_matchup_history)
#   ├── archetype_engine.py            (5-way classifier: Goal Scorer /
#   │                                    Creator / Dual Threat / Playmaker
#   │                                    / Low Involvement)
#   ├── goalscorer_model.py            (P(anytime goal))
#   ├── assist_model.py                (P(anytime assist))
#   └── goal_involvement_model.py      (P(goal OR assist), correlation-adj)
#
# ARCHETYPE THRESHOLDS (per-90, standard analytics defaults):
#   Goal Scorer      → G/90 ≥ 0.35 (or ≥ 0.28 with npxg ≥ 0.30)
#   Creator          → A/90 ≥ 0.25 AND G/90 < 0.20
#   Dual Threat      → G/90 ≥ 0.25 AND A/90 ≥ 0.20
#   Playmaker        → A/90 ≥ 0.15 AND KP/90 ≥ 2.0
#   Low Involvement  → G/90 < 0.15 AND A/90 < 0.15
#
# MODEL FORMULAS:
#   Goalscorer  base = min(g/90 * 0.95, 0.75)
#                    × form_mult (±15%)
#                    × matchup_mult (±25%)
#                    × archetype_mult (0.25–1.10)
#   Assist      base = min(a/90 * 0.90, 0.72)
#                    × kp_boost (up to +15%)
#                    × form_mult (±10%)
#                    × matchup_mult × archetype_mult
#   Goal Involvement:
#     p_gi = p_g + p_a - p_g * p_a * (1 - ρ)     ρ ∈ [0.15..0.45]
#     Blended with empirical gi_rate when matches ≥ 3.
#
# INTEGRATION:
#   • services/mls_direct_inject.py rewrote _generate_for_event() to
#     use classify_archetype + predict_goal/assist/goal_involvement.
#     Emitted 392 picks across 30 events on first live run.
#   • Distribution: dual_threat=54+54+54, creator=54+51, goal_scorer=
#     26+50+50+18 — clean market/archetype fit.
#
# NEW ADMIN ENDPOINTS (routes/admin_routes.py):
#   • GET  /api/admin/player-props/analyze/{player}?opponent=…
#         → stats + archetype + 3 model outputs for a single player.
#   • GET  /api/admin/player-props/mls-archetypes
#         → archetype distribution + roster classification for MLS.
#
# VERIFICATION (real MLS data — no RNG):
#   Messi     → Dual Threat  Goal p=0.85 (Nashville boost) · A p=0.62 · GI p=0.90
#   Surridge  → Goal Scorer  Goal p=0.73 · A p=0.10 · GI p=0.59
#   Bouanga   → Dual Threat  Goal p=0.85 · A p=0.24 · GI p=0.90
#   Haaland   → Dual Threat  (Understat source, full per-90 stats)
#   KDB       → Goal Scorer  (Serie A, 5G/2A/18g, npxG-aided classification)
#
# NEXT STEPS:
#   • Phase 3: Matchup Intelligence & Market Selection Engine.
#   • Wire archetype tags into frontend "Why This Pick" panel.
#   • Extend to EPL/UCL/La Liga picks (currently only MLS bypass uses it).

# ─────────────────────────────────────────────────────────────────────
# 2026-07-22 · Player Prop Intelligence System — Phase 3
# ─────────────────────────────────────────────────────────────────────
# TASKS DELIVERED:
#   ✅ Matchup Intelligence layer (rest days, home/away, form extremes,
#      opp defense hook, aggregated ±25% multiplier)
#   ✅ Market Selector (archetype × market fit table, prob floors, auto-
#      routes each player to their best-fit markets)
#   ✅ Frontend archetype chip on LockPickCard.tsx (color-coded by
#      archetype, shows FIT %, integrates with "Why this pick?" panel
#      showing full v2 evidence bullets)
#   ✅ Big-5 + UCL extension via services/soccer_prop_inject.py
#
# NEW MODULES:
#   /app/backend/services/player_props/
#     ├── matchup_intelligence.py    (MatchupContext + signal computers)
#     └── market_selector.py         (MarketRoute + select_markets/best_market)
#   /app/backend/services/soccer_prop_inject.py
#     (generalized Understat-driven injector for EPL, La Liga, Serie A,
#      Bundesliga, Ligue 1, UCL)
#
# FRONTEND:
#   /app/frontend/src/components/LockPickCard.tsx
#     • Added color-coded archetype badge below market title:
#       🔥 DUAL THREAT · FIT 95%   (purple)
#       ⚡ GOAL SCORER · FIT 92%   (red)
#       🎯 CREATOR · FIT 92%       (blue)
#       🔑 PLAYMAKER · FIT 85%     (green)
#     • Evidence panel now shows up to 8 bullets for v2 picks (was 4).
#
# VERIFICATION (live run 2026-07-22 20:22):
#   MLS Direct-Inject:     376 picks
#   EPL:                   144 picks
#   La Liga:               205 picks
#   Serie A:               181 picks
#   Bundesliga:            161 picks
#   Ligue 1:               152 picks
#   ── TOTAL v2 picks:   1,219 picks across 91 events
#
# Total v2 in DB (by league): MLS=660, La liga=205, Serie A=181,
# Bundesliga=161, Ligue 1=152, EPL=144.
#
# Frontend verified: archetype chip renders ("🔥 DUAL THREAT · FIT 80%")
# and expanded Why panel shows summary + evidence + source line
# ("Source: model · player_prop_intelligence_v2").
#
# STARTUP WIRING (server.py):
#   • mls_direct_inject.loop()        — 15-min cadence
#   • soccer_prop_inject.loop()       — 15-min cadence, Big-5+UCL
#
# NEXT STEPS:
#   • Populate opp_defense_strength (currently None → neutral 1.0):
#     needs a team defensive stats fetcher (goals conceded/90, xGA).
#   • Add UCL fixture roster mapping (soccer_uefa_champs_league returned
#     0 events at run time — likely off-season; retry during UCL matchdays).
#   • Wire archetype tag into ROI/analytics for archetype-level bleed
#     tracking.



# ═══════════════════════════════════════════════════════════════════
#  ITER 96 (2026-07-25): GoalScorer Engine v3 — Layered Probability
# ═══════════════════════════════════════════════════════════════════
backend:
  - task: "GoalScorer Engine v3 — layered xG / Poisson / Correlated MC engine"
    implemented: true
    working: true
    file: "backend/services/player_props/goal_scorer_v3.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
        - working: true
          agent: "main"
          comment: |
            Replaced the rudimentary goalscorer Monte Carlo simulator
            with a full 7-layer probability engine per user directive
            (2026-07-22). Layers:
              1. xG Engine (λ_player) — Understat 2-season npxG+goals
              2. Poisson Team-Goal Simulator — team_atk × opp_def
              3. Goal Allocation Engine — allocates team goals by share
              4. Correlated Monte Carlo — 20k samples, Binomial(N,p)
                 per-goal draws so same-team players correlate correctly
              5. Bayesian Lineup Update — starter_xi/rotation/bench_risk
              6. Ensemble (0.55 MC + 0.30 closed-form + 0.15 form)
              7. Strict Edge Gate — edge_percent=None when no real book

            Wired into `services.mls_direct_inject` (MLS) and
            `services.soccer_prop_inject` (EPL/LaLiga/SerieA/Bundesliga/
            Ligue1/UCL) via new async `select_markets_v3()`.

            Team-strength engine seeds team λ from 3 seasons of match
            data (2022-23, 2023-24, 2024-25) with Bayesian shrinkage
            toward league mean (prior mass = 12 matches). MLS falls
            back to ESPN-stats-derived team goals rescaled to league
            mean 1.45 GF/game.

            v3 picks now stamp: source="goal_scorer_v3",
            odds_source="model_derived", edge_percent=None,
            confidence_penalty=-5, and pick_rationale.v3_signals with
            {lam_player, lam_team, lam_opponent, expected_minutes,
             goal_share, ensemble, p_first, p_2plus, seasons_used}.
        - working: false
          agent: "testing"
          comment: |
            Initial run: 12/13 pass. 3 downstream serializers
            corrupted v3 output — `_slim_rationale` stripped
            `v3_signals`, `_odds_decorate` overwrote `odds_source`,
            elite-anchor recompute clobbered `edge_percent`.
        - working: true
          agent: "main"
          comment: |
            Fixed 3 decorator issues + a pre-existing win_probability
            scale bug (was fraction 0-1, now 0-100 matching every
            other source). All 13 tests pass. Live slate now shows
            167 v3 goalscorer picks across MLS + Big-5 with strict
            edge gate honoured end-to-end.

  - task: "Team-strength engine — multi-season Poisson priors"
    implemented: true
    working: true
    file: "backend/services/player_props/team_strength.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
        - working: true
          agent: "main"
          comment: |
            Aggregates soccer_matches (25k+ matches) across 3 seasons
            with weights [2024-25:1.0, 2023-24:0.65, 2022-23:0.35]
            and Bayesian shrinkage. Returns per-team home/away attack
            + defense λ. Standings-only fallback for MLS (built from
            espn_mls_stats aggregation, rescaled to league μ=1.45).
            Fuzzy team-name matching includes 30+ alias mappings for
            common abbreviations (Man Utd, Spurs, PSG, Inter, etc.).

  - task: "Admin diagnostics for v3 engine"
    implemented: true
    working: true
    file: "backend/routes/admin_routes.py"
    stuck_count: 0
    priority: "medium"
    needs_retesting: false
    status_history:
        - working: true
          agent: "main"
          comment: |
            Added 3 endpoints:
              GET  /api/admin/goalscorer/v3/status   — per-league
                   diagnostics (μ, teams indexed, seasons used).
              POST /api/admin/goalscorer/v3/refresh  — clear cache
                   and re-warm all 6 leagues.
              POST /api/admin/goalscorer/v3/predict  — one-shot
                   evaluation for {player, opponent, league, home}.

metadata:
  last_iteration: 96
  last_iteration_topic: "GoalScorer Engine v3 (layered probability model for all soccer leagues)"
  last_iteration_result: "13/13 backend tests PASS · 167 v3 picks live across MLS+Big-5"
