# PerksLocks Backend Architecture

**Document version:** Phase 1a draft (2026-08-06)
**Status:** Design contract — implementation in progress
**Scope:** Backend prediction pipeline, publication lifecycle, service ownership

---

## 1. High-level topology

```
┌─────────────────────────────────────────────────────────────────────┐
│                   PerksLocks Full-Stack Layout                       │
├─────────────────────────────────────────────────────────────────────┤
│  Expo React Native (frontend)      port 3000  (dev)                  │
│         │                                                             │
│         │  HTTPS  /api/*                                              │
│         ▼                                                             │
│  FastAPI + Uvicorn (backend)       port 8001                         │
│         │                                                             │
│         │  Motor (async pymongo)                                      │
│         ▼                                                             │
│  MongoDB (standalone — NOT a replica set)                            │
└─────────────────────────────────────────────────────────────────────┘
```

## 2. Canonical prediction pipeline

Every prediction on the board flows through the following stages **in
this exact order**.  Publication is the single write barrier that seals
the numbers.

```
┌──────────────────────────────────────────────────────────────────┐
│  1. DATA INGESTION                                                │
│     • odds_cache (SWR) → The Odds API                             │
│     • ESPN / MLB Stats API / Understat / SportDB / Football-Data  │
│     • per-sport fetchers in `sports_engine.fetch_*_picks`         │
│                                                                    │
│  2. FEATURE ENGINEERING                                            │
│     • ml/feature_builder.py                                        │
│     • ml/features/mlb.py / nba.py / tennis.py / soccer.py         │
│                                                                    │
│  3. ML MODELS                                                      │
│     • ml/train_prop_model.py (offline)                             │
│     • services/trained_prediction_engine.py (inference)            │
│                                                                    │
│  4. PREDICTION FUSION                                              │
│     • services/prediction_fusion_engine.py                         │
│     • Combines ML output + prior + odds signal into a single       │
│       probability + reasoning bundle                               │
│                                                                    │
│  5. MAGIC TIER (Lock V2)                                           │
│     • services/magic_tier_v2.py                                    │
│     • services/adaptive_learning/*                                 │
│     • Assigns lock_score + grade + confidence                      │
│                                                                    │
│  6. QUALITY GATE                                                   │
│     • quality_gate.py                                              │
│     • Applies coherence caps, coherence floor, no_bet flags        │
│                                                                    │
│  7. BOARD VALIDATOR                                                │
│     • board_validator.py                                           │
│     • Global slate coherence + contradiction rejection             │
│                                                                    │
│  8. PREDICTION PUBLICATION      ← **WRITE BARRIER**                │
│     • services/prediction_publication_service.py                   │
│     • Emits an immutable snapshot                                  │
│     • Stamps published_probability / edge / lock_score / grade /   │
│       confidence / reasoning / line / odds                         │
│     • Stamps board_version, model_version, scoring_version,        │
│       calibration_version, validator_version, simulation_version,  │
│       fusion_version, feature_snapshot_version                     │
│     • payload_hash + idempotency key ⇒ safe under retry            │
│                                                                    │
│  9. SETTLEMENT                                                     │
│     • Reads from prediction_snapshots (never mutates)              │
│     • Writes settlement_events                                     │
│                                                                    │
│ 10. LEARNING / CALIBRATION                                         │
│     • Reads from prediction_snapshots + settlement_events          │
│     • Writes model / calibration versions used by future runs      │
└──────────────────────────────────────────────────────────────────┘
```

**Contract:** After stage 8, no service may mutate any published field.

## 3. Service ownership

| Concern | Owning module | Reads | Writes |
|---|---|---|---|
| Data ingestion | `sports_engine.fetch_*`, `services/odds_cache` | External APIs | `odds_cache`, in-mem candidate list |
| Feature engineering | `ml/feature_builder`, `ml/features/*` | ingested candidates + `player_game_logs`, `soccer_player_form`, `mlb_statcast_*`, `tennis_matches_history` | *(pure)* |
| Model inference | `services/trained_prediction_engine`, `soccer/predictor` | features | *(pure)* |
| Fusion | `services/prediction_fusion_engine` | model outputs | *(pure)* |
| Magic Tier V2 | `services/magic_tier_v2`, `services/adaptive_learning/*` | fusion output + learning params | *(pure)* |
| Quality gate | `quality_gate` | prior stages | *(pure — annotates in-mem candidate)* |
| Board validator | `board_validator` | prior stages | *(pure — annotates in-mem candidate)* |
| **PUBLICATION** | **`services/prediction_publication_service`** | validated candidate | **`prediction_snapshots` (create) + `picks` (dual-write)** |
| Settlement | `settlement_engine`, `prop_settlement`, `soccer_espn_settle`, `kbo_settlement`, `tennis_extra/settle`, `brain/nrfi_engine` | `prediction_snapshots` (via helper) | **`settlement_events` (Phase 1c)** |
| Learning | `learning_engine`, `learning_system_v2`, `bandit`, `services/adaptive_learning/*` | `prediction_snapshots` + `settlement_events` | model params only |
| Enrichment (post-publication) | `pick_enrichment`, `services/pick_matchup_wiring`, `services/signal_engine`, xG modules | `prediction_snapshots` | **`pick_enrichment` collection only — NEVER touches `published_*`** |

## 4. Collection ownership

| Collection | Owner | Mutable? | Purpose |
|---|---|---|---|
| `picks` | `_refresh_picks` (writer), all endpoints (readers) | ✅ during Phase 1 dual-write · will become **presentation view** in Phase 1c | Current mutable board view; will become a projection of latest snapshot |
| **`prediction_snapshots`** *(new)* | **`prediction_publication_service`** | ❌ **immutable after write** | Source of truth for published predictions |
| **`settlement_events`** *(new — Phase 1c)* | `settlement_engine` et al. | ✅ append-only | Settlement decisions referencing snapshots |
| **`pick_enrichment`** *(new — Phase 1a)* | `pick_enrichment`, `signal_engine`, xG modules | ✅ append/upsert | Presentation-only enrichment (never touches `published_*`) |
| `odds_cache` | `services/odds_cache` | ✅ SWR TTL | Odds API response cache |
| `odds_api_request_log` | `services/odds_cache` | append-only | Audit log of upstream calls |
| `odds_bad_market_registry` | `services/bad_market_registry` | ✅ 24h TTL | Deny-list for 422 markets |
| `live_alt_lines` | `alt_lines_feed` | ✅ upsert by market_id | Real DK/FanDuel alt-line rows |
| `parlays`, `parlay_history` | Parlay routes | ✅ | User-saved parlays |
| `users` | Auth routes | ✅ | User accounts |
| `soccer_player_game_logs`, `soccer_player_form` | soccer ingest | append-only | Understat rolling data |
| `player_game_logs` | MLB/NBA/NFL ingest | append-only | Per-game stats |
| `tennis_matches_history`, `tennis_players` | tennis ingest | append/upsert | Tennis Elo + form |
| `mlb_lineups`, `mlb_statcast_*`, `mlb_stuff_plus` | MLB ingest | append/upsert | MLB signals |
| `pick_line_history` | line observer | append-only | Line movement snapshots |
| `fusion_predictions` | fusion engine | ✅ upsert | Fusion output cache |
| `learning_snapshots` | learning engine | ✅ upsert | Per-bucket historical ROI |

## 5. Scheduler architecture

All background workers are started inside `server.py`'s FastAPI startup
lifecycle (`_deferred_task` / `asyncio.create_task`).  There is **no
external scheduler** (no APScheduler / Celery / cron).

**Continuous loops (must be reviewed / migrated in Phase 1b–1c):**
- `_settlement_loop` — every 5 min
- `_grading_validator_loop` — every 15 min
- `_stuck_pick_reaper` — every 60 min
- `_line_observer_loop` — every ~90 s pre-game
- `_closing_snapshotter` — at kick-off events
- `_steam_detector_loop` — every 10 min pre-game
- MLB pregame refresh loop — every 5 min during afternoon
- Live-scores loop

**Scheduled snapshots (migrated 2026-08 Phase A/B):**
- `alt_lines_feed` — 3×/day (12:00 / 18:00 / 23:00 UTC)
- `mls_direct_inject` — 3×/day (same cadence)
- `soccer_prop_inject` — 3×/day (same cadence)

**Pipeline entrypoints (call `_refresh_picks` → `generate_all_picks` →
publication):**
- `startup_event`: initial refresh
- Timed refresh loop (every hour during game windows)
- MLB pregame loop (`sport_filter="MLB"`)
- Admin-triggered `/api/admin/refresh` endpoint

## 6. Data flow — publication lifecycle

```
sports_engine.generate_all_picks(date_str)
     │
     ▼
_refresh_picks(date_str)                        ← server.py:1559
     │
     │  Phase 2.5+ enrichment (xG, career history, GK quality)
     │  Tennis Extra scrape
     │  MLB BvP enrichment
     │  UUID assignment
     │  SportDB enrichment
     │  Learning engine (apply_learning)
     │  Elite Player Boost
     │  Goalscorer dedup + Top-3
     │  Tennis Totals cap
     │  Player Form (±5 lock)
     │  Multi-Armed Bandit (±lift lock)
     │  MLB Prop Simulator
     │  Fusion Enrichment
     │
     │  ── Phase 1a WRITE BARRIER inserted here ──
     ▼
prediction_publication_service.publish_batch(candidates)
     │
     ├─► prediction_snapshots (immutable insert per candidate)
     │
     └─► picks (dual-write w/ published_* fields — Phase 1b will
         switch endpoints to read these fields; Phase 1c will make
         `picks` a lightweight projection)

     ▼
   response served to frontend
```

## 7. Environment variables (names only)

**Backend (`/backend/.env`):**
- `MONGO_URL` — MongoDB connection string
- `DB_NAME` — Database name
- `THE_ODDS_API_KEY` — The Odds API
- `EMERGENT_LLM_KEY` — universal LLM key
- `FOOTBALL_DATA_API_KEY` — Football-Data.org
- `API_SPORTS_KEY` — API-Sports
- `JWT_SECRET` — Auth signing
- `JWT_ALGORITHM` — Typically HS256
- `BCRYPT_ROUNDS` — Password hashing rounds

**Frontend (`/frontend/.env`):**
- `EXPO_PACKAGER_PROXY_URL` — Emergent-set
- `EXPO_PACKAGER_HOSTNAME` — Emergent-set
- `EXPO_PUBLIC_BACKEND_URL` — Backend URL

## 8. External APIs

| API | Key | Files that call it |
|---|---|---|
| The Odds API | `THE_ODDS_API_KEY` | `services/odds_cache`, `alt_lines_feed`, `sports_engine`, `brain/nrfi_engine`, `services/soccer_prop_inject`, `services/mls_direct_inject` |
| Football-Data.org | `FOOTBALL_DATA_API_KEY` | `historical/soccer.py`, soccer ingest |
| API-Sports | `API_SPORTS_KEY` | soccer/tennis stats modules |
| ESPN (public) | *(none)* | `services/espn_*`, `csl_espn_live`, `espn_settlement`, `uefa_espn_ingest`, `ufc_espn_ingest` |
| MLB Stats API (public) | *(none)* | `mlb_lineup`, `brain/nrfi_engine`, `mlb_bvp` |
| Understat (scraper) | *(none)* | `soccer_player_form`, `services/soccer_ingest` |
| TennisExplorer (scraper) | *(none)* | `tennis_extra/*` |
| SportDB | *(none)* | `sportdb_client`, `sportdb_xg_totals`, `sportdb_player_scorer` |
| Emergent LLM | `EMERGENT_LLM_KEY` | AI rationale + deep-dive endpoints |

## 9. Publication lifecycle guarantees

After a candidate is published via `PredictionPublicationService.publish()`:

1. **Immutability** — every field on the snapshot is frozen.  Publication
   service is the only writer to `prediction_snapshots`; it uses an
   append-only pattern with a unique index that forbids overwriting a
   `(prediction_id, snapshot_version)` tuple.
2. **Idempotency** — a re-publish of the same candidate returns the
   existing snapshot instead of creating a new one.  Detection is via a
   deterministic `idempotency_key` and cross-checked with `payload_hash`.
3. **Concurrency safety** — MongoDB single-doc atomic upserts + the
   unique index guarantee at most one snapshot per version even under
   parallel `publish()` calls.  Multi-doc transactions are **not**
   required (and are not available on our standalone deployment).
4. **Read-time invariance** — every endpoint returns the exact
   `published_*` values regardless of code path.  In Phase 1a we
   dual-write and log any mismatches; in Phase 1b we cut endpoints
   over.
5. **Provenance** — `model_version`, `fusion_version`, `scoring_version`,
   `calibration_version`, `validator_version`, `simulation_version`,
   `feature_snapshot_version` are all captured at publish time.

## 10. Standalone MongoDB constraint

MongoDB is deployed as a **standalone** node (no replica set).  This
means:
- Multi-document transactions are **not available** in production.
- Single-document atomic operations (`update_one` with upsert, `insert`
  with unique index) **are** atomic and are sufficient for publication
  idempotency.
- `prediction_publication_service` is intentionally designed to require
  only single-document atomicity — see `PUBLICATION_CONTRACT.md` for
  the invariants + proof-of-safety.
