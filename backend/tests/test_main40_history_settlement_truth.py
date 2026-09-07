"""Item 2 — History / Settlement Truth surgical closure.

Verifies:
  1. Historical eligibility ignores mutable current-board fields
     (`off_board`, `no_bet`).  A pick that legitimately made the
     board and later flipped off_board still classifies as
     PROVEN_PUBLISHED.
  2. `unresolved` is a first-class settlement result — provider
     failures / missing data map to it (not to VOID).
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from services.published_results_truth import (
    classify_publication, canonical_query,
    CANONICAL_PUBLICATION_LOCK_FLOOR,
)
from services.settlement_service import (
    VALID_RESULTS, _pick_status_from_result,
)


def test_published_pick_now_off_board_still_proven_published():
    pick = {
        "lock_score": 92,
        "on_main_board_at": "2026-06-01T12:00:00+00:00",
        "published_at":     "2026-06-01T12:00:00+00:00",
        "off_board":        True,           # ← flipped after publication
        "status":           "pending",
    }
    assert classify_publication(pick) == "PROVEN_PUBLISHED"


def test_published_pick_now_no_bet_still_proven_published():
    pick = {
        "published_lock_score": 88,
        "on_main_board_at":     "2026-06-01T12:00:00+00:00",
        "no_bet":               True,       # ← flipped after publication
        "status":                "won",
    }
    assert classify_publication(pick) == "PROVEN_PUBLISHED"


def test_canonical_query_does_not_filter_off_board_or_no_bet():
    q = canonical_query(days=30)
    payload = str(q)
    # Historical eligibility no longer uses these mutable fields.
    assert '"off_board"' not in payload
    assert "'off_board'" not in payload
    assert '"no_bet"' not in payload
    assert "'no_bet'" not in payload
    # Explicit exclusion flags are still enforced.
    assert '"hide_from_main_board"' in payload or "'hide_from_main_board'" in payload
    assert '"excluded_from_history"' in payload or "'excluded_from_history'" in payload


def test_unresolved_is_valid_settlement_result():
    assert "unresolved" in VALID_RESULTS


def test_unresolved_maps_to_unresolved_status_not_void():
    """Provider-missing terminator must NOT contaminate VOID
    outcomes — `unresolved` is its own terminal status."""
    assert _pick_status_from_result("unresolved") == "unresolved"
    # And VOID stays VOID (unchanged), for real book VOIDs.
    assert _pick_status_from_result("void") == "void"
    assert _pick_status_from_result("cancelled") == "void"


def test_settlement_engine_uses_unresolved_for_14d_terminator():
    """Load settlement_engine module text and confirm the stale
    terminator emits `unresolved`, not `void`.
    """
    import services  # noqa
    import importlib.util
    path = os.path.join(ROOT, "settlement_engine.py")
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    # The stale-14d block must call SettlementService with the
    # unresolved result now.
    assert 'result="unresolved"' in src, "14d terminator still emits VOID"
    assert 'settlement_engine:unresolved_stale_14d' in src


if __name__ == "__main__":
    test_published_pick_now_off_board_still_proven_published()
    test_published_pick_now_no_bet_still_proven_published()
    test_canonical_query_does_not_filter_off_board_or_no_bet()
    test_unresolved_is_valid_settlement_result()
    test_unresolved_maps_to_unresolved_status_not_void()
    test_settlement_engine_uses_unresolved_for_14d_terminator()
    print("OK — history/settlement truth Item #2 verified.")
