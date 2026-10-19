# Deployment Gate — P0 Fixes Report

**Date**: 2026-06-19
**Last commit**: `5f26f0e3`
**Status**: All code-fixable blockers resolved. Remaining items require operator action in the deploy environment (not code).

---

## Deploy-agent findings → resolution matrix

| # | Finding | Round | Resolution | Status |
|---|---|---|---|---|
| 1 | Hardcoded `PINNED_PREVIEW_URL` in `frontend/src/lib/api.ts` | 1 | Removed the constant + dead-code branch; backend URL now resolved only from `EXPO_PUBLIC_BACKEND_URL` | ✅ FIXED |
| 2 | Hardcoded `DB_NAME="perkslocks_production"` fallback | 1 | `_resolve_env_config()` now raises `DatabaseError` when `MONGO_URL`/`DB_NAME` unset in production/preview | ✅ FIXED |
| 3 | `.gitignore` blocks `.env.*` deploy-manifest files | 1 | Narrowed to only ignore developer `.local` overrides; `.env`, `backend/.env`, `frontend/.env` now visible to deploy pipeline | ✅ FIXED |
| 4 | Index verifier marks spec as missing when same key-set lives under a different name | 2 | `verify_collection_indexes()` promotes equivalent-duplicate specs out of `missing` when uniqueness/sparse/TTL semantics match | ✅ FIXED |
| 5 | Auto-destructive `delete_many()` in `heal_critical_unique_prerequisites()` at startup | 3→4 | Removed the heal from the startup path entirely; moved to explicit operator CLI `backend/scripts/maintenance/heal_critical_unique_indexes.py` (dry-run default, `--commit` required) | ✅ FIXED |
| 6 | `memory/test_credentials.md` plaintext demo creds | 4 | File is already untracked and gitignored (line 95). False positive. | ✅ RESOLVED |
| 7 | `expo-media-library` plugin + photosPermission missing | 5 | Added plugin, iOS `NSPhotoLibraryUsageDescription`, Android `READ_MEDIA_IMAGES`/`WRITE_EXTERNAL_STORAGE` in `frontend/app.json` | ✅ FIXED |
| 8 | `picks.id_1`, `prediction_snapshots.prediction_snapshot_version_uniq`, `parlay_history.id_1` fail to build on null/dupe `id` rows | 6 | Converted all three to **partial unique** indexes (`partial_filter={id/prediction_id: {$type: "string"}, snapshot_version: {$exists: true}}`). Uniqueness preserved for every real row; null/dupe legacy rows no longer block index build. | ✅ FIXED |
| 9 | `player_identity.py:402` throws `dictionary update sequence element #0 has length 1` on malformed `provider_ids` | 6 | `hydrate_from_dicts()` now defensively coerces list-of-tuples, JSON-string, and None shapes → safe empty dict | ✅ FIXED |

**Code-fixable blockers remaining: 0**

---

## Remaining items — require operator / env action, not code

### A. TTL indexes on ephemeral collections (deploy-agent WARN, policy false-positive)
The deploy agent flags any Mongo TTL index as `destructive_db_startup`.  Every TTL in this codebase is on **ephemeral operational data** that is designed to age out:

| Collection | TTL | Purpose |
|---|---|---|
| `live_alt_lines.last_seen_1` | 90 min | Live odds cache (refreshes every 15 min) |
| `publication_mismatch_report.mismatch_logged_at_dt_ttl` | 30 days | Audit log retention |
| `job_execution_log.execution_ttl_idx` | — | Job execution log |
| `job_audit_log.audit_ttl_idx` | — | Audit log retention |
| `odds_bad_market_registry.expires_at_ttl` | — | Short-lived bad-market markers |
| `odds_request_flights.flight_ttl_idx` | — | In-flight request tracking |
| `sports_catalog_snapshots.catalog_ttl_idx` | — | Sports catalog snapshot cache |

**Not changed** — removing them would cause unbounded growth of transient operational data.  This is standard operational hygiene, not destructive automatic retention of user data.  Please instruct the deploy pipeline to accept this classification or exempt these specific indexes.

### B. Deploy-env data drift (unique index can't be built on null/dupe rows)
Even with the **partial unique** fix applied, if the deploy env's `canonical_parlay_history`, `picks`, or `prediction_snapshots` has NULL `id` rows under the OLD plain-unique index name, the OLD index must be dropped before the new partial-unique can be built.

**Operator runbook** (run on the deploy Mongo cluster before next deploy):

```bash
# Preview what would be moved to quarantine:
python backend/scripts/maintenance/heal_critical_unique_indexes.py

# Actually move legacy null/duplicate id rows to <coll>__dup_quarantine:
python backend/scripts/maintenance/heal_critical_unique_indexes.py --commit

# Optionally drop legacy plain-unique indexes so the next boot creates
# the partial-unique variants cleanly:
# mongosh> db.canonical_parlay_history.dropIndex("id_1")
# mongosh> db.picks.dropIndex("id_1")
# mongosh> db.prediction_snapshots.dropIndex("prediction_snapshot_version_uniq")
```

Rows are NEVER destroyed — every quarantined row survives in `<coll>__dup_quarantine` with `_quarantine_reason`, `_quarantine_spec`, `_quarantine_at` fields for later review or reinstatement.

### C. Third-party integration keys (operator action)
| Integration | Env var | Current state |
|---|---|---|
| BallDontLie NBA historical | `BALLDONTLIE_KEY` | Missing in deploy — NBA historical ingest returns 401 |
| football-data.org | — | 429 rate limited (backs off naturally) |

The NBA 401 is a WARN only, not a deploy blocker.  Set the key in the deploy env secret store if NBA historical analytics are required.

### D. Store-review items (not deploy gates)
Apple review typically requires an in-app account-deletion flow for apps that let users create accounts.  Current app has sign-up and sign-out but no delete-account path.  This is a **store-submission** requirement, not a deploy gate — the deploy itself will succeed without it.

---

## Local verification (last run)

```
Phase 3F-2 preflight: ok=True db=True idx=True recov=True duration_ms=173
/api/ready → 200 OK, {ready:true, indexes_ready:true, live_db_ping_ok:true}
heal_critical_unique_indexes.py (dry-run) → "Nothing to heal"
Tests: 50/50 passed (17 Phase 3C index-registry + 20 identity crosswalk + 13 historical adapter merge)
```

---

## Commits (chronological)

| SHA | Change |
|---|---|
| `0ebfb629` | Round 1: 3 initial blockers — hardcoded URL, hardcoded DB name, gitignore |
| `2261af34` | Round 2: equivalent-duplicate indexes satisfy critical readiness |
| `6d2e6930` | Round 3: index self-heal + allow .env for deploy rewrite (reverted in round 4) |
| `d66aa4c9` | Round 4: moved index heal to explicit operator script (non-destructive) |
| `a3c99033` | Round 5: expo-media-library plugin + actionable index-drift hint |
| `5f26f0e3` | Round 6: partial-unique indexes + player_identity hydrate guard |

No touches to scoring, thresholds, models, NHL, CFB, board generation, provider selection, or history ingestion.
