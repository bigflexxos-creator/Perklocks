"""test_resolver_250_era_fallback — R3 Resume #18 regression.

Covers the exact failure pattern reported by the latest zero-write
canary artifact: 2 417 BATCH_LAYOUT_MISMATCHes across four collections
where historical 1000-era batches 0..N were later supplemented by
independent 250-era partition runs with batch_no starting at N+1,
whose stored ``content_hash`` was computed from
``accepted_docs[bno*250 : bno*250+250]`` — NOT from the cumulative
offset the pre-#18 resolver was summing.

Observed failed ranges reproduced here (synthetic data, same
topology, same resolution rule):

    settlement_events          bnos 5..1973  size=250
    soccer_player_game_logs    bnos 29..313  size=250
    team_game_actuals          bnos 29..153  size=250
    tennis_matches_history     bnos 29..66   size=250

Each scenario is constructed so that:
  * batches 0..K-1 are 1000-era with ``stored_hash = hash(cumulative
    slice)`` — the pre-#18 resolver already handled these correctly.
  * batches K..M are 250-era with ``stored_hash = hash(accepted_docs
    [bno*250 : bno*250+250])`` — the pre-#18 resolver reported a
    BATCH_LAYOUT_MISMATCH because its cumulative offset was wrong.
  * post-#18 resolver's fixed-era-250 fallback resolves them
    correctly, zero unresolved mismatches.

Plus the exact scenario from the canary artifact:

    settlement_events batch 5 with 1000-era predecessors MUST
    reconstruct from offset 1250 (``bno*250``) and MUST NOT
    reconstruct from offset 5000 (cumulative).  The hash equality
    rule — not any collection-name heuristic — makes the correct
    choice.

Also enforced:
  * leftover (odd-size, not 250/1000) still has ONLY the cumulative
    candidate; no multiplication guess.
  * hash equality remains the sole acceptance authority — collection
    names and batch_no numbers are not primary resolver rules.
  * CANARY_ONLY and live Resume use the SAME resolver output (shared
    resolver — asserted structurally in
    test_resolver_cumulative_offsets.py).

All tests are PROD-FREE — no network calls, no credentials needed.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import tempfile


_SCRIPTS = pathlib.Path("/app/reconcile_workspace/scripts")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_accel = _load("push_canonical_accelerated",
                _SCRIPTS / "push_canonical_accelerated.py")


def _write_ndjson(tmp: pathlib.Path, coll: str, n: int) -> pathlib.Path:
    """Emit a synthetic NDJSON file with ``n`` deterministic rows.
    Field set covers every logical-key field across the 21
    collections so a single generator serves all tests."""
    src = tmp / f"{coll}.ndjson"
    with open(src, "w") as f:
        for i in range(n):
            f.write(json.dumps({
                "doc": {
                    "_id":              f"doc{i:08d}",
                    # Settlement
                    "settlement_id":    f"s{i}",
                    # Soccer player game logs
                    "match_id":         f"m{i}",
                    "player_id":        f"pl{i}",
                    # Team game actuals
                    "sport":            "nfl",
                    "event_id":         f"e{i}",
                    "canonical_team_id": f"t{i}",
                    # Tennis
                    "tourney_id":       f"tn{i}",
                    "winner_id":        f"w{i}",
                    "loser_id":         f"l{i}",
                    # Also present on other collections for sanity
                    "id":               f"id{i}",
                    "snapshot_hash":    f"h{i}",
                    "n":                i,
                },
            }) + "\n")
    return src


def _authoritative(coll: str, docs: list[dict]) -> str:
    return _accel._server_batch_content_hash(coll, docs)


def _build_mixed_manifest(coll: str,
                          accepted_docs: list[dict],
                          thousand_era_count: int,
                          two_fifty_era_last_bno: int,
                          include_tail_leftover: bool = False,
                          leftover_size: int = 0,
                          ) -> dict[int, dict]:
    """Build a manifest that reproduces the overlapping-batch-no
    topology.  Batches 0..K-1 are 1000-era (``stored_hash =
    hash(docs[bno*1000 : bno*1000+1000])``); batches K..M are 250-era
    (``stored_hash = hash(docs[bno*250 : bno*250+250])``).

    By using the raw batch_no * fixed-era-size offset to compute
    stored_hash, we exactly reproduce how the ORIGINAL historical
    import computed the authoritative content_hash before anyone
    ever introduced cumulative tracking.
    """
    manifest: dict[int, dict] = {}
    K = thousand_era_count
    # 1000-era: batch_no * 1000
    for bno in range(K):
        start = bno * 1000
        batch_docs = accepted_docs[start:start + 1000]
        manifest[bno] = {
            "doc_count":   1000,
            "status":      "succeeded",
            "content_hash": _authoritative(coll, batch_docs),
        }
    # 250-era: batch_no * 250
    for bno in range(K, two_fifty_era_last_bno + 1):
        start = bno * 250
        batch_docs = accepted_docs[start:start + 250]
        if len(batch_docs) < 250:
            raise ValueError(
                f"NDJSON too short for 250-era bno={bno} start={start}: "
                f"only {len(batch_docs)} docs available "
                f"(need {start + 250} total)"
            )
        manifest[bno] = {
            "doc_count":   250,
            "status":      "failed",   # replay target
            "content_hash": _authoritative(coll, batch_docs),
        }
    if include_tail_leftover and leftover_size > 0:
        tail_bno = two_fifty_era_last_bno + 1
        # Position after the last 250-era batch
        tail_start = (two_fifty_era_last_bno + 1) * 250
        tail_docs = accepted_docs[tail_start:tail_start + leftover_size]
        if len(tail_docs) == leftover_size:
            manifest[tail_bno] = {
                "doc_count":   leftover_size,
                "status":      "failed",
                "content_hash": _authoritative(coll, tail_docs),
            }
    return manifest


# ─── R3 Resume #18 regression: settlement_events bnos 5..1973 ──────
def test_settlement_events_250_era_fallback_resolves_1969_batches():
    """Mirror of the canary artifact:
      - batches 0..4   = 1000-era  (hash via accepted_docs[bno*1000:+1000])
      - batches 5..1973 = 250-era  (hash via accepted_docs[bno*250:+250])
    Pre-#18: 1 969 BATCH_LAYOUT_MISMATCH.  Post-#18: 0 mismatches,
    all 250-era batches resolved via ``fixed-era-250`` strategy.
    """
    coll = "settlement_events"
    # Need accepted_docs long enough for 1973*250+250 = 493 500
    total_rows = 493_750
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="r3_fallback_settl_"))
    try:
        src = _write_ndjson(tmp, coll, total_rows)
        accepted = []
        for d in _accel._ndjson(src):
            if _accel._is_excluded(d):
                continue
            accepted.append(d)
        assert len(accepted) == total_rows

        manifest = _build_mixed_manifest(
            coll, accepted,
            thousand_era_count=5,
            two_fifty_era_last_bno=1973,
        )
        resolved, mm, summary = _accel._resolve_existing_batches(
            coll, src, manifest, new_batch_size=1000)

        assert len(mm) == 0, (
            f"expected zero mismatches post-#18, got {len(mm)}:  "
            f"first 3 = {mm[:3]}"
        )
        # 1000-era batches 0..4 → cumulative strategy
        for bno in range(5):
            r = next(r for r in resolved if r["batch_no"] == bno)
            assert r["match"] is True
            assert r["resolver_strategy"] == "cumulative", r
            assert r["start_offset"] == bno * 1000, r
            assert r["doc_count"] == 1000
        # 250-era batches 5..1973 → fixed-era-250 strategy
        two_fifty_count = 0
        for r in resolved:
            if r["batch_no"] in (0, 1, 2, 3, 4):
                continue
            if r["stored_status"] == "new":
                continue
            assert r["match"] is True
            assert r["resolver_strategy"] == "fixed-era-250", r
            assert r["start_offset"] == r["batch_no"] * 250, r
            assert r["doc_count"] == 250
            two_fifty_count += 1
        assert two_fifty_count == (1973 - 5 + 1) == 1969
        # Strategy counts
        assert summary["strategy_counts"]["cumulative"] == 5
        assert summary["strategy_counts"]["fixed-era-250"] == 1969
        assert summary["strategy_counts"]["none"] == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── soccer_player_game_logs bnos 29..313 ──────────────────────────
def test_soccer_player_game_logs_250_era_fallback_resolves_285_batches():
    coll = "soccer_player_game_logs"
    # Rows must cover BOTH the 1000-era cumulative range (29 × 1000)
    # AND the 250-era fixed-offset range ((end_bno+1) × 250).
    total_rows = max(29 * 1000, (313 + 1) * 250) + 250
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="r3_fallback_soccer_"))
    try:
        src = _write_ndjson(tmp, coll, total_rows)
        accepted = [d for d in _accel._ndjson(src) if not _accel._is_excluded(d)]
        manifest = _build_mixed_manifest(
            coll, accepted,
            thousand_era_count=29,     # observed: 250-era starts at 29
            two_fifty_era_last_bno=313,
        )
        resolved, mm, summary = _accel._resolve_existing_batches(
            coll, src, manifest, 1000)
        assert len(mm) == 0, mm[:3]
        assert summary["strategy_counts"]["cumulative"]    == 29
        assert summary["strategy_counts"]["fixed-era-250"] == (313 - 29 + 1) == 285
        assert summary["strategy_counts"]["none"]          == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── team_game_actuals bnos 29..153 ────────────────────────────────
def test_team_game_actuals_250_era_fallback_resolves_125_batches():
    coll = "team_game_actuals"
    total_rows = max(29 * 1000, (153 + 1) * 250) + 250
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="r3_fallback_tga_"))
    try:
        src = _write_ndjson(tmp, coll, total_rows)
        accepted = [d for d in _accel._ndjson(src) if not _accel._is_excluded(d)]
        manifest = _build_mixed_manifest(
            coll, accepted,
            thousand_era_count=29,
            two_fifty_era_last_bno=153,
        )
        resolved, mm, summary = _accel._resolve_existing_batches(
            coll, src, manifest, 1000)
        assert len(mm) == 0, mm[:3]
        assert summary["strategy_counts"]["cumulative"]    == 29
        assert summary["strategy_counts"]["fixed-era-250"] == (153 - 29 + 1) == 125
        assert summary["strategy_counts"]["none"]          == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── tennis_matches_history bnos 29..66 ────────────────────────────
def test_tennis_matches_history_250_era_fallback_resolves_38_batches():
    coll = "tennis_matches_history"
    total_rows = max(29 * 1000, (66 + 1) * 250) + 250
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="r3_fallback_tennis_"))
    try:
        src = _write_ndjson(tmp, coll, total_rows)
        accepted = [d for d in _accel._ndjson(src) if not _accel._is_excluded(d)]
        manifest = _build_mixed_manifest(
            coll, accepted,
            thousand_era_count=29,
            two_fifty_era_last_bno=66,
        )
        resolved, mm, summary = _accel._resolve_existing_batches(
            coll, src, manifest, 1000)
        assert len(mm) == 0, mm[:3]
        assert summary["strategy_counts"]["cumulative"]    == 29
        assert summary["strategy_counts"]["fixed-era-250"] == (66 - 29 + 1) == 38
        assert summary["strategy_counts"]["none"]          == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── Hash equality is the SOLE authority (collection name is NOT) ──
def test_hash_equality_is_authority_not_collection_name():
    """settlement_events batch 5 with 1000-era predecessors MUST
    resolve to the fixed-era-250 slice [1250:1500] and MUST NOT
    accept the cumulative slice [5000:5250].

    We prove this is driven by hash equality (not by any
    collection-name-specific rule) by swapping the collection name
    to a non-settlement string: the resolver still picks fixed-era
    because that's what hashes to the authoritative content.
    """
    for coll in ("settlement_events", "some_other_collection"):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix=f"r3_auth_{coll}_"))
        try:
            total_rows = 20_000
            src = _write_ndjson(tmp, coll, total_rows)
            accepted = [d for d in _accel._ndjson(src)
                        if not _accel._is_excluded(d)]
            # 1000-era batches 0..4 (cumulative-correct)
            # 250-era batch 5 (hash from bno*250 = 1250)
            manifest = {}
            for bno in range(5):
                manifest[bno] = {
                    "doc_count":    1000,
                    "status":       "succeeded",
                    "content_hash": _authoritative(
                        coll, accepted[bno*1000:(bno+1)*1000]),
                }
            batch5_authoritative = _authoritative(coll, accepted[1250:1500])
            batch5_cumulative    = _authoritative(coll, accepted[5000:5250])
            # Pre-condition: the two slices must differ
            assert batch5_authoritative != batch5_cumulative, (
                "test is only meaningful if the two candidate slices "
                "produce different hashes")
            manifest[5] = {
                "doc_count":    250,
                "status":       "failed",
                "content_hash": batch5_authoritative,
            }
            resolved, mm, summary = _accel._resolve_existing_batches(
                coll, src, manifest, 1000)
            assert len(mm) == 0, mm
            r5 = next(r for r in resolved if r["batch_no"] == 5)
            assert r5["match"]              is True
            assert r5["resolver_strategy"]  == "fixed-era-250"
            assert r5["start_offset"]       == 1250
            assert r5["computed_hash"]      == batch5_authoritative
            assert r5["computed_hash"]      != batch5_cumulative
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ─── leftover (odd-size) batches have NO fixed-era fallback ────────
def test_leftover_odd_size_batches_do_not_guess_via_multiplication():
    """A batch with ``doc_count`` that is neither 250 nor 1000 must
    be resolved by cumulative alone — no ``bno*doc_count``
    multiplication guess.  If cumulative does not match, fail
    closed."""
    coll = "settlement_events"
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="r3_leftover_"))
    try:
        src = _write_ndjson(tmp, coll, 1000)
        accepted = [d for d in _accel._ndjson(src)
                    if not _accel._is_excluded(d)]
        # One leftover batch of size 173
        bad_hash = _authoritative(coll, accepted[173:173 + 173])
        manifest = {
            0: {
                "doc_count":    173,
                "status":       "failed",
                "content_hash": bad_hash,
            },
        }
        # Cumulative is accepted_docs[0:173], not accepted_docs[173:346].
        # No fixed-era-173 fallback allowed → MISMATCH expected.
        resolved, mm, summary = _accel._resolve_existing_batches(
            coll, src, manifest, 1000)
        assert len(mm) == 1
        r0 = resolved[0]
        assert r0["match"] is False
        # The resolver must have tried ONLY cumulative (not fixed-era-173).
        assert r0["candidates_tried"] == ["cumulative"], r0
        # And summary strategy_counts["none"] accounts for it.
        assert summary["strategy_counts"]["none"] == 1
        assert summary["strategy_counts"]["fixed-era-250"]  == 0
        assert summary["strategy_counts"]["fixed-era-1000"] == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── Both cumulative AND fixed-era match → ambiguous=True ──────────
def test_cumulative_and_fixed_era_both_match_sets_ambiguous_flag():
    """When batch 0 has size 250 and cumulative_start = 0 and
    bno*250 = 0, the two candidates slice the SAME docs.  The
    resolver must still accept (cumulative chosen) and not report
    this as ambiguous — the hashes are the same by construction
    because the slices are the same.

    A real ambiguity is only possible if different slices happen to
    hash equally, which is a 1/2^256 collision — not reproducible
    deterministically.  So this test asserts that when the slices
    collapse to the same slice, no false-positive ambiguity flag."""
    coll = "settlement_events"
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="r3_ambig_"))
    try:
        src = _write_ndjson(tmp, coll, 500)
        accepted = [d for d in _accel._ndjson(src)
                    if not _accel._is_excluded(d)]
        manifest = {
            0: {
                "doc_count":    250,
                "status":       "failed",
                "content_hash": _authoritative(coll, accepted[0:250]),
            },
            1: {
                "doc_count":    250,
                "status":       "failed",
                "content_hash": _authoritative(coll, accepted[250:500]),
            },
        }
        resolved, mm, _ = _accel._resolve_existing_batches(
            coll, src, manifest, 1000)
        assert len(mm) == 0
        # For batch 0: cumulative_start = 0 = bno*250 → single candidate
        r0 = resolved[0]
        assert r0["resolver_strategy"] == "cumulative"
        assert r0["ambiguous"] is False
        assert r0["candidates_tried"] == ["cumulative"], r0
        # For batch 1: cumulative_start = 250 = 1*250 → single candidate
        r1 = resolved[1]
        assert r1["resolver_strategy"] == "cumulative"
        assert r1["ambiguous"] is False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── Fail-closed when neither candidate matches ────────────────────
def test_fail_closed_when_no_candidate_matches():
    """If cumulative AND fixed-era-250 BOTH disagree with the stored
    hash, the batch is a mismatch; the resolver does not silently
    accept any candidate."""
    coll = "settlement_events"
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="r3_fail_closed_"))
    try:
        src = _write_ndjson(tmp, coll, 10_000)
        # Build a manifest where batch 5 has a 250-size payload but
        # the stored_hash is a completely fabricated value matching
        # neither cumulative nor fixed-era-250 reconstruction.
        manifest = {}
        accepted = [d for d in _accel._ndjson(src)
                    if not _accel._is_excluded(d)]
        for bno in range(5):
            manifest[bno] = {
                "doc_count":    1000,
                "status":       "succeeded",
                "content_hash": _authoritative(
                    coll, accepted[bno*1000:(bno+1)*1000]),
            }
        manifest[5] = {
            "doc_count":    250,
            "status":       "failed",
            "content_hash": "deadbeef" * 8,   # won't match anything
        }
        resolved, mm, summary = _accel._resolve_existing_batches(
            coll, src, manifest, 1000)
        assert len(mm) == 1
        assert mm[0]["batch_no"]      == 5
        assert mm[0]["expected_hash"] == "deadbeef" * 8
        # Both candidates were tried
        assert set(mm[0]["tried_strategies"]) == {"cumulative", "fixed-era-250"}
        assert resolved[5]["match"] is False
        assert resolved[5]["resolver_strategy"] == "none"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── CANARY_ONLY and live Resume still use the SAME resolver object ─
def test_canary_and_live_use_identical_resolver_output_after_18():
    """Structural: still exactly one definition of
    _resolve_existing_batches; still called from the live planner
    and from both CANARY_ONLY branches."""
    src_text = (_SCRIPTS / "push_canonical_accelerated.py").read_text()
    call_sites = sum(1 for ln in src_text.splitlines()
                      if "_resolve_existing_batches(" in ln
                      and not ln.lstrip().startswith("#"))
    def_sites = sum(1 for ln in src_text.splitlines()
                     if "def _resolve_existing_batches(" in ln)
    assert def_sites == 1, def_sites
    assert call_sites >= 3, call_sites


# ─── Pinned phase5 regression: batch 5 → 93667abc with 1000-era prefix ─
_SERVER_EXPECTED_HASH_SETTLEMENT_EVENTS_BATCH_5 = (
    "93667abc7aeffbe1dcbbf39e5bda56fc4803463b456006a02cf26cb23d22dadd"
)


def test_pinned_settlement_events_batch5_resolves_via_fixed_era_250():
    """When the manifest claims batches 0..4 are 1000-era (the
    historical topology the canary artifact proved), the pre-#18
    resolver would compute batch 5 from cumulative offset 5000 and
    get a WRONG hash.  Post-#18 must try the fixed-era-250 slice at
    ``5*250 = 1250`` and recover the authoritative server hash
    ``93667abc...``.
    """
    import tarfile
    tarball = pathlib.Path(
        "/app/reconcile_workspace/checkpoints/phase5_20261003_190628Z.tar.gz"
    )
    if not tarball.exists():
        import pytest
        pytest.skip("pinned phase5 tarball not present")
    extract_root = pathlib.Path("/tmp/p5x_fallback")
    canon_src = extract_root / "v2_phase5" / "canonical" / "settlement_events.ndjson"
    if not canon_src.exists():
        extract_root.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tarball, "r:gz") as t:
            t.extractall(extract_root)
    assert canon_src.exists()

    # Preload accepted docs up to offset 5250.
    accepted = []
    for d in _accel._ndjson(canon_src):
        if _accel._is_excluded(d):
            continue
        accepted.append(d)
        if len(accepted) >= 5300:
            break
    assert len(accepted) >= 5300

    # Build the 1000-era-then-250-era manifest shape that the canary
    # artifact proved reflects Production reality.
    coll = "settlement_events"
    manifest = {}
    for bno in range(5):
        manifest[bno] = {
            "doc_count":    1000,
            "status":       "succeeded",
            "content_hash": _authoritative(coll, accepted[bno*1000:(bno+1)*1000]),
        }
    # Batch 5: 250-era, authoritative hash derived from bno*250 = 1250.
    batch_5_expected = _authoritative(coll, accepted[1250:1500])
    assert batch_5_expected == _SERVER_EXPECTED_HASH_SETTLEMENT_EVENTS_BATCH_5, (
        "server-canonical hash drift on pinned phase5 settlement_events "
        "batch 5 (cross-check against earlier regression)"
    )
    manifest[5] = {
        "doc_count":    250,
        "status":       "failed",
        "content_hash": batch_5_expected,
    }
    # Also verify the WRONG cumulative candidate hash is in fact
    # different (so the fallback is meaningful).
    cum_candidate_hash = _authoritative(coll, accepted[5000:5250])
    assert cum_candidate_hash != batch_5_expected

    resolved, mm, summary = _accel._resolve_existing_batches(
        coll, canon_src, manifest, 1000)

    assert len(mm) == 0, (
        f"expected zero mismatches after #18; got {len(mm)}: {mm[:2]}"
    )
    r5 = next(r for r in resolved if r["batch_no"] == 5)
    assert r5["match"]             is True
    assert r5["resolver_strategy"] == "fixed-era-250"
    assert r5["start_offset"]      == 1250
    assert r5["doc_count"]         == 250
    assert r5["computed_hash"] == _SERVER_EXPECTED_HASH_SETTLEMENT_EVENTS_BATCH_5
    assert r5["computed_hash"] != cum_candidate_hash


# ─── Canary artifact bucket count exactly reproduced ───────────────
def test_combined_four_collection_canary_total_matches_2417():
    """Compose all four collections' 250-era mismatches into one
    aggregate and prove post-#18 fallback resolves EVERY one of the
    2 417 mismatches reported in the canary artifact.
    """
    plan = [
        ("settlement_events",         5,  1973, 1969),
        ("soccer_player_game_logs", 29,   313,  285),
        ("team_game_actuals",       29,   153,  125),
        ("tennis_matches_history",  29,    66,   38),
    ]
    assert sum(p[3] for p in plan) == 2417

    total_resolved_fallback = 0
    total_mismatch = 0

    for coll, start_bno, end_bno, expected_count in plan:
        total_rows = max(start_bno * 1000, (end_bno + 1) * 250) + 250
        tmp = pathlib.Path(tempfile.mkdtemp(prefix=f"r3_c250_{coll}_"))
        try:
            src = _write_ndjson(tmp, coll, total_rows)
            accepted = [d for d in _accel._ndjson(src)
                         if not _accel._is_excluded(d)]
            manifest = _build_mixed_manifest(
                coll, accepted,
                thousand_era_count=start_bno,
                two_fifty_era_last_bno=end_bno,
            )
            resolved, mm, summary = _accel._resolve_existing_batches(
                coll, src, manifest, 1000)
            total_mismatch += len(mm)
            total_resolved_fallback += summary["strategy_counts"]["fixed-era-250"]
            assert summary["strategy_counts"]["fixed-era-250"] == expected_count, (
                f"{coll}: expected {expected_count} fixed-era-250 "
                f"resolutions, got {summary['strategy_counts']['fixed-era-250']}"
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    assert total_resolved_fallback == 2417, total_resolved_fallback
    assert total_mismatch          == 0,    total_mismatch


if __name__ == "__main__":
    import time
    tests = [
        test_settlement_events_250_era_fallback_resolves_1969_batches,
        test_soccer_player_game_logs_250_era_fallback_resolves_285_batches,
        test_team_game_actuals_250_era_fallback_resolves_125_batches,
        test_tennis_matches_history_250_era_fallback_resolves_38_batches,
        test_hash_equality_is_authority_not_collection_name,
        test_leftover_odd_size_batches_do_not_guess_via_multiplication,
        test_cumulative_and_fixed_era_both_match_sets_ambiguous_flag,
        test_fail_closed_when_no_candidate_matches,
        test_canary_and_live_use_identical_resolver_output_after_18,
        test_pinned_settlement_events_batch5_resolves_via_fixed_era_250,
        test_combined_four_collection_canary_total_matches_2417,
    ]
    for t in tests:
        s = time.time()
        t()
        print(f"✓ {t.__name__}  ({time.time()-s:.1f}s)")
    print(f"\nOK — {len(tests)} 250-era fallback scenarios passed")
