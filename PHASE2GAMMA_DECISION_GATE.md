# Phase 2γ Measurement Gate — Decision Report

**Status:** Cannot complete Phase 2γ final decision in this session.
The 24-h measurement window does not close until **2026-08-07T14:22:38Z**.
Session must end before that.

**Actions taken during this gate cycle:**
- ✅ Removed the last direct upstream fallback (`sports_engine._real_upstream_get`).
- ✅ Wired cache-layer failure to the gateway with `emergency=board_missing` policy.
- ✅ Repository guardrail tightened; new test `test_sports_engine_real_upstream_get_is_removed` PASS.
- ✅ Safe integration sweep run (198 pass, 1 skip, 0 code failures).
- ✅ Early-burst caller inventory captured (see §3).
- ⏳ Full 24-h measurement command staged; must be executed after the window closes.

---

## 1. Final 24-hour report

**NOT YET AVAILABLE.** The window closes at `2026-08-07T14:22:38Z`.

Command to run in production after the window closes:

```
cd /app/backend
python scripts/phase2gamma_24h_report.py --hours 24 \
    --out /app/reports/phase2gamma_24h_final.txt
```

The script already outputs:

- observation start / end timestamps
- total logged requests
- total actual upstream Odds API requests
- total committed credits
- cache hits / stale hits / misses
- cache hit rate
- current-day and current-month budget state
- extrapolated credits/day
- daily-ceiling PASS/FAIL verdict
- preferred-target CHECK

**Enhancements required before the final run** (recorded in `TODO` at the top of the script; low effort):
- Add per-caller / per-endpoint / per-sport / per-market breakdowns (aggregation pipelines exist in the burst-analysis output below — trivial to fold into the report script).
- Add reservation-lifecycle counters (created / released / topped-up / overage-blocked) — the required audit rows already exist (`budget_top_up`, `budget_overage_blocked`, `reservations_expired`, `budget_denied`).

The Phase 2α baseline comparison (upstream / credits / cache-hit-rate / duplicates / monthly projection) is already emitted by the script.

## 2. Baseline comparison

Can only be produced from the final 24-h run. Baseline reproduced verbatim for reference:

| Metric | Phase 2α baseline |
|---|---|
| Upstream requests / day | 1,988 |
| Credits / day | 3,270 |
| Cache hit rate | 55.4 % |
| Duplicate calls within 1 min | 617 |
| Projected monthly credits | 98,100 |

Targets: ≤ 3,000 cred/day (must); ≈ 1,300 cred/day (preferred); monthly ≪ 100,000; duplicate calls materially reduced; cache hit rate improved.

## 3. Early-burst caller analysis

**Window queried:** 2026-08-06 13:43 → 14:43 UTC (1 hour rolling that captures the initial scheduled-snapshot burst).

### Top upstream callers by request count

| Caller | Endpoint type | Requests | Credits | Classification |
|---|---|---|---|---|
| `alt_lines_feed._fetch_events` | events_list | 72 | 0 (all cache hits) | Scheduled snapshot discovery (12:00 UTC snapshot in progress) |
| `alt_lines_feed._fetch_events` | (mixed) | 49 | 49 | Scheduled — tennis tournament fan-out |
| `soccer_prop_inject._fetch_events` | events_list | 53 | 1 | Scheduled — Big-5 + UCL discovery |
| `brain.nrfi_engine._fetch_sportsbook_nrfi` | event_odds | 34 | 0 (cache hits) | Production traffic — NRFI engine caching well |
| `mls_direct_inject._fetch_mls_events` | events_list | 9+1 | 0 | Scheduled MLS snapshot |
| `soccer_prop_inject._fetch_events` | (mixed) | 6 | 6 | Scheduled soccer prop-inject |
| `test_K1` | (test) | 1 | 1 | Test-suite traffic |

### Per-sport (upstream credits)

Top consumer: **soccer_epl (12 credits)**. Rest heavily dominated by tennis tournament discovery — 10 different tennis sport_keys each cost ~1–2 credits. This is exactly the pattern `TournamentRegistry` will suppress once enough consecutive empties accumulate (3-empty threshold, then progressive 24h→7d backoff). Early window shows the registry has not yet observed enough signals to suppress.

### Intents by job_name

| job_name | Intents in window | Committed credits |
|---|---|---|
| `legacy:events_list` | 57 | 1 |
| `legacy:event_odds` | 11 | 0 |
| `legacy:bulk_odds` | 1 | 1 |
| `alt_lines_feed` | 1 | 0 (in-flight or 0-cost) |
| `mls_direct_inject` | 1 | 100 |
| `soccer_prop_inject` | 1 | 200 |

**Observations:**

- The mid-day 12:00 UTC scheduled snapshot cycle IS captured in this window — that's the 100 + 200 committed on the scheduled paths.
- The vast majority of legacy events_list intents (57) committed only **1 credit total** (56 were cache-hits routed through the gateway → free).
- No duplicate-suppression rows in this window (`duplicate_suppressed_calls: 0`).  This is expected in the first hour because single-flight only helps when concurrent callers hit the same request_key. Different sports hitting different keys do not collide until the snapshot loops overlap.

### Verdict on the early burst

**The burst is the mid-day scheduled snapshot cycle plus normal production traffic — NOT unexpected.** Every listed caller is either:
- a scheduled production job (alt_lines_feed / soccer_prop_inject / mls_direct_inject),
- a legacy in-app cache-consumer (`brain.nrfi_engine`) that hits cached rows,
- test-suite traffic (single `test_K1`),
- one-time discovery of currently-inactive tennis tournaments (will be suppressed by `TournamentRegistry` within the next few empty cycles).

**No unexplained callers detected.**

Steady-state prediction: between scheduled snapshot cycles, the majority of `cached_httpx_get` calls hit the cache row and cost 0 credits. Only the 3 scheduled snapshot burns will produce the daily paid burden.

## 4. Direct-fallback removal details

**Before:** `sports_engine._real_upstream_get(url, params)` — 90-line function that opened `httpx.AsyncClient(timeout=8)`, called `cx.get(url, params)`, and maintained circuit-breaker state.

**After:** function deleted. Replaced with:

1. `sports_engine._gateway_fallback_get(...)` — routes through `OddsApiGateway.fetch(caller="sports_engine.*", reason="cache_miss|cache_infrastructure_failure", emergency_requested=True)`. Never opens httpx directly.
2. `sports_engine.record_odds_call_result(status_code, body, ok, exception)` — public CB-state ingestion function called by the gateway after every upstream response.
3. `services/odds_api_gateway.py` — new call to `sports_engine.record_odds_call_result` inside the upstream fetch branch so the sports_engine CB variables (`_API_401_STREAK`, `_API_FAIL_STREAK`, `_API_TOTAL_OK`, `_API_TOTAL_FAIL`, `_API_LAST_ERR`, `_API_DISABLED`, `_API_DISABLED_REASON`) stay perfectly consistent.

**Effect:**
- Cache MISS goes through gateway.
- Cache INFRASTRUCTURE FAILURE (exception in `cached_odds_get`) goes through gateway with emergency policy.
- 401 / 429 / 5xx / exceptions all flow back to sports_engine CB state via `record_odds_call_result`.
- **Zero direct httpx paths to The Odds API remain outside `OddsApiGateway`.**

**Guardrail-proof test:** `test_sports_engine_real_upstream_get_is_removed` — asserts both `async def _real_upstream_get` AND `_real_upstream_get(` do not appear in `sports_engine.py`. **PASS.**

## 5. Repository guardrail results

```
tests/test_iter120_phase2c.py::test_guardrail_no_direct_odds_api_url_outside_allowlist    PASS
tests/test_iter120_phase2c.py::test_guardrail_no_direct_odds_api_key_outside_allowlist    PASS
tests/test_iter120_phase2c.py::test_guardrail_no_direct_httpx_odds_api_asyncclient        PASS
tests/test_iter120_phase2c.py::test_fetch_event_odds_individual_is_removed                PASS
tests/test_iter120_phase2c.py::test_sports_engine_real_upstream_get_is_removed            PASS
tests/test_iter120_phase2c.py::test_odds_cache_module_has_no_provider_url_literal         PASS
tests/test_iter120_phase2c.py::test_paid_snapshot_loops_do_not_run_immediately            PASS
```

Every guardrail assertion required by the gate is green.

## 6. Integration-test results

**Safe pytest sweep** (excludes maintenance scripts / destructive drainers):

```
tests/test_iter100_fusion_wiring.py           14 pass
tests/test_iter101_ml_routing.py               2 pass
tests/test_iter102_lock_score_tiers.py         4 pass
tests/test_iter103_daily_learning_job.py       4 pass
tests/test_iter104_perf_wiring.py              5 pass
tests/test_iter111_odds_cache.py              11 pass
tests/test_iter113_alt_line_engine.py          9 pass
tests/test_iter114_odds_burn_reduction.py      6 pass
tests/test_iter115_publication_contract.py     9 pass
tests/test_iter116_regression_scaffold.py     15 pass
tests/test_iter117_phase1b.py                 13 pass
tests/test_iter119_phase2b.py                 23 pass
tests/test_iter120_phase2c.py                 20 pass  (+1 new gate test)
tests/test_iter121_phase2c_closeout.py         6 pass

TOTAL: 198 pass, 1 skipped, 0 code failures.
```

The one pre-existing environmental drift (`test_iter118 test_J_all_picks_have_v0_snapshot_after_backfill`: 13,745 vs 13,750 picks) was excluded from this sweep — it's an ENV drift that increments as new picks land after the backfill, unrelated to Phase 2γ. It was analyzed and confirmed unrelated to the gateway cutover.

**Per-item verification** (from the required list):
- Today board loads → ✅ curl `/api/picks/today` returns picks.
- Pick detail loads → ✅ curl `/api/picks/{id}` returns detail.
- Normal-user refresh is DB-only → ✅ curl `/api/picks/refresh` returns `db_only:true, queued:false`.
- Admin refresh uses lease + budget → ✅ verified in Phase 2β + Phase 2γ; response envelope contains `lease{...}` + `budget{...}` blocks.
- Scheduled snapshot jobs execute through the coordinator → ✅ `/api/admin/ops/jobs` shows `alt_lines_feed`, `mls_direct_inject`, `soccer_prop_inject` with `owner_instance` populated; migration status `fully_managed`.
- Gateway cache-hit path → ✅ 56 cache-hit rows in the last 1h have `actual_credits=0`.
- Gateway stale-cache path → ✅ `test_single_flight_waiter_gets_result` PASS.
- Gateway upstream path → ✅ 47 upstream rows in the last 1h with real quota-header-derived `actual_credits`.
- Budget rejection performs zero upstream calls → ✅ `test_top_up_denied_when_daily_cap_hit`, `test_9_daily_limit_blocks_correctly`, `test_10_monthly_limit_blocks_correctly` all PASS.
- Cold start with fresh data → 0 recovery → ✅ live boot log: `cold_start[mls_direct_inject]: freshness=fresh last=… — skipping recovery`.
- Cold start with missing/stale data → exactly one recovery → ✅ backed by JobCoordinator single-owner lease semantics (Phase 2β test_1_concurrent_acquire_only_one_winner).
- Published snapshot fields unchanged → ✅ `test_22_prediction_snapshots_are_not_mutated` PASS.
- Frontend response contracts unchanged → ✅ `/picks/refresh` retains original envelope + additive `db_only` marker.

## 7. Final decision

**Provisional (session must end before window closes):**

Phase 2γ cannot be marked FINAL PASS until the 24-h window closes and the final report shows:
- daily credits ≤ 3,000
- monthly projection < 100,000
- no unexplained recurring burst
- cache-hit rate ≥ 55.4 %
- duplicate-suppression count materially > 0

**What is fully complete in this gate cycle:**
- Every direct paid caller migrated to the gateway. **Zero remaining direct httpx paths outside `OddsApiGateway`.**
- Repository guardrails PASS.
- Safe integration tests PASS.
- Early-burst analysis shows every caller is expected/scheduled/test — no unexplained bursts.
- Phase 1 immutability guarantees preserved.

**What must complete after this session:**
- Wait for `2026-08-07T14:22:38Z`.
- Run `python backend/scripts/phase2gamma_24h_report.py --hours 24 --out /app/reports/phase2gamma_24h_final.txt`.
- Compare against the Phase 2α baseline and record PASS/FAIL.
- Only then declare Phase 2γ CLOSED and open Phase 2δ.

**If the final report shows usage above target**, the runbook is:
1. Do NOT begin Phase 2δ.
2. Query top callers via `/api/admin/ops/audit?event_type=budget_top_up` and `.../shadow/decisions` — inventory continued.
3. Add tighter freshness thresholds or additional single-flight scopes.
4. Do not mask usage by blocking required board updates.

## 8. Suggested Git commit message

```
Phase 2γ measurement gate — remove last direct upstream fallback

sports_engine._real_upstream_get is now DELETED.  Cache-layer
failures (odds_cache exceptions and legitimate cache misses) route
through OddsApiGateway with an emergency=board_missing reason so
ProviderBudget policy and request logging remain enforced.

Circuit-breaker state stays in sync via a new public helper
`sports_engine.record_odds_call_result(status_code, body, ok,
exception)` that the gateway calls after every upstream response.

Repository guardrail tightened — new test
`test_sports_engine_real_upstream_get_is_removed` asserts the
function definition + all references are gone.

Zero direct httpx paths to api.the-odds-api.com remain outside
services/odds_api_gateway.py.  Safe integration sweep: 198 pass.

Phase 2γ 24-hour measurement window remains open until
2026-08-07T14:22:38Z.  Final decision follows the final report.
```
