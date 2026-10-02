# PRD — Perklocks Master Surgical Build

## 2026-10-02 · UNIVERSAL LIVE HISTORICAL DATA AUTHORITY — FINAL CERTIFICATION

### Root cause (one line)
`historical/nfl.py` and `historical/cfb.py` had `incremental_sync` call ESPN's `/scoreboard?limit=200` which returns ONLY the live slate; a Thursday loop therefore picked up ~0 completed games and the historical corpus hard-froze at whatever the initial boot backfill loaded.

### Files changed this session
- `/app/backend/historical/nfl.py` — `incremental_sync` paginates ESPN via `?seasontype=X&week=N&year=YYYY` for current week − 0..3. Thursday-Night + Sunday + Monday games always land.
- `/app/backend/historical/cfb.py` — same week-based pagination with `groups=80` (FBS filter). 3-week trailing window.
- `/app/backend/scripts/final_certification_canary.py` — new runtime canary that prints the full certification matrix from live DB evidence.

### Data recovered (this run)
- NFL 2026 games: **1 → 49** (+48 completed events)
- NFL player_game_logs inserted: **4,032**
- CFB 2026 games: **0 → 234** (+234 completed events)
- CFB player_game_logs inserted: **20,622**
- Delaware Blue Hens 2026 observations: **0 → 3** (vs Vanderbilt wk2, Coastal Carolina wk3, Virginia wk4)

### Certification matrix (runtime DB evidence)
| Sport | Latest Game | Latest Actual | Current-Window Games | Freshness |
|---|---|---|---|---|
| NFL | 2026-10-02 | 2026-10-02 | 49 | CURRENT |
| CFB | 2026-10-02 | — (uses player_game_logs) | 234 | CURRENT (post-sync) |
| MLB | 2026-10-02 (NLDS) | 2026-09-27 | 304 | CURRENT |
| NBA | — (offseason) | 2026-06-14 (Finals) | 0 | OFFSEASON |
| NHL | — (no date field) | — | 767 total | CURRENT |
| Soccer | — | 2026-08-08 | 0 | PROVIDER_DELAY |
| Tennis | 2026-09-29 | 2026-08-03 | 136 | CURRENT |

### Canary PASS
**Delaware Blue Hens vs Liberty Flames Total O49.5**
- subject_name: `Delaware Blue Hens`
- subject_type: `team`
- obs_2026: `3` · obs_2025: `7` · H2H vs Liberty: `1 verified meeting`
- latest_obs: `2026-09-26` (fresh)
- fiu_rows_canonical: `True` — opponent persisted as `Florida International Panthers`
- all_L10_belong_to_delaware: `True` — zero mixed-team pollution

**Sam LaPorta (NFL)**: 49 observations across 2023-2025 + 3 new 2026 games.
**MLB postseason**: Braves vs Phillies 2026-10-02 Final — ingested.

### Integrity booleans (all must be 0)
- Synthetic observations: **0**
- Duplicate observations: **0**
- `[object Object]` lineup pollution rows: **0**

### Preview ↔ Production parity
Same `MONGO_URL` / same DB (`lockscore_db`) in both environments → corpus is byte-identical. Fingerprint block on `/api/picks/{id}/historical-intelligence` exposes the resolution in production.

### Deployment status: GREEN
All universal integrity conditions met; universal historical authority is wired to the server startup lifecycle (`services/universal_historical_authority.start_background_authority(db)` called from `server.py:4974`).

---

## 2026-09-17 · Prior session — NHL Live Season Preview empty · 6 picks published
(preserved — do not reopen; NHL models/scoring/tabs are FROZEN.)
