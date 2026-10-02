# Preview ↔ Production Reconciliation Manifest
*2026-10-02 · PLAN ONLY · zero database writes · no deployment · no migration*

Preview inventory source: `/app/memory/preview_canonical_inventory_2026-10-02.json`
(preview fingerprint `ac7ea1130f39`, pod-local MongoDB)

Production inventory source: manual read-only inventory supplied in the request
(115 collections, listed counts; Production MONGO_URL never pasted into chat).

The plan below targets a **three-way merge into a brand-new empty Atlas database** —
*never* into either of the two existing source databases. Both sources remain read-only;
all merge work happens in the staging target.

---

## 1 · Collection classification table

Legend:
`A = MUST RECONCILE / preserve both sides` · `B = Rebuildable / cache / derived` ·
`C = Environment-specific — do not merge` · `D = Needs review` ·
`SRC = authoritative source citation (path:line)`

### A — MUST RECONCILE
| # | Collection | Preview | Prod | Authority proof |
|---|---|---:|---:|---|
| A1 | `picks` | 272,712 | 283,559 | unique `id` index `services/index_registry.py:88-90`; writer `routes/picks_routes.py:887/3107/4268`, `settlement_engine.py:802/826`; reader `routes/picks_routes.py:143/4104` |
| A2 | `prediction_snapshots` | *not inventoried on Preview* | *not inventoried on Prod* | unique `(prediction_id, snapshot_version)` + `(prediction_id, idempotency_key)` `services/index_registry.py:100-108`; writer `services/prediction_publication_service.py:351` immutable `insert_one`, supersession `update_many :375` |
| A3 | `publication_events` | — | 97,659 | append-only audit; **no unique index** — writer `services/prediction_publication_service.py:356-373` always `insert_one`; treat as log, dedupe by `(prediction_id, publication_version, publication_timestamp)` best-effort |
| A4 | `pregame_snapshots` | — | 7,081 | immutable hash-sealed authority; writer `services/production_truth/pregame_snapshot.py:144/182/191-193`; dedupe filter `{canonical_prediction_id, supersedes: None}` |
| A5 | `settlement_events` | — | 465,747 | writer `services/settlement_service.py:49 COLLECTION`, `insert_one :262`; indexes `(prediction_id, settled_at)` + `(prediction_id, is_active)` `services/index_registry.py:144-150` |
| A6 | `player_game_actuals` (× sport) | 352,584 (nfl 133,857 · mlb 128,622 · nba 51,190 · tennis 85,628 · soccer 4,487) | 106,481 total | writer `services/providers/canonical_actuals_normalizer.py:62-123`; identity `(sport, canonical_event_id, canonical_player_id | player_name)` |
| A7 | `team_game_actuals` | — | 60,204 | unique index `team_backfill_unique` on `(sport, canonical_team_id, event_id)` `services/providers/canonical_actuals_normalizer.py:130-134`; writer :151-189 |
| A8 | `player_game_logs` (× sport) | 362,381 (cfb 172,775 · nfl 71,688 · mlb 66,823 · nhl 30,680 · nba 20,415) | 193,735 total | provider-specific upserts: `historical/nhl.py:276/340`, `historical/nfl.py:310`, `historical/cfb.py:160`, `historical/mlb.py:72-90`; identity = `(sport, canonical_player_id, canonical_event_id, game_date)` composite |
| A9 | `nfl_player_weekly` | 133,077 | 133,063 | writer `services/nfl_data_ingest.py:184 refresh_nfl_weekly()`; identity `_id = "{player_id}_{season}_{week}"` (`:17/180/228`); upsert via `UpdateOne({"_id":...}, {"$set":row}, upsert=True)` |
| A10 | `games` (× sport) | 6,471 (cfb 2,465 · mlb 2,130 · nfl 907 · nhl 767 · tennis 6,207) | *not line-itemed* | writer per-sport historical modules; identity `(sport, game_id)` (`historical/mlb.py:72-90`, `historical/nhl.py:121/173`, `historical/nba.py:102/220`, `historical/nfl.py:144/228`) |
| A11 | `soccer_matches` | 25,658 | 18,194 | writer `services/soccer/cache.py:38-63`; identity composite `_match_key = (league, season, home_team, away_team, date)` (`:25-35`) |
| A12 | `soccer_player_game_logs` | 99,524 | — | reader-heavy; writer surface in `scripts/normalize_soccer_game_logs.py:23`; identity to confirm during export (fallback: `(player_name, match_id, date)`) — **flagged needs-review for exact dedupe key, see §D** |
| A13 | `tennis_matches_history` | 38,380 | 38,380 | writer `services/tennis/fallback.py:71/82/91`; identity `_match_key = (tourney_id, winner_id, loser_id, …)` (`:41-45`) |
| A14 | `users` | (not inventoried) | present | unique `email` `services/index_registry.py:97-100` — **Production account activity must never be lost** |
| A15 | `user_bets` | — | present | partial-unique `user_bet_id` `services/index_registry.py:218-224`; secondary partial-unique `(user_id, client_bet_id)` + `(user_id, idempotency_key)` :234-247; legacy-migration-guard `(migration_source, migration_source_id)` :248-254 |
| A16 | `rollover_slates` | — | present | unique `(slate_date, scope)` `services/rollover_official_slate.py:130-136` |
| A17 | `rollover_slate_events` | — | present | `(slate_date, at)` non-unique :139-143 — append-only; dedupe via natural key `(slate_date, event_id, at)` |
| A18 | `parlay_history` | — | present | legacy unique `id` `services/index_registry.py:260-262`; **migrating toward `user_bets`** per source comments → still MUST RECONCILE until migration completes |
| A19 | `player_identities` | 61,493 | 192,213 (large on prod per inventory total of 193,735 ÷ … *not actually proven, mark NOT_CAPTURED*) | unique `canonical_player_id` `services/player_identity.py:505-506`; writer `:554-703`, `services/universal_identity_ingest.py` |
| A20 | `historical_ingestion_state` | — | present | `_id = _state_key(sport, season)` `historical/multi_season.py:76-130`; resumability state — must preserve per-sport, per-season progress |
| A21 | `prediction_snapshots` (listed as A2) | — | — | — |
| A22 | `nfl_ingest_meta` | — | — | writer `services/nfl_data_ingest.py:249-252` `replace_one({"_id":"nfl_player_weekly"})` — latest-ingest watermark; dedupe by `_id` |

### B — REBUILDABLE / CACHE / DERIVED (safe to NOT merge)
| Collection | Why it's cache-like | Evidence |
|---|---|---|
| `live_alt_lines` | TTL 5400s on `last_seen`; unique `market_id` | `services/index_registry.py:386-401` |
| `odds_api_cache`, `odds_request_flights`, `sports_catalog_snapshots` | TTL `expire_after_seconds` | `services/index_registry.py:329-407` |
| `job_execution_log`, `job_audit_log` | TTL on timestamps; per-env coordinator state | `services/index_registry.py:186-210` |
| `publication_mismatch_report` | TTL 30 days on `logged_at_dt`; audit telemetry | `services/index_registry.py:138-143` |
| `production_truth_observations` | derived append-only telemetry keyed by `canonical_prediction_id` | `services/production_truth/publication_observer.py:132` |
| `provider_cache_state` / provider feed caches | regenerated on next provider pass | ad-hoc, in individual providers |

### C — ENVIRONMENT-SPECIFIC — DO NOT MERGE
| Collection | Why | Evidence |
|---|---|---|
| `canonical_worker_leases` | per-pod ownership token; `owner_instance_id` meaningless across environments; stale rows would confuse Production election | `services/canonical_worker_lease.py:53-306` |
| `scheduled_jobs` | per-deployment lease_until/status | `services/index_registry.py:186-210` |
| `provider_budget_state`, `provider_request_intents` | per-env API budget counters tied to real external quotas; cross-env merge would corrupt accounting | `services/index_registry.py:302-326` |
| `board_generations` | in-flight build pointer (`"active"` sentinel) + recent generation audit; env-local operational state | `services/board_generation.py:30/93/107-125` |

### D — NEEDS REVIEW (do not auto-merge)
| Collection | Reason |
|---|---|
| `team_identities` | **SOURCE-ABSENT in backend** — only referenced in `scripts/preview_canonical_inventory.py:168`. Preview reports 0. No writer, no index. Classify as DO-NOT-MERGE until a writer is proven. |
| `publication_events` | append-only ledger with **no unique index**; silent duplicates possible during merge. Treat as log — merge all, but write with `(prediction_id, publication_version, publication_timestamp)` best-effort dedupe and log any residual dupes to the mismatch report. |
| `soccer_player_game_logs` | writer path visible in `scripts/normalize_soccer_game_logs.py` but exact upsert key not isolated in-source. **Operator must confirm key** (likely `(player_name OR canonical_player_id, match_id, date)`) before unattended merge. |
| any collection not listed above and present only on one side | treat as D until authority proven; abort or escalate. |

---

## 2 · Identity / dedupe keys (verified from source)

| Collection | Identity key | Source |
|---|---|---|
| `picks` | `id` | `services/index_registry.py:88-90`; `routes/picks_routes.py:143/887/3107/4104/4268` |
| `prediction_snapshots` | `(prediction_id, snapshot_version)` **and** `(prediction_id, idempotency_key)` | `services/index_registry.py:100-108` |
| `publication_events` | `(prediction_id, publication_version, publication_timestamp)` *(best-effort — no DB unique index)* | `services/prediction_publication_service.py:356-373` |
| `pregame_snapshots` | `canonical_prediction_id` where `supersedes IS NULL` | `services/production_truth/pregame_snapshot.py:144/182/191-193` |
| `settlement_events` | `(prediction_id, settled_at)` (active row filter: `is_active=true`) | `services/index_registry.py:144-150`; `services/settlement_service.py:49/262/296` |
| `player_game_actuals` | `(sport, canonical_event_id, canonical_player_id OR player_name, market_or_stat)` | `services/providers/canonical_actuals_normalizer.py:62-123` |
| `team_game_actuals` | `(sport, canonical_team_id, event_id)` — unique index `team_backfill_unique` | `services/providers/canonical_actuals_normalizer.py:130-134/151-189` |
| `player_game_logs` | `(sport, canonical_player_id, canonical_event_id, game_date)` composite — provider-specific | `historical/mlb.py:72-90`, `historical/nhl.py:121/173/276/340`, `historical/nfl.py:144/228/310`, `historical/cfb.py:160`, `historical/nba.py:102/220` |
| `nfl_player_weekly` | `_id = "{player_id}_{season}_{week}"` | `services/nfl_data_ingest.py:17/180/228` |
| `games` | `(sport, game_id)` | `historical/mlb.py:72-90`, `historical/nhl.py:121/173`, `historical/nba.py:102/220`, `historical/nfl.py:144/228` |
| `soccer_matches` | `_match_key = (league, season, home_team, away_team, date)` | `services/soccer/cache.py:25-35/38-63` |
| `tennis_matches_history` | `_match_key = (tourney_id, winner_id, loser_id, …)` | `services/tennis/fallback.py:41-45/71/82/91` |
| `users` | `email` (unique) | `services/index_registry.py:97-100` |
| `user_bets` | `user_bet_id` partial-unique **AND** `(user_id, client_bet_id)` **AND** `(user_id, idempotency_key)` | `services/index_registry.py:218-247` |
| `rollover_slates` | `(slate_date, scope)` unique | `services/rollover_official_slate.py:130-136` |
| `rollover_slate_events` | `(slate_date, event_id, at)` best-effort (no unique index) | `services/rollover_official_slate.py:139-143` |
| `parlay_history` | `id` unique (legacy) | `services/index_registry.py:260-262` |
| `player_identities` | `canonical_player_id` unique | `services/player_identity.py:505-506/554-703` |
| `historical_ingestion_state` | `_id` = `_state_key(sport, season)` | `historical/multi_season.py:76-130` |
| `nfl_ingest_meta` | `_id = "nfl_player_weekly"` singleton + per-sport analog keys | `services/nfl_data_ingest.py:249-252` |
| `canonical_worker_leases` | `lease_name` unique (do NOT merge across envs — class C) | `services/canonical_worker_lease.py:53/158` |

**Rule**: Never dedupe by Mongo `_id` unless the writer actually sets `_id` deterministically from identity fields (true for `nfl_player_weekly`, `board_generations`, `historical_ingestion_state`, `nfl_ingest_meta`).

---

## 3 · Conflict policy

### Universal rules (apply to every A-row)
1. **Identical record → keep one.** Compare canonical fields in a deterministic order, hash both sides, drop one if the hash matches.
2. **One-side-only record (Preview-only or Production-only) → preserve.** Record `provenance = "preview"|"production"` on the merged row.
3. **Same canonical identity, different fields → explicit rule per collection (below).**
4. **Authority outranks timestamp.** "Newer timestamp wins" is NEVER applied universally — a stale Production `settlement_events` must not be overwritten by a Preview-generated event just because Preview wrote it later during testing.
5. **Immutable outranks mutable.** `prediction_snapshots` values always outrank the mirrored presentation fields on `picks`. `pregame_snapshots` always outranks any later-written mutable game state.
6. **High-authority conflict → abort merge for that collection**; emit the conflicting pair to `conflict_report.jsonl` and require an operator decision.

### Per-collection policy

| # | Collection | Policy on same-identity, different-fields |
|---|---|---|
| A1 | `picks` | Production is the canonical presentation board. If `publication_state == PUBLISHED` on BOTH sides for the same `id`, Production wins on `publication_state`, `settled_at`, `result`, `status`, `settlement_authority`. Preview wins ONLY if Production record is missing. Any divergence of `canonical_pick_id` or `prediction_id` → ABORT (identity collision). |
| A2 | `prediction_snapshots` | immutable — if canonical fields differ for the same `(prediction_id, snapshot_version)` → ABORT; this indicates two independent canonical writes and MUST be investigated |
| A3 | `publication_events` | append-only — union both sides; best-effort dedupe by key tuple; residual duplicates logged, not blocked |
| A4 | `pregame_snapshots` | immutable — same policy as A2; ABORT on divergence |
| A5 | `settlement_events` | **Production authority**. For the same `(prediction_id, settled_at)` where `is_active=true`, Production ALWAYS wins. Preview's settlement_events are TEST-SIDE data and MUST NOT replace Production's. If Production has no event for a prediction that Preview settled → operator decision (likely preserve Production unresolved state). |
| A6 | `player_game_actuals` | **Provider-backed canonical only.** If the same identity exists on both sides, keep whichever row's `provenance` is in the known-authoritative set (`espn`, `nflverse`, `the_odds_api`, etc. per `canonical_actuals_normalizer`). Never fabricate missing fields. If both have provenance and values diverge → ABORT (requires provider-source re-fetch). |
| A7 | `team_game_actuals` | same as A6 |
| A8 | `player_game_logs` | same as A6 |
| A9 | `nfl_player_weekly` | same `_id` on both sides → prefer newer `source_fetched_at` IF both provenance is `nflverse` (idempotent nflverse re-pull is safe). If sources differ → ABORT. |
| A10 | `games` | identity `(sport, game_id)` — prefer the row whose `status` has stronger finality (FINAL > SCHEDULED > CANCELLED > POSTPONED). Divergence on `score`/`winner` with both FINAL → ABORT. |
| A11 | `soccer_matches` | same identity key → prefer row with populated `scores` + `closing_odds` (see `cache.py:49` existing merge intent). |
| A12 | `soccer_player_game_logs` | HOLD — see §D. |
| A13 | `tennis_matches_history` | identity composite → prefer row with completed stats (more non-null fields in the stat dict) |
| A14 | `users` | **Production is canonical for every user**. Preview-only users become `preview_only` tagged in staging and do NOT propagate to Production credentials. Duplicates on `email` → Production wins all account/credential fields; Preview retains only non-conflicting demo data. |
| A15 | `user_bets` | Production authority. Preview-only user_bets (ones belonging to Preview-only users) travel with the Preview user as `preview_only`. Shared user: Production wins. ABORT on identity collision across different users. |
| A16 | `rollover_slates` | identity `(slate_date, scope)` — prefer row that is `frozen=true` (operational truth) |
| A17 | `rollover_slate_events` | union both; best-effort dedupe |
| A18 | `parlay_history` | legacy `id` unique → prefer the one carrying the newer `user_bets` migration state |
| A19 | `player_identities` | `canonical_player_id` unique → prefer row with more identity-resolver provenance fields populated (`espn_id`, `nflverse_id`, `the_odds_api_id`, …). ABORT on divergent `canonical_player_name` for the same `canonical_player_id`. |
| A20 | `historical_ingestion_state` | prefer the side whose `last_ingested_at` is later AND whose `cursor` monotonically moves forward — never move the cursor backward. |

---

## 4 · Reconciliation manifest (per-collection action card)

Each A-row below is actionable. B- and C-rows are included explicitly for completeness
with their action set to `SKIP_MERGE`.

| # | Collection | Class | Prev count | Prod count | Identity key | Authority | Conflict policy | Merge action | Risk |
|---|---|:-:|---:|---:|---|---|---|---|:-:|
| A1 | picks | A | 272,712 | 283,559 | `id` | Production (publication_state) | §3 A1 | MERGE_INTO_STAGING | ⚠ H |
| A2 | prediction_snapshots | A | NOT_CAPTURED | NOT_CAPTURED | `(prediction_id, snapshot_version)` | Immutable | §3 A2 | MERGE_INTO_STAGING | H |
| A3 | publication_events | A | NOT_CAPTURED | 97,659 | best-effort tuple | Append-only | §3 A3 | APPEND_UNION | L |
| A4 | pregame_snapshots | A | NOT_CAPTURED | 7,081 | `canonical_prediction_id` + supersedes | Immutable | §3 A4 | MERGE_INTO_STAGING | M |
| A5 | settlement_events | A | NOT_CAPTURED | 465,747 | `(prediction_id, settled_at)` | **Production wins** | §3 A5 | MERGE_PROD_PRIORITY | ⚠ H |
| A6 | player_game_actuals | A | 352,584 | 106,481 | `(sport, event, player, market)` | Provider-backed | §3 A6 | MERGE_INTO_STAGING | ⚠ H |
| A7 | team_game_actuals | A | 0 | 60,204 | `(sport, team, event)` | Provider-backed | §3 A6 | COPY_FROM_PRODUCTION | L |
| A8 | player_game_logs | A | 362,381 | 193,735 | provider composite | Provider-backed | §3 A6 | MERGE_INTO_STAGING | ⚠ H |
| A9 | nfl_player_weekly | A | 133,077 | 133,063 | `_id` composite | nflverse | §3 A9 | MERGE_INTO_STAGING | L |
| A10 | games | A | 6,471 | NOT_CAPTURED | `(sport, game_id)` | Provider | §3 A10 | MERGE_INTO_STAGING | M |
| A11 | soccer_matches | A | 25,658 | 18,194 | `_match_key` | Provider | §3 A11 | MERGE_INTO_STAGING | M |
| A12 | soccer_player_game_logs | A | 99,524 | NOT_CAPTURED | **needs review** | Provider | §3 A12 | HOLD — operator confirms key | ⚠ H |
| A13 | tennis_matches_history | A | 38,380 | 38,380 | `_match_key` | TML-Database | §3 A13 | MERGE_INTO_STAGING | L |
| A14 | users | A | NOT_CAPTURED | present | `email` | **Production wins** | §3 A14 | MERGE_PROD_PRIORITY | ⚠ H |
| A15 | user_bets | A | NOT_CAPTURED | present | partial-unique keys | **Production wins** | §3 A15 | MERGE_PROD_PRIORITY | ⚠ H |
| A16 | rollover_slates | A | NOT_CAPTURED | present | `(slate_date, scope)` | frozen=true wins | §3 A16 | MERGE_INTO_STAGING | M |
| A17 | rollover_slate_events | A | NOT_CAPTURED | present | tuple | Append-only | §3 A17 | APPEND_UNION | L |
| A18 | parlay_history | A | NOT_CAPTURED | present | `id` | — | §3 A18 | MERGE_INTO_STAGING | M |
| A19 | player_identities | A | 61,493 | NOT_CAPTURED | `canonical_player_id` | — | §3 A19 | MERGE_INTO_STAGING | M |
| A20 | historical_ingestion_state | A | NOT_CAPTURED | NOT_CAPTURED | `_id` | cursor-forward only | §3 A20 | MERGE_FORWARD_ONLY | M |
| A22 | nfl_ingest_meta | A | NOT_CAPTURED | NOT_CAPTURED | `_id` | later watermark wins | §3 A20 | MERGE_FORWARD_ONLY | L |
| B* | live_alt_lines / odds_* / job_* / publication_mismatch_report / production_truth_observations / provider_cache_state | B | — | — | — | — | — | **SKIP_MERGE** (regenerated by Production workers after cutover) | — |
| C1 | canonical_worker_leases | C | — | — | `lease_name` | per-env | — | **SKIP_MERGE** (new empty collection on staging target) | — |
| C2 | scheduled_jobs, job_execution_log, job_audit_log | C | — | — | — | per-env | — | **SKIP_MERGE** | — |
| C3 | provider_budget_state, provider_request_intents | C | — | — | — | per-env | — | **SKIP_MERGE** | — |
| C4 | board_generations | C | — | 15,438 | `_id` | per-env in-flight | — | **SKIP_MERGE** (staging starts clean) | — |
| D1 | team_identities | D | 0 | NOT_CAPTURED | — | SOURCE-ABSENT | — | **SKIP_MERGE** until writer proven | — |

**Legend**: `NOT_CAPTURED` = Production count not line-itemed in the inventory supplied; do **not** invent counts.
Any Production collection not line-itemed above is present in the 115-collection total but marked `NOT_CAPTURED` and must either (a) have a count added to the inventory or (b) be processed under `MERGE_PROD_ONLY` fallback.

---

## 5 · Dry-run reconciliation tooling

Created at `/app/backend/scripts/reconcile_dry_run.py`.

* READ-ONLY to both source databases (preview + production dumps).
* Target staging DB is required but we **refuse to write** unless `--i-have-made-backups --execute-into-staging-db <uri>` is passed. Default mode is `--dry-run` and emits only reports — no connections to a target DB are even opened.
* Fail-closed on: unresolved high-authority conflict, source DB missing, identity key fetch failure.
* Emits (to stdout + files under `/app/memory/reconcile_dry_run/`):
  * `collection_classification.json` — the §1 table as data
  * `identity_keys.json` — the §2 table
  * `collection_counts.json` — per-source counts (Preview from local, Production from JSON input the operator pastes)
  * `conflict_report.jsonl` — one line per detected cross-source conflict
  * `provenance.jsonl` — one line per record decision in dry-run (sampled or exhaustive)
  * `checksums_before.json` / `checksums_after.json` — collection-level hashes of a stable
    sort of identity keys only (not record contents — avoids massive memory load).

The tool imports NOTHING from the backend runtime and uses its own Motor client.
It never mutates `MONGO_URL`, `DB_NAME`, or any env variable.
It will NOT run against the real Production MONGO_URL from inside this coding environment —
it requires explicit CLI args pointing at `mongodump`-ed local snapshots or an operator-provided URI.

---

## 6 · Collections UNSAFE to automatically merge

* `settlement_events` (465,747) — any wrong overwrite directly affects user bet grading.
* `users` + `user_bets` — any wrong overwrite is a real-money / account-trust violation.
* `prediction_snapshots` — immutable; cross-env identity collision indicates a bug, not a merge.
* `pregame_snapshots` — same immutability reason.
* `soccer_player_game_logs` — dedupe key not isolated in source; needs operator confirmation.
* `team_identities` — SOURCE-ABSENT; treat as DO-NOT-MERGE until a writer is proven.
* Anything the operator inventory marks `NOT_CAPTURED`.

---

## 7 · Exact next human/export step after this plan

The remaining steps are **owner-only**, and none of them run from this coding environment:

1. `mongodump` the Preview DB locally (read-only against pod-local Mongo).
2. From Manage Publishes → Database, `mongodump` the Production DB (read-only).
3. Run `reconcile_dry_run.py` against the two local dump mongos:
   `python scripts/reconcile_dry_run.py --preview-uri mongodb://<local-preview-dump> --production-uri mongodb://<local-production-dump> --dry-run`
4. Review `conflict_report.jsonl` and all `D`/`H` risk rows. For every high-authority conflict, make an explicit operator decision and append to `conflict_decisions.json`.
5. Provision the new empty Atlas cluster, `lockscore_db`.
6. Re-run the reconciliation with `--execute-into-staging-db <new atlas uri> --i-have-made-backups --conflict-decisions /path/to/conflict_decisions.json`.
7. Verify counts + spot-check canaries on the staging cluster.
8. Then (and only then) proceed with Phase 7 Production cutover per the previous session's runbook.

**Both source databases remain intact.** The old Preview + Production DBs are only cut off *after* the staging target has been accepted and Production is switched over.

---

## 8 · Confirmation — zero database writes occurred during this pass

- `git status --short` shows only new/edited **plan and tooling files**:
  - `memory/reconciliation_manifest_2026-10-02.md` (this file)
  - `backend/scripts/reconcile_dry_run.py` (new, dry-run default)
- No `MONGO_URL` changed, no `.env` touched, no Mongo write issued.

## 9 · Confirmation — zero sports / model / scoring changes occurred

No changes in: `sports_engine.py`, NHL V1, NHL V2 (still dormant), Probability Authority,
Lock Score, 85+ eligibility, settlement rules, canonical publication semantics,
NFL alt writer, canonical worker lease logic.

**STOP.** No Atlas provisioning, no migration, no deployment.
