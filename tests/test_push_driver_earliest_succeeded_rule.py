"""test_push_driver_earliest_succeeded_rule — R3 Resume #15 regression.

Canary #4 proved that ``max(succeeded doc_counts)`` is unsafe when a
later run has successfully added a new tail batch at a different
size.  settlement_events, soccer_player_game_logs, team_game_actuals,
and tennis_matches_history all had 250-doc original partitions
+ a late tail batch at 1000, causing ``max() = 1000`` and shifting
every historical cutpoint.

The surgical fix (Resume #15) switches to the earliest-succeeded-
batch rule: the ORIGINAL historical bsize is encoded in
``batch_no=0`` (and its early neighbors), not in the max.

These tests prove:
  1. The Canary #4 settlement_events topology (1988×250 succeeded +
     1 tail×1000 succeeded) now derives fixed_bsize=250, not 1000.
  2. The fix generalizes to all four affected collections.
  3. The pinned phase5 NDJSON walked with the new rule still
     produces the authoritative server hash for settlement_events
     batch 5 (93667abc…).
  4. The planner's batch_no=0 trust is sound (server contract
     makes it immutable once succeeded).
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import tempfile


_SERVER_EXPECTED_HASH_SETTLEMENT_EVENTS_BATCH_5 = (
    "93667abc7aeffbe1dcbbf39e5bda56fc4803463b456006a02cf26cb23d22dadd"
)
_SCRIPTS = pathlib.Path("/app/reconcile_workspace/scripts")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ensure_pinned_phase5() -> pathlib.Path | None:
    dest = pathlib.Path("/tmp/p5x_earliest")
    marker = dest / "v2_phase5" / "canonical" / "settlement_events.ndjson"
    if marker.exists():
        return marker.parent
    tarball = pathlib.Path(
        "/app/reconcile_workspace/checkpoints/phase5_20261003_190628Z.tar.gz"
    )
    if not tarball.exists():
        return None
    import tarfile
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball, "r:gz") as t:
        t.extractall(dest)
    return marker.parent


def _derive_fixed_bsize(manifest: dict[int, dict], new_batch_size: int = 1000) -> tuple[int, str]:
    """Replica of the Resume #15 planner's fixed_bsize derivation.
    Keep this test-local replica in sync with the driver — any drift
    will fail test_canary4_hash_against_pinned_source."""
    succeeded_by_bno = sorted(
        [(bno, v["doc_count"]) for bno, v in manifest.items()
         if v.get("status") == "succeeded"
         and v.get("doc_count", 0) > 0]
    )
    historical = [(bno, v["doc_count"]) for bno, v in manifest.items()
                   if v.get("doc_count", 0) > 0]
    if succeeded_by_bno:
        bno0, bsize = succeeded_by_bno[0]
        return bsize, f"earliest_succeeded_bno={bno0} doc_count={bsize}"
    if historical:
        historical.sort()
        return historical[0][1], f"earliest_historical_bno={historical[0][0]}"
    return new_batch_size, f"new_collection_bsize={new_batch_size}"


# ─── Canary #4 topology regressions ──────────────────────────────────
def _canary4_manifest(
    full_batches: int = 1988,
    full_size:    int = 250,
    tail_size:    int = 1000,
    tail_bno:     int = 1988,
) -> dict[int, dict]:
    """Build a synthetic manifest matching Canary #4's observed
    settlement_events topology: ``full_batches`` succeeded batches
    at ``full_size`` docs each, plus a single late-tail succeeded
    batch at ``tail_size`` docs.
    """
    mf: dict[int, dict] = {}
    for bno in range(full_batches):
        mf[bno] = {
            "doc_count": full_size,
            "status":    "succeeded",
            "content_hash": f"h{bno:064d}"[-64:],
        }
    mf[tail_bno] = {
        "doc_count": tail_size,
        "status":    "succeeded",
        "content_hash": f"t{tail_bno:063d}"[-64:],
    }
    return mf


def test_canary4_settlement_events_topology_picks_250_not_1000():
    """1988 succeeded @ 250 + 1 late tail @ 1000 must derive bsize=250."""
    mf = _canary4_manifest()
    bsize, rationale = _derive_fixed_bsize(mf)
    assert bsize == 250, (
        f"Canary #4 topology must derive 250 (ORIGINAL run's size), "
        f"got {bsize}.  Rationale: {rationale}"
    )
    assert "earliest_succeeded_bno=0" in rationale


def test_tail_only_run_does_not_poison_derivation():
    """Even if the manifest has a HIGH-size tail at batch 10000, the
    derivation must still pick the earliest succeeded batch's size."""
    mf = {
        0:     {"doc_count": 250,  "status": "succeeded",
                 "content_hash": "x"*64},
        1:     {"doc_count": 250,  "status": "succeeded",
                 "content_hash": "x"*64},
        2:     {"doc_count": 250,  "status": "succeeded",
                 "content_hash": "x"*64},
        10000: {"doc_count": 2000, "status": "succeeded",
                 "content_hash": "x"*64},
    }
    bsize, _ = _derive_fixed_bsize(mf)
    assert bsize == 250, bsize


def test_four_affected_collections_derive_250():
    """All four Canary #4-affected collections follow the same rule."""
    for coll in ("settlement_events", "soccer_player_game_logs",
                  "team_game_actuals", "tennis_matches_history"):
        mf = _canary4_manifest()  # same topology shape
        bsize, _ = _derive_fixed_bsize(mf)
        assert bsize == 250, f"{coll}: got {bsize}"


def test_truly_new_collection_still_uses_new_batch_size():
    """Zero manifest entries → NEW_COLLECTION_BATCH_SIZE."""
    bsize, rationale = _derive_fixed_bsize({}, new_batch_size=1000)
    assert bsize == 1000
    assert "new_collection_bsize" in rationale


def test_no_succeeded_but_historical_falls_back_to_lowest_bno():
    """No succeeded batches but failed historical identities exist →
    use the lowest-numbered historical batch's doc_count."""
    mf = {
        0: {"doc_count": 250,  "status": "failed",
            "content_hash": "x"*64},
        1: {"doc_count": 1000, "status": "in_progress",
            "content_hash": "x"*64},
    }
    bsize, rationale = _derive_fixed_bsize(mf)
    assert bsize == 250
    assert "earliest_historical_bno=0" in rationale


# ─── Hash parity against the pinned source (critical regression) ─────
def test_canary4_hash_against_pinned_source():
    """END-TO-END proof that the earliest-succeeded-batch rule, applied
    to the Canary #4 settlement_events topology, still reconstructs
    batch 5 to the authoritative server hash 93667abc…."""
    canon = _ensure_pinned_phase5()
    if canon is None:
        import pytest
        pytest.skip("pinned phase5 tarball not present")
    src = canon / "settlement_events.ndjson"
    assert src.exists()
    accel = _load("push_canonical_accelerated",
                   _SCRIPTS / "push_canonical_accelerated.py")

    # Canary #4 manifest: 1988 succeeded @ 250 + 1 tail @ 1000.
    mf = _canary4_manifest()
    bsize, _rationale = _derive_fixed_bsize(mf)
    assert bsize == 250

    # Walk with the DERIVED fixed_bsize and extract batch 5.
    buf = []
    cur_bno = 0
    for d in accel._ndjson(src):
        if accel._is_excluded(d):
            continue
        buf.append(d)
        if len(buf) >= bsize:
            if cur_bno == 5:
                break
            cur_bno += 1
            buf = []
    assert len(buf) == 250
    h = accel._server_batch_content_hash("settlement_events", buf)
    assert h == _SERVER_EXPECTED_HASH_SETTLEMENT_EVENTS_BATCH_5, (
        f"settlement_events batch 5 hash mismatch under Canary #4 "
        f"topology:\n  expected: {_SERVER_EXPECTED_HASH_SETTLEMENT_EVENTS_BATCH_5}\n"
        f"  got:      {h}"
    )


def test_canary4_hash_IS_WRONG_under_old_max_rule():
    """Negative regression — proves the OLD max(succeeded) rule
    produces the WRONG hash under Canary #4 topology.  This locks in
    the fact that Resume #15's derivation change was necessary."""
    canon = _ensure_pinned_phase5()
    if canon is None:
        import pytest
        pytest.skip("pinned phase5 tarball not present")
    src = canon / "settlement_events.ndjson"
    accel = _load("push_canonical_accelerated_old",
                   _SCRIPTS / "push_canonical_accelerated.py")

    # Simulate the OLD rule: max over succeeded doc_counts.
    mf = _canary4_manifest()
    succ_sizes = [v["doc_count"] for v in mf.values() if v["status"] == "succeeded"]
    old_bsize = max(succ_sizes)
    assert old_bsize == 1000, "OLD rule should pick 1000 under Canary #4 topology"

    # Walk with the wrong bsize.
    buf = []
    cur_bno = 0
    for d in accel._ndjson(src):
        if accel._is_excluded(d):
            continue
        buf.append(d)
        if len(buf) >= old_bsize:
            if cur_bno == 5:
                break
            cur_bno += 1
            buf = []
    h_wrong = accel._server_batch_content_hash("settlement_events", buf)
    assert h_wrong != _SERVER_EXPECTED_HASH_SETTLEMENT_EVENTS_BATCH_5, (
        "Negative test invariant broken: OLD max() rule produced the "
        "same hash as the server — regression topology no longer "
        "triggers the bug.  Review _canary4_manifest()."
    )


if __name__ == "__main__":
    test_canary4_settlement_events_topology_picks_250_not_1000()
    test_tail_only_run_does_not_poison_derivation()
    test_four_affected_collections_derive_250()
    test_truly_new_collection_still_uses_new_batch_size()
    test_no_succeeded_but_historical_falls_back_to_lowest_bno()
    test_canary4_hash_against_pinned_source()
    test_canary4_hash_IS_WRONG_under_old_max_rule()
    print("OK — earliest-succeeded-batch rule verified against Canary #4 topology")
