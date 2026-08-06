# Phase 3C — Index Audit

**Date:** 2026-08-06
**Scope:** Every collection currently indexed by the eight legacy
`ensure_indices()` sites + the critical collections listed in the
Phase 3C contract + every ad-hoc `create_index` call in `server.py`
startup that touches a critical collection.
**Method:** Live introspection via `db.<coll>.index_information()`
against the running Mongo instance, compared to declarations in the
new central registry `services/index_registry.py`.

---

## Summary
- **Registry declares:** 67 indexes across 21 collections.
- **Critical:** 23. **TTL:** 6. **Unique:** 15.
- **Live parity (after Phase 3C ensure_all_indexes):** 66 matching, 1 newly created (`provider_request_intents.request_key_idx` — see conflict report), 0 same-name conflicts, 0 equivalent duplicates on registry-owned collections.

## Per-collection registry summary
| Collection | Total | Critical | TTL | Unique | Owner |
|---|---:|---:|---:|---:|---|
| picks | 5 | 4 | 0 | 1 | server_startup |
| users | 1 | 1 | 0 | 1 | server_startup |
| prediction_snapshots | 6 | 5 | 0 | 2 | prediction_publication_service |
| publication_mismatch_report | 2 | 0 | 0 | 0 | prediction_publication_service |
| settlement_events | 4 | 2 | 0 | 0 | settlement_service |
| pick_enrichment | 4 | 2 | 0 | 0 | enrichment_service |
| user_bets | 4 | 1 | 0 | 0 | user_bets_routes |
| parlay_history | 3 | 0 | 0 | 1 | parlay_routes |
| scheduled_jobs | 5 | 1 | 0 | 1 | job_coordinator |
| job_execution_log | 3 | 0 | 1 | 0 | job_coordinator |
| job_audit_log | 3 | 0 | 1 | 0 | job_coordinator |
| provider_budget_state | 1 | 1 | 0 | 1 | provider_budget |
| provider_request_intents | 5 | 1 | 0 | 1 | provider_budget |
| odds_api_cache | 2 | 1 | 0 | 1 | odds_cache |
| odds_api_request_log | 2 | 0 | 0 | 0 | odds_cache |
| odds_bad_market_registry | 2 | 1 | 1 | 1 | bad_market_registry |
| odds_tournament_registry | 4 | 1 | 0 | 1 | tournament_registry |
| odds_request_flights | 3 | 1 | 1 | 1 | single_flight |
| sports_catalog_snapshots | 2 | 1 | 1 | 1 | sports_catalog |
| live_alt_lines | 4 | 0 | 1 | 1 | alt_lines_feed |
| learning_snapshots | 2 | 0 | 0 | 1 | server_startup |

## TTL indexes catalogued
| Collection | Index | TTL field | Field type | Notes |
|---|---|---|---|---|
| job_execution_log | execution_ttl_idx | ttl_at | BSON Date | 30-day retention per job_coordinator |
| job_audit_log | audit_ttl_idx | ttl_at | BSON Date | 180-day retention (security audit) |
| odds_bad_market_registry | expires_at_ttl | expires_at | BSON Date | Explicit expiry |
| odds_request_flights | flight_ttl_idx | ttl_at | BSON Date | 5-minute cleanup |
| sports_catalog_snapshots | catalog_ttl_idx | ttl_at | BSON Date | Snapshot retention |
| live_alt_lines | last_seen_1 | last_seen | BSON Date | 30-minute freshness |

`publication_mismatch_report` **does not** have a TTL — see conflict report.

## Ad-hoc `server.py` `create_index` calls migrated
- `db.users.create_index("email", unique=True)` → `users.email_1`
- `db.picks.create_index([("pick_date", 1), ("sport", 1)])` → `picks.pick_date_1_sport_1`
- `db.picks.create_index([("pick_date", 1), ("lock_score", -1)])` → `picks.pick_date_1_lock_score_-1`
- `db.picks.create_index([("status", 1), ("settled_at", -1)])` → `picks.status_1_settled_at_-1`
- `db.picks.create_index("id", unique=True)` → `picks.id_1`
- `db.picks.create_index([("pick_date", 1), ("signal_score", -1)])` → `picks.pick_date_1_signal_score_-1`
- `db.learning_snapshots.create_index([("generated_at", -1)], name="learning_generated_idx")` → registry
- `db.learning_snapshots.create_index("snapshot_date", unique=True, name="learning_date_idx")` → registry

## Ad-hoc `server.py` `create_index` calls NOT migrated (auxiliary, out-of-scope)
Left in place under `on_startup()` (startup-only, not hot-path) — will move to registry in a later Phase 3 session. All are on domain-specific auxiliary collections:
- `fusion_predictions` (5 indexes)
- `learning_log`
- `soccer_matches` (3)
- `players`, `games` (unique compound), `player_game_logs`, `season_totals`, `team_form`, `historical_ingestion_state`, `props_history`
- `soccer_predictions` (4), `soccer_accuracy`
- `espn_team_meta` (unique compound), `espn_injury_notes`, `soccer_player_form` (3)
- `tennis_players` (unique)

## Legacy `ensure_indices()` migration status
| File | Function | Post-3C behaviour |
|---|---|---|
| `services/job_coordinator.py` | `ensure_indices` | Wrapper → `index_registry.ensure_collection(...)` × 3 |
| `services/provider_budget.py` | `ensure_indices` | Wrapper → `index_registry.ensure_collection(...)` × 2 |
| `services/single_flight.py` | `ensure_indices` | Wrapper → registry |
| `services/tournament_registry.py` | `ensure_indices` | Wrapper → registry |
| `services/sports_catalog.py` | `ensure_indices` | Wrapper → registry |
| `services/bad_market_registry.py` | `ensure_indices` | Wrapper → registry |
| `services/odds_cache.py` | `_ensure_indexes` | Wrapper → registry (keeps per-loop cache flag) |
| `alt_lines_feed.py` | `ensure_indices` | Wrapper → registry |
| `services/settlement_service.py` | `ensure_indices` | Wrapper → registry |
| `services/prediction_publication_service.py` | `ensure_indices` | Wrapper → registry |
| `services/enrichment_service.py` | `ensure_indices` | Wrapper → registry |

## Hot-path guardrail — `create_index` in runtime paths
Scan of `sports_engine.py`, `settlement_engine.py`, `routes/picks_routes.py`, `routes/parlay_routes.py` → **zero** `create_index` calls. Enforced by `test_no_lazy_create_index_calls_in_hot_paths`.

## Startup lifecycle
```
1. Load .env (dotenv)
2. AppSettings.load()                       # Phase 3A
3. initialize_database()                    # Phase 3B — idempotent
4. await ping_database()                    # Phase 3B — logs safe_diagnostics
5. await ensure_all_indexes(db)             # Phase 3C — creates missing, verifies critical
6. await JobCoordinator(db).ensure_indices()  # now delegates to registry (compat log line)
7. await ProviderBudget(db).ensure_indices()  # now delegates to registry
8. ... rest of on_startup() (deferred background loops)
```

## Startup performance
- Total `ensure_all_indexes(db)` first-run duration: **~85 ms** (63 verify + 1 create + summary log)
- Steady-state (all indexes already present) duration: **~55 ms**
- No noticeable impact on warm `/api/picks/today` latency (0.35–0.80 s pre and post).

## Rollback surface
- Legacy per-service `ensure_indices()` bodies now delegate to the registry. Reverting removes 11 files' delegation blocks and restores the original inline `create_index` calls — parity is trivial because the registry declarations were copied verbatim from those bodies (with the one correction to `provider_request_intents.request_key_idx`).
- No production indexes were dropped or recreated. `provider_request_intents.request_key_idx` was newly created because its old declaration was silently rejected by MongoDB (`$ne: null` in a partial filter).
