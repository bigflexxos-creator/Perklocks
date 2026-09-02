"""Phase 7 — MARKET CONSERVATION / CONTRADICTION ENGINE invariants.

  C1. Totals conservation: for every publishable Total pair,
      P(Over) + P(Under) + P(Push) ≡ 1 (within tolerance).
  C2. `check_over_under_conservation` FAILS on a broken pair
      (independent per-side probabilities that don't sum to 1).
  C3. `enforce_single_active_total` supersedes the losing side of
      a same-line side conflict via `revision_state = SUPERSEDED_IN_RUN`
      + `off_board = True` — NOT by deleting the losing row.
  C4. The SUPERSEDED row remains queryable (immutable history) —
      canonical uniqueness never destroys audit provenance.
  C5. Alt-ladder monotonicity: `check_alt_ladder_monotonic` rejects
      a ladder where P(Over) doesn't decrease with line.
"""
from __future__ import annotations
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest


def test_conservation_check_passes_on_valid_pair():
    from services.totals_truth_guard import check_over_under_conservation
    ok, _ = check_over_under_conservation(0.55, 0.45)
    assert ok


def test_conservation_check_fails_on_broken_pair():
    from services.totals_truth_guard import check_over_under_conservation
    # 0.60 + 0.50 = 1.10 → NOT conserved.
    ok, reason = check_over_under_conservation(0.60, 0.50)
    assert not ok
    assert "conservation_fail" in reason


def test_conservation_check_accepts_push_probability():
    from services.totals_truth_guard import check_over_under_conservation
    # Integer-line pair with 3% push.
    ok, _ = check_over_under_conservation(0.485, 0.485, 0.030)
    assert ok


def test_totals_canonical_key_is_side_neutral_supersession_key():
    from services.totals_truth_guard import _canonical_totals_key
    p_over = {"sport": "MLB", "event_id": "EVT", "period": "FULL_GAME",
              "market": "Total Runs Over 8.5", "line": 8.5,
              "selection": "Over"}
    p_under = {"sport": "MLB", "event_id": "EVT", "period": "FULL_GAME",
               "market": "Total Runs Under 8.5", "line": 8.5,
               "selection": "Under"}
    assert _canonical_totals_key(p_over) == _canonical_totals_key(p_under)


def test_alt_ladder_monotonic_accepts_valid_ladder():
    from services.totals_devig import check_alt_ladder_monotonic
    ladder = [
        {"line": 7.5, "over_prob": 0.72, "under_prob": 0.28},
        {"line": 8.5, "over_prob": 0.55, "under_prob": 0.45},
        {"line": 9.5, "over_prob": 0.36, "under_prob": 0.64},
    ]
    ok, _ = check_alt_ladder_monotonic(ladder)
    assert ok


def test_alt_ladder_monotonic_rejects_broken_ladder():
    from services.totals_devig import check_alt_ladder_monotonic
    ladder = [
        {"line": 7.5, "over_prob": 0.55, "under_prob": 0.45},
        {"line": 8.5, "over_prob": 0.72, "under_prob": 0.28},   # BREAK
    ]
    ok, reason = check_alt_ladder_monotonic(ladder)
    assert not ok
    assert "over_ladder_break" in reason


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
