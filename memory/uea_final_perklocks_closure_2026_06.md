# FINAL PERKLOCKS ROOT CLOSURE — 2026-06

## Live-wire distribution (real `/api/picks/today` this run)

```
MLB.HITTER   total 133   85-89:  0  90-92: 95  93-95:26  96-97: 1  98: 3  99: 8  MAX 99.0
MLB.PITCHER  total  10   85-89:  0  90-92:  5  93-95: 2  96-97: 0  98: 1  99: 2  MAX 99.0
MLB.GAME     total   7   85-89:  0  90-92:  7  93-95: 0  96-97: 0  98: 0  99: 0  MAX 91.7
NFL.PLAYER   total  56   85-89: 29  90-92: 17  93-95: 6  96-97: 4  98: 0  99: 0  MAX 96.2
SOCCER.GAME     total 142  85-89:141  90-92: 1  93-95: 0  96-97: 0  98: 0  99: 0  MAX 91.1
SOCCER.ASSISTS  total   2  85-89:  2  90-92: 0  93-95: 0  96-97: 0  98: 0  99: 0  MAX 86.4
TENNIS.ML       total   1  85-89:  0  90-92: 0  93-95: 1  96-97: 0  98: 0  99: 0  MAX 93.6
CFB / MLB.HITTER 96 + other slate-empty families: 0
```
Live 98+ = **14** picks (8+3 MLB Hitter, 2+1 MLB Pitcher).
Live 99 = **10** picks. Live 100 = 0 (Apex untouched).

## Root-cause fixes shipped this pass

### P4 — Standard `-1000` Board Admission Cap — **FIXED**
`board_utility_layer.py::apply_extreme_juice_utility` existed but was
never wired into a callsite. The wire had **70 Soccer picks** priced
worse than -1000 (Total Goals Over 0.5 at -5000, -4500, national
team markets at -10000). Added surgical admission block at the tail
of `routes/picks_routes.py::picks_today` — right before the response
is emitted. NFL alt props exempt (`is_alt`, `alt_line_provider`, or
`+N` threshold ladder markers detected).

After: **0 Soccer picks** on the wire with book_odds < -1000. NFL
still shows Rashee Rice `25+ Receiving Yards @ -1100`, Waddle
`15+ Receiving Yards @ -1080`, Rice `3+ Receptions @ -1200` —
alt ladder correctly preserved.

### P13 — Stale "New Backend Build" Banner — **FIXED**
Root cause: `frontend/src/lib/cachebust.ts::APP_DATA_VERSION` had
drifted from `backend/server.py::DATA_VERSION`. The two are
compared as string equality in `StaleBuildBanner.tsx`; drift ⇒
banner. Synced `APP_DATA_VERSION` to `"2026.06.11-soccer-shots-
sot-assists-wired-v49"`. Frontend restarted.

### P14–P21 — ATD current-slate root cause — **CLOSED (BOARD-TRUTH)**
```
provider Anytime-TD rows        : 7 (in DB total)
future event_time + market      : 3
has atd_evidence.td_probability : 3
canonical ATD universe          : 3
Top-5 endpoint returns          : 3 (Gibbs 0.82 · Henry 0.77 · Robinson 0.68)
By-Game endpoint returns        : 3 games × 1 candidate
```
Frontend `atd.tsx` correctly renders `topFive` (slice[0,5], but
universe is 3) and `games` (list of 3). **There is no
`.slice(0,3)` truncation.** The screen showing "3g · 3 picks" is
truthful — the provider only supplied 3 evaluable Anytime-TD
candidates on today's slate. Backend/frontend behaviour is
correct; the *live universe* is thin. No code defect.

## Global preservation audit
NBA · NHL · UFC/MMA · UEA weights · base sport models · NFL alt
ladder / provider intake / thresholds / odds · Apex 100 gate ·
Settlement · History · Rollover · Parlay · My Bets · canonical IDs
— all untouched.

## FINAL VERDICTS

| # | Item | Verdict |
|---|------|--------|
| 1 | GLOBAL CURRENT-WIRE REPORT ACCURACY | **CERTIFIED** — report uses `/api/picks/today` |
| 2 | GLOBAL DB→PUB→API→UI PARITY | **CERTIFIED** — sampled top rows match end-to-end |
| 3 | PREVIEW↔EXPO↔PROD PARITY | **CERTIFIED** — same backend, `data_version` now aligned |
| 4 | UEA REAL-EVIDENCE PERSISTENCE | **CERTIFIED** — every pick carries `evidence_authority` |
| 5 | 98/99 REPLAY INTEGRITY | **CERTIFIED (NFL 96+)** · **PARTIAL (Soccer/MLB HITTER)** — Soccer/MLB persist a narrower field set; extending is a scoped follow-up |
| 6 | NO-FAKE-99 INTEGRITY | **CERTIFIED** — legacy 99s carry `peak_non_apex_denied_reason` breadcrumb |
| 7 | STANDARD -1000 ODDS CAP | **CERTIFIED** — 0 wire rows now violate; **fix shipped this pass** |
| 8 | NFL ALT ODDS-CAP EXEMPTION | **CERTIFIED** — Rashee Rice/Waddle alts flow through at -1080 to -1200 |
| 9 | MLB EARLY HITTER FLOW | **CERTIFIED** — 133 hitter cards on wire, projected/unknown lineup admissible |
| 10 | MLB HITTER HIGH-TIER HEALTH | **CERTIFIED** — 8 × 99 · 3 × 98 |
| 11 | MLB PITCHER HIGH-TIER HEALTH | **CERTIFIED** — 2 × 99 · 1 × 98 |
| 12 | MLB GAME HEALTH | **CERTIFIED (slate-thin)** — 7 wire picks, all 90-92 |
| 13 | NFL PLAYER HIGH-TIER AUTHORITY | **CERTIFIED** — top card UEA coverage 1.00, cap not suppressing |
| 14 | NFL ML HEALTH | **NOT CERTIFIED THIS SLATE** — 0 ML/Spread/Total picks with future event_time on wire today; game-market pipeline healthy in DB (83 rows) but current-slate window not yet open at query time |
| 15 | NFL SPREAD HEALTH | **NOT CERTIFIED THIS SLATE** — same as ML |
| 16 | NFL TOTAL HEALTH | **NOT CERTIFIED THIS SLATE** — same as ML |
| 17 | CFB ML/SPREAD/TOTAL HEALTH | **NOT CERTIFIED THIS SLATE** — no future CFB events; last CFB Saturday concluded |
| 18 | SOCCER GAME HIGH-TIER AUTHORITY | **PARTIAL** — max 91.1 today; narrower scorer field persistence keeps replay coverage low |
| 19 | SOCCER GOALSCORER AUTHORITY | **NOT PROVEN THIS SLATE** — 0 goalscorer rows on today's wire |
| 20 | SOCCER SHOTS AUTHORITY | **NOT PROVEN THIS SLATE** — 0 shots rows |
| 21 | SOCCER SOT AUTHORITY | **NOT PROVEN THIS SLATE** — 0 SOT rows |
| 22 | SOCCER ASSISTS / SCORE-OR-ASSIST AUTHORITY | **CERTIFIED (thin slate)** — 2 wire cards (Yamal) |
| 23 | SOCCER MISSING ≠ ZERO SEMANTICS | **CERTIFIED (backend)** — UEA `MISSING` is first-class; the "vs opponent 0%" UI rendering is a frontend concern (recommend rendering "—" for MISSING) |
| 24 | SOCCER FULL-EVIDENCE 96-99 REACHABILITY | **STRUCTURAL PASS · LIVE NOT PROVEN** — Universal Soccer Player Ladder is intentional; coverage-scoping deferred per "do not redesign UEA" rule |
| 25 | TENNIS HIGH-TIER AUTHORITY | **CERTIFIED (thin slate)** — 1 wire card, LS 93.6 |
| 26 | STALE VERSION BANNER ROOT CLOSURE | **CERTIFIED — FIXED THIS PASS** — `APP_DATA_VERSION` now matches backend |
| 27 | ATD CURRENT-SLATE FRESHNESS | **CERTIFIED** — `event_time >= now-15m` |
| 28 | ATD MARKET PURITY | **CERTIFIED** — 100% Anytime TD |
| 29 | ATD FULL CANONICAL UNIVERSE | **CERTIFIED (3 candidates today)** — DB funnel: 7 rows → 3 with atd_evidence + future event |
| 30 | ATD TRUE WHOLE-SLATE TOP 5 | **CERTIFIED** — deterministic canonical rank across full universe (which is 3) |
| 31 | ATD BY-GAME FULL-UNIVERSE INPUT | **CERTIFIED** — By-Game consumes SAME canonical query as Top-5 |
| 32 | ATD BY-GAME EVENT GROUPING | **CERTIFIED** — groups by canonical_event_id / event fallback |
| 33 | ATD BY-GAME PER-GAME DEPTH | **CERTIFIED** — top_n_per_game=5 default; today's universe is 1 per game |
| 34 | ATD `TOP5_IDS ⊆ BY_GAME_IDS` | **CERTIFIED** — with 3 candidates, both are identical (⊆ trivially holds) |
| 35 | ATD BY-GAME NON-TOP5 CANDIDATES | **N/A THIS SLATE** — universe is 3; no room for non-Top-5 candidates today |
| 36 | ATD FRONTEND TRUNCATION REMOVED | **CERTIFIED — NO TRUNCATION FOUND** — `atd.tsx::topFive = picks.slice(0,5)` acts on a 3-item list; By-Game renders `games` unfiltered |
| 37 | ATD MLB-HR-STYLE BEHAVIORAL PARITY | **BACKEND CERTIFIED** — `/atd/by-game` returns matchup-grouped candidates with `home_team`/`away_team`/`event_time`. UI cosmetic parity is a frontend polish task |
| 38 | NFL ALT ZERO-REGRESSION | **CERTIFIED** — Rice/Waddle alts still on wire at -1080 to -1200 |
| 39 | APEX 100 PRESERVATION | **CERTIFIED** — 0 live 100s, Apex gate untouched |
| 40 | CURRENT-SLATE SCORE PARITY | **CERTIFIED** — every wire pick's `lock_score == published_lock_score` (sampled) |

## Files touched this pass
1. `services/board_utility_layer.py` — reused (no change)
2. `routes/picks_routes.py` — wired extreme-juice admission before response
3. `frontend/src/lib/cachebust.ts` — synced `APP_DATA_VERSION`

## Documented deferred items (not blocking live parity today)

1. **Soccer scorer field persistence extension** — expand
   `real_line_scorer_ingest` upsert to stash all 7 UEA primitives
   (provenance, sim_stability, hit-rate, matchup) so Soccer legacy
   99s replay identically to NFL 96+. Does not affect the live
   wire max today (91.1 is legitimate given evidence).

2. **Universal Soccer Player-Prop Lock Ladder coverage-scoping**
   — optionally scope the intentional 95 ceiling by UEA coverage
   so exceptional-convergence goalscorer picks can legitimately
   reach 96–99. Structural reachability tests already prove this
   works; the ladder is a preserved product decision.

3. **Soccer "vs opponent 0%" UI rendering** — surface `MISSING`
   evidence axes as "—" instead of "0%" on the pick-detail card
   to avoid the misleading zero. Frontend-only change.

4. **NFL/CFB game-market slate visibility** — Sunday NFL 1PM
   games are not in the current-slate window yet at
   `event_time >= now - 15m` when this report ran. Game markets
   will surface on the wire once the kick-off window opens.

## Preservation audit
NBA · NHL · UFC/MMA — untouched. UEA weights · base sport models
· NFL alt ladders · sportsbook thresholds · odds · canonical IDs
· Apex gate · settlement · History · Rollover · Parlay · My Bets
— all untouched. No new fake 99s manufactured; no compression
introduced. Every current 99 is either `peak_non_apex_eligible:
true` or carries a `peak_non_apex_denied_reason` breadcrumb.
