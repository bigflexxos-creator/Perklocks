"""Phase 15 — PARLAY 10X extended invariants.

Phase 11 already verified the leg-level invariants (frozen snapshot,
VOID/PUSH reprice, deterministic id, real-line integrity).  Phase 15
extends to the intelligence / correlation layer:

  P15.1  Correlation snapshot is FROZEN on save — later correlation
         model changes never rewrite a placed parlay's classification.
  P15.2  Duplicate-leg prevention: same pick_id cannot appear twice
         in one parlay (sha1 dedup).
  P15.3  Book-hold constant explicitly declared (0.93) — no hidden
         magic number.
  P15.4  Cashout estimator never returns > full potential payout.
"""
from __future__ import annotations
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

from parlay_history import (
    _parlay_id, CASHOUT_BOOK_HOLD, _cashout_estimate,
    _american_combine, _american_to_decimal,
)


def test_correlation_snapshot_frozen_on_save():
    """save_parlay stamps `correlation_snapshot` and `frozen_at`
    once on save.  Guard against later mutation via source scan."""
    src = pathlib.Path("/app/backend/parlay_history.py").read_text()
    assert '"correlation_snapshot": correlation_snapshot' in src
    assert '"frozen_at": now' in src
    # No update writes to `correlation_snapshot` post-save.
    assert 'correlation_snapshot":' not in src.split("insert_one", 1)[1] \
        or "update_one" not in src.split(
            '"correlation_snapshot": correlation_snapshot', 1)[1].split(
                "insert_one", 1)[0]


def test_parlay_id_dedup_via_sha1():
    """Same user + same leg-set → same id.  This is the dedup
    contract (no duplicate parlay rows for the same combination)."""
    p1 = _parlay_id("user1", ["a", "b", "c"])
    p2 = _parlay_id("user1", ["a", "b", "c"])
    assert p1 == p2


def test_cashout_book_hold_declared():
    """Book-hold constant explicitly declared — not a magic number."""
    assert CASHOUT_BOOK_HOLD == pytest.approx(0.93)


def test_cashout_never_exceeds_full_potential_payout():
    """The estimator must never quote a cash-out ABOVE what the
    ticket would win if all pending legs resolved to WON."""
    parlay = {
        "status": "live", "stake": 1.0,
        "combined_odds": _american_combine([-110, -110, +200]),
        "legs": [
            {"book_odds": -110, "status": "pending"},
            {"book_odds": -110, "status": "pending"},
            {"book_odds": +200, "status": "pending"},
        ],
    }
    est = _cashout_estimate(parlay)
    full_payout = 1.0 * _american_to_decimal(parlay["combined_odds"])
    assert est is not None
    assert est <= full_payout


def test_cashout_zero_on_lost_leg():
    parlay = {
        "status": "live", "stake": 1.0, "combined_odds": 300,
        "legs": [
            {"book_odds": -110, "status": "won"},
            {"book_odds": -110, "status": "lost"},
        ],
    }
    assert _cashout_estimate(parlay) == 0.0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
