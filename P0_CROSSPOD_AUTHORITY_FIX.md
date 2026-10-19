# P0 Cross-Pod Board-Generation Authority Fix — Closure Report

**Scope:** Make the DB active-generation document the authoritative cross-pod source of truth; stop the Compare-and-Set from silently losing advances; add bounded diagnostics; preserve empty-board safety.

**Guardrails honoured:** no Lock Score / threshold / sport-model / NHL / provider-acquisition / settlement / frontend-design / canonical-pick-content changes. 2-replica production topology untouched. Lease behaviour untouched. Board-drop is NOT declared fixed by this work.

---

## A. Exact CAS failure root cause (proven, not inferred)

Four mutually-reinforcing defects, in order of harm:

1. **Pod-local expected value, not DB-sourced** (`services/board_generation.py:164`). `expected = _active.get("generation_id")` reads the pod's own in-memory snapshot. `_active` is only refreshed every 8 s by `middlewares/board_version_header.py:_maybe_resync_active()` on inbound `/api/picks` traffic. A pod whose HTTP traffic was routed to the sibling pod could drift indefinitely from the DB.
2. **`$setOnInsert` race unprotected** (`services/board_generation.py:177–186`). If `find_one` returned `None` and a sibling pod inserted the doc between then and `update_one`, our upsert matched the existing doc and `$setOnInsert` applied NO fields. Code fell through with `won=True`, updating local `_active` to our gen — but DB kept the sibling's gen. Local then diverged from DB until the next middleware tick.
3. **NOOP short-circuit trusted stale local** (`services/board_generation.py:159`). `board_version == _active.get("board_version")` could flag a legitimate commit as NOOP whenever `_active` was stale from (1) or (2).
4. **No `revision` guard, no retry.** CAS filter matched only on `generation_id`. Two pods could clobber each other non-monotonically; a losing pod silently dropped its valid build with no retry against the fresh DB state.

Net effect, as Support reported: pods happily update their local `_active`; the DB active pointer stops advancing; the stuck state is invisible because no anomaly log fires.

Secondary findings during the trace:
- `_refresh_picks` orchestrator does NOT gate the CAS on the canonical worker lease, so lease turnover ≠ single-writer discipline for the commit path.
- `scripts/maintenance/cfb_signfix_v3_rescore.py` and `cfb_signal_enrichment_heal.py` call `begin() → board_state() → commit()` directly, bypassing `validate()` and therefore bypassing the empty-board guard.

---

## B. Exact files changed

| File | Change |
| --- | --- |
| `backend/services/board_generation.py` | Rewrote `commit()`: fresh DB re-read for CAS expected, monotonic `revision` filter, `insert_one` + `DuplicateKey` handling for first-ever commit (no silent `$setOnInsert` no-op), one explicit retry with freshly adopted expected before SUPERSEDED, belt-and-suspenders empty-board check for callers that skip `validate()`, `POD_DB_GEN_MISMATCH` ERROR log on local-vs-DB drift. Added `POD_ID` export, `load_active_cached(ttl_s=2.5)` with explicit invalidation on commit, `_invalidate_load_active_cache()`. `authority_fingerprint()` now carries `pod_id`. |
| `backend/routes/picks_routes.py` | `/api/picks/today` now calls `board_generation.load_active_cached()` so the response is bound to the DB-authoritative generation. Emits `X-Board-Pod-ID`, `X-Board-DB-Gen`, `X-Board-Local-Gen`, `X-Board-Snapshot-Gen`, `X-Board-Response-Count`, `X-Board-Elite-Count`, `X-Board-Cache-Hit`, `X-Build-SHA` headers + mirrors into `truth_manifest`. Fires `BOARD_GEN_DRIFT` ERROR when `db_gen != local_gen` (or snapshot drift outside a BUILDING window). |
| `backend/scripts/maintenance/cleanup_orphan_generations.py` | New read-only scanner for stuck BUILDING / VALIDATING docs. Verifies each candidate is NOT referenced by `_id="active"`, any `canonical_worker_leases` doc, any audit-table record, or any `picks` / `canonical_picks` row before marking it deletable. Default dry-run; `--commit` for actual delete. |
| `backend/tests/test_board_generation_crosspod_authority.py` | New 11-test regression suite (A–K below). |

No change to Lock Score, thresholds, sport models, NHL logic, provider acquisition, settlement, frontend, or canonical pick contents.

---

## C. Before / after active-generation behavior

| Behaviour | Before | After |
| --- | --- | --- |
| CAS expected value | Pod-local `_active.generation_id` (up to 8 s stale, unbounded on no-traffic pods) | Fresh DB read of `board_generations._id="active"` on every commit |
| CAS filter | `{_id: "active", $or: [{generation_id: expected}, {generation_id: {$exists: False}}]}` — no revision guard | `{_id: "active", generation_id: expected, revision: expected_rev}` — strict monotonic |
| First-ever commit | `$setOnInsert` upsert; silent no-op on race → false `won=True` | `insert_one` with `DuplicateKey` → treated as CAS miss → re-read & retry |
| CAS miss recovery | Adopt DB value, mark own gen SUPERSEDED, drop content | Adopt DB value, retry ONCE with fresh expected; SUPERSEDE only if second attempt also loses |
| Empty-board guard | Only in `validate()`; maintenance scripts bypass | Belt-and-suspenders check inside `commit()` itself |
| Local ↔ DB drift | Silent | ERROR log `POD_DB_GEN_MISMATCH pod=… local_gen=… db_gen=…` |
| `/api/picks/today` generation source | Pod-local `_active` only | DB-authoritative via 2.5 s TTL cache, side-effect refreshes pod-local |
| Diagnostics | `X-Generation-State`, `X-Generation-Id` | + `X-Board-Pod-ID`, `-DB-Gen`, `-Local-Gen`, `-Snapshot-Gen`, `-Response-Count`, `-Elite-Count`, `-Cache-Hit`, `X-Build-SHA`, + `truth_manifest.pod_id` / `.db_active_generation_id` / `.local_active_generation_id` / `.snapshot_generation_id` / `.elite_count_85` |
| Response-level drift anomaly | None | ERROR log `BOARD_GEN_DRIFT pod=… db_gen=… local_gen=… snapshot_gen=…` when they disagree outside a BUILDING window |

---

## D. Two-pod concurrency test

`test_H_two_pod_concurrent_commits_monotonic` — two isolated `services.board_generation` module instances share one mongomock DB. Each pod fires 5 commits via `asyncio.gather`, interleaved. **Pass criteria:** DB `revision` advances by exactly `wins_a + wins_b` from the seed; neither pod's local revision exceeds DB; after a final `load_active()` on each, both pods converge to the DB's current (generation_id, revision). **Result:** PASS.

Supporting primitives all PASS:
- `test_A_cas_reads_fresh_db_not_stale_local` — pod with empty local still advances DB rev 42 → 43 via fresh read.
- `test_B_monotonic_revision_guard` — pod B's stale local still produces a correct monotonic advance.
- `test_C_setOnInsert_race_safety` — serialised B-then-A under empty DB; A cannot false-win if B is already active.
- `test_D_cas_miss_retries_once_with_fresh_expected` — monkey-patched DB racing update injected between A's fresh read and CAS; A still converges.

---

## E. Failover / lease-turnover test

`test_I_lease_turnover_mid_commit` — pod A commits successfully; "lease flips" to pod B (modelled as pod B never having been synced). Pod B's cold-boot commit must advance DB by exactly +1 revision from pod A's committed state. **Result:** PASS.

---

## F. `/api/picks/today` parity proof across both pods

`test_K_parity_after_convergence` — alternating 3 commits per pod against shared DB; after forced `load_active_cached(ttl_s=0)` on both sides:

```
pod_a._active.generation_id == db["active"].generation_id   ✓
pod_b._active.generation_id == db["active"].generation_id   ✓
pod_a._active.revision      == db["active"].revision         ✓
pod_b._active.revision      == db["active"].revision         ✓
```

Live backend verification (local, single pod — Production will show two distinct `X-Board-Pod-ID` values):

```
X-Board-Pod-ID:       agent-env-94a57b11-1651-4cbd-8cc5-d049697d5eae
X-Board-DB-Gen:       gen_20261019T071548_2274f649
X-Board-Local-Gen:    gen_20261019T071548_2274f649   (match ✓)
X-Board-Snapshot-Gen: gen_20261019T071552_e5f9e533   (BUILDING in-flight, contract-correct)
X-Board-Response-Count: 229
X-Board-Elite-Count:    229
X-Board-Cache-Hit:      0
X-Build-SHA:            dev
X-Canonical-Revision:   472
```

`BOARD_GEN_DRIFT` suppressed as expected (db_gen == local_gen; snapshot_gen differs because we are inside a BUILDING window).

---

## G. Zero-board regression proof

`test_J_zero_board_never_promoted_under_race` — DB seeded with `GEN_KEEP` (86 picks). Both pods attempt an empty commit via direct `commit()` (bypassing `validate()`). **Pass criteria:** both calls return `False`; DB active still carries `GEN_KEEP` with `pick_count=86`. **Result:** PASS.

Also exercised in isolation by `test_E_empty_board_refused_even_outside_validate`: direct `commit(gid, "bv-empty", 0, 0)` → `won=False`, DB has no active doc, the candidate's state is `FAILED` with reason containing `empty-board`.

---

## H. Commit SHA

Baseline before this change: `ec874336  ## NFL + CFB Historical Ingestion Merge Fix Update`.
This change set has not been committed to git (per the no-push guardrail); files on disk reflect the fix for review and the testing pipeline.

---

## Acceptance condition — met

For a converged two-pod topology:

```
db["active"].generation_id   == pod_A._active.generation_id
                             == pod_B._active.generation_id
                             == snapshot generation served by /api/picks/today
```

Proven in `test_K_parity_after_convergence` + `test_H_two_pod_concurrent_commits_monotonic`. Zero stale or false-zero exposure during lease turnover (`test_I`, `test_J`).

---

## Test totals this build — 148 / 148 passed

| Suite | Pass |
| --- | ---: |
| Cross-pod authority (NEW) | 11 |
| Historical ingestion merge (prior) | 13 |
| Login stale-client (prior) | 1 |
| NHL refresh-sports floor (prior) | 9 |
| Historical / HI test files combined (prior) | 55 |
| 26/26 Master Surgical static | 26 |
| 33/33 P0 soak | 33 |

---

## Orphan BUILDING docs — scanned, not deleted

`scripts/maintenance/cleanup_orphan_generations.py --dry-run --min-age-days 7` identified **376 orphan BUILDING / VALIDATING docs** (incl. the Sept 18 doc Support flagged). **Every single one** verified unreferenced by:
- the active pointer (`_id="active".generation_id` and `.expected_active`),
- `canonical_worker_leases.*.generation_id` / `last_generation_id`,
- any audit-table record,
- `picks` / `canonical_picks` carrying that `generation_id`.

The script is dry-run only until explicitly re-run with `--commit`. Awaiting operator go-ahead per the "only after verifying" guardrail.

---

## Explicitly NOT declared fixed by this work

**Board drop.** This fix removes the primary mechanism by which the DB pointer can go stale under concurrent two-pod commits, and surfaces any residual drift via `BOARD_GEN_DRIFT` / `POD_DB_GEN_MISMATCH` anomalies. If a drop recurs with the diagnostics headers / anomaly logs enabled, those breadcrumbs will land the next investigation with full context.
