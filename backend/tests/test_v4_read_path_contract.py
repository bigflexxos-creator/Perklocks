"""
v4 READ-PATH CONTRACT — CANONICAL PUBLICATION AUTHORITY
========================================================
(2026-06-14)

Locks in the surgical read-path fix that prevents `hydrate()` from
overwriting a legitimate persisted v4 ``lock_score`` with a stale v3
``published_lock_score`` frozen snapshot.

Root cause: For a v4-authoritative pick, `hydrate()` unconditionally
copied `published_lock_score` → `lock_score` and
`published_probability` → `win_probability`, silently mutating v4
authority back to the frozen v3 snapshot.

Fix: `hydrate()` now branches on `lock_score_version ==
"v4.confidence_first.2026-06-14"`; when present it (a) SKIPS the
snapshot→legacy overwrite of scoring-authority fields, and (b)
REFRESHES the published_* fields from persisted v4 truth so
downstream contract consumers see coherent data.

Run:
    cd /app/backend && python -m pytest tests/test_v4_read_path_contract.py -v
"""
from __future__ import annotations

import sys
sys.path.insert(0, "/app/backend")

from services.published_prediction_reader import hydrate

V4 = "v4.confidence_first.2026-06-14"


def _v4_row(*, lock_score, win_prob, edge, published_lock_score,
            published_probability, published_edge):
    """Build a fake DB row simulating a v4-authoritative pick whose
    frozen v3 published_* snapshot is now STALE (LS 64.5 v4 vs the
    old published 99.0 v3)."""
    return {
        "id": "test-v4-authority",
        "sport": "MLB", "market": "Test Prop",
        "selection": "William Contreras",
        "lock_score": lock_score,                    # v4 authoritative
        "win_probability": win_prob,                 # v4 authoritative
        "edge_percent": edge,                        # v4 authoritative
        "lock_score_version": V4,                    # ← flag
        "calibrated_win_probability": round(win_prob / 100.0, 4),
        "grade": "Pass",                             # will be recalculated
        "book_odds": -125,
        "published_lock_score": published_lock_score,  # STALE v3 snapshot
        "published_probability": published_probability,
        "published_edge": published_edge,
        "published_grade": "Elite Lock",
    }


# ═════════════════════════════════════════════════════════════
# P0-C · v4 score survives hydrate unchanged
# ═════════════════════════════════════════════════════════════
class TestV4LockScoreSurvivesHydrate:
    def test_v4_lock_score_not_overwritten_by_stale_published(self):
        """The exact Contreras regression: v4 LS 64.5 must not be
        replaced by stale v3 published_lock_score 99.0."""
        row = _v4_row(lock_score=64.5, win_prob=64.9, edge=9.3,
                      published_lock_score=99.0,
                      published_probability=0.6727,
                      published_edge=11.67)
        hyd = hydrate(row)
        assert hyd["lock_score"] == 64.5, (
            f"v4 lock_score corrupted: {hyd['lock_score']} (expected 64.5)"
        )
        assert hyd["win_probability"] == 64.9
        assert hyd["edge_percent"] == 9.3

    def test_v4_published_fields_refreshed(self):
        """Hydrate must refresh published_* aliases so downstream
        canonical contract consumers see v4 truth, not stale v3."""
        row = _v4_row(lock_score=64.5, win_prob=64.9, edge=9.3,
                      published_lock_score=99.0,
                      published_probability=0.6727,
                      published_edge=11.67)
        hyd = hydrate(row)
        assert hyd["published_lock_score"] == 64.5
        assert abs(hyd["published_probability"] - 0.649) < 1e-3
        assert hyd["published_edge"] == 9.3

    def test_v4_provenance_marker_present(self):
        row = _v4_row(lock_score=64.5, win_prob=64.9, edge=9.3,
                      published_lock_score=99.0,
                      published_probability=0.6727,
                      published_edge=11.67)
        hyd = hydrate(row)
        assert hyd.get("_v4_read_authority") == V4
        assert hyd.get("_prediction_source") == "snapshot_v4_authoritative"


# ═════════════════════════════════════════════════════════════
# P0-C · Legacy (non-v4) path preserved
# ═════════════════════════════════════════════════════════════
class TestLegacyPathUnchanged:
    def test_v3_pick_still_uses_published_snapshot(self):
        """Pre-v4 rows (no version stamp) must continue to hydrate
        from the frozen snapshot — behavior unchanged for legacy."""
        row = {
            "id": "test-v3-legacy",
            "sport": "MLB", "selection": "Legacy Pick",
            "lock_score": 55.0,           # legacy value
            "win_probability": 55.0,
            "edge_percent": 3.0,
            # No lock_score_version stamp
            "book_odds": -110,
            "published_lock_score": 92.0,   # snapshot
            "published_probability": 0.63,
            "published_edge": 5.5,
            "published_grade": "Lock",
        }
        hyd = hydrate(row)
        # Legacy behavior: published_lock_score → lock_score
        assert hyd["lock_score"] == 92.0
        assert hyd["win_probability"] == 63.0    # 0.63 * 100
        assert hyd["edge_percent"] == 5.5
        assert hyd.get("_v4_read_authority") is None

    def test_no_snapshot_row_passes_through(self):
        row = {"id": "no-snap", "lock_score": 78.0,
               "win_probability": 60.0, "edge_percent": 2.5,
               "sport": "MLB", "book_odds": -140}
        hyd = hydrate(row)
        assert hyd["lock_score"] == 78.0
        assert hyd.get("_prediction_source") == "legacy_unpublished"


# ═════════════════════════════════════════════════════════════
# P0-C · Grade coherence follows v4 lock_score
# ═════════════════════════════════════════════════════════════
class TestGradeCoherentWithV4Score:
    def test_grade_matches_v4_lock_score_band(self):
        """A v4 LS = 64.5 pick must render grade = 'Pass', not
        'Elite Lock' (which was the frozen v3 grade at LS 99)."""
        row = _v4_row(lock_score=64.5, win_prob=64.9, edge=9.3,
                      published_lock_score=99.0,
                      published_probability=0.6727,
                      published_edge=11.67)
        hyd = hydrate(row)
        # Canonical grade band for 64.5 = Pass
        assert hyd["grade"] == "Pass", (
            f"grade not coherent with v4 score: {hyd['grade']}"
        )


# ═════════════════════════════════════════════════════════════
# P0-H · Self-heal safety on v4 rows
# ═════════════════════════════════════════════════════════════
class TestSelfHealDoesNotMutateV4:
    def test_hydrate_never_mutates_input(self):
        """`hydrate()` returns a copy; the input dict must be
        untouched.  Guards against accidental read-time DB write-
        back through a shared reference."""
        row = _v4_row(lock_score=64.5, win_prob=64.9, edge=9.3,
                      published_lock_score=99.0,
                      published_probability=0.6727,
                      published_edge=11.67)
        pre = dict(row)
        _ = hydrate(row)
        for k, v in pre.items():
            assert row[k] == v, f"input mutated at key={k}"


# ═════════════════════════════════════════════════════════════
# Contreras / Yordan / Yohandy regression proof
# ═════════════════════════════════════════════════════════════
class TestKnownRegressionCases:
    def test_contreras_wire_matches_persisted_v4(self):
        """Named smoking-gun case: DB v4 LS 64.5 must survive to wire."""
        row = _v4_row(lock_score=64.5, win_prob=64.9, edge=9.3,
                      published_lock_score=99.0,
                      published_probability=0.6727,
                      published_edge=11.67)
        row["selection"] = "William Contreras"
        hyd = hydrate(row)
        assert hyd["lock_score"] == 64.5

    def test_yordan_alvarez_wire_matches_persisted_v4(self):
        row = _v4_row(lock_score=67.1, win_prob=79.9, edge=11.1,
                      published_lock_score=98.0,
                      published_probability=0.799,
                      published_edge=11.1)
        row["selection"] = "Yordan Alvarez"
        hyd = hydrate(row)
        assert hyd["lock_score"] == 67.1

    def test_yohandy_morales_wire_matches_persisted_v4(self):
        row = _v4_row(lock_score=67.4, win_prob=74.5, edge=17.6,
                      published_lock_score=98.0,
                      published_probability=0.745,
                      published_edge=17.6)
        row["selection"] = "Yohandy Morales"
        hyd = hydrate(row)
        assert hyd["lock_score"] == 67.4


if __name__ == "__main__":
    import subprocess as _sp
    _r = _sp.run(
        [sys.executable, "-m", "pytest", __file__, "-v", "--tb=short"],
        cwd="/app/backend",
    )
    sys.exit(_r.returncode)
