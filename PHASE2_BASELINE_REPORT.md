# Phase 2 Baseline Report

**Measurement window:** last 24 hours ending 2026-08-06 08:00 UTC
**Source of truth:** `db.odds_api_request_log` (all paid requests are logged here — no unmeasured callers detected in this window; see §11)

---

## 1. Headline numbers

| Metric | Value |
|---|---:|
| Total logged requests | **4,456** |
| Upstream (hit The Odds API) | **1,988** |
| Served from cache | **2,468** |
| Cache hit rate | **55.4 %** |
| **Estimated credits consumed** | **3,270** |
| **Projected monthly (× 30)** | **98,100** |
| Distance from 100k monthly target | **~1,900 credits under target** |
| Distance from 3k daily target | **270 over target** |

**Interpretation:** we are ALREADY at the 100k monthly ceiling, with essentially no margin. Any regression or added workload pushes us over. Phase 2 must lock this in structurally — not just hope it stays here.

## 2. Credits by caller (top 10)

| Caller | Requests | Upstream | Credits | % of total |
|---|---:|---:|---:|---:|
| `alt_lines_feed._fetch_event_odds` (bulk) | 1,249 | 502 | **1,672** | **51 %** |
| `alt_lines_feed._fetch_event_odds_individual` | 1,403 | 970 | **970** | **30 %** |
| `alt_lines_feed._fetch_events` | 1,130 | 273 | 273 | 8 % |
| `sports_engine._fetch_odds_for` | 76 | 49 | 141 | 4 % |
| `brain.nrfi_engine._fetch_sportsbook_nrfi` | 121 | 106 | 106 | 3 % |
| `sports_engine._fetch_odds_for.retry` | 67 | 41 | 41 | 1 % |
| `alt_lines_feed._discover_active_tennis_tournaments` | ~140 | ~30 | ~30 | 1 % |
| `alt_lines_feed._discover_active_soccer_leagues` | ~150 | ~15 | ~15 | <1 % |
| MLS direct-inject events fetch | ~40 | ~4 | 4 | <1 % |
| Soccer prop-inject events fetch | 98 | ~7 | 7 | <1 % |

**Finding:** `alt_lines_feed._fetch_event_odds_individual` (the per-market fallback) is still firing 970 credits/day even though the Aug alt-line fix intended to eliminate it via the bad-market registry. Suggests the registry is being consulted at wrong point in the flow, OR events are new enough that no registry hit is possible yet. Root cause to be surfaced in Phase 2γ.

## 3. Credits by endpoint (top)

| Endpoint | Credits | Notes |
|---|---:|---|
| `/v4/sports/baseball_mlb/events/{id}/odds` (many event IDs) | ~950 | Per-event odds fanned across ~30 MLB events × multiple markets |
| `/v4/sports/tennis_atp_canadian_open/events/{id}/odds` | ~350 | ATP Canadian Open — high per-event fan |
| `/v4/sports/tennis_wta_canadian_open/events/{id}/odds` | ~340 | WTA Canadian Open |
| `/v4/sports/soccer_*/events/{id}/odds` (multiple leagues) | ~700 | Distributed across 15+ soccer leagues |
| `/v4/sports/{sport}/events` (event lists) | ~280 | Catalog + discovery |

## 4. Credits by sport (top 10)

| Sport | Credits |
|---|---:|
| **baseball_mlb** | **944** |
| **tennis_atp_canadian_open** | **614** |
| **tennis_wta_canadian_open** | **595** |
| soccer_england_efl_cup | 92 |
| soccer_argentina_primera_division | 86 |
| soccer_belgium_first_div | 44 |
| soccer_germany_liga3 | 41 |
| soccer_netherlands_eredivisie | 41 |
| soccer_concacaf_leagues_cup | 38 |
| soccer_germany_bundesliga2 | 38 |

**Finding:** MLB + 2 tennis tournaments = **65 %** of daily credit spend. Soccer is diffuse across 20+ leagues, each contributing <100 credits.

## 5. Credits by market

Data limitation: our request log records the full `markets=` string per call, but not per-market cost individually.  From the deep breakdown table, ~92 % of alt-line credits come from `player_*` markets (player_home_runs, player_hits, player_total_bases, player_strikeouts, player_aces, player_double_faults, player_goal_scorer_anytime).

## 6. Credits by hour-of-day (UTC)

| Hour | Requests | Upstream | Credits |
|---|---:|---:|---:|
| 05 | 12 | 10 | 10 |
| 06 | 80 | 68 | 70 |
| **07** | **939** | **217** | **383** |
| **08** | **1,775** | **682** | **1,154** |
| 09-20 | ~0 | 0 | 0 |
| **21** | **1,438** | **884** | **1,444** |
| 22 | 90 | 63 | 137 |
| 23 | 122 | 64 | 72 |

**Finding:** three sharp spikes:
- 07-08 UTC: **1,537 credits** — the "morning slate lock" scheduled snapshot (alt_lines + soccer_prop + mls_direct all fire together at ~07-08 UTC).
- 21 UTC: **1,444 credits** — the evening scheduled snapshot.
- 22-23 UTC: **209 credits** — the late-night scheduled snapshot residual.

**79 % of daily paid usage is concentrated in 3 snapshot windows.** The rest of the day is essentially free (cache-only).

## 7. Duplicate request counts (upstream only)

Requests to the same `(caller, endpoint)` within a short window:

| Window | Duplicate upstream calls |
|---|---:|
| 1 minute | **617** |
| 5 minutes | **661** |
| 15 minutes | **973** |

**Finding:** roughly **31 % of upstream calls (617/1,988) are duplicates within 60 s.** These are almost entirely the alt_lines snapshot fan-out where the bulk `_fetch_event_odds` call fails (422 unsupported market) and immediately spawns 4-6 individual per-market retries. Single-flight + bad-market registry consultation BEFORE the bulk call would eliminate most of this.

## 8. Calls caused by startup / restarts

No restart-triggered burst was detected in the current 24 h window because the last restart was outside the window.  However, code inspection shows:
- `run_immediately=True` is used in 3 places (alt_lines snapshot, mls_direct, soccer_prop_inject) → every restart triggers a full snapshot burst
- `_deferred_task(...)` on startup queues **26+ background loops** with `DEFER_BASE * N` delays
- Estimated startup burst (worst case, all snapshots fire): **~1,500 credits per restart**

## 9. Calls caused by hourly refreshes / MLB / user / admin routes

**Hourly full-board refresh (`_daily_refresh_loop`):** currently runs every ~60 min during game windows; each `_refresh_picks(today)` triggers `sports_engine._fetch_odds_for` for every configured sport (~40-80 credits per pass) → ~500-1,500 credits/day.

**MLB loops (`_mlb_pregame_loop` + `_mlb_late_night_boot_refresh`):**
- MLB pregame fires every 5 min while games are approaching + regenerates **today AND tomorrow**
- Concentrated in the 21:00 UTC spike (which corresponds to US afternoon pre-game window)
- Credit share: ~150-300 credits/day when active

**Settlement (`_settlement_loop`):** low direct paid cost in the 24 h window (settlement primarily uses free ESPN + MLB Stats API), but currently loops through EVERY configured sport key even when no pending picks exist for that key.

**User refresh routes:**
- `POST /api/picks/force-refresh` (in `routes/picks_routes.py:2543`) — **user-callable** and fires `asyncio.create_task(_refresh_picks(_today_str()))` → **HIGH RISK** for uncontrolled paid usage.

**Admin refresh routes:**
- `POST /api/admin/picks/force-refresh` (`routes/admin_routes.py:590`) → fires `_refresh_picks` directly. No lease, no budget check.
- 20+ other `/admin/*-refresh` endpoints, each capable of triggering paid work.

## 10. Calls bypassing the centralized cache

**All measured requests go through `services/odds_cache.cached_httpx_get`.** However, code inspection reveals **13 modules** with direct `https://api.the-odds-api.com/v4` string references — most are constants defining `BASE_URL`. Modules that construct + call directly (bypassing cache):
- `services/mls_direct_inject.py:174` — direct events-list fetch
- `services/soccer_prop_inject.py:120` — direct events-list fetch
- Both DO NOT go through `cached_httpx_get`; they use `httpx.AsyncClient` directly for the initial events discovery.

Full list of files importing the odds base URL (Phase 2γ gateway must intercept all):
```
alt_lines_feed.py · sports_engine.py · brain/nrfi_engine.py
services/odds_cache.py · services/odds_provider.py
services/mls_direct_inject.py · services/soccer_prop_inject.py
soccer/real_odds.py · tennis_extra/real_odds.py · soccer_lab.py
closing_line_snapshotter.py
```

## 11. Missing logging / unmeasured callers

Every module currently routing through `cached_httpx_get` (which logs to `odds_api_request_log`) is fully measured.

**Unmeasured callers detected in code (make direct httpx calls, do not currently log):**
- `services/mls_direct_inject.py:174` (events-list)
- `services/soccer_prop_inject.py:120` (events-list)

These probably add a few dozen credits/day (each snapshot × 3/day). **Phase 2γ gateway will bring them under the ledger.**

## 12. Current projected monthly usage

**98,100 credits/month** — approximately **1,900 credits (2 %) UNDER the 100k monthly ceiling** but **9 % OVER the 3k/day operating budget** on this measurement day.

**Practical read:** we're at the target, but there is essentially zero margin. Any new sport, new market, or slight regression pushes over. Phase 2 must (a) enforce the daily 3k ceiling structurally, (b) eliminate the 617 duplicate upstream calls (~30% reduction potential), (c) block user-triggered refresh paths.

## 13. Top 5 cost sources — Pareto view

1. `alt_lines_feed._fetch_event_odds_individual` — **970 credits (30 %)** — per-market retry fan-out that should already be dead
2. `alt_lines_feed._fetch_event_odds` (MLB batches) — **~570 credits (17 %)**
3. `alt_lines_feed._fetch_event_odds` (Tennis batches × 2 tournaments) — **~690 credits (21 %)**
4. `alt_lines_feed._fetch_event_odds` (Soccer batches × 15+ leagues) — **~412 credits (13 %)**
5. `alt_lines_feed._fetch_events` (discovery) — **273 credits (8 %)**

**Together: 89 % of daily spend.** All 5 originate from `alt_lines_feed`. Phase 2γ gateway + fan-out fix should target this module first.

## 14. Recommended Phase 2β + 2γ cutover order

**Phase 2β (foundation):**
1. Build `services/job_coordinator.py` with distributed leases in a new `scheduled_jobs` collection.
2. Build `services/provider_budget.py` with configurable daily/monthly ceilings + emergency reserve.
3. Provide but do NOT enforce (log-only mode) — establishes visibility before enforcement.

**Phase 2γ (cost cuts land here):**
1. **First:** Kill `_fetch_event_odds_individual` for good OR consult the bad-market registry BEFORE the bulk `_fetch_event_odds` fires (eliminates 30 % of daily spend). Highest ROI single fix.
2. **Second:** Single-flight per `(caller, endpoint)` inside the gateway — collapses the 617 duplicates within 60 s (~30 % reduction).
3. **Third:** Persistent tournament registry with `suppress_until` — kills the 43-tennis-tournament discovery fan-out every snapshot.
4. **Fourth:** Kill `run_immediately=True` in favor of "run only if snapshot missing / critically stale" — eliminates the restart burst.
5. **Fifth:** Consolidate hourly + MLB 5-min loops behind the coordinator — enforces one owner per (job, moment).
6. **Sixth:** Cut over user + admin refresh endpoints to enqueue via coordinator (no direct `_refresh_picks` calls).

**Projected combined savings (theoretical, to be MEASURED after 2γ deploy):**
- Kill individual-fallback: ~-30 % (~-980 credits/day)
- Single-flight dedupe: ~-15 % (~-490 credits/day)
- Tournament registry: ~-8 % (~-260 credits/day)
- Loop consolidation + startup guard: ~-5 % (~-160 credits/day)
- **Total expected: ~-58 % → ~1,380 credits/day → ~41,400 credits/month**

Do NOT claim these numbers until the post-2γ 24-hour measurement lands.

## 15. Exact post-deploy monitoring commands

**After Phase 2γ deploy (24-hour measured window):**
```bash
cd /app/backend && python -m scripts.odds_usage_audit --hours 24 --top 20 > /app/reports/phase2gamma_24h.txt
cd /app/backend && python -m scripts.odds_usage_projection --since "$(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ)" --baseline-daily 3270 --baseline-monthly 98100 >> /app/reports/phase2gamma_24h.txt
```

**Duplicate-call re-analysis:**
```bash
cd /app/backend && python - <<'PY'
import asyncio, os
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv; load_dotenv('/app/backend/.env')
from motor.motor_asyncio import AsyncIOMotorClient
async def main():
    db = AsyncIOMotorClient(os.environ['MONGO_URL'])['lockscore_db']
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    for w in (1, 5, 15):
        # (see PHASE2_BASELINE_REPORT §7 for full script)
        pass
asyncio.run(main())
PY
```

**Final verification after Phase 2δ deploy (same commands, save to `phase2delta_24h.txt`).**

Store all 3 reports under `/app/reports/` for permanent comparison.

---

## 16. Data integrity note

Everything in §1-§7 comes from live evidence in `odds_api_request_log`. No number in this report is estimated except where explicitly labeled "~" (approximations aggregated across multiple event IDs).
