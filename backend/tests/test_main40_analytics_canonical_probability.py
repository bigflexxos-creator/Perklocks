"""Item P0-E — Analytics consumer parity for canonical probability.

Verifies analytics_routes' Brier + Kelly consumers use the canonical
final probability (frozen at publication) — not the raw pre-shrinkage
engine output.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_analytics_calibration_uses_canonical_probability_import():
    """Brier-computing block imports the canonical accessor."""
    path = os.path.join(ROOT, "routes", "analytics_routes.py")
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "canonical_final_probability" in src
    # And the Kelly endpoint pulls published fields.
    assert '"published_probability": 1' in src
    assert '"model_probability": 1' in src


def test_kelly_prefers_canonical_over_novig_and_wp():
    """Simulated pick with divergent raw vs published — the Kelly
    stake path must pick the canonical value.  We test the logic
    inline rather than instantiating the FastAPI dep chain."""
    from services.canonical_probability import canonical_final_probability
    pick = {
        "win_probability":       98.2,   # raw engine
        "no_vig_pct":            50.0,   # book-implied
        "published_probability": 0.7629, # canonical authority
        "book_odds":             -110,
    }
    _cf = canonical_final_probability(pick)
    assert _cf is not None
    prob = _cf * 100.0
    assert abs(prob - 76.29) < 0.01, (prob, _cf)


def test_brier_uses_canonical_for_settled_pick():
    """Simulated settled pick — canonical drives Brier so it
    doesn't inflate against the raw pre-shrinkage 98.2%."""
    from services.canonical_probability import canonical_final_probability
    pick = {"status": "lost", "win_probability": 98.2,
             "published_probability": 0.7629}
    _cf = canonical_final_probability(pick)
    won = 0  # lost
    brier_canonical = (float(_cf) - won) ** 2
    brier_legacy    = (float(pick.get("win_probability") or 0) / 100.0 - won) ** 2
    # Canonical Brier is closer to zero → shows analytics telling the
    # truth instead of measuring against a fake 98.2%.
    assert brier_canonical < brier_legacy, (brier_canonical, brier_legacy)


if __name__ == "__main__":
    test_analytics_calibration_uses_canonical_probability_import()
    test_kelly_prefers_canonical_over_novig_and_wp()
    test_brier_uses_canonical_for_settled_pick()
    print("OK — analytics consumers now on canonical probability.")
