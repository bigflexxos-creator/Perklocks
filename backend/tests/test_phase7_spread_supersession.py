"""Phase 7 defect A closure — Spread contradiction/supersession.

Regression fixture: the exact Preview screenshot pair
(Akron @ Wake Forest, spread ±24.5) that caused board flapping.
"""
from __future__ import annotations
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest
from services.spread_truth_guard import (
    enforce_single_active_spread,
    _canonical_spread_key,
)


def _wake_minus():
    return {
        "id": "wake_-24.5",
        "sport": "CFB",
        "event_id": "cfb_akron_wake_20260902",
        "market": "Spread",
        "market_family": "spread",
        "selection": "Wake Forest Demon Deacons",
        "side": "Wake Forest -24.5",
        "line": -24.5,
        "book_odds": -110,
        "lock_score": 92.0,
        "win_probability": 56.52,
        "edge_percent": 6.52,
        "model_probability": 0.5652,
    }


def _akron_plus():
    return {
        "id": "akron_+24.5",
        "sport": "CFB",
        "event_id": "cfb_akron_wake_20260902",
        "market": "Spread",
        "market_family": "spread",
        "selection": "Akron Zips",
        "side": "Akron +24.5",
        "line": 24.5,
        "book_odds": -115,
        "lock_score": 92.0,
        "win_probability": 54.7,
        "edge_percent": 3.6,
        "model_probability": 0.547,
    }


def test_wake_and_akron_share_canonical_spread_key():
    """Wake -24.5 and Akron +24.5 are the SAME canonical wager
    (opposite sides of ONE spread market).  Side-neutral key on
    abs(line) collapses them."""
    k1 = _canonical_spread_key(_wake_minus())
    k2 = _canonical_spread_key(_akron_plus())
    assert k1 == k2
    assert "24.5" in k1


def test_different_absolute_lines_are_distinct_wagers():
    p1 = _wake_minus()
    p2 = _wake_minus() | {"id": "wake_-27.5", "line": -27.5}
    assert _canonical_spread_key(p1) != _canonical_spread_key(p2)


def test_spread_supersession_deterministic_wake_wins():
    """Wake Forest has higher edge (+6.52 vs +3.6) → Wake wins."""
    picks = [_wake_minus(), _akron_plus()]
    stats = enforce_single_active_spread(picks)
    assert stats["superseded"] == 1
    winners = [p for p in picks if p["revision_state"] == "ACTIVE"]
    losers = [p for p in picks if p["revision_state"] == "SUPERSEDED_IN_RUN"]
    assert len(winners) == 1 and winners[0]["id"] == "wake_-24.5"
    assert len(losers) == 1 and losers[0]["id"] == "akron_+24.5"
    assert losers[0]["off_board"] is True
    assert "SPREAD_SIDE_CONFLICT" in losers[0]["off_board_reasons"]
    assert losers[0]["superseded_by_pick_id"] == "wake_-24.5"


def test_spread_supersession_stable_across_input_order():
    """Reversing the input order MUST NOT change the winner —
    proves the deterministic sort key eliminates board flapping."""
    picks_fwd = [_wake_minus(), _akron_plus()]
    picks_rev = [_akron_plus(), _wake_minus()]
    enforce_single_active_spread(picks_fwd)
    enforce_single_active_spread(picks_rev)
    fwd_winner = [p for p in picks_fwd if p["revision_state"] == "ACTIVE"][0]
    rev_winner = [p for p in picks_rev if p["revision_state"] == "ACTIVE"][0]
    assert fwd_winner["id"] == rev_winner["id"] == "wake_-24.5"


def test_spread_supersession_tiebreak_by_pick_id():
    """When edge/mp/lock are identical, deterministic tiebreak is
    lexicographic pick_id ASC."""
    a = _wake_minus() | {"id": "aaa"}
    b = _akron_plus() | {"id": "bbb", "edge_percent": 6.52,
                          "model_probability": 0.5652,
                          "lock_score": 92.0}
    picks = [b, a]   # bbb first
    enforce_single_active_spread(picks)
    winner = [p for p in picks if p["revision_state"] == "ACTIVE"][0]
    assert winner["id"] == "aaa"


def test_non_spread_markets_untouched():
    """Total markets do not share the spread supersession key."""
    total_pick = {"id": "t1", "sport": "MLB", "event_id": "e1",
                  "market": "Total Runs Over 8.5", "line": 8.5,
                  "selection": "Over"}
    assert _canonical_spread_key(total_pick) is None


def test_mlb_run_line_treated_as_spread_family():
    """MLB Run Line +1.5 / -1.5 must also collapse via abs(line)."""
    p1 = {"id": "yanks_-1.5", "sport": "MLB", "event_id": "mlb_yanks_sox",
          "market": "Run Line", "line": -1.5,
          "selection": "Yankees", "lock_score": 88, "edge_percent": 4.0}
    p2 = {"id": "sox_+1.5", "sport": "MLB", "event_id": "mlb_yanks_sox",
          "market": "Run Line", "line": 1.5,
          "selection": "Red Sox", "lock_score": 87, "edge_percent": 2.0}
    picks = [p1, p2]
    stats = enforce_single_active_spread(picks)
    assert stats["superseded"] == 1
    # Yankees has higher edge — wins
    active = [p for p in picks if p["revision_state"] == "ACTIVE"]
    assert active[0]["id"] == "yanks_-1.5"


def test_side_aware_wager_identity_still_preserved():
    """Phase 4 identity: Wake -24.5 and Akron +24.5 remain DISTINCT
    observed wagers via `canonical_wager_identity` (side-aware).
    The supersession happens at the guard layer AFTER both rows
    are stored as distinct observed wagers."""
    from services.pick_identity_enricher import canonical_wager_identity
    w = _wake_minus() | {"market_family": "spread"}
    a = _akron_plus() | {"market_family": "spread"}
    assert canonical_wager_identity(w) != canonical_wager_identity(a)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
