# Prediction Publication Contract

**Document version:** Phase 1a (2026-08-06)
**Owner:** `services/prediction_publication_service.py`
**Status:** Design contract — dual-write phase

---

## Purpose

Define the exact, immutable contract for how a prediction becomes
"published" and what invariants must hold from that point forward.

**One rule to rule them all:**
> After a candidate is published, no service — anywhere in the codebase
> — may mutate any field listed in §2 of this document.

---

## 1. Ownership

**Single owner:** `services/prediction_publication_service.PredictionPublicationService`

No other module may:
- write to `prediction_snapshots`
- assign a `snapshot_version`
- compute the `payload_hash` or `idempotency_key`
- decide when a candidate is "publishable"

Every caller of the publication service must go through
`publish()` or `publish_batch()`.  Direct writes to
`prediction_snapshots` are forbidden and enforced by convention +
code review (Mongo doesn't have per-collection write ACLs on our
deployment).

---

## 2. The immutable field set (`published_*`)

Once a snapshot is written, these fields are **frozen forever**:

| Field | Type | Source |
|---|---|---|
| `prediction_id` | str (uuid) | Stable identity — matches `picks.id` |
| `pick_id` | str | Alias for prediction_id (backwards compat) |
| `snapshot_version` | int | Monotone increment per prediction_id (v0 = legacy backfill) |
| `board_version` | str | Board-generation id (semver-ish or unix ts) |
| `published_probability` | float [0, 1] | Model + fusion output |
| `published_edge` | float | percentage points (e.g. 3.5 = +3.5pp) |
| `published_lock_score` | float [0, 99] | Lock V2 output post quality-gate |
| `published_grade` | str | e.g. "Elite Lock" / "Strong Lock" / "Playable Bet" / "Pass" |
| `published_confidence` | float [0, 100] | Confidence percentage |
| `published_reasoning` | str \| dict | Bullet list, rationale summary, or structured dict |
| `published_line` | float \| None | Prop line if applicable |
| `published_odds` | int \| None | American odds at publication |
| `model_version` | str | e.g. "mlb_prop_v3.2" or "legacy_unknown" |
| `fusion_version` | str | e.g. "fusion_v4" |
| `scoring_version` | str | e.g. "lockscore_v2.1" |
| `calibration_version` | str | e.g. "cal_2026-08-01" or "legacy_unknown" |
| `validator_version` | str | Board validator version |
| `simulation_version` | str | Monte-Carlo simulator version |
| `feature_snapshot_version` | str | Hash / semver of feature builder |
| `published_at` | ISO 8601 UTC | Publication timestamp |
| `publication_source` | str | "canonical_pipeline" \| "mls_direct_inject" \| "soccer_prop_inject" \| "legacy_backfill" |
| `is_legacy` | bool | True only for `snapshot_version=0` records created by backfill |
| `payload_hash` | str | SHA-256 of canonical JSON of the published payload |
| `idempotency_key` | str | Deterministic key for retry safety (see §4) |
| `is_active` | bool | Exactly one snapshot per prediction_id has `is_active=True` |

Explicit "unknown" tokens (used when we can't determine a real value):
- `"legacy_unknown"` for missing version strings on backfilled records
- `None` for missing line / odds
- Never invent version numbers when the pipeline stage was never
  invoked; use `"legacy_unknown"` and record the gap in the audit log.

---

## 3. What may NEVER be mutated post-publication

The **published record** on `prediction_snapshots` is append-only.
The following legacy fields on `picks` shall become read-only projections
of the corresponding `published_*` fields (Phase 1b converts endpoints;
Phase 1c enforces write-side guards):

- `lock_score` (currently mutated by 30+ writers — see PHASE1_AUDIT.md)
- `lock_score_v2`, `lock_score_raw`, `lock_score_peak` (all shadow fields)
- `win_probability`, `probability`
- `edge`, `edge_percent`
- `grade`
- `confidence`, `confidence_score`
- `reasoning`, `pick_rationale.summary`
- `book_odds`, `american_odds`, `odds`
- `line`

Any post-publication learning / calibration / enrichment must:
- write to `pick_enrichment` (presentation-only side-cars), **or**
- write to `settlement_events` (for graded results), **or**
- update model / calibration state that will be picked up on the NEXT
  publication run

but must not touch the immutable `published_*` fields or their legacy
aliases on the mutable `picks` document (after Phase 1c).

---

## 4. Idempotency

Publication must be safe under retry.  The service guarantees:

**Deterministic idempotency key:**
```
idempotency_key = sha256(
    prediction_id + "|" +
    board_version + "|" +
    published_probability_rounded_to_6dp + "|" +
    published_lock_score_rounded_to_2dp + "|" +
    published_edge_rounded_to_3dp + "|" +
    published_line_or_"none" + "|" +
    published_odds_or_"none"
)
```

**Behavior:**
1. `publish()` first computes the key.
2. It attempts an atomic `insert_one` into `prediction_snapshots`.
3. If the unique index on `(prediction_id, idempotency_key)` rejects the
   insert (E11000), we fetch and return the existing snapshot.
4. `payload_hash` is compared; if it drifts from the existing hash, we
   log a WARN (should be impossible if the key is deterministic — a
   drift indicates a bug in the key formula or the payload builder).

**Race safety:** even without transactions, two concurrent `publish()`
calls for the same candidate cannot produce two snapshots because the
unique index rejects the second insert atomically.

---

## 5. Versioning + snapshot promotion

`snapshot_version` is a monotone integer per `prediction_id`:
- `v0` = legacy backfill (Phase 1c)
- `v1` = first canonical publication
- `v2`, `v3`, ... = future deliberate re-publications (Phase 2+)

**Phase 1a scope:** the service will only ever write `v1` for new
publications.  A deliberate re-publish workflow is out of scope for
Phase 1 and will be designed separately.

**Active snapshot flag:** exactly one snapshot per `prediction_id` has
`is_active=True`.  For Phase 1a, the newest `snapshot_version` is
always active.  Marking a previous version active would require a
re-publication workflow, which Phase 1 explicitly does not implement.

---

## 6. Concurrency + atomicity

**Constraint:** MongoDB is deployed as a standalone (no replica set),
so multi-document transactions are unavailable.

**Design consequence:** the publication service uses only single-document
atomic operations:
- `insert_one` with unique index on `(prediction_id, snapshot_version)`
  and `(prediction_id, idempotency_key)`
- `update_one` on `picks` with `$set` for dual-write

The dual-write to `picks` is **not** transactionally coupled to the
snapshot insert.  Failure modes:
- If snapshot insert succeeds but `picks` update fails → the snapshot
  is the source of truth; a subsequent publication attempt will find
  the existing snapshot (idempotency) and re-attempt the `picks`
  update.  Detection: dual-write mismatch report will flag rows
  where `picks.lock_score ≠ snapshot.published_lock_score`.
- If `picks` update succeeds but snapshot insert fails → this cannot
  happen because we always insert the snapshot FIRST.
- If both fail → caller retries; idempotency handles it cleanly.

**Ordering (per candidate):**
1. Compute payload + hashes
2. Insert snapshot (atomic, idempotent)
3. Upsert into `picks` with `published_*` fields
4. Return snapshot

The snapshot insert is the atomic commit point.  Everything after that
is an eventually-consistent projection.

---

## 7. Dual-write mode (Phase 1a only)

During Phase 1a we run in **dual-write mode**:

- Publication service is inserted into the pipeline tail.
- Snapshots are created for every publication.
- The `picks` document continues to carry the legacy fields
  (`lock_score`, `edge_percent`, etc.) AND the new `published_*`
  fields.
- Endpoints continue to read the legacy fields.  No user-facing change.
- The publication service **logs but does not correct** any mismatch
  between `snapshot.published_*` and `picks.<legacy_field>`.
- The mismatch report is persisted to `publication_mismatch_report` and
  surfaced at `/api/admin/publication-mismatches`.

Phase 1b will switch endpoints over to `published_*`.
Phase 1c will remove the legacy fields entirely.

---

## 8. What breaks the contract

Any of the following is a Phase-1 defect:
- A serializer / decorator / cache layer that recomputes
  `lock_score`, `probability`, or `edge`.
- A "max(v1, v2)" style repair path anywhere in the read path.
- A background worker that writes to `picks.lock_score` (or any
  `published_*` alias) AFTER `publication_source` has been stamped.
- An endpoint that returns a value different from the snapshot.
- A publication path that bypasses `PredictionPublicationService`.

Phase 1b will add automated regression tests that assert none of the
above holds.

---

## 9. Legacy backfill (Phase 1c only)

Scope: create a `v0` snapshot for every existing row in `picks` at the
time of migration.

- `snapshot_version = 0`
- `is_legacy = True`
- `publication_source = "legacy_backfill"`
- `model_version = "legacy_unknown"`
- `fusion_version = "legacy_unknown"`
- ... other version fields = `"legacy_unknown"`
- `published_*` fields are populated from the current `picks` values

The backfill script (`scripts/backfill_v0_snapshots.py`) is delivered
in Phase 1a as **dry-run only** with mismatch reporting.  The actual
write happens in Phase 1c after review.
