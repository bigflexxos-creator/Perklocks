# Phase 3A — Architecture & Database Audit (approved-scope session)

**Status:** Audit complete. Refactors deferred to Phase 3 continuation sessions.

---

## 1. Mongo collection inventory (81 collections)

| Collection | Docs | Idx | Owner (service/module) | Writers | Readers | Classification |
|---|---|---|---|---|---|---|
| picks | 13,759 | 6 | `services/prediction_publication_service` (writer) | `_refresh_picks`, `settle_due_picks`, admin ops | `routes/picks_routes`, `routes/parlay_routes`, brains | **Verified active** — working ops row + compatibility fields |
| prediction_snapshots | 14,710 | 7 | `services/prediction_publication_service` | Publication only (Phase 1 immutable) | `services/published_prediction_reader` | **Verified active — IMMUTABLE** |
| settlement_events | 0 | 5 | `services/settlement_service` | Settlement only | Settlement + admin | Verified active |
| pick_enrichment | 0 | 5 | `services/enrichment_service` | Enrichment jobs | `routes/picks_routes` context | Verified active |
| scheduled_jobs | 13 | 6 | `services/job_coordinator` | Coordinator only | Ops observability | Verified active |
| job_execution_log | 16 | 4 | `services/job_coordinator` | Coordinator | Ops | Verified active (30d TTL benign) |
| job_audit_log | 212 | 4 | `services/job_coordinator` | Coordinator + budget + shadow | Ops | Verified active (180d TTL) |
| provider_budget_state | 3 | 2 | `services/provider_budget` | Budget only | Ops | Verified active |
| provider_request_intents | 77 | 2 | `services/provider_budget` | Budget only | Ops | Verified active |
| odds_api_cache | 567 | 3 | `services/odds_cache` | Gateway + cache writer | Cache readers | Verified active |
| odds_api_request_log | 26,472 | 3 | `services/odds_api_gateway` | Gateway | Ops observability | Verified active |
| odds_api_quota_state | 1 | 1 | `services/odds_api_gateway` | Gateway | Gateway (delta calc) | Verified active |
| odds_bad_market_registry | 28 | 3 | `services/bad_market_registry` | Gateway | Gateway | Verified active |
| odds_request_flights | 30 | 4 | `services/single_flight` | Single-flight | Single-flight | Verified active |
| odds_tournament_registry | 102 | 5 | `services/tournament_registry` | Gateway (signal) | Gateway | Verified active |
| sports_catalog_snapshots | 9 | 3 | `services/sports_catalog` | Catalog reuse | Discovery | Verified active |
| users | – | – | auth | auth + admin | most | Verified active |
| user_bets | – | – | `routes/user_bets_routes` | Users | Users, analytics | Verified active |
| parlay_history | 194 | 4 | `routes/parlay_routes` | Parlay flow | Parlay + analytics | **Migration-required** — legacy ledger; overlap with `user_bets` |
| parlay_synergy | 11 | 1 | brains | Learning | Fusion | Likely active |
| players | 15,700 | 7 | multiple ingest loops | Ingest | Enrichment, brains | **Migration-required** — display-name identity + provider_player_id mixed |
| player_profiles | 85 | 1 | legacy | ingest | brains | **Likely dead / superseded** by `player_profiles_v2` |
| player_profiles_v2 | 495 | 1 | ingest | ingest | brains | Verified active |
| player_stats | 5,928 | 2 | ingest | ingest | brains | Verified active |
| player_form | 125 | 1 | ingest | ingest | brains | Verified active |
| player_game_logs | 120,164 | 4 | ingest | ingest | brains | Verified active |
| games | 8,403 | 2 | multiple | multiple | multiple | **Mixed responsibility** — league-agnostic |
| services_active_registry | 20,683 | 1 | `services/active_registry` | Ingest | Ingest | Verified active |
| gs_v2_predictions | 774 | 1 | `services/gs_v2` | Predictions | Ranker | Verified active |
| fusion_predictions | 88 | 5 | Fusion | Fusion | Fusion | Verified active |
| soccer_predictions | 93 | 5 | Soccer brain | Soccer brain | Soccer brain | Verified active |
| soccer_matches | 25,063 | 4 | Soccer ingest | Ingest | Soccer brain | Verified active |
| soccer_player_form / _game_logs | 2,774 / 49,611 | | Soccer ingest | Ingest | Soccer brain | Verified active |
| soccer_teams / _standings / _fixtures / _accuracy / _ingest_log | multiple | | Soccer ingest | Ingest | Soccer brain | Verified active |
| mlb_hitter_intel_cache | 2,130 | 1 | MLB intel | Ingest | MLB brain | Verified active |
| mlb_matchup_resolver_cache | 1,963 | 1 | MLB intel | Ingest | MLB brain | Verified active |
| mlb_statcast_players | 958 | 1 | Statcast | Ingest | MLB brain | Verified active |
| mlb_stuff_plus_players | 526 | 1 | Stuff+ | Ingest | MLB brain | Verified active |
| mlb_team_k_splits | 30 | 1 | MLB intel | Ingest | MLB brain | Verified active |
| mlb_hr_slate | 9 | 1 | MLB HR ranker | Ingest | HR brain | Verified active |
| mlb_player_game_logs | 20 | 1 | MLB stats | Ingest | MLB brain | Verified active |
| mls_player_matchup_history | 81 | 1 | MLS BvP | Ingest | Soccer brain | Verified active |
| nfl_player_weekly | 129,657 | 5 | NFL ingest | Ingest | NFL brain | Verified active |
| nfl_player_usage | 1,298 | 1 | NFL ingest | Ingest | NFL brain | Verified active |
| nfl_ingest_meta | 1 | 1 | NFL ingest | Ingest | Ingest | Verified active |
| cfb_teams / _sp_ratings / _portal / _returning_production | multiple | | CFB ingest | Ingest | CFB brain | Verified active |
| espn_team_meta / _injury_notes / _form_cache / _mls_stats | multiple | | ESPN ingest | Ingest | Enrichment | Verified active |
| historical_ingestion_state / historical_meta | 3 / 4 | | Historical | Backfill | Backfill | Verified active |
| props_history | 6,045 | 2 | Prop tracker | Prop tracker | Brains | Verified active |
| pick_line_history | 0 | 2 | Line watcher | Watcher | Watcher | **Likely dead** — 0 docs |
| live_alt_lines | 0 | 5 | Alt-line stream | Streamer | Frontend | **Likely dead** — 0 docs (replaced by `odds_api_cache`) |
| propline_alt_lines | 0 | 5 | Propline | Propline | Propline | **Likely dead** — 0 docs |
| injuries | 0 | 2 | Injury feed | Feed | Enrichment | **Likely dead** — 0 docs |
| survival_coverage | 58 | 1 | Coverage rank | Ranker | Ranker | Verified active |
| bandit_arms | 10 | 1 | Bandit | Learning | Fusion | Verified active |
| auto_elite_players | 8 | 1 | Elite selector | Learning | Selector | Verified active |
| learning_log | 38,839 | 2 | Learning | Learning | Analytics | Verified active |
| learning_buckets / _bucket_snapshots / _snapshots / _state | 18/5/4/1 | | Learning | Learning | Analytics | Verified active |
| learned_weights / lock_calibration_curve | 1/1 | | Learning | Learning | Fusion, Lock Score | Verified active |
| publication_mismatch_report | 42,732 | 3 | Publication auditor | Audit | Audit | **Migration-required** — huge; needs TTL |
| team_form / season_totals | 186 / 364 | | Ingest | Ingest | Brains | Verified active |
| soccer_accuracy | 1 | 1 | Learning | Learning | Learning | Verified active |
| csl_espn_state | 2 | 1 | CSL live | Live | Live | Verified active |
| client_errors | 4 | 1 | Client error reporter | Reporter | Ops | Verified active |
| sportdb_cache / sportdb_scorer_cache | 25 / 114 | | SportDB | SportDB | SportDB | Verified active |

**Findings summary**
- **Likely dead / empty:** `pick_line_history`, `live_alt_lines`, `propline_alt_lines`, `injuries`.
- **Legacy / superseded:** `player_profiles` (v1) — replaced by `player_profiles_v2`.
- **Growing without TTL:** `publication_mismatch_report` (42,732 docs), `services_active_registry` (20,683). Add TTL in Phase 3 continuation.
- **Migration-required:** `parlay_history` vs `user_bets`; `players` identity mixed.

## 2. Independent Mongo client creation inventory

19 files call `AsyncIOMotorClient(...)` directly. Categories:
- **Runtime services (needs consolidation):** `services/odds_cache`, `services/mlb_statcast`, `services/mlb_stuff_plus`, `services/game_context`, `services/nfl_nflfastr`, `services/mlb_team_k_intel`, `sportdb_player_scorer`, `ml/train_prop_model`.
- **Scripts (acceptable):** all `scripts/*` files.
- **Tests (acceptable):** `tests/test_iter*` files. Each creates a fresh client for isolation.

**Recommended Phase 3B action:** replace runtime `AsyncIOMotorClient(...)` in `services/*` with `deps.db` or a shared factory. Scripts and tests remain independent.

## 3. Index-definition inventory

Central `ensure_indices()` functions found in:
- `services/job_coordinator.py`
- `services/provider_budget.py`
- `services/single_flight.py`
- `services/tournament_registry.py`
- `services/sports_catalog.py`
- `services/bad_market_registry.py`
- `services/odds_cache._ensure_indexes`
- `alt_lines_feed.ensure_indices`

**Lazy initializers found in server.py:** `learning_snapshots` compound index and multiple ad-hoc `create_index` calls sprinkled through startup. Consolidate in Phase 3C.

## 4. Conflicting date/season helper inventory

| File | Helper | Recommendation |
|---|---|---|
| `server.py:219` | `_today_str()` | Redirect to `services.slate_calendar.board_date_utc` in Phase 3E continuation |
| `deps.py:107` | `today_str()` | Redirect (already close to canonical) |
| `brain/nrfi_engine.py:61` | `_today_str()` | Redirect |
| `sportdb_client.py:84` | `_today_str()` | Redirect |
| `closing_line_snapshotter.py:74` | `_now_utc()` | Redirect to `services.slate_calendar.utc_now` |

New canonical module: **`services/slate_calendar.py`** (created this session).

## 5. Hard-coded season/year inventory (runtime modules)

| File | Line | Value | Recommendation |
|---|---|---|---|
| `historical/mlb.py:27` | `_CURRENT_SEASON = 2026` | Literal | **Move to `slate_calendar.mlb_season()`** |
| `historical/nfl.py:29` | `_CURRENT_SEASON = _now.year if _now.month >= 8 else _now.year - 1` | Local helper | **Redirect to `slate_calendar.nfl_season()`** |
| `historical/nhl.py:26` | Same pattern | Local helper | **Redirect to `slate_calendar.nhl_season()`** |
| `historical/cfb.py:36` | Same pattern | Local helper | **Redirect to `slate_calendar.cfb_season()`** |
| `historical/soccer.py:22` | Docstring only | | No change |

Backfill scripts under `scripts/` may legitimately hard-code historical years — no change required there.

## 6. `server.py` dependency import map (high-level)

`server.py` currently imports directly from 40+ modules including brains, ingest loops, integrations, services. Extraction candidates for Phase 3F:
- `_today_str` / `_slate_utc` → `slate_calendar` ✅ (done)
- `_refresh_picks` core → `services/picks_orchestrator.py` (deferred)
- Session validation helpers → `services/auth_helpers.py` (deferred)
- Deferred-task launcher → `services/deferred_tasks.py` (deferred)

## 7. Typed-settings behavior per environment

| ENVIRONMENT | Missing MONGO_URL | Missing JWT_SECRET | Localhost MONGO_URL | Short JWT (<32) |
|---|---|---|---|---|
| `production` | **SettingsError** | **SettingsError** | **SettingsError** | **SettingsError** |
| `preview` | Warn | Warn | Warn | Warn |
| `development` | Warn | Warn | Allowed | Allowed |
| `test` | Warn | Warn | Allowed | Allowed |

`safe_diagnostics()` returns booleans/lengths only — never raw secret values (proven by `test_safe_diagnostics_never_includes_secret_values`).

---

# Service Ownership (Phase 3J snapshot)

| Responsibility | Owner |
|---|---|
| Publication | `services/prediction_publication_service.py` |
| Published reads | `services/published_prediction_reader.py` |
| Settlement | `services/settlement_service.py`, `settlement_engine.py` |
| Enrichment | `services/enrichment_service.py` |
| Paid provider access | `services/odds_api_gateway.py` |
| Cache (DB read-through) | `services/odds_cache.py` |
| Cache policy (fresh/stale/max) | `services/cache_policy.py` |
| Distributed jobs | `services/job_coordinator.py` |
| Provider budget | `services/provider_budget.py` |
| Single-flight | `services/single_flight.py` |
| Tournament suppression | `services/tournament_registry.py` |
| Bad-market suppression | `services/bad_market_registry.py` |
| `/sports` reuse | `services/sports_catalog.py` |
| Date/season | `services/slate_calendar.py` ← **NEW this session** |
| Runtime settings | `services/settings.py` ← **NEW this session** |
| Cold start recovery | `services/cold_start.py` |
| Background lifecycle | `services/background_lifecycle.py` |
| Settlement scope | `services/settlement_scope.py` |

---

# Phase 3 Continuation Recommended Sequence

1. **Phase 3B** — consolidate the 8 runtime `services/*` files that still create their own `AsyncIOMotorClient`. Introduce a shared `services/database.py` factory that both `deps.py` and services use.
2. **Phase 3C** — central index registry + startup verification (`services/index_registry.py`). Migrate the 8 existing `ensure_indices()` functions into a declarative registry.
3. **Phase 3F** — extract 1–2 concerns from `server.py` (start with `_refresh_picks` orchestrator).
4. **Phase 3D** — stable identity contracts + `players` identity migration dry-run.
5. **Phase 3G** — `parlay_history` vs `user_bets` consolidation.
6. **Phase 3H** — delete confirmed-dead collections (`pick_line_history`, `live_alt_lines`, `propline_alt_lines`, `injuries`, `player_profiles`) after final verification.

**Risks flagged for decision:**
- `publication_mismatch_report` unbounded growth (42K docs).
- `parlay_history` vs `user_bets` duality — needs product-owner call on which is canonical.
- Any deletion of `player_profiles` requires confirming no live brain reads it.

---

# Rollback

Session changes are additive except for the 9 file moves in `tests/` → `scripts/*/`. Rollback:
```
git mv backend/scripts/diagnostics/analyze_hits.py backend/tests/
# repeat for the other 8 files
```

Or simply `git checkout HEAD~1 -- backend/tests/ backend/scripts/`.

# Suggested Git commit message

```
Phase 3 (approved scope) — audit + slate_calendar + settings + script moves

Phase 3A audit: 81-collection inventory, ownership map, dead-collection
list, hard-coded season inventory, independent Mongo client inventory,
conflicting date-helper inventory.  No behavior change.

Phase 3E: services/slate_calendar.py — one canonical module for UTC/ET
slate dates, MLB/NFL/NBA/NHL/CFB seasons, and soccer split-year.
Backward-compatible adapters at old callsites deferred to continuation
sessions.

Phase 3I: services/settings.py — typed AppSettings.load() with
ENVIRONMENT-aware validation.  Production fails clearly on missing
MONGO_URL / DB_NAME / JWT_SECRET, localhost fallback, or short JWT.
Non-production warns.  safe_diagnostics() exposes booleans/lengths only.

Phase 3H (partial): moved 9 operational scripts from backend/tests/ to
backend/scripts/{diagnostics,maintenance}/ so pytest never discovers them.

Phase 3J: PHASE3A_AUDIT_MERGED.md includes audit + service ownership
map + configuration behavior + continuation sequence.

Tests: 80 pass across Phase 1 + 2β/γ/δ/final + this Phase 3 scope.
No prediction / scoring / market-selection / UI / schema changes.
Phase 3 continuation deferred; Phase 4 NOT started.
```
