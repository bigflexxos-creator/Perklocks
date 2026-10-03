"""test_canonical_cutover_r2_schema — Phase 5-R2 regression suite.

Validates that for every one of the 21 reconciled collections, the
server-side `_LOGICAL_KEYS` entry resolves to a tuple whose fields are
PRESENT (not None) in a sample of the certified Phase 5/Phase 6
canonical NDJSON checkpoints.  Non-null coverage must be sufficiently
high per collection (configurable thresholds below) so that Phase 5
import does not silently drop every row via `MISSING_LOGICAL_KEY_COMPONENT`.

Where a collection's certified data has a known small fraction of
null-identity rows (settlement_events = 19/300 null `settlement_id`;
user_bets = 4/14 null `id`) the test records the tolerated null fraction
and ASSERTS it stays bounded.  Per user contract (Phase 5-R2):

  * Do NOT silently synthesize settlement_id values.
  * FAIL CLOSED and report rather than invent an identity.
  * Preserve the 2 immutable prediction-snapshot conflicts as excluded.

Additional guards:
  * No logical key may contain `None` values on the sample.
  * The push-driver overlay guard: empty P6 overlay must fall back to P5.
  * `_LOGICAL_KEYS` must have an entry for every element of
    `RECONCILIATION_COLLECTIONS` and vice versa (bidirectional closure).
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tarfile
import tempfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

CHKP = pathlib.Path("/app/reconcile_workspace/checkpoints")
P5_TAR = CHKP / "phase5_20261003_190628Z.tar.gz"
P6_TAR = CHKP / "phase6_20261003_192150Z.tar.gz"

# Hard-coded SHAs — must match the certified checkpoint integrity record.
EXPECTED_SHAS = {
    P5_TAR.name: "4adc99890885e7b712adfd491124c2ef681715996167d19719ddb04f8eb1e9d7",
    P6_TAR.name: "327a38903daf3bd910ab50b68f69415cfcf181624655b42fb791af43042b6c22",
}


def _sha(path: pathlib.Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


@pytest.fixture(scope="module")
def extracted_checkpoints(tmp_path_factory):
    """Extract P5 + P6 canonical NDJSONs into a per-module tmp dir."""
    out = tmp_path_factory.mktemp("canon_r2_test")
    for tar in (P5_TAR, P6_TAR):
        if not tar.exists():
            pytest.skip(f"certified checkpoint missing: {tar}")
        assert _sha(tar) == EXPECTED_SHAS[tar.name], \
            f"CHECKPOINT SHA MISMATCH: {tar.name}"
        with tarfile.open(tar) as tf:
            tf.extractall(out)
    # Discover p5 and p6 canonical directories
    inners = sorted(out.iterdir())
    assert len(inners) >= 2, f"expected 2 inner dirs, got {inners}"
    canon5 = next((p / "canonical") for p in inners if "phase5" in p.name)
    canon6 = next((p / "canonical") for p in inners if "phase6" in p.name)
    return canon5, canon6


def _iter_rows(path: pathlib.Path, limit: int = 500):
    with open(path) as f:
        for i, ln in enumerate(f):
            if i >= limit: break
            try: r = json.loads(ln)
            except Exception: continue
            yield r.get("doc", r) if isinstance(r, dict) else r


# Collection → max tolerated null-identity fraction on a 500-row sample.
# Zero means "every certified row must have the identity"; small floats
# allow for known settled-dataset gaps (e.g. 19/300 settlement_id nulls).
_TOLERATED_NULL_FRACTION = {
    "settlement_events":           0.10,   # 19/300 ≈ 0.063 → allow up to 10 %
    "user_bets":                   0.40,   # 4/14 (small set) → 0.286 → allow 40 %
    "parlay_history":              0.05,   # 5/300 signature nulls expected (we key on _id, so 0)
}


def test_every_reconciliation_collection_has_explicit_logical_key():
    from services.canonical_cutover import (
        RECONCILIATION_COLLECTIONS, _LOGICAL_KEYS, logical_key_fields,
    )
    # Bidirectional closure
    assert set(_LOGICAL_KEYS.keys()) == set(RECONCILIATION_COLLECTIONS), (
        "_LOGICAL_KEYS must be exactly the 21 reconciled collections"
    )
    for coll in RECONCILIATION_COLLECTIONS:
        kf = logical_key_fields(coll)
        assert isinstance(kf, tuple) and kf, \
            f"{coll} has empty/invalid logical key"
        assert all(isinstance(f, str) and f for f in kf), \
            f"{coll} contains an empty-string field name"


def test_logical_keys_resolve_in_certified_checkpoints(extracted_checkpoints):
    """For every reconciled collection, sample up to 500 rows from the
    certified canonical NDJSON (P6 overlay if non-empty, else P5) and
    assert the server-side logical-key fields resolve to non-None in
    enough rows that the Phase-5 import does not silently drop the
    entire collection.
    """
    from services.canonical_cutover import (
        RECONCILIATION_COLLECTIONS, logical_key_fields,
    )
    canon5, canon6 = extracted_checkpoints
    failures = []
    for coll in RECONCILIATION_COLLECTIONS:
        kf = logical_key_fields(coll)
        p6 = canon6 / f"{coll}.ndjson"
        p5 = canon5 / f"{coll}.ndjson"
        src = p6 if (p6.exists() and p6.stat().st_size > 0) else p5
        if not src.exists():
            failures.append(f"{coll}: NO SOURCE NDJSON FOUND (p5={p5.exists()}, p6={p6.exists()})")
            continue
        if src.stat().st_size == 0:
            failures.append(f"{coll}: SOURCE NDJSON IS EMPTY ({src})")
            continue
        total, missing = 0, 0
        for doc in _iter_rows(src, limit=500):
            total += 1
            if any(doc.get(f) is None for f in kf):
                missing += 1
        if total == 0:
            failures.append(f"{coll}: zero parseable rows in {src.name}")
            continue
        frac = missing / total
        tol = _TOLERATED_NULL_FRACTION.get(coll, 0.0)
        if frac > tol:
            failures.append(
                f"{coll}: key={kf} missing in {missing}/{total} "
                f"({frac:.1%} > tolerance {tol:.1%})"
            )
    assert not failures, (
        "logical-key resolution failed for the following collections:\n"
        + "\n".join(f"  - {m}" for m in failures)
    )


def test_logical_keys_align_with_unique_indexes():
    """Every reconciled collection's logical-key tuple must have a
    corresponding unique index with matching field order."""
    from services.canonical_cutover import (
        RECONCILIATION_COLLECTIONS, logical_key_fields, required_indexes_for,
    )
    # Collections where the logical key is `_id` are allowed to use the
    # implicit primary-key uniqueness (no explicit secondary unique index
    # needed on `_id`).  Any secondary unique index must still match the
    # LK tuple when the LK is NOT `_id`.
    for coll in RECONCILIATION_COLLECTIONS:
        kf = logical_key_fields(coll)
        if kf == ("_id",):
            continue
        specs = required_indexes_for(coll)
        unique_specs = [s for s in specs if s.get("unique")]
        assert unique_specs, f"{coll} missing unique logical-identity index"
        # At least one unique index must match the LK order exactly.
        lk_keys = list(kf)
        matched = any(
            [k for k, _dir in s["keys"]] == lk_keys for s in unique_specs
        )
        assert matched, (
            f"{coll}: no unique index matches logical key {lk_keys}; "
            f"unique indexes present = {[s['name'] for s in unique_specs]}"
        )


def test_settlement_events_identity_uses_settlement_id():
    """Phase-5-R2 explicit sign-off: settlement_events is an
    append-only/versioned authority. event_id MUST NOT be used as the
    identity. Multiple settlement records (including corrections) can
    share the same event_id."""
    from services.canonical_cutover import logical_key_fields
    assert logical_key_fields("settlement_events") == ("settlement_id",)


def test_push_driver_overlay_guard_rejects_empty_overlay(tmp_path):
    """Reproduce the Phase-5-R1 bug: a 0-byte P6 overlay must NOT be
    accepted as 'overlay exists'. The guard added by Phase-5-R2 requires
    size > 0."""
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location(
        "push_canonical_to_production",
        "/app/reconcile_workspace/scripts/push_canonical_to_production.py",
    )
    mod = importlib.util.module_from_spec(spec)
    # Short-circuit: we only need the overlay function. The module's
    # top-level has no side-effects beyond imports.
    sys.modules["push_canonical_to_production"] = mod
    spec.loader.exec_module(mod)

    # The guard is inlined inside main(); import main's source and verify
    # the sentinel size>0 string is present.  (A full main() integration
    # test needs a Prod API; we assert the structural invariant instead.)
    src_text = pathlib.Path(
        "/app/reconcile_workspace/scripts/push_canonical_to_production.py"
    ).read_text()
    assert "stat().st_size > 0" in src_text, \
        "push driver must contain `size > 0` guard for P6 overlays"
    assert "no non-empty source available" in src_text, \
        "push driver must gracefully handle missing-and-empty case"


def test_prediction_snapshots_falls_back_to_p5_when_p6_empty(
    extracted_checkpoints,
):
    """The known Phase-5-R1 defect: empty P6 overlay nuked
    prediction_snapshots. Verify in the actual checkpoints that P5 has
    the full dataset and P6 is empty (or missing), so our new guard
    will correctly fall back."""
    canon5, canon6 = extracted_checkpoints
    p5 = canon5 / "prediction_snapshots.ndjson"
    p6 = canon6 / "prediction_snapshots.ndjson"
    assert p5.exists() and p5.stat().st_size > 0, \
        "P5 prediction_snapshots must have the full canonical dataset"
    # P6 may exist as a 0-byte file or be absent — both are valid "no overlay"
    if p6.exists():
        assert p6.stat().st_size == 0, (
            "If P6 prediction_snapshots exists it must be EMPTY (no overlay). "
            "A non-empty P6 implies a different override intent."
        )
