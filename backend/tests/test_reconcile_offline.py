"""Guard tests for the offline reconciliation tool.

Confirms the live-DB refusal path + classification + logical-key
coverage.  Zero DB access — pure unit tests.
"""
from __future__ import annotations

import os
import pathlib
import tempfile
import zipfile
import pytest

from scripts.reconcile_offline import (
    _refuse_live_input,
    _logical_key,
    CLASSIFICATION,
    BackupSource,
)


# ─── Live-DB input refusal ────────────────────────────────────────────
def test_rejects_production_mongodb_uri():
    with pytest.raises(SystemExit):
        _refuse_live_input("mongodb+srv://user:pw@prod.example.net/", "x")


def test_rejects_preview_localhost_uri():
    with pytest.raises(SystemExit):
        _refuse_live_input("mongodb://localhost:27017", "x")


def test_rejects_127_0_0_1():
    with pytest.raises(SystemExit):
        _refuse_live_input("mongodb://127.0.0.1:27017", "x")


def test_accepts_file_path():
    # Must NOT raise for a plain filesystem path.
    _refuse_live_input("/tmp/backup.zip", "x")
    _refuse_live_input("./dump", "x")


# ─── Missing / malformed backup ───────────────────────────────────────
def test_missing_backup_fails_closed():
    with pytest.raises(SystemExit):
        BackupSource("/tmp/definitely-not-there-12345.zip", "preview")


def test_malformed_zip_fails_closed(tmp_path):
    p = tmp_path / "bad.zip"
    p.write_bytes(b"this is not a zip")
    with pytest.raises(SystemExit):
        BackupSource(str(p), "preview")


# ─── Classification table ─────────────────────────────────────────────
def test_core_must_reconcile_collections_class_A():
    for c in ("picks", "prediction_snapshots", "publication_events",
              "pregame_snapshots", "settlement_events",
              "player_game_actuals", "team_game_actuals",
              "player_game_logs", "nfl_player_weekly", "games",
              "soccer_matches", "soccer_player_game_logs",
              "tennis_matches_history", "users", "user_bets",
              "rollover_slates", "rollover_slate_events",
              "parlay_history", "player_identities",
              "historical_ingestion_state", "nfl_ingest_meta"):
        assert CLASSIFICATION[c] == "A", c


def test_rebuildable_collections_class_B():
    for c in ("live_alt_lines", "odds_api_cache", "publication_mismatch_report",
              "production_truth_observations"):
        assert CLASSIFICATION[c] == "B", c


def test_environment_specific_collections_class_C():
    for c in ("canonical_worker_leases", "scheduled_jobs",
              "provider_budget_state", "provider_request_intents",
              "board_generations"):
        assert CLASSIFICATION[c] == "C", c


def test_team_identities_review_required():
    assert CLASSIFICATION["team_identities"] == "D"


# ─── Logical key resolution ──────────────────────────────────────────
def test_picks_key_prefers_id():
    assert _logical_key("picks", {"id": "abc"}) == ("id", "abc")
    assert _logical_key("picks", {}) is None


def test_prediction_snapshots_key_requires_version():
    assert _logical_key("prediction_snapshots",
                        {"prediction_id": "p1"}) is None
    assert _logical_key("prediction_snapshots",
                        {"prediction_id": "p1", "snapshot_version": 1})[0] \
        == "prediction_id+snapshot_version"


def test_pregame_snapshot_hash_key():
    assert _logical_key("pregame_snapshots",
                        {"snapshot_hash": "deadbeef"})[0] == "snapshot_hash"


def test_player_game_actuals_composite_key():
    k = _logical_key("player_game_actuals", {
        "sport": "nhl",
        "canonical_event_id": "nhl_g1",
        "canonical_player_id": "nhl_x",
        "market": "goals",
    })
    assert k[0] == "sport+player+event+market"
    assert k[1] == "nhl"


def test_team_game_actuals_requires_team_and_event():
    k = _logical_key("team_game_actuals",
                     {"sport": "nhl", "canonical_team_id": "nhl_t_edm",
                      "event_id": "g1"})
    assert k[0] == "sport+team+event"


def test_users_key_email_lower():
    k = _logical_key("users", {"email": "A@EXAMPLE.COM"})
    assert k == ("email", "a@example.com")


def test_nfl_player_weekly_player_season_week():
    k = _logical_key("nfl_player_weekly",
                     {"player_id": "p1", "season": 2026, "week": 5})
    assert k[0] == "player+season+week"


def test_unknown_collection_returns_none():
    assert _logical_key("totally_made_up_collection", {"_id": "x"}) is None


# ─── Script path produces reports for Preview-only mode ─────────────
def test_preview_only_mode_integration(tmp_path, monkeypatch):
    """End-to-end: run the tool in Preview-only mode using a tiny
    hand-rolled backup zip to prove the extraction + inventory path."""
    import gzip
    import bson
    db = tmp_path / "dump" / "lockscore_db"
    db.mkdir(parents=True)
    # one minimal collection: picks with 2 docs.
    with gzip.open(db / "picks.bson.gz", "wb") as f:
        f.write(bson.encode({"id": "p1", "lock_score": 85}))
        f.write(bson.encode({"id": "p2", "lock_score": 92}))
    zpath = tmp_path / "mini.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for root, _, files in os.walk(tmp_path):
            for fn in files:
                if fn.endswith(".zip"):
                    continue
                p = pathlib.Path(root) / fn
                zf.write(p, p.relative_to(tmp_path))
    bs = BackupSource(str(zpath), "preview", "lockscore_db")
    assert bs.collections() == ["picks"]
    docs = list(bs.stream_docs("picks"))
    assert len(docs) == 2
    assert {d["id"] for d in docs} == {"p1", "p2"}
    bs.close()
