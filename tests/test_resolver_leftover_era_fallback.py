"""test_resolver_leftover_era_fallback — R3 Resume #19 regression.

Canary #18 remaining mismatches were 10 odd-size LEFTOVER batches
whose stored content_hash was computed from
``accepted_docs[bno*era : bno*era + size]`` where ``era ∈ {250, 1000}``
but ``size`` is a leftover remainder.  The #18 resolver skipped
fixed-era fallback for odd sizes and left them with
``tried_strategies=[]``.

#19 generalises: era-width START offsets (250 and 1000) are tried
for EVERY batch, slice LENGTH is always the authoritative stored
``doc_count``.  Hash equality is the sole acceptance rule.
Collection names are never a resolver rule.

Exact 10 leftovers from the canary artifact (reproduced
synthetically here with each possible era source, hash-authoritative
selection):

    player_game_actuals        bno 29    size 103
    player_game_logs           bno 106   size 69
    player_identities          bno 251   size 235
    prediction_snapshots       bno 928   size 89
    pregame_snapshots          bno 54    size 101
    publication_events         bno 629   size 83
    settlement_events          bno 1988  size 47
    soccer_player_game_logs    bno 400   size 25
    team_game_actuals          bno 240   size 206
    tennis_matches_history     bno 153   size 130

Also regression-tests:
  * 1000-doc buckets still 0 mismatches
  * 250-doc buckets still 0 mismatches
  * settlement_events batch 5 under 1000-era prefix still resolves
    to 93667abc…
  * leftover where cumulative is authoritative still accepted via
    cumulative strategy
  * ambiguous (byte-identical multi-match) accepted with
    ambiguous=True
  * ambiguous (hash collision with differing payloads) fails closed
    as AMBIGUOUS_HASH_COLLISION — note: not deterministically
    reproducible, so we only run the "safe multi-match" scenario.

All tests PROD-FREE — no network calls, no credentials needed.
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
    """Deterministic multi-schema NDJSON (serves all 21 collections)."""
    src = tmp / f"{coll}.ndjson"
    with open(src, "w") as f:
        for i in range(n):
            f.write(json.dumps({
                "doc": {
                    "_id":                 f"doc{i:08d}",
                    "settlement_id":       f"s{i}",
                    "match_id":            f"m{i}",
                    "player_id":           f"pl{i}",
                    "sport":               "nfl",
                    "event_id":            f"e{i}",
                    "canonical_team_id":   f"t{i}",
                    "tourney_id":          f"tn{i}",
                    "winner_id":           f"w{i}",
                    "loser_id":            f"l{i}",
                    "id":                  f"id{i}",
                    "snapshot_hash":       f"h{i}",
                    "prediction_id":       f"pr{i}",
                    "snapshot_version":    i,
                    "payload_hash":        f"ph{i}",
                    "canonical_player_id": f"cp{i}",
                    "pub_at":              i,
                    "n":                   i,
                },
            }) + "\n")
    return src


def _authoritative(coll: str, docs: list[dict]) -> str:
    return _accel._server_batch_content_hash(coll, docs)


# ─── (a) leftover whose stored_hash came from 250-era start ────────
def test_leftover_250_era_authoritative_start_resolves_via_fixed_era_250():
    """A size=47 leftover at bno=1988 whose authoritative content_hash
    is computed from ``accepted_docs[1988*250 : 1988*250 + 47]``.
    Pre-#19 resolver skipped fixed-era for odd sizes → tried only
    cumulative → failed.  #19 must try fixed-era-250 and accept."""
    coll = "settlement_events"
    bno = 1988
    size = 47
    era_start = bno * 250
    # Need enough rows so the fixed-era-250 slice fits + a prefix of
    # earlier batches with mismatched cumulative sum.
    total_rows = era_start + size + 100
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="r3_lv250_"))
    try:
        src = _write_ndjson(tmp, coll, total_rows)
        accepted = [d for d in _accel._ndjson(src)
                     if not _accel._is_excluded(d)]
        # Prefix of 5 × 1000-era so cumulative is DIFFERENT from
        # bno*250 for the leftover.
        manifest = {}
        for i in range(5):
            manifest[i] = {
                "doc_count":    1000,
                "status":       "succeeded",
                "content_hash": _authoritative(
                    coll, accepted[i*1000:(i+1)*1000]),
            }
        # The leftover's stored_hash comes from the 250-era start.
        auth_hash = _authoritative(coll, accepted[era_start:era_start + size])
        manifest[bno] = {
            "doc_count":    size,
            "status":       "failed",
            "content_hash": auth_hash,
        }
        resolved, mm, summary = _accel._resolve_existing_batches(
            coll, src, manifest, 1000)
        # Zero unresolved
        assert len(mm) == 0, mm[:2]
        rL = next(r for r in resolved if r["batch_no"] == bno)
        assert rL["match"]             is True
        assert rL["resolver_strategy"] == "fixed-era-250"
        assert rL["start_offset"]      == era_start
        assert rL["doc_count"]         == size
        assert rL["computed_hash"]     == auth_hash
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── (b) leftover whose stored_hash came from 1000-era start ───────
def test_leftover_1000_era_authoritative_start_resolves_via_fixed_era_1000():
    """A size=89 leftover at bno=928 whose authoritative content_hash
    is computed from ``accepted_docs[928*1000 : 928*1000 + 89]``.
    #19 must try fixed-era-1000 and accept.
    """
    coll = "prediction_snapshots"
    bno = 928
    size = 89
    era_start = bno * 1000
    total_rows = era_start + size + 100
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="r3_lv1000_"))
    try:
        src = _write_ndjson(tmp, coll, total_rows)
        accepted = [d for d in _accel._ndjson(src)
                     if not _accel._is_excluded(d)]
        # Prefix mixed to force cumulative != bno*1000 for the leftover.
        manifest = {}
        for i in range(10):
            manifest[i] = {
                "doc_count":    250,
                "status":       "succeeded",
                "content_hash": _authoritative(
                    coll, accepted[i*250:(i+1)*250]),
            }
        auth_hash = _authoritative(coll, accepted[era_start:era_start + size])
        manifest[bno] = {
            "doc_count":    size,
            "status":       "failed",
            "content_hash": auth_hash,
        }
        resolved, mm, summary = _accel._resolve_existing_batches(
            coll, src, manifest, 1000)
        assert len(mm) == 0, mm[:2]
        rL = next(r for r in resolved if r["batch_no"] == bno)
        assert rL["match"]             is True
        assert rL["resolver_strategy"] == "fixed-era-1000"
        assert rL["start_offset"]      == era_start
        assert rL["doc_count"]         == size
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── (c) leftover where cumulative remains authoritative ───────────
def test_leftover_cumulative_remains_authoritative():
    """A size=89 leftover that is the LAST batch of a dense cumulative
    partition.  Cumulative candidate is the authoritative one.  Must
    be accepted via ``cumulative`` strategy (not fixed-era).
    """
    coll = "prediction_snapshots"
    N1 = 232         # 232 × 1000
    LEFTOVER = 89
    total = N1 * 1000 + LEFTOVER
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="r3_lvcum_"))
    try:
        src = _write_ndjson(tmp, coll, total)
        accepted = [d for d in _accel._ndjson(src)
                     if not _accel._is_excluded(d)]
        manifest = {}
        pos = 0
        for i in range(N1):
            manifest[i] = {
                "doc_count":    1000,
                "status":       "succeeded",
                "content_hash": _authoritative(coll, accepted[pos:pos+1000]),
            }
            pos += 1000
        manifest[N1] = {
            "doc_count":    LEFTOVER,
            "status":       "succeeded",
            "content_hash": _authoritative(coll, accepted[pos:pos+LEFTOVER]),
        }
        resolved, mm, summary = _accel._resolve_existing_batches(
            coll, src, manifest, 1000)
        assert len(mm) == 0, mm[:2]
        rL = next(r for r in resolved if r["batch_no"] == N1)
        assert rL["match"]             is True
        assert rL["resolver_strategy"] == "cumulative"
        assert rL["start_offset"]      == N1 * 1000
        assert rL["doc_count"]         == LEFTOVER
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── Exact 10-leftover canary scenario — mirrors #18 artifact ──────
_CANARY_18_LEFTOVERS = [
    # (collection, batch_no, doc_count, era) — era is the historical
    # partition width that produced the stored content_hash.  We
    # verify the generic #19 resolver recovers each regardless of
    # which era, from hash alone.
    ("player_game_actuals",       29,    103, 250),
    ("player_game_logs",         106,     69, 250),
    ("player_identities",        251,    235, 250),
    ("prediction_snapshots",     928,     89, 1000),
    ("pregame_snapshots",         54,    101, 250),
    ("publication_events",       629,     83, 250),
    ("settlement_events",       1988,     47, 250),
    ("soccer_player_game_logs",  400,     25, 250),
    ("team_game_actuals",        240,    206, 250),
    ("tennis_matches_history",   153,    130, 250),
]


def test_all_10_canary_18_leftovers_resolved_generically():
    """For every one of the 10 reported leftovers, construct a
    synthetic manifest where the stored content_hash was computed
    from the authoritative era's slice; verify #19 resolver resolves
    each one via the correct fixed-era strategy, with NO unresolved
    mismatches total."""
    total_resolved = 0
    strategy_per_batch: dict[str, str] = {}
    for coll, bno, size, era in _CANARY_18_LEFTOVERS:
        era_start = bno * era
        total_rows = era_start + size + 50
        tmp = pathlib.Path(tempfile.mkdtemp(prefix=f"r3_lv_{coll}_"))
        try:
            src = _write_ndjson(tmp, coll, total_rows)
            accepted = [d for d in _accel._ndjson(src)
                         if not _accel._is_excluded(d)]
            # One prior "succeeded" batch to force cumulative to NOT
            # coincide with era_start (so the test actually exercises
            # the fallback, not a lucky cumulative hit).
            prior_size = 1000 if era == 250 else 250
            manifest = {
                0: {"doc_count":    prior_size,
                     "status":       "succeeded",
                     "content_hash": _authoritative(
                         coll, accepted[0:prior_size])},
                bno: {
                     "doc_count":    size,
                     "status":       "failed",
                     "content_hash": _authoritative(
                         coll, accepted[era_start:era_start + size])},
            }
            resolved, mm, summary = _accel._resolve_existing_batches(
                coll, src, manifest, 1000)
            # Zero unresolved for this collection
            assert len(mm) == 0, (coll, bno, mm[:1])
            rL = next(r for r in resolved if r["batch_no"] == bno)
            assert rL["match"]        is True
            assert rL["doc_count"]    == size
            assert rL["start_offset"] == era_start
            assert rL["resolver_strategy"] == f"fixed-era-{era}", (
                coll, bno, size, era, rL)
            strategy_per_batch[f"{coll}:bno{bno}"] = rL["resolver_strategy"]
            total_resolved += 1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    assert total_resolved == 10
    assert len(strategy_per_batch) == 10


# ─── Regression: 1000 and 250 buckets still 0 mismatches ───────────
def test_regression_1000_and_250_buckets_stay_zero_under_19():
    """After #19 generalisation, the four collections' 2 417
    fixed-era-250 resolutions (canary #17 → #18 fix) must still
    resolve.  Sample 100 batches each to keep runtime reasonable."""
    coll = "settlement_events"
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="r3_regr250_"))
    try:
        # 5 × 1000-era + bnos 5..104 × 250-era = 100 fixed-era-250 cases
        total_rows = max(5 * 1000, 105 * 250) + 250
        src = _write_ndjson(tmp, coll, total_rows)
        accepted = [d for d in _accel._ndjson(src)
                     if not _accel._is_excluded(d)]
        manifest = {}
        for i in range(5):
            manifest[i] = {
                "doc_count":    1000,
                "status":       "succeeded",
                "content_hash": _authoritative(coll, accepted[i*1000:(i+1)*1000]),
            }
        for bno in range(5, 105):
            manifest[bno] = {
                "doc_count":    250,
                "status":       "failed",
                "content_hash": _authoritative(
                    coll, accepted[bno*250:bno*250+250]),
            }
        resolved, mm, summary = _accel._resolve_existing_batches(
            coll, src, manifest, 1000)
        assert len(mm) == 0
        assert summary["strategy_counts"]["cumulative"]    == 5
        assert summary["strategy_counts"]["fixed-era-250"] == 100
        assert summary["strategy_counts"]["none"]          == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── Regression: settlement_events batch 5 still 93667abc under #19 ─
_SERVER_EXPECTED_HASH_SETTLEMENT_EVENTS_BATCH_5 = (
    "93667abc7aeffbe1dcbbf39e5bda56fc4803463b456006a02cf26cb23d22dadd"
)


def test_settlement_events_batch5_still_93667abc_under_19():
    """Pinned phase5 NDJSON + 1000-era-prefix manifest ⇒ batch 5 must
    still resolve via fixed-era-250 at start_offset=1250 with hash
    93667abc… (identical to the #18 regression)."""
    import tarfile
    tarball = pathlib.Path(
        "/app/reconcile_workspace/checkpoints/phase5_20261003_190628Z.tar.gz"
    )
    if not tarball.exists():
        import pytest
        pytest.skip("pinned phase5 tarball not present")
    extract_root = pathlib.Path("/tmp/p5x_19")
    canon_src = extract_root / "v2_phase5" / "canonical" / "settlement_events.ndjson"
    if not canon_src.exists():
        extract_root.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tarball, "r:gz") as t:
            t.extractall(extract_root)
    assert canon_src.exists()

    accepted = []
    for d in _accel._ndjson(canon_src):
        if _accel._is_excluded(d):
            continue
        accepted.append(d)
        if len(accepted) >= 5300:
            break

    coll = "settlement_events"
    manifest = {}
    for bno in range(5):
        manifest[bno] = {
            "doc_count":    1000,
            "status":       "succeeded",
            "content_hash": _authoritative(coll, accepted[bno*1000:(bno+1)*1000]),
        }
    batch_5_expected = _authoritative(coll, accepted[1250:1500])
    assert batch_5_expected == _SERVER_EXPECTED_HASH_SETTLEMENT_EVENTS_BATCH_5
    manifest[5] = {
        "doc_count":    250,
        "status":       "failed",
        "content_hash": batch_5_expected,
    }
    resolved, mm, _ = _accel._resolve_existing_batches(
        coll, canon_src, manifest, 1000)
    assert len(mm) == 0, mm
    r5 = next(r for r in resolved if r["batch_no"] == 5)
    assert r5["resolver_strategy"] == "fixed-era-250"
    assert r5["start_offset"] == 1250
    assert r5["computed_hash"] == _SERVER_EXPECTED_HASH_SETTLEMENT_EVENTS_BATCH_5


# ─── Byte-identical multi-match → accepted ambiguous=True ──────────
def test_byte_identical_multi_match_is_accepted_as_ambiguous():
    """When two candidates slice offsets that happen to hold
    byte-identical documents (e.g. deliberate test setup with a
    repeating payload), the resolver accepts (``cumulative`` wins
    the tiebreak) and flags ``ambiguous=True``.  Different payloads
    with the same hash would be an impossible SHA-256 collision and
    cannot be constructed deterministically."""
    coll = "settlement_events"
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="r3_byte_ident_"))
    try:
        # Write 500 rows where rows 0..249 are byte-identical to
        # rows 250..499 (every doc_id identical).  Then an era-start
        # at 0 and a cumulative at 250 both slice the same payload.
        src = tmp / f"{coll}.ndjson"
        with open(src, "w") as f:
            for cycle in range(2):
                for i in range(250):
                    f.write(json.dumps({"doc": {
                        "_id": f"docfixed{i:04d}",
                        "settlement_id": f"sfixed{i}",
                        "n": i,  # same value in both cycles
                    }}) + "\n")
        accepted = [d for d in _accel._ndjson(src)
                     if not _accel._is_excluded(d)]
        assert accepted[0:250] == accepted[250:500]  # pre-condition

        # Batch 1 at bno=1, size=250.  cum_start=250 (after a
        # succeeded 250-size batch 0).  fixed-era-250 start = 250.
        # fixed-era-1000 start = 1000 (out of range, skipped).
        # So cumulative and fixed-era-250 collapse to same start → dedup.
        # That's not multi-match.  Need a different arrangement:
        #
        # Batch 0 at bno=0, size=250. cum_start=0. fixed-era-250 start=0 (deduped).
        # fixed-era-1000 start=0 (deduped).  Single candidate.
        #
        # To get a REAL multi-match from distinct starts producing
        # IDENTICAL payloads, we need cum_start != era_start AND the
        # slices at both starts to be byte-identical.
        #
        # Setup: a size-250 batch at bno=1 with cumulative_start=0
        # (impossible normally — would require batch 0 to have size 0).
        # Instead: use NO prior batches.  Batch at bno=1, size=250,
        # cum_start=0.  fixed-era-250 start = 1*250 = 250.  Both
        # slices equal because of the identical-cycle data.
        auth_hash = _authoritative(coll, accepted[0:250])
        assert auth_hash == _authoritative(coll, accepted[250:500])
        manifest = {
            1: {
                "doc_count":    250,
                "status":       "failed",
                "content_hash": auth_hash,
            },
        }
        resolved, mm, _ = _accel._resolve_existing_batches(
            coll, src, manifest, 1000)
        assert len(mm) == 0
        r1 = next(r for r in resolved if r["batch_no"] == 1)
        assert r1["match"]        is True
        assert r1["ambiguous"]    is True
        # Cumulative wins the tiebreak.
        assert r1["resolver_strategy"] == "cumulative"
        assert r1["start_offset"] == 0
        # Both strategies in candidates_tried
        assert set(r1["candidates_tried"]) == {"cumulative", "fixed-era-250"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── Fail-closed when NO candidate hash matches ────────────────────
def test_leftover_fails_closed_when_no_candidate_hash_matches():
    coll = "settlement_events"
    bno, size = 1988, 47
    era_start_250 = bno * 250
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="r3_lv_fail_"))
    try:
        total_rows = era_start_250 + size + 100
        src = _write_ndjson(tmp, coll, total_rows)
        accepted = [d for d in _accel._ndjson(src)
                     if not _accel._is_excluded(d)]
        manifest = {}
        for i in range(5):
            manifest[i] = {
                "doc_count":    1000,
                "status":       "succeeded",
                "content_hash": _authoritative(
                    coll, accepted[i*1000:(i+1)*1000]),
            }
        manifest[bno] = {
            "doc_count":    size,
            "status":       "failed",
            "content_hash": "deadbeef" * 8,  # matches nothing
        }
        resolved, mm, _ = _accel._resolve_existing_batches(
            coll, src, manifest, 1000)
        assert len(mm) == 1
        assert mm[0]["batch_no"] == bno
        # All three strategies evaluated (cum, fixed-250, fixed-1000)
        tried = set(mm[0]["tried_strategies"])
        assert "cumulative" in tried
        assert "fixed-era-250" in tried
        # fixed-era-1000 slice at 1988*1000 = 1988000 is out of range
        # (NDJSON too short), so it is not added to tried list.
        rL = next(r for r in resolved if r["batch_no"] == bno
                                       and r["stored_status"] == "failed")
        assert rL["match"] is False
        assert rL["resolver_strategy"] == "none"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── Dedup: cumulative == era-start → single candidate, not multi ──
def test_dedup_when_cumulative_equals_era_start():
    """At bno=0, cum_start=0 and bno*250=0 and bno*1000=0.  The
    resolver must dedupe to a single ``cumulative`` candidate and
    NOT report ambiguity."""
    coll = "settlement_events"
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="r3_dedup_"))
    try:
        src = _write_ndjson(tmp, coll, 500)
        accepted = [d for d in _accel._ndjson(src)
                     if not _accel._is_excluded(d)]
        manifest = {
            0: {"doc_count":    250, "status": "failed",
                 "content_hash": _authoritative(coll, accepted[0:250])},
        }
        resolved, mm, _ = _accel._resolve_existing_batches(
            coll, src, manifest, 1000)
        assert len(mm) == 0
        r0 = resolved[0]
        assert r0["match"]             is True
        assert r0["resolver_strategy"] == "cumulative"
        assert r0["ambiguous"]         is False
        assert r0["candidates_tried"] == ["cumulative"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    import time
    tests = [
        test_leftover_250_era_authoritative_start_resolves_via_fixed_era_250,
        test_leftover_1000_era_authoritative_start_resolves_via_fixed_era_1000,
        test_leftover_cumulative_remains_authoritative,
        test_all_10_canary_18_leftovers_resolved_generically,
        test_regression_1000_and_250_buckets_stay_zero_under_19,
        test_settlement_events_batch5_still_93667abc_under_19,
        test_byte_identical_multi_match_is_accepted_as_ambiguous,
        test_leftover_fails_closed_when_no_candidate_hash_matches,
        test_dedup_when_cumulative_equals_era_start,
    ]
    for t in tests:
        s = time.time()
        t()
        print(f"✓ {t.__name__}  ({time.time()-s:.1f}s)")
    print(f"\nOK — {len(tests)} leftover era-fallback scenarios passed")
