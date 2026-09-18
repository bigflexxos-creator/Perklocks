# PERKLOCKS — FINAL UNIVERSAL ROOT CLOSURE · Build Memo (2026-09-18)

One continuous surgical build. No rebuild, no model redesign, no broad refactor.

## Root cause of the 89 vs 85 split (P0.1) — FIXED
1. `GET /picks/{id}` re-ran `govern_pick` (rescoring) for any pick lacking `win_probability_raw`,
   mutating top-level `lock_score/win_probability/edge/grade` (88.9 → 82.7, Playable → Pass).
2. `published_prediction_reader.hydrate()` had a "v4 read-authority" branch that let those MUTABLE
   top-level fields supersede the frozen `published_*` snapshot at read time.
Result: Locks (list) showed the snapshot; Pick Breakdown showed the rescored value.

### Fix (P0.2 / P0.3 / P0.4)
- `hydrate()`: snapshot ALWAYS wins. v4 branch removed. Exposes `publication_version`.
- `pick_detail`, `/picks/today` lazy governance, `quality_gate.apply_quality_gate`: no cap / govern /
  score movement for any pick carrying `published_lock_score`. GETs are readers.
- Detail ESPN enrichment is cache-overlay only (background warm) — zero provider calls in the GET.
- `PredictionPublicationService.publish()`: versioned re-publication (monotone `snapshot_version`,
  prior deactivated, nothing deleted) + `published_grade` derived from the canonical mapping over the
  published Lock Score + append-only `publication_events`.
- One-time reconciliation (`scripts/reconcile_publication_truth.py`): 73,319 pending picks scanned,
  23,571 v4 rows re-published (what the board already displayed), 36,722 alias realignments,
  37,511 grade repairs. No displayed number changed; the snapshot became the persisted truth.
- Soccer regen (`soccer_scorer_regen_via_authority`) now publishes through the service (v2/v3 snapshots).

## P0.5 truth manifest
`/picks/today` → `truth_manifest{api_origin, environment, board_version, publication_version, generated_at,
data_as_of, pick_count}` + `board_version`; every row + detail carries `truth_fingerprint` (sha over
canonical scored fields). `services/truth_manifest.py`.

## Frontend (P0.6–P0.11)
- P0.9 single-flight auth verification (`verifyAuthSingleFlight` in `src/lib/api.ts`): an unexpected 401 →
  ONE `/auth/me` check; only a definitive 401 clears the token. Network/timeout/5xx keep the token.
- P0.7 board cache `locks_picks_cache_v2` keyed by API origin + board_version; restored slate flagged STALE.
- P0.11 `STALE/UPDATING · LAST UPDATED · BOARD xxxx` pill + retry banner meta; last-good never replaced.
- P0.10 `src/lib/connectivity.ts` — one global connectivity authority (AppState + reachability probe,
  single debounced foreground signal); Locks board subscribes to it.
- P0.6 DEV HUD shows SURFACE / API ORIGIN / ENVIRONMENT / BOARD VERSION / DATA AS OF / BUNDLE / CONNECTIVITY.

## P4 ATD
- `GET /api/nfl/atd/slate` — ONE universe (`services/nfl_atd_slate.py`), built once per 10-min window
  (single-flight, stale-while-revalidate, warmed at boot), explicit `candidate_state` PUBLISHED/ON_DEMAND,
  dedupe on (event, player, market) with PUBLISHED winning, deterministic rank (td desc, conf desc,
  player_id asc) — hash tiebreak removed. `/atd/leaderboard` and `/atd/by-game` consume the same universe.
- `app/(tabs)/atd.tsx`: one slate fetch, last-good cache keyed by origin, stale strip, By Game grouped
  cards (header once, ranked 1..3, VIEW ALL (n)), no client-derived grade (canonical grade / data badge).

## P6 Rollover
- `rollover_slates` + `rollover_slate_events` (`services/rollover_official_slate.py`): frozen official Top 3,
  served reader-first (`rollover_version=v6-official-slate`), untouched by game start/settlement/refresh;
  only pregame invalidation replaces a leg with an audit event. Filtered requests never touch it.
- `ev_score`: primary = conservative calibrated win probability; market boosts + alt bonus removed;
  no double-count when sim absent. History reads official slates; tagger output = RESEARCH_REPLAY.

## P1.2 Soccer identity/freshness
- Resolver selects form rows deterministically (season desc, updated_at desc, ≥90 min) and attaches
  `prior_season_form`; hydrator shrinks current-season rates toward the player's OWN prior season
  (minutes-weighted) — prior supports, never replaces. Kane: 2026 row (217 min) + 2025 prior → 31.9% ATG.
- Sørloth: transliteration + legacy-dropped-letter variants ("srloth") → resolved (32.2% ATG).
- Endrick: MONONYM_EXACT match; 2026 (2 min) insufficient → PRIOR_SEASON Lyon 2025 flagged (20.7%).

## P10 harness
`scripts/p10_final_parity_harness.py`: 92 picks (all ≥90 + 25 NFL + 25 Soccer) DB = list = detail,
0 unexplained differences; fixtures Dortmund BTTS / Kane / Sørloth / Endrick / Wheeler PARITY;
ATD top5 == leaderboard; Rollover legs == detail truth. Testing agent: 11/11 backend, frontend PASS.

## Final matrix
| SYSTEM | BACKEND | PREVIEW | WEB | EXPO | IDENTITY | EVIDENCE | MODEL | CANONICAL | HI | H2H | SETTLEMENT | STATUS |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Publication read contract (P0.2–P0.4) | ✔ | ✔ | ✔ | same API | – | – | – | ✔ | – | – | – | CERTIFIED |
| Dortmund/Stuttgart BTTS parity | 89/79.11/+2.92/Playable | ✔ | ✔ | same API | ✔ | ✔ | ✔ | ✔ | – | – | – | CERTIFIED |
| Truth manifest / fingerprint | ✔ | ✔ | ✔ | ✔ | – | – | – | ✔ | – | – | – | CERTIFIED |
| Kane freshness | ✔ | ✔ | ✔ | ✔ | ✔ | CURRENT+PRIOR | 31.9% | v3 | – | – | – | CERTIFIED |
| Kane VS Union (real game logs) | – | – | – | – | ✔ | aggregate only | – | – | – | ✖ | – | BLOCKED_BY_REAL_DATA (no match-level ingest) |
| Sørloth identity | ✔ | ✔ | ✔ | ✔ | ✔ | 2025 only | 32.2% | v2 | – | – | – | CERTIFIED |
| Endrick mononym | ✔ | ✔ | ✔ | ✔ | ✔ | PRIOR_SEASON | 20.7% | v2 | – | – | – | PARTIAL (current-season sample 2 min) |
| Wheeler Outs | parity ✔ | ✔ | ✔ | ✔ | – | not re-audited | – | v1 | – | – | – | PARTIAL |
| NBA authority split-brain | – | – | – | – | – | – | no NBA slate | – | – | – | MODEL_UNAVAILABLE (no NBA picks in window) |
| ATD Top 5 | ✔ | ✔ | ✔ | ✔ | ✔ | badge | xTD unchanged | slate | – | – | – | CERTIFIED |
| ATD By Game | ✔ | ✔ | ✔ | ✔ | ✔ | – | – | slate | – | – | – | CERTIFIED (nav entry = deep-link only) |
| Settlement authority (P5) | publication_events ✔ | – | – | – | – | – | – | – | – | – | multi-writer remains | PARTIAL |
| Rollover frozen Top 3 | ✔ | ✔ | ✔ | ✔ | – | – | – | ✔ | – | – | – | CERTIFIED |
| Connection / auth (P0.9–P0.11) | – | ✔ | ✔ | code-level | – | – | – | – | – | – | – | PARTIAL (P11 device matrix not run) |
| Preview/Web/Expo parity | same API origin | ✔ | ✔ | not device-tested | – | – | – | ✔ | – | – | – | PARTIAL |
| Intelligence 2.0 / media / P1.1 all-sport injection / H2H contract | – | – | – | – | – | – | – | – | – | – | – | NOT_CERTIFIED (not built this session) |
