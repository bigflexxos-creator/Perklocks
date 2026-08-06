# Phase 2 Paid API Call Inventory

**Snapshot taken:** 2026-08-06.  Every code path in the backend that makes a paid call to The Odds API.

**Legend:** 🟢 through-cache · 🟡 through-cache but bypassable · 🔴 direct httpx (unlogged)

---

## 1. Direct string references to `api.the-odds-api.com/v4`

13 files import the base URL.  Not all make direct calls — some hold it as a constant used only by the cache layer.

| File | Purpose | Path status |
|---|---|:-:|
| `services/odds_cache.py` | Centralised cache — writes `odds_api_request_log` | 🟢 hub |
| `services/odds_provider.py` | Health probe + `/sports` catalog fetch | 🟡 has direct `_PROBE_URL` |
| `alt_lines_feed.py` | Alt-line snapshot generator | 🟢 all calls via cache |
| `sports_engine.py` | Main pick generator — `_fetch_odds_for` | 🟢 via cache |
| `brain/nrfi_engine.py` | NRFI odds fetcher | 🟢 via cache |
| **`services/mls_direct_inject.py`** | MLS direct-inject events discovery | 🔴 **direct `httpx.get` on line 174 — no cache, no log** |
| **`services/soccer_prop_inject.py`** | Soccer prop-inject events discovery | 🔴 **direct `httpx.get` on line 120 — no cache, no log** |
| `soccer/real_odds.py` | Soccer real-odds fetcher | 🟢 via cache |
| `tennis_extra/real_odds.py` | Tennis extra odds fetcher | 🟢 via cache |
| `soccer_lab.py` | Soccer Lab live-scan | 🟡 review needed |
| `closing_line_snapshotter.py` | Closing line snapshot | 🟢 via cache |
| `services/odds_cache.py` (2 more constants) | shipped docstrings only | — |

**Direct-call violations to fix in Phase 2γ:** 🔴 `mls_direct_inject` and `soccer_prop_inject` events discovery.  These bypass both the cache layer and the request log.  All other modules already flow through `cached_httpx_get`.

## 2. Every `_refresh_picks` caller

| Caller | File:line | Trigger | Notes |
|---|---|---|---|
| Startup one-shot | `server.py:3007` | boot-time | fires if `_should_refresh_on_boot()` |
| `_daily_refresh_loop` | `server.py:3916` | hourly during game window | `_refresh_picks(current_date)` |
| `_daily_refresh_loop` (tomorrow) | `server.py:3927` | hourly | `_refresh_picks(tomorrow)` |
| `_mlb_pregame_loop` (today) | `server.py:3972` | every 5 min | `_refresh_picks(_today_str(), "MLB")` |
| `_mlb_pregame_loop` (tomorrow) | `server.py:3978` | every 5 min | `_refresh_picks(tomorrow, "MLB")` |
| `_mlb_late_night_boot_refresh` | `server.py:4500` | once at boot | `_refresh_picks(_today_str(), "MLB")` |
| **`POST /api/picks/force-refresh`** | `routes/picks_routes.py:2543` | **user-callable** | 🔴 `asyncio.create_task(_refresh_picks(...))` |
| **`POST /api/admin/picks/force-refresh`** | `routes/admin_routes.py:614` | admin | `asyncio.create_task(_refresh_picks(...))` |
| **`POST /api/admin/picks/force-refresh` (tomorrow variant)** | `routes/admin_routes.py:651` | admin | `asyncio.create_task(_refresh_picks(tomorrow_str))` |

**Total distinct `_refresh_picks` call sites: 9.** Every one of them enters the same 45-second pipeline that pulls paid odds. Phase 2β/2γ target: all 9 must acquire a shared lease + pass budget check.

## 3. Every direct The Odds API caller (paid endpoints)

Grouped by endpoint family:

### /sports (catalog)
- `services/odds_provider.py:_PROBE_URL` — health probe
- `alt_lines_feed._discover_active_tennis_tournaments` — via cache
- `alt_lines_feed._discover_active_soccer_leagues` — via cache

### /sports/{sport}/events (event lists)
- `alt_lines_feed._fetch_events` — via cache ✅
- **`services/mls_direct_inject.py:174` — direct httpx 🔴**
- **`services/soccer_prop_inject.py:120` — direct httpx 🔴**

### /sports/{sport}/events/{id}/odds (event odds — most expensive)
- `alt_lines_feed._fetch_event_odds` — via cache ✅
- `alt_lines_feed._fetch_event_odds_individual` — via cache ✅ but **still fires 970 credits/day** (the fallback that Phase 1a intended to kill)
- `sports_engine._fetch_odds_for` — via cache
- `brain/nrfi_engine._fetch_sportsbook_nrfi` — via cache
- `closing_line_snapshotter._snapshot_event` — via cache

### /sports/{sport}/odds (bulk odds)
- `sports_engine._fetch_bulk_odds` — via cache

### /sports/{sport}/scores (scores — used in settlement fallback)
- Present in code but rare firing in the 24 h window

### /sports/{sport}/players (player list — where supported)
- Not currently a paid caller in our codebase

## 4. Endpoint types and current cache TTLs

Per `services/odds_cache._TTL_POLICY`:

| Endpoint type | Fresh TTL | Stale TTL | Notes |
|---|---:|---:|---|
| `sports_list` | 24 h | 7 d | Rarely paid |
| `event_list` | 5 min | 15 min | Cheap (1 credit/call) |
| `event_odds` | 15 min | 2 h | Expensive (5-15 credits/call) |
| `event_alt_lines` | 15 min | 4 h | Most expensive |
| `bulk_odds` | 15 min | 2 h | Expensive |
| `scores` | 60 s | 5 min | For live scores |
| `generic` | 5 min | 15 min | fallback |

Phase B off-peak multiplier applies 2× TTL during 03-14 UTC.

## 5. Retry / fallback expansion paths

- **`alt_lines_feed._fetch_event_odds_individual`** — fires 4-6 additional per-market requests when the bulk `_fetch_event_odds` returns null.  Bad-market registry check runs INSIDE `_fetch_event_odds`, not before → the fallback still fans out on newly-inserted events.  **Phase 2γ P0 fix.**
- **`sports_engine._fetch_odds_for.retry`** — fires 41 credits/day of narrower retries.  Currently retries on any error, not just 422 → **Phase 2γ P0 fix**.
- **NRFI single-event retries** — bounded; low cost.

## 6. Process-local vs distributed safety

- Cache single-flight uses `asyncio.Lock` — **process-local**.  If we deploy 2+ backend replicas, both can hit The Odds API simultaneously for the same event.
- No cross-process budget tracking.  Every process independently spends against the same daily quota → 2 replicas can burn 2× credits before either notices.
- **Phase 2β target:** `scheduled_jobs` + `provider_budget` collections replace both.

## 7. Recommended Phase 2γ eliminations

Ordered by expected savings vs baseline (3,270 credits/day):

| Fix | Expected savings | Files touched |
|---|---:|---|
| Consult bad-market registry BEFORE bulk `_fetch_event_odds` (kill `_fetch_event_odds_individual`) | **~980 credits/day (30 %)** | `alt_lines_feed.py` |
| Single-flight distributed (via `scheduled_jobs` lease) collapses 617 duplicates/day | **~490 credits/day (15 %)** | `services/odds_cache.py` + new gateway |
| Persistent tournament registry with `suppress_until` — kill 43-tennis-tournament fan-out | **~260 credits/day (8 %)** | new `services/tournament_registry.py` |
| Kill `run_immediately=True` on 3 snapshot jobs — startup burst | **~-1,500 per restart** | `server.py` |
| Retry only on 422 (not on 401/429/5xx/timeout) | **~40 credits/day** | `alt_lines_feed`, `sports_engine` |
| Consolidate MLB pregame + hourly refresh via coordinator | **~200 credits/day** | `server.py` |
| Route user + admin `_refresh_picks` through coordinator + budget check | **eliminates uncontrolled bursts** | routes + `server.py` |

**Combined theoretical:** ~1,970 credits/day saved → ~1,300 credits/day steady state → ~39,000 credits/month (61 % under 100k target).

**Must be MEASURED after Phase 2γ deploy.**  See `PHASE2_BASELINE_REPORT.md` §15 for exact monitoring commands.
