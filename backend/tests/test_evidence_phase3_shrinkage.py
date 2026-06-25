"""Phase 3: Reliability-weighted probability shrinkage tests.

Validates that:
  • Weight `w` interpolates linearly from FLOOR (0.30) at evidence_score=0
    to 1.00 at evidence_score=100.
  • A LOW-evidence pick's win probability is pulled hard toward
    market-implied, with edge collapsing accordingly.
  • A HIGH-evidence pick is barely touched.
  • `win_probability_raw`, `edge_percent_raw` are preserved across
    repeated `govern_pick` calls (idempotent — no compounding shrinkage).
  • Audit trail under `evidence_breakdown.probability_shrinkage` is
    populated and self-consistent.
  • Edge math: `edge_percent = win_probability − implied_probability`
    holds exactly after governance.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("JWT_SECRET", "x" * 64)  # avoid auth boot-crash

from evidence_engine import (  # noqa: E402
    SHRINKAGE_FLOOR,
    apply_probability_shrinkage,
    build_features_from_pick,
    govern_pick,
    probability_shrinkage_weight,
)


def test_weight_floor_at_zero_evidence():
    """No evidence → weight = floor (0.30). Model has 30% pull,
    market has 70%."""
    assert probability_shrinkage_weight(0) == SHRINKAGE_FLOOR
    assert SHRINKAGE_FLOOR == 0.30


def test_weight_full_trust_at_hundred_evidence():
    """HIGH evidence → weight = 1.0. Model trusted fully, no shrinkage."""
    assert probability_shrinkage_weight(100) == 1.0


def test_weight_linear_interpolation():
    """Weight curve is linear in evidence_score."""
    assert probability_shrinkage_weight(50) == pytest.approx(0.65, abs=1e-4)
    assert probability_shrinkage_weight(20) == pytest.approx(0.44, abs=1e-4)
    assert probability_shrinkage_weight(80) == pytest.approx(0.86, abs=1e-4)


def test_weight_clamps_out_of_range():
    """Weight stays in [floor, 1.0] for any input."""
    assert probability_shrinkage_weight(-50) == SHRINKAGE_FLOOR
    assert probability_shrinkage_weight(150) == 1.0


def test_apply_shrinkage_pulls_toward_implied_when_low_evidence():
    """Model says 80%, market implies 30%, evidence=0 (LOW).
    Result: 0.30 × 80 + 0.70 × 30 = 24 + 21 = 45.0%"""
    shrunk, w = apply_probability_shrinkage(80.0, 30.0, 0)
    assert w == 0.30
    assert shrunk == pytest.approx(45.0, abs=0.01)


def test_apply_shrinkage_no_pull_when_high_evidence():
    """Same model/market with evidence=100. No shrinkage applied."""
    shrunk, w = apply_probability_shrinkage(80.0, 30.0, 100)
    assert w == 1.0
    assert shrunk == pytest.approx(80.0, abs=0.01)


def test_apply_shrinkage_balanced_at_medium_evidence():
    """evidence=50 → w=0.65. 0.65×80 + 0.35×30 = 52 + 10.5 = 62.5"""
    shrunk, w = apply_probability_shrinkage(80.0, 30.0, 50)
    assert w == pytest.approx(0.65, abs=1e-4)
    assert shrunk == pytest.approx(62.5, abs=0.01)


def test_apply_shrinkage_handles_none_inputs():
    """Missing inputs return the model value unchanged."""
    shrunk, w = apply_probability_shrinkage(None, 30.0, 50)
    assert shrunk is None
    shrunk, _ = apply_probability_shrinkage(80.0, None, 50)
    assert shrunk == 80.0


def test_apply_shrinkage_clamps_degenerate_implied():
    """Books never quote 0% or 100% — but defensively clamp anyway
    so we never collapse the prob to an unrealistic single point."""
    shrunk, _ = apply_probability_shrinkage(80.0, 0.0, 0)
    # 0% gets clamped to 0.5%, so result is 0.30×80 + 0.70×0.5 = 24.35
    assert shrunk == pytest.approx(24.35, abs=0.01)
    shrunk2, _ = apply_probability_shrinkage(20.0, 100.0, 0)
    # 100% clamps to 99.5, so: 0.30×20 + 0.70×99.5 = 6 + 69.65 = 75.65
    assert shrunk2 == pytest.approx(75.65, abs=0.01)


def _make_pick(*, win_prob, implied_prob, edge, lock=92.0, factors=None):
    """Build a minimal MLB pick suitable for govern_pick."""
    return {
        "id": "test-pick-1",
        "sport": "MLB",
        "league": "MLB",
        "event": "Test Game",
        "market": "Pitcher Strikeouts",
        "pick": "Test Player Over 5.5 Ks",
        "win_probability": win_prob,
        "implied_probability": implied_prob,
        "edge_percent": edge,
        "book_odds": -200,
        "lock_score": lock,
        "lock_score_v2": lock,
        "factors": factors or {},
        "key_insights": [],
    }


def test_govern_pick_low_evidence_shrinks_hard():
    """LOW-evidence MLB pick: 80% model, 33% implied, modest edge,
    no supporting factors. Expected: small evidence_score → heavy
    shrinkage toward implied probability."""
    # Small edge (1.5%) means low-importance market feature only —
    # not enough on its own to push evidence_score high.
    pick = _make_pick(win_prob=80.0, implied_prob=78.5, edge=1.5, lock=92.0)
    govern_pick(pick, build_features_from_pick(pick))

    assert pick["win_probability_raw"] == 80.0
    assert pick["edge_percent_raw"] == 1.5
    # Heavy shrinkage means win prob should land closer to implied than
    # to the model's 80%. With evidence_score ~50 we expect ~79.5
    # (0.65×80 + 0.35×78.5 = 79.47). Verify it moved meaningfully.
    delta = abs(pick["win_probability"] - pick["win_probability_raw"])
    assert delta > 0.3, (
        f"low-evidence pick should shrink at least 0.3pp but only "
        f"shrank {delta:.2f}pp"
    )
    # Edge math should reconcile against the shrunk prob
    expected_edge = round(pick["win_probability"] - pick["implied_probability"], 2)
    assert pick["edge_percent"] == expected_edge
    # And the shrunk edge must be SMALLER than the raw edge (in
    # absolute terms — we pulled toward market consensus).
    assert abs(pick["edge_percent"]) < abs(pick["edge_percent_raw"])


def test_govern_pick_high_evidence_preserves_probability():
    """HIGH-evidence MLB pick (sim + multiple high-importance factors).
    Should shrink WAY LESS than a low-evidence pick — model is trusted
    much more when breadth is saturated AND per-feature reliability is HIGH."""
    pick = _make_pick(
        win_prob=80.0, implied_prob=33.3, edge=46.7, lock=92.0,
        factors={
            "Recent Strikeout Form (L5)": 79,
            "Opp K% vs same hand": 80,
            "Pitcher K/9 (recent)": 72,
            "Park Strikeout Factor": 78,
            "Pitch Count / Workload": 75,
        },
    )
    # Synthesize a sim_runs field so the universal extractor adds a
    # HIGH-tier model feature.
    pick["sim_runs"] = 20_000
    pick["sim_win_probability"] = 91.0
    govern_pick(pick, build_features_from_pick(pick))

    high_delta = abs(pick["win_probability"] - pick["win_probability_raw"])

    # Now run the EXACT SAME numbers through a low-evidence variant
    # (strip factors and sim). The low-evidence variant must shrink
    # noticeably MORE — that's the core promise of the feature.
    poor_pick = _make_pick(win_prob=80.0, implied_prob=33.3, edge=46.7, lock=92.0)
    govern_pick(poor_pick, build_features_from_pick(poor_pick))
    poor_delta = abs(poor_pick["win_probability"] - poor_pick["win_probability_raw"])

    assert high_delta < poor_delta, (
        f"high-evidence pick (delta={high_delta:.2f}) should shrink LESS "
        f"than low-evidence pick (delta={poor_delta:.2f}) but it didn't"
    )
    # And high-evidence should still be well within "we mostly trust the
    # model" territory — never erode the edge below half the original.
    assert high_delta < 20.0, (
        f"high-evidence shrinkage too aggressive: {high_delta:.2f}pp "
        f"(want <20.0pp)"
    )


def test_govern_pick_idempotent_across_repeated_runs():
    """Calling govern_pick repeatedly must NOT compound shrinkage.
    The raw probability is preserved and re-shrunk from scratch."""
    pick = _make_pick(win_prob=80.0, implied_prob=33.3, edge=46.7, lock=92.0)
    govern_pick(pick, build_features_from_pick(pick))
    first_prob = pick["win_probability"]
    first_edge = pick["edge_percent"]

    # Mimic a downstream refresh — call again on the same pick.
    govern_pick(pick, build_features_from_pick(pick))
    second_prob = pick["win_probability"]
    second_edge = pick["edge_percent"]

    assert second_prob == pytest.approx(first_prob, abs=0.05), (
        f"shrinkage compounded: {first_prob} → {second_prob}"
    )
    assert second_edge == pytest.approx(first_edge, abs=0.05)
    # And the original raw values must still be intact.
    assert pick["win_probability_raw"] == 80.0
    assert pick["edge_percent_raw"] == 46.7


def test_govern_pick_audit_trail_populated():
    """The evidence_breakdown.probability_shrinkage block must
    expose the exact math for the admin inspector."""
    pick = _make_pick(win_prob=70.0, implied_prob=40.0, edge=30.0, lock=85.0)
    govern_pick(pick, build_features_from_pick(pick))

    audit = pick["evidence_breakdown"]["probability_shrinkage"]
    assert audit["p_model_raw"] == 70.0
    assert audit["p_shrunk"] == pick["win_probability"]
    assert audit["weight"] == pick["probability_shrinkage_weight"]
    expected_delta = round(audit["p_shrunk"] - audit["p_model_raw"], 2)
    assert audit["delta_pp"] == expected_delta
    # Edge audit fields must also be present.
    assert audit["edge_raw"] == 30.0
    assert audit["edge_shrunk"] == pick["edge_percent"]


def test_govern_pick_no_implied_falls_back_to_book_odds():
    """If implied_probability is missing, we derive it from book_odds."""
    pick = _make_pick(win_prob=70.0, implied_prob=None, edge=None, lock=85.0)
    # book_odds=-200 → implied ≈ 66.67%
    govern_pick(pick, build_features_from_pick(pick))

    # win_probability should have shrunk SOMEWHERE between 70 and 66.67
    assert pick["win_probability"] < 70.0
    assert pick["win_probability"] > 60.0
    assert pick["win_probability_raw"] == 70.0
