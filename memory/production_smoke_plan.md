# Perklocks — Production Smoke Plan (Post Master Surgical Fix Build)

**Build base commit:** `a0d1aa87` (`feat(perklocks): Blocks 5-21 continuous surgical build`)
**Surgical closure commit:** (set at commit time; appended below)

**Guardrails (unchanged):** No Lock Score weight changes, no 85+ threshold
changes, no NFL alt ladder changes, no NHL 5-game floor changes, no
evidence-threshold changes, no NBA model activation, no Phase 7/8
migration moves.

The smoke plan is **split in two sections** by trust surface:
PUBLIC/API tests only use user-visible endpoints; INTERNAL/SUPPORT
tests exercise ops-facing stores and background subsystems that
should never leak into the user surface.

---

## 1 · PUBLIC / API

Every test here MUST pass without any direct DB inspection and
without any internal auth. These are the user-visible invariants.

### 1.1 `GET /api/picks/today` — Sport counts

* **Expectation:** HTTP 200, JSON array (or `{picks: [...]}` wrapper
  per current response shape).
* **Assertion:** Per-sport counts for `NFL`, `MLB`, `NHL`, `Soccer`,
  `Tennis`, `CFB` are each **≥ 0**, and every pick returned has:
  - `sport`
  - `market` (non-empty)
  - `line` present when `market_class != "moneyline"`
  - `odds` present
  - `lock_score` numeric in `[85, 100]`
* **Negative:** Zero NBA picks (NBA remains MODEL_UNAVAILABLE across
  all families — see §2.1 Universal Apex report).

### 1.2 NFL 97+ visibility

* **Expectation:** When current NFL slate has 97+ scored picks,
  `/api/picks/today?sport=nfl` returns them with `lock_score ≥ 97`.
* **Assertion:** No 97+ NFL pick is silently filtered out (if the
  slate produces 0, the HI consumer-store NFL section in §2.4 must
  explain *why* — not the API).

### 1.3 Soccer / Tennis / NHL visibility

* **Expectation:** Each of `sport=soccer`, `sport=tennis`,
  `sport=nhl` returns picks with their canonical families only:
  - Soccer: `moneyline`, `game_total`, `btts`, `double_chance`,
    `goalscorer_anytime`, `goalscorer_first`, `goalscorer_score_or_assist`
  - Tennis: `tennis_match_winner`, `tennis_game_handicap`,
    `tennis_total_games`
  - NHL: `moneyline`, `puck_line`, `game_total`, `nhl_goals`,
    `nhl_sog`, `nhl_assists`, `nhl_points`
* **Assertion:** No unknown market strings; no NBA families leak.

### 1.4 Provenance fields where exposed

* **Expectation:** On any pick where provenance is exposed to the
  client (e.g. `evidence_block`, `independent_model_probability`,
  `market_intel.price_source`), the field is self-consistent:
  - `price_source` ∈ {`real_book`, `provider`} — NEVER `implied` on
    a published pick (§3 full-evidence bypass covers this invariant).
  - `independent_model_probability` ∈ (0, 1).
  - `evidence_block` keys match the Universal Evidence Authority
    schema (same per sport as the Preview build).

### 1.5 No direct DB assumptions

* The entire PUBLIC/API section MUST be runnable from the public
  internet with zero Mongo access and zero internal routes.  If a
  check requires a Mongo peek, it belongs in §2 (INTERNAL/SUPPORT).

---

## 2 · INTERNAL / SUPPORT

Everything here is for ops / on-call verification. It validates
the state of the authority stores that back the public surface.

### 2.1 `canonical_picks` states

* Count by `state`: expected set ⊆ `{active, superseded, retired,
  not_published}`.
* `active_revision=true` picks MUST all have `lock_score ≥ 85` and
  all have `publication_barrier.allowed=true`.
* No `active_revision=true` pick may have `publication_blocker` set.

### 2.2 `bad_market_registry`

* Table is readable and no NEW bad-market entries have appeared
  since the last Preview snapshot.  Any new row MUST have a human
  reason and NOT be caused by a false positive from the universal
  market contract (double-check against §2.5 historical IDs).

### 2.3 `funnel_telemetry`

* Each supported sport × family emits a funnel row per orchestrator
  cycle.  Per the universal reachability contract
  (`services/sport_market_reachability.py`), any
  `published == 0` row MUST carry a `reason` ∈ {`NO_EVENTS`,
  `NO_REAL_MARKETS`, `NORMALIZATION_FAILURE`, `IDENTITY_FAILURE`,
  `MODEL_UNAVAILABLE`, `INTEGRITY_REJECTED`, `LEGITIMATELY_BELOW_85`,
  `PUBLICATION_FAILURE`, `API_FAILURE`, `FRONTEND_FAILURE`}.
* **NBA families** MUST report reason `MODEL_UNAVAILABLE` (not any
  of the other "upstream missing" reasons).  This is the universal
  harness invariant tested in
  `tests/test_nba_universal_apex_reachability_harness.py`.

### 2.4 Historical consumer-store freshness

Run:

```
cd /app/backend && python -m tests.run_hi_freshness_report
```

Produces, for each of **NFL, MLB, NBA, NHL, Soccer, Tennis, CFB**:

```
sport, status, lag_seconds, provider_latest_final, consumer_latest_final
```

Status set: `CURRENT | INGESTION_LAG | COVERAGE_LIMITED | STALE | UNKNOWN`.

* **CURRENT** = consumer-store caught up with provider inside the
  per-sport stale window.
* **INGESTION_LAG** = provider has newer rows than the consumer
  (projection didn't close the gap this cycle).
* **COVERAGE_LIMITED** = source genuinely has zero rows (NOT a
  projector bug).
* **STALE** = consumer is older than the per-sport stale window.

Operator decision table:

| Status             | Action                                                                 |
|--------------------|------------------------------------------------------------------------|
| `CURRENT`          | OK                                                                     |
| `COVERAGE_LIMITED` | Expected during off-season for a given sport; verify in-season         |
| `INGESTION_LAG`    | Re-trigger `project_<sport>_actuals(db)`, then re-run freshness report |
| `STALE`            | Investigate: provider feed outage or projector exception               |
| `UNKNOWN`          | Check DB connectivity & permissions; do NOT flip canonical write live  |

### 2.5 Projection / freshness status

* `services/historical_intelligence_projectors.py` → one function
  per sport (`project_nfl_actuals`, `project_mlb_actuals`,
  `project_nba_actuals`, `project_nhl_actuals`,
  `project_soccer_actuals`, `project_tennis_actuals`,
  `project_cfb_actuals`).
* Must be hooked into `pick_refresh_orchestrator` after each
  provider-side ingest and before publication.  Confirm the
  orchestrator log contains `hi_projector:<sport>:ok` (or an
  explicit `INGESTION_LAG` payload) per cycle.
* `project_nba_actuals` MUST advertise
  `board_model_state_independent: True` in its return payload
  (asserted by test
  `test_universal_harness_nba_historical_projection_independent`).

### 2.6 Universal Apex Reachability (NBA report)

Run:

```
cd /app/backend && python -m pytest \
    tests/test_nba_universal_apex_reachability_harness.py -v -s
```

Expected output: every NBA family returns

```
MODEL_UNAVAILABLE   /   NOT TESTABLE / NOT ELIGIBLE
```

If ANY NBA family returns anything else, the NBA board is being
silently activated — HARD STOP.

### 2.7 Apex Reachability (constant-probability test)

Run:

```
cd /app/backend && python -m pytest \
    tests/test_apex_reachability_constant_model_probability.py -v -s
```

Must pass. The matrix holds `independent_model_probability`
constant across odds `-500 … +150` and proves:

* elite evidence + any price in that range → Apex reachable
* weak evidence + any price in that range → Apex blocked

### 2.8 Full-Evidence Bypass (real-path)

Run:

```
cd /app/backend && python -m pytest \
    tests/test_full_evidence_bypass_real_path.py -v -s
```

Six scenarios — all MUST pass:

1. Elite fixture, +150  → Apex reachable
2. Elite fixture, -110  → Apex reachable
3. Elite fixture, -500  → Apex reachable
4. Weak fixture, -500   → Apex blocked (legacy 60+WP*40 cap applies)
5. Implied-only fixture → Rejected (no real book price)
6. Elite fixture with full-evidence stripped → legacy cap applies

---

## 3 · Smoke run — single-command convenience

```
cd /app/backend && \
  python -m pytest \
    tests/test_apex_reachability_constant_model_probability.py \
    tests/test_full_evidence_bypass_real_path.py \
    tests/test_nba_universal_apex_reachability_harness.py \
    -v -s && \
  python -m tests.run_hi_freshness_report
```

Exit code 0 across both commands = Master Surgical Fix Build smoke pass.

---

## 4 · Pre-cutover checklist (do NOT skip)

- [ ] `DATA_AUTHORITY=production` ONLY on the Production pod
- [ ] `CANONICAL_WRITE_ENABLED=true` ONLY on the Production pod
- [ ] `BACKGROUND_WORKERS_ENABLED=true` ONLY on the Production pod
- [ ] `USE_CANONICAL_DB=false` until Phase 8 passes (then flip)
- [ ] `CANONICAL_IMPORT_ENABLED=true` only during cutover window
- [ ] `CANONICAL_IMPORT_TOKEN` set to a cryptographic random secret
- [ ] Preview `.env` left untouched (keep its default-safe values)
- [ ] HI projectors ran at least ONCE per sport post-cutover
- [ ] `/api/picks/today` returns ≥ 1 pick for an in-season sport
- [ ] NBA count on `/api/picks/today` is exactly `0`
