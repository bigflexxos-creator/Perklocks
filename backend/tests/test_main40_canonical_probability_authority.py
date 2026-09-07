"""Item P0-E — Canonical Final Probability Authority.

Verifies that the canonical accessor prefers:
    model_probability > published_probability > win_probability
and normalises percentage inputs to fractions.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_canonical_prefers_model_probability():
    from services.canonical_probability import canonical_final_probability, canonical_final_probability_source

    pick = {
        "model_probability":     0.72,
        "published_probability": 0.65,
        "win_probability":       0.58,
    }
    assert canonical_final_probability(pick) == 0.72
    assert canonical_final_probability_source(pick) == "model_probability"


def test_canonical_falls_through_to_published_then_win():
    from services.canonical_probability import canonical_final_probability, canonical_final_probability_source

    p1 = {"published_probability": 0.65, "win_probability": 0.58}
    assert canonical_final_probability(p1) == 0.65
    assert canonical_final_probability_source(p1) == "published_probability"

    p2 = {"win_probability": 0.58}
    assert canonical_final_probability(p2) == 0.58
    assert canonical_final_probability_source(p2) == "win_probability"


def test_canonical_normalises_percent_inputs():
    from services.canonical_probability import canonical_final_probability
    assert canonical_final_probability({"model_probability": 72}) == 0.72
    assert canonical_final_probability({"win_probability": 5}) == 0.05


def test_canonical_returns_none_when_absent():
    from services.canonical_probability import canonical_final_probability
    assert canonical_final_probability({}) is None
    assert canonical_final_probability({"model_probability": None,
                                          "published_probability": None,
                                          "win_probability":       None}) is None
    # Invalid types → None (no fabrication).
    assert canonical_final_probability({"model_probability": "banana"}) is None
    assert canonical_final_probability({"model_probability": float("nan")}) is None


def test_canonical_ignores_non_authoritative_fields():
    """``sim_probability`` / ``implied_probability`` / ``fusion_probability``
    are INPUTS, not the FINAL authority.  They must never be picked
    up by the canonical accessor."""
    from services.canonical_probability import canonical_final_probability
    pick = {
        "sim_probability":      0.99,
        "implied_probability":  0.55,
        "fusion_probability":   0.80,
    }
    assert canonical_final_probability(pick) is None


if __name__ == "__main__":
    test_canonical_prefers_model_probability()
    test_canonical_falls_through_to_published_then_win()
    test_canonical_normalises_percent_inputs()
    test_canonical_returns_none_when_absent()
    test_canonical_ignores_non_authoritative_fields()
    print("OK — canonical final probability authority verified.")
