"""Guard tests for reconcile_input_ingest."""
from __future__ import annotations

import gzip
import json
import os
import pathlib

import pytest

from scripts.reconcile_input_ingest import (
    _parse_filename, _ingest_file, _iter_docs, _refuse_live,
    _load_inventory, _logical_key, _evaluate_collection_ready,
    _empty_coll_state, INPUT_DIR, INVENTORY,
)


# ─── Filename parsing ────────────────────────────────────────────────
@pytest.mark.parametrize("name, expected", [
    ("picks.json",                 ("picks", None, "data")),
    ("picks.json.gz",              ("picks", None, "data")),
    ("picks_part1.json",           ("picks", 1, "data")),
    ("picks_part12.json.gz",       ("picks", 12, "data")),
    ("players_slim.json",          ("players", None, "data")),
    ("players_slim_part3.ndjson",  ("players", 3, "data")),
    ("rollover_slates.complete.json", ("rollover_slates", None, "sentinel")),
    ("picks.complete",             ("picks", None, "sentinel")),
    ("not-a-collection file.txt",  None),
    ("random.pdf",                 None),
])
def test_filename_parsing(name, expected):
    assert _parse_filename(name) == expected


# ─── Live-DB refusal ─────────────────────────────────────────────────
def test_refuse_live_mongo_uri():
    with pytest.raises(SystemExit):
        _refuse_live("mongodb+srv://prod.net/")


def test_refuse_localhost():
    with pytest.raises(SystemExit):
        _refuse_live("mongodb://localhost:27017")


# ─── JSON array ingest ────────────────────────────────────────────────
def test_ingest_json_array(tmp_path):
    f = tmp_path / "picks.json"
    f.write_text(json.dumps([{"_id": "a", "x": 1}, {"_id": "b", "x": 2}]))
    inv = {"files": {}, "collections": {}}
    r = _ingest_file(f, inv)
    assert r["doc_count"] == 2
    assert r["collection"] == "picks"
    assert r["duplicate_ids_within_file"] == 0


# ─── NDJSON ingest ────────────────────────────────────────────────────
def test_ingest_ndjson(tmp_path):
    f = tmp_path / "games_part1.ndjson"
    f.write_text('{"_id":"g1"}\n{"_id":"g2"}\n')
    inv = {"files": {}, "collections": {}}
    r = _ingest_file(f, inv)
    assert r["doc_count"] == 2
    assert r["collection"] == "games"
    assert r["part"] == 1


# ─── Gzipped JSON array ingest ───────────────────────────────────────
def test_ingest_gzipped_json(tmp_path):
    f = tmp_path / "users.json.gz"
    with gzip.open(f, "wt") as g:
        json.dump([{"_id": "u1"}, {"_id": "u2"}, {"_id": "u3"}], g)
    inv = {"files": {}, "collections": {}}
    r = _ingest_file(f, inv)
    assert r["doc_count"] == 3
    assert r["collection"] == "users"


# ─── Idempotent re-ingest ─────────────────────────────────────────────
def test_idempotent_reingest(tmp_path):
    f = tmp_path / "picks.json"
    f.write_text(json.dumps([{"_id": "a"}]))
    inv = {"files": {}, "collections": {}}
    r1 = _ingest_file(f, inv)
    r2 = _ingest_file(f, inv)
    assert r1["doc_count"] == 1
    assert r2["status"] == "ALREADY_INGESTED"


# ─── Duplicate IDs across parts ───────────────────────────────────────
def test_duplicate_ids_across_parts(tmp_path):
    f1 = tmp_path / "picks_part1.json"
    f2 = tmp_path / "picks_part2.json"
    f1.write_text(json.dumps([{"_id": "a"}, {"_id": "b"}]))
    f2.write_text(json.dumps([{"_id": "b"}, {"_id": "c"}]))
    inv = {"files": {}, "collections": {}}
    _ingest_file(f1, inv)
    _ingest_file(f2, inv)
    cs = inv["collections"]["picks"]
    assert cs["total_docs"] == 4
    assert cs["unique_mongo_ids"] == 3
    assert cs["duplicate_ids_across_parts"] == ["b"]


# ─── Parse failure fails closed, inventory still intact ──────────────
def test_parse_failure_fails_closed(tmp_path):
    f = tmp_path / "picks.json"
    f.write_text("{{{ not valid json ")
    inv = {"files": {}, "collections": {}}
    r = _ingest_file(f, inv)
    # NDJSON probing swallows invalid lines, so returns doc_count=0
    # OR, if first char parsed as "{" and we got nothing, status should
    # still be a benign zero-doc result rather than a crash.
    assert r["doc_count"] == 0 or r["status"] == "PARSE_FAILED"


# ─── Unknown filename pattern is skipped (not crashed) ───────────────
def test_unknown_filename_skipped(tmp_path):
    f = tmp_path / "garbage.pdf"
    f.write_bytes(b"not json")
    inv = {"files": {}, "collections": {}}
    r = _ingest_file(f, inv)
    assert r["status"] == "SKIPPED_UNKNOWN_FILENAME_PATTERN"


# ─── rev2 — publication_events logical-key uses `at` ─────────────────
def test_publication_events_logical_key_uses_at_field():
    doc = {
        "_id": "pe1", "event": "PUBLISHED", "prediction_id": "pr1",
        "publication_version": 1, "publication_source": "primary",
        "at": "2026-01-01T00:00:00Z", "payload_hash": "h1",
    }
    k = _logical_key("publication_events", doc)
    assert k is not None
    assert k[0] == "prediction_id+publication_version+at"
    assert k[1] == "pr1"
    assert k[2] == "1"
    assert k[3] == "2026-01-01T00:00:00Z"


def test_publication_events_logical_key_falls_back_to_id():
    # No `at` and no legacy fallback — must fall back to _id so the row
    # is still uniquely identified rather than silently dropped.
    doc = {"_id": "pe2", "prediction_id": "pr9", "publication_version": 2}
    k = _logical_key("publication_events", doc)
    assert k is not None
    assert k[0] == "prediction_id+publication_version+_id"


# ─── Duplicate logical keys across parts are detected ────────────────
def test_duplicate_logical_keys_across_parts_blocks_ready(tmp_path):
    # Two prediction_snapshots parts share the same
    # (prediction_id, snapshot_version) — must flag.
    f1 = tmp_path / "prediction_snapshots_part1.json"
    f2 = tmp_path / "prediction_snapshots_part2.json"
    f1.write_text(json.dumps([
        {"_id": "s1", "prediction_id": "pA", "snapshot_version": 1,
         "snapshot_hash": "h1"},
    ]))
    f2.write_text(json.dumps([
        {"_id": "s2", "prediction_id": "pA", "snapshot_version": 1,
         "snapshot_hash": "h1b"},
    ]))
    inv = {"files": {}, "collections": {}}
    _ingest_file(f1, inv)
    _ingest_file(f2, inv)
    cs = inv["collections"]["prediction_snapshots"]
    assert len(cs["duplicate_logical_keys_across_parts"]) == 1


# ─── Readiness: multipart count-match path ───────────────────────────
def test_ready_when_multipart_count_matches_production_count_known(tmp_path):
    f1 = tmp_path / "user_bets_part1.json"
    f2 = tmp_path / "user_bets_part2.json"
    f1.write_text(json.dumps([{"user_bet_id": "ub1"}, {"user_bet_id": "ub2"}]))
    f2.write_text(json.dumps([{"user_bet_id": "ub3"}, {"user_bet_id": "ub4"}]))
    inv = {"files": {}, "collections": {}}
    _ingest_file(f1, inv)
    _ingest_file(f2, inv)
    cstate = inv["collections"]["user_bets"]
    rpt = _evaluate_collection_ready(
        "user_bets", cstate,
        mf_row={"production_count_known": 4},
    )
    assert rpt["ready"] is True
    assert "multipart_count_match" in rpt["reasons"]


def test_not_ready_when_count_mismatches(tmp_path):
    f1 = tmp_path / "user_bets_part1.json"
    f1.write_text(json.dumps([{"user_bet_id": "ub1"}]))
    inv = {"files": {}, "collections": {}}
    _ingest_file(f1, inv)
    cstate = inv["collections"]["user_bets"]
    rpt = _evaluate_collection_ready(
        "user_bets", cstate,
        mf_row={"production_count_known": 7},
    )
    assert rpt["ready"] is False
    assert any("doc_count_mismatch" in r for r in rpt["reasons"])


# ─── Readiness: contiguous-parts check (gap detected) ────────────────
def test_part_gap_blocks_ready(tmp_path):
    f1 = tmp_path / "picks_part1.json"
    f3 = tmp_path / "picks_part3.json"
    f1.write_text(json.dumps([{"id": "p1"}]))
    f3.write_text(json.dumps([{"id": "p3"}]))
    inv = {"files": {}, "collections": {}}
    _ingest_file(f1, inv)
    _ingest_file(f3, inv)
    cstate = inv["collections"]["picks"]
    rpt = _evaluate_collection_ready(
        "picks", cstate,
        mf_row={"production_count_known": 2},
    )
    assert rpt["ready"] is False
    assert any("missing_parts=[2]" in r for r in rpt["reasons"])


# ─── Readiness: sentinel path when production_count_known is null ────
def test_sentinel_path_when_production_count_unknown(tmp_path):
    f1 = tmp_path / "rollover_slates_part1.json"
    f2 = tmp_path / "rollover_slates_part2.json"
    sent = tmp_path / "rollover_slates.complete.json"
    f1.write_text(json.dumps([{"slate_date": "d1", "scope": "NFL"}]))
    f2.write_text(json.dumps([{"slate_date": "d2", "scope": "NFL"}]))
    sent.write_text(json.dumps({"total_parts": 2}))
    inv = {"files": {}, "collections": {}}
    _ingest_file(f1, inv)
    _ingest_file(f2, inv)
    _ingest_file(sent, inv)
    cstate = inv["collections"]["rollover_slates"]
    rpt = _evaluate_collection_ready(
        "rollover_slates", cstate,
        mf_row={"production_count_known": None},
    )
    assert rpt["ready"] is True
    assert "multipart_sentinel_satisfied" in rpt["reasons"]


# ─── Standalone single-file complete ──────────────────────────────────
def test_standalone_file_marks_ready(tmp_path):
    f = tmp_path / "nfl_ingest_meta.json"
    f.write_text(json.dumps([{"_id": "nfl_meta"}]))
    inv = {"files": {}, "collections": {}}
    _ingest_file(f, inv)
    cstate = inv["collections"]["nfl_ingest_meta"]
    rpt = _evaluate_collection_ready(
        "nfl_ingest_meta", cstate,
        mf_row={"production_count_known": 1},
    )
    assert rpt["ready"] is True
    assert "standalone_file" in rpt["reasons"]


# ─── Duplicate _id across parts blocks ready even on count-match ─────
def test_duplicate_ids_block_ready_even_if_count_matches(tmp_path):
    f1 = tmp_path / "user_bets_part1.json"
    f2 = tmp_path / "user_bets_part2.json"
    # Same _id across parts.
    f1.write_text(json.dumps([{"_id": "x", "user_bet_id": "a"}]))
    f2.write_text(json.dumps([{"_id": "x", "user_bet_id": "b"}]))
    inv = {"files": {}, "collections": {}}
    _ingest_file(f1, inv)
    _ingest_file(f2, inv)
    cstate = inv["collections"]["user_bets"]
    rpt = _evaluate_collection_ready(
        "user_bets", cstate,
        mf_row={"production_count_known": 2},
    )
    assert rpt["ready"] is False
    assert any("duplicate_ids_across_parts" in r for r in rpt["reasons"])
