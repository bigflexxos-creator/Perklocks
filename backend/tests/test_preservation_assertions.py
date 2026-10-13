"""Block 58 — Preservation Assertions.

Verifies the master constants that the global preservation contract
forbids us from touching.  Checks:
  * 85+ threshold unchanged (snapshot sanity)
  * APEX constants unchanged
  * NHL 5-game floors present
  * Working Soccer game-market importable
  * NFL alt engine importable
  * Canonical publication boundary SYNTHETIC_BOOK_ODDS rejection intact
"""
from __future__ import annotations

import pytest

from services.preservation_contract import snapshot


def test_apex_thresholds_preserved():
    snap = snapshot()
    assert snap.get("APEX_MIN_BASE_SCORE") == 97.0
    assert snap.get("APEX_MIN_POSITIVE_CATEGORIES") == 5


def test_apex_eligible_sports_unchanged():
    snap = snapshot()
    assert snap.get("APEX_ELIGIBLE_SPORTS") == sorted(
        ["MLB", "Soccer", "NBA", "Tennis", "NFL", "CFB", "NHL"]
    )


def test_apex_unavailable_sports_unchanged():
    snap = snapshot()
    assert snap.get("APEX_UNAVAILABLE_SPORTS") == sorted(
        ["UFC", "MMA", "KBO"]
    )


def test_nhl_evidence_floors_preserved():
    snap = snapshot()
    # Either the explicit constants or the source-marker check must
    # evidence a 5-game floor.
    floor_t = snap.get("NHL_MIN_TEAM_RECENT_GAMES")
    floor_p = snap.get("NHL_MIN_PLAYER_RECENT_GAMES")
    if floor_t is not None or floor_p is not None:
        assert floor_t == 5
        assert floor_p == 5
    else:
        assert snap.get("NHL_floor_marker_present") is True


def test_alt_line_engine_importable():
    snap = snapshot()
    assert snap.get("ALT_LINE_ENGINE_IMPORTABLE") is True


def test_soccer_market_gate_importable():
    snap = snapshot()
    assert snap.get("SOCCER_MARKET_GATE_IMPORTABLE") is True


def test_canonical_publication_boundary_rejection_reasons_intact():
    snap = snapshot()
    reasons = snap.get("RejectionReason_members") or []
    for required in ("SYNTHETIC_BOOK_ODDS", "MODEL_LINE_NOT_REAL_OFFERING",
                     "NO_REAL_LINE_WITH_ODDS"):
        assert required in reasons, (
            f"{required} missing from RejectionReason enum")


def test_provider_median_banned_from_real_odds_sources():
    snap = snapshot()
    real = snap.get("_REAL_ODDS_SOURCES") or []
    synthetic = snap.get("_SYNTHETIC_ODDS_SOURCES") or []
    assert "provider_median" in synthetic
    assert "provider_median" not in real
    assert "median" not in real
    assert "consensus" not in real


def test_apex_gate_version_present():
    snap = snapshot()
    assert isinstance(snap.get("APEX_GATE_VERSION"), str)
    assert "apex_gate" in snap["APEX_GATE_VERSION"]


if __name__ == "__main__":
    import subprocess, sys
    sys.exit(subprocess.call(["python", "-m", "pytest", __file__, "-v"]))
