"""
P0.3 READ-PATH CONTRACT — IMMUTABLE PUBLICATION WINS
====================================================
(FINAL UNIVERSAL ROOT CLOSURE)

Supersedes the 2026-06-14 "v4 read authority" contract, which let mutable
top-level fields (lock_score / win_probability / edge / grade) override the
frozen ``published_*`` snapshot at read time.  That produced the observed
Locks 89 / Pick Breakdown 85 split.

New contract:
  * If a pick carries a publication snapshot, ``hydrate()`` aliases legacy
    fields FROM the snapshot — always.  A v4 stamp does not change that.
  * Legitimate re-scores go through ``PredictionPublicationService.publish``
    which produces a NEW monotone ``snapshot_version`` (exposed on the wire
    as ``publication_version``) with the grade derived from the canonical
    mapping over the published Lock Score.
  * ``hydrate()`` never mutates its input.

Run:
    cd /app/backend && python -m pytest tests/test_v4_read_path_contract.py -v
"""
from __future__ import annotations

import sys
sys.path.insert(0, "/app/backend")

from services.published_prediction_reader import hydrate

V4 = "v4.confidence_first.2026-06-14"


def _row(*, lock_score, win_prob, edge, grade, published_lock_score,
         published_probability, published_edge, published_grade, version=1):
    return {
        "id": "test-pick",
        "lock_score": lock_score,
        "lock_score_version": V4,
        "win_probability": win_prob,
        "edge_percent": edge,
        "grade": grade,
        "published_lock_score": published_lock_score,
        "published_probability": published_probability,
        "published_edge": published_edge,
        "published_grade": published_grade,
        "snapshot_version": version,
        "book_odds": -320,
    }


class TestSnapshotAlwaysWins:
    def test_mutated_top_level_never_overrides_snapshot(self):
        # Detail-side rescoring mutated top-level to 82.7 / Pass; the
        # frozen publication says 88.9 / Playable.  Snapshot wins.
        row = _row(lock_score=82.7, win_prob=82.17, edge=-1.16, grade="Pass",
                   published_lock_score=88.9, published_probability=0.8132,
                   published_edge=-2.013, published_grade="Playable")
        out = hydrate(row)
        assert out["lock_score"] == 88.9
        assert out["win_probability"] == 81.32
        assert out["edge_percent"] == -2.013
        assert out["grade"] == "Playable"

    def test_v4_stamp_does_not_create_read_authority(self):
        row = _row(lock_score=64.5, win_prob=55.0, edge=1.0, grade="Pass",
                   published_lock_score=91.0, published_probability=0.71,
                   published_edge=4.2, published_grade="Lock")
        out = hydrate(row)
        assert out["lock_score"] == 91.0
        assert out["_prediction_source"] == "snapshot"
        assert "_v4_read_authority" not in out

    def test_publication_version_exposed(self):
        row = _row(lock_score=90, win_prob=70, edge=2, grade="Lock",
                   published_lock_score=90, published_probability=0.70,
                   published_edge=2, published_grade="Lock", version=3)
        out = hydrate(row)
        assert out["publication_version"] == 3


class TestGradeInvariant:
    def test_grade_follows_published_lock_score_band(self):
        row = _row(lock_score=85.0, win_prob=77.97, edge=1.78, grade="Pass",
                   published_lock_score=89.0, published_probability=0.7911,
                   published_edge=2.92, published_grade="Pass")
        out = hydrate(row)
        # 89 maps to Playable in the canonical mapping — never PASS.
        assert out["grade"] == "Playable"
        assert out["_grade_repaired_from"] == "Pass"


class TestLegacyPathUnchanged:
    def test_no_snapshot_row_passes_through(self):
        row = {"id": "x", "lock_score": 77.0, "win_probability": 60.0,
               "edge_percent": 1.0, "grade": "Pass"}
        out = hydrate(row)
        assert out["lock_score"] == 77.0
        assert out["_prediction_source"] == "legacy_unpublished"
        assert "publication_version" not in out


class TestHydrateIsPure:
    def test_hydrate_never_mutates_input(self):
        row = _row(lock_score=82.7, win_prob=82.17, edge=-1.16, grade="Pass",
                   published_lock_score=88.9, published_probability=0.8132,
                   published_edge=-2.013, published_grade="Playable")
        snapshot = dict(row)
        hydrate(row)
        assert row == snapshot


class TestPublicationGradeDerivation:
    def test_build_payload_derives_grade_from_lock_score(self):
        from services.prediction_publication_service import PredictionPublicationService
        svc = PredictionPublicationService(db=None, board_version="test")
        payload = svc._build_payload(
            {"id": "p1", "lock_score": 89.0, "win_probability": 79.11,
             "edge_percent": 2.92, "grade": "Pass", "book_odds": -320},
            "test",
        )
        assert payload.published_lock_score == 89.0
        assert payload.published_grade == "Playable"
