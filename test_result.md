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

  - task: "Locks tab (today's picks feed)"
    implemented: true
    working: true
    file: "/app/frontend/app/(tabs)/index.tsx"
    stuck_count: 0
    priority: "high"
    needs_retesting: true

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
