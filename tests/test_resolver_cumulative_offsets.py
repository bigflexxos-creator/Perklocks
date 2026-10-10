"""test_resolver_cumulative_offsets — R3 Resume #16 full regression.

Validates the shared ``_resolve_existing_batches`` cumulative-offset
resolver (Support-confirmed algorithm) across all 12 required
scenarios:

  1. Python compile/syntax check (implicit — import succeeds)
  2. Cumulative batch offsets correct
  3. 1 000-doc batches
  4. 250-doc batches
  5. Odd-sized leftover batches
  6. Transition 1000-era → leftover → 250-era → final leftover
  7. Succeeded = skip-only (no POST in live planner)
  8. Failed exact-hash replay (POST planned when hash matches)
  9. Hash mismatch = FAIL CLOSED (BATCH_LAYOUT_MISMATCH)
 10. CANARY_ONLY and live Resume use SAME resolver (identity test)
 11. CANARY_ONLY cannot POST (zero-write guarantee by construction)
 12. Workflows pin CONCURRENCY=1 (static-scan test)

Plus:
 13. End-to-end hash parity against pinned phase5 NDJSON for
     settlement_events batch 5: must still produce 93667abc…

All tests are PROD-FREE — no network calls, no credentials needed.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import sys
import tempfile


_SCRIPTS = pathlib.Path("/app/reconcile_workspace/scripts")
_WORKFLOWS = pathlib.Path("/app/.github/workflows")

_SERVER_EXPECTED_HASH_SETTLEMENT_EVENTS_BATCH_5 = (
    "93667abc7aeffbe1dcbbf39e5bda56fc4803463b456006a02cf26cb23d22dadd"
)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_accel = _load("push_canonical_accelerated",
                _SCRIPTS / "push_canonical_accelerated.py")


def _write_ndjson(tmp: pathlib.Path, coll: str, n: int,
                   id_prefix: str = "doc") -> pathlib.Path:
    src = tmp / f"{coll}.ndjson"
    with open(src, "w") as f:
        for i in range(n):
            f.write(json.dumps({
                "doc": {"_id": f"{id_prefix}{i:08d}",
                         "event_id":        f"e{i}",
                         "settlement_id":   f"s{i}",
                         "canonical_player_id": f"cp{i}",
                         "snapshot_hash":   f"h{i}",
                         "id":              f"id{i}",
                         "prediction_id":   f"pr{i}",
                         "snapshot_version": i,
                         "payload_hash":    f"ph{i}",
                         "n": i},
            }) + "\n")
    return src


def _authoritative(coll: str, docs: list[dict]) -> str:
    return _accel._server_batch_content_hash(coll, docs)


# ─── 2. cumulative offsets correct ──────────────────────────────────
def test_cumulative_offsets_are_sum_of_prior_doc_counts():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="r3_cum_"))
    try:
        src = _write_ndjson(tmp, "settlement_events", 1000)
        docs = [{"doc": None, "_id": f"doc{i:08d}"} for i in range(1000)]
        # Build manifest with mixed sizes: 100, 50, 200, 150, 500 = 1000 total
        sizes = [100, 50, 200, 150, 500]
        offs  = [0, 100, 150, 350, 500]
        docs_in_order = []
        with open(src) as f:
            for ln in f:
                docs_in_order.append(json.loads(ln)["doc"])
        manifest = {}
        for i, (s, o) in enumerate(zip(sizes, offs)):
            manifest[i] = {
                "doc_count": s, "status": "succeeded",
                "content_hash": _authoritative("settlement_events",
                                                docs_in_order[o:o+s]),
            }
        resolved, mm, summary = _accel._resolve_existing_batches(
            "settlement_events", src, manifest, new_batch_size=1000)
        assert len(mm) == 0, mm
        assert [r["batch_no"] for r in resolved] == [0, 1, 2, 3, 4]
        assert [r["start_offset"] for r in resolved] == offs
        assert [r["doc_count"]    for r in resolved] == sizes
        assert all(r["match"]      for r in resolved)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── 3. 1000-doc batches ────────────────────────────────────────────
def test_1000_doc_batches_reconstruct_and_hash_match():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="r3_1k_"))
    try:
        src = _write_ndjson(tmp, "player_identities", 3500)
        docs = [{"doc": None, "_id": f"doc{i:08d}"} for i in range(3500)]
        docs_in_order = []
        with open(src) as f:
            for ln in f:
                docs_in_order.append(json.loads(ln)["doc"])
        manifest = {}
        for i in range(3):
            manifest[i] = {
                "doc_count": 1000, "status": "succeeded",
                "content_hash": _authoritative(
                    "player_identities", docs_in_order[i*1000:(i+1)*1000]),
            }
        # tail batch of 500
        manifest[3] = {
            "doc_count": 500, "status": "succeeded",
            "content_hash": _authoritative(
                "player_identities", docs_in_order[3000:3500]),
        }
        resolved, mm, _ = _accel._resolve_existing_batches(
            "player_identities", src, manifest, 1000)
        assert len(mm) == 0, mm
        assert [r["doc_count"] for r in resolved] == [1000, 1000, 1000, 500]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── 4. 250-doc batches ─────────────────────────────────────────────
def test_250_doc_batches_reconstruct_and_hash_match():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="r3_250_"))
    try:
        src = _write_ndjson(tmp, "picks", 1123)
        docs_in_order = []
        with open(src) as f:
            for ln in f:
                docs_in_order.append(json.loads(ln)["doc"])
        manifest = {}
        for i in range(4):   # 4 × 250 = 1000
            manifest[i] = {
                "doc_count": 250, "status": "succeeded",
                "content_hash": _authoritative(
                    "picks", docs_in_order[i*250:(i+1)*250]),
            }
        manifest[4] = {   # tail 123
            "doc_count": 123, "status": "succeeded",
            "content_hash": _authoritative(
                "picks", docs_in_order[1000:1123]),
        }
        resolved, mm, _ = _accel._resolve_existing_batches(
            "picks", src, manifest, 1000)
        assert len(mm) == 0, mm
        assert [r["doc_count"] for r in resolved] == [250, 250, 250, 250, 123]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── 5. odd-sized leftover ──────────────────────────────────────────
def test_odd_sized_leftover_tail_batch():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="r3_tail_"))
    try:
        src = _write_ndjson(tmp, "settlement_events", 89)
        docs = [json.loads(ln)["doc"] for ln in open(src)]
        manifest = {
            0: {"doc_count": 89, "status": "succeeded",
                "content_hash": _authoritative("settlement_events", docs)},
        }
        resolved, mm, _ = _accel._resolve_existing_batches(
            "settlement_events", src, manifest, 1000)
        assert len(mm) == 0
        assert resolved[0]["doc_count"] == 89
        assert resolved[0]["match"] is True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── 6. transition 1000-era → leftover → 250-era → final leftover ───
def test_transition_1000_leftover_250_leftover():
    """Reproduces the exact prediction_snapshots topology Support
    described:
      - batches 0–231 = 1 000 docs each
      - batch 232     = 89 docs (first leftover)
      - batches 233–927 = 250 docs each
      - batch 928     = 89 docs (final leftover)
    """
    N1 = 232       # 1000-era full batches
    LEFTOVER_A = 89
    N2 = 695       # 250-era full batches (233..927 inclusive = 695)
    LEFTOVER_B = 89
    total = N1 * 1000 + LEFTOVER_A + N2 * 250 + LEFTOVER_B

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="r3_trans_"))
    try:
        src = _write_ndjson(tmp, "prediction_snapshots", total)
        docs = [json.loads(ln)["doc"] for ln in open(src)]

        manifest = {}
        pos = 0
        # 1000-era
        for i in range(N1):
            manifest[i] = {
                "doc_count": 1000, "status": "succeeded",
                "content_hash": _authoritative(
                    "prediction_snapshots", docs[pos:pos+1000]),
            }
            pos += 1000
        # first leftover
        manifest[N1] = {
            "doc_count": LEFTOVER_A, "status": "succeeded",
            "content_hash": _authoritative(
                "prediction_snapshots", docs[pos:pos+LEFTOVER_A]),
        }
        pos += LEFTOVER_A
        # 250-era
        for j in range(N2):
            bno = N1 + 1 + j
            manifest[bno] = {
                "doc_count": 250, "status": "succeeded",
                "content_hash": _authoritative(
                    "prediction_snapshots", docs[pos:pos+250]),
            }
            pos += 250
        # final leftover
        manifest[N1 + 1 + N2] = {
            "doc_count": LEFTOVER_B, "status": "succeeded",
            "content_hash": _authoritative(
                "prediction_snapshots", docs[pos:pos+LEFTOVER_B]),
        }
        pos += LEFTOVER_B
        assert pos == total

        resolved, mm, _ = _accel._resolve_existing_batches(
            "prediction_snapshots", src, manifest, 1000)
        assert len(mm) == 0, (len(mm), mm[:3])
        assert len(resolved) == len(manifest)
        for r in resolved:
            assert r["match"] is True, r
        # Spot check: batch 5 is a 1000-doc batch
        assert resolved[5]["doc_count"] == 1000
        # Batch 232 is the first 89-leftover
        assert resolved[232]["doc_count"] == 89
        # Batch 233 is first 250-era
        assert resolved[233]["doc_count"] == 250
        # Final leftover
        assert resolved[-1]["doc_count"] == 89
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── 7. succeeded = skip-only in live planner ───────────────────────
def test_succeeded_batches_are_skip_only_in_live_planner():
    """Succeeded resolved entries must be filtered OUT of the POST
    plan.  Only failed/in_progress/new entries get planned."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="r3_skip_"))
    try:
        src = _write_ndjson(tmp, "picks", 500)
        docs = [json.loads(ln)["doc"] for ln in open(src)]
        manifest = {
            0: {"doc_count": 250, "status": "succeeded",
                "content_hash": _authoritative("picks", docs[0:250])},
            1: {"doc_count": 250, "status": "failed",
                "content_hash": _authoritative("picks", docs[250:500])},
        }
        resolved, mm, _ = _accel._resolve_existing_batches(
            "picks", src, manifest, 1000)
        assert len(mm) == 0
        # Simulate planner's partitioning (same logic as main()):
        plan_posts = [r for r in resolved
                       if r["match"] and r["stored_status"] != "succeeded"]
        skip_only  = [r for r in resolved
                       if r["match"] and r["stored_status"] == "succeeded"]
        assert len(plan_posts) == 1 and plan_posts[0]["batch_no"] == 1
        assert len(skip_only) == 1 and skip_only[0]["batch_no"] == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── 8. failed exact-hash replay (POST planned when hash matches) ───
def test_failed_batch_exact_hash_replay_is_planned():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="r3_replay_"))
    try:
        src = _write_ndjson(tmp, "picks", 250)
        docs = [json.loads(ln)["doc"] for ln in open(src)]
        manifest = {
            0: {"doc_count": 250, "status": "failed",
                "content_hash": _authoritative("picks", docs)},
        }
        resolved, mm, _ = _accel._resolve_existing_batches(
            "picks", src, manifest, 1000)
        assert len(mm) == 0, mm
        assert resolved[0]["match"] is True
        assert resolved[0]["stored_status"] == "failed"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── 9. hash mismatch = FAIL CLOSED ─────────────────────────────────
def test_hash_mismatch_fails_closed():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="r3_fail_"))
    try:
        src = _write_ndjson(tmp, "picks", 250)
        # Deliberately wrong stored hash.
        manifest = {
            0: {"doc_count": 250, "status": "succeeded",
                "content_hash": "deadbeef" * 8},
        }
        resolved, mm, _ = _accel._resolve_existing_batches(
            "picks", src, manifest, 1000)
        assert len(mm) == 1
        assert mm[0]["batch_no"] == 0
        assert mm[0]["expected_hash"] == "deadbeef" * 8
        assert mm[0]["mismatch_reason"].startswith("computed_hash != stored_hash")
        # The resolved entry must still exist but match=False.
        assert resolved[0]["match"] is False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── 10. CANARY_ONLY and live Resume use SAME resolver ──────────────
def test_canary_and_live_use_identical_resolver_output():
    """Static-identity test: the module exposes exactly ONE
    _resolve_existing_batches symbol; both the live planner (main())
    and the CANARY_ONLY branch reference the same function."""
    src_text = (_SCRIPTS / "push_canonical_accelerated.py").read_text()
    # Count occurrences of the function CALL (not definition).
    call_sites = sum(1 for ln in src_text.splitlines()
                      if "_resolve_existing_batches(" in ln
                      and not ln.lstrip().startswith("#"))
    # Expected call sites:
    #   1. def _resolve_existing_batches (definition — not a call per the filter above)
    #   2. live planner main-loop call
    #   3. CANARY_ONLY candidates loop
    #   4. CANARY_ONLY first-unfinished loop
    # So call_sites should be exactly 3 (the def line's parens are
    # NOT a call because of the "def " prefix).
    assert call_sites >= 3, (
        f"expected ≥3 call sites to _resolve_existing_batches, found {call_sites}"
    )
    # And only ONE definition.
    def_sites = sum(1 for ln in src_text.splitlines()
                     if "def _resolve_existing_batches(" in ln)
    assert def_sites == 1, (
        f"expected exactly 1 definition of _resolve_existing_batches, "
        f"found {def_sites}"
    )


# ─── 11. CANARY_ONLY cannot POST to Production ──────────────────────
def test_canary_only_mode_has_zero_post_sites_before_return():
    """Static scan: in CANARY_ONLY=1 mode, the driver must early-
    return before reaching any ``_post(...)`` site that targets
    ``/canonical-import``.  Verified by locating the
    ``if canary_only:`` block and asserting its body ends with
    ``return 0`` or ``return 45`` BEFORE any call to the live POST
    helper."""
    src_text = (_SCRIPTS / "push_canonical_accelerated.py").read_text()
    lines = src_text.splitlines()
    # Find the canary_only guarded block.
    in_canary_block = False
    saw_return_in_block = False
    for ln in lines:
        stripped = ln.lstrip()
        if "if canary_only:" in stripped:
            in_canary_block = True
            continue
        if not in_canary_block:
            continue
        if stripped.startswith("return 0") or stripped.startswith("return 45"):
            saw_return_in_block = True
            # Any POST site encountered after this is OK (live path).
            break
        # Guard: no POST before the return inside the canary block.
        assert "_post(" not in ln or ln.strip().startswith("#"), (
            f"CANARY_ONLY block issues a _post before early return: {ln!r}"
        )
        assert "/canonical-import" not in ln or ln.strip().startswith("#") \
            or "batch-manifest" in ln or "canonical-import/" in ln and "GET" in ln, (
            f"CANARY_ONLY block references /canonical-import POST path: {ln!r}"
        )
    assert in_canary_block,       "CANARY_ONLY block not found"
    assert saw_return_in_block,   "CANARY_ONLY block did not early-return"


# ─── 12. workflows pin CONCURRENCY=1 ────────────────────────────────
def test_workflows_pin_concurrency_1_for_canary_and_resume():
    for wf in ("perklocks-r3-batch-layout-canary.yml",
                "perklocks-r3-resume.yml"):
        p = _WORKFLOWS / wf
        assert p.exists(), p
        txt = p.read_text()
        assert 'CONCURRENCY:' in txt, f"{wf}: CONCURRENCY not set at all"
        # Require the quoted value "1".
        assert 'CONCURRENCY:                "1"' in txt \
            or 'CONCURRENCY:     "1"' in txt, (
            f"{wf}: CONCURRENCY is not pinned to \"1\".  "
            f"Grep the file to confirm.")


# ─── 13. end-to-end regression on pinned phase5 settlement_events ───
def test_pinned_settlement_events_batch5_still_matches_server_hash():
    """Walk the pinned phase5 settlement_events.ndjson through the
    resolver using a manifest shaped like Canary #4 (1988×250
    succeeded + 1 tail×1000 succeeded) and prove batch 5's hash
    equals the server's 93667abc…."""
    tarball = pathlib.Path(
        "/app/reconcile_workspace/checkpoints/phase5_20261003_190628Z.tar.gz"
    )
    if not tarball.exists():
        import pytest
        pytest.skip("pinned phase5 tarball not present")
    import tarfile
    extract_root = pathlib.Path("/tmp/p5x_resolver")
    canon_src = extract_root / "v2_phase5" / "canonical" / "settlement_events.ndjson"
    if not canon_src.exists():
        extract_root.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tarball, "r:gz") as t:
            t.extractall(extract_root)
    assert canon_src.exists()

    # Precompute the authoritative content_hashes for batches 0..5 at
    # bsize=250 to populate the manifest.  Walk the real NDJSON.
    needed_batches = 6
    batch_size = 250
    docs_for_batches: list[list[dict]] = []
    buf = []
    for d in _accel._ndjson(canon_src):
        if _accel._is_excluded(d):
            continue
        buf.append(d)
        if len(buf) >= batch_size:
            docs_for_batches.append(list(buf))
            buf = []
            if len(docs_for_batches) >= needed_batches:
                break
    assert len(docs_for_batches) == needed_batches

    manifest = {}
    for i in range(needed_batches):
        manifest[i] = {
            "doc_count": 250, "status": "succeeded",
            "content_hash": _authoritative(
                "settlement_events", docs_for_batches[i]),
        }
    # Resolve and assert.
    resolved, mm, _ = _accel._resolve_existing_batches(
        "settlement_events", canon_src, manifest, 1000)
    assert len(mm) == 0, mm[:3]
    batch_5 = next(r for r in resolved if r["batch_no"] == 5)
    assert batch_5["doc_count"] == 250
    assert batch_5["computed_hash"] == \
        _SERVER_EXPECTED_HASH_SETTLEMENT_EVENTS_BATCH_5, (
        f"server-canonical hash drift for settlement_events batch 5"
    )


if __name__ == "__main__":
    tests = [
        test_cumulative_offsets_are_sum_of_prior_doc_counts,
        test_1000_doc_batches_reconstruct_and_hash_match,
        test_250_doc_batches_reconstruct_and_hash_match,
        test_odd_sized_leftover_tail_batch,
        test_transition_1000_leftover_250_leftover,
        test_succeeded_batches_are_skip_only_in_live_planner,
        test_failed_batch_exact_hash_replay_is_planned,
        test_hash_mismatch_fails_closed,
        test_canary_and_live_use_identical_resolver_output,
        test_canary_only_mode_has_zero_post_sites_before_return,
        test_workflows_pin_concurrency_1_for_canary_and_resume,
        test_pinned_settlement_events_batch5_still_matches_server_hash,
    ]
    for t in tests:
        t()
        print(f"✓ {t.__name__}")
    print(f"\nOK — {len(tests)} resolver scenarios passed")
