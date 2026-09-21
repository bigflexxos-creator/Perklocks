"""Phase A Root-Closure — Probability Unit + Publication Impossibility
regression suite.

Covers §§3-6 of the final Root-Closure spec + the exact Newcastle @
Coventry "Over 7.5 · +3500 · Lock 85.4 · Edge -1348% · undefined%"
physical fixture.
"""
from __future__ import annotations

import math
import sys
sys.path.insert(0, "/app/backend")

import pytest


# ─────────────────────────────────────────────────────────────────────
# §3-4 — Probability unit contract
# ─────────────────────────────────────────────────────────────────────

class TestProbabilityUnits:
    def test_to_fraction_percent_semantics(self):
        from services.probability_units import to_fraction
        assert to_fraction(0.05, assume="fraction") == 0.05
        assert to_fraction(5.0, assume="percent") == 0.05
        assert to_fraction(50.0, assume="percent") == 0.5
        # Out of range
        assert to_fraction(150.0, assume="percent") is None
        assert to_fraction(1.5, assume="fraction") is None
        # Non-finite
        assert to_fraction(float("nan")) is None
        assert to_fraction(float("inf")) is None

    def test_implied_probability_from_odds(self):
        from services.probability_units import implied_probability_from_odds
        # +3500 → 100/(3500+100) = 0.02778
        assert abs(implied_probability_from_odds(3500) - 0.02778) < 0.001
        # -110 → 110/210 = 0.5238
        assert abs(implied_probability_from_odds(-110) - 0.5238) < 0.001
        # +500 → 100/600 = 0.1667
        assert abs(implied_probability_from_odds(500) - 0.1667) < 0.001
        # -200 → 200/300 = 0.6667
        assert abs(implied_probability_from_odds(-200) - 0.6667) < 0.001
        # -600
        assert abs(implied_probability_from_odds(-600) - (600/700)) < 0.001
        # Zero and non-finite
        assert implied_probability_from_odds(0) is None
        assert implied_probability_from_odds(float("nan")) is None

    def test_edge_percentage_points_no_1348_bug(self):
        """+3500 odds, model prob = 0.0143 → edge ≈ -1.35 pp, NOT -1348."""
        from services.probability_units import edge_percentage_points
        edge, err = edge_percentage_points(
            0.0143, 3500, impl_is_odds=True, assume_model="fraction",
        )
        assert err is None
        assert abs(edge - (-1.35)) < 0.05, f"edge={edge}"
        # Clamp behaviour — unit mixing should NEVER emit -1348.
        # If a caller accidentally passes model_prob=1.43 (percent
        # scale), the auto helper still handles it correctly.
        edge2, err2 = edge_percentage_points(
            1.43, 3500, impl_is_odds=True, assume_model="auto",
        )
        assert err2 is None
        assert abs(edge2 - (-1.35)) < 0.05, f"edge2={edge2}"

    @pytest.mark.parametrize("odds", [+3500, +500, +100, -110, -200, -600])
    def test_all_book_odds_produce_finite_implied(self, odds):
        from services.probability_units import implied_probability_from_odds
        p = implied_probability_from_odds(odds)
        assert p is not None and 0.0 < p < 1.0 and math.isfinite(p)


# ─────────────────────────────────────────────────────────────────────
# §6 — Publication Impossibility Guard
# ─────────────────────────────────────────────────────────────────────

class TestPublicationImpossibilityGuard:
    def _base_pick(self, **overrides):
        p = {
            "id": "test-pick-1",
            "prediction_id": "test-pick-1",
            "book_odds": 3500,
            "odds_source": "the_odds_api",
            "identity_class": "AUTHORITATIVE",
            "model_probability": 0.0143,
            "win_probability": 0.0143,
            "sport": "Soccer",
            "market": "Total Goals",
        }
        p.update(overrides)
        return p

    def test_finite_edge_within_bounds_publishes(self):
        from services.canonical_publication_boundary import evaluate_publication
        v = evaluate_publication(self._base_pick(edge_percent=-1.35))
        # Any REJECT reason for impossibility must not fire.
        for r in v.reasons:
            assert "IMPOSSIBLE" not in r, f"unexpected reject reason: {r}"

    def test_infinite_edge_rejected(self):
        from services.canonical_publication_boundary import evaluate_publication
        v = evaluate_publication(self._base_pick(edge_percent=float("inf")))
        assert "IMPOSSIBLE_EDGE_MAGNITUDE" in v.reasons

    def test_absurd_edge_magnitude_rejected(self):
        from services.canonical_publication_boundary import evaluate_publication
        v = evaluate_publication(self._base_pick(edge_percent=-1348.0))
        assert "IMPOSSIBLE_EDGE_MAGNITUDE" in v.reasons

    def test_nan_win_probability_rejected(self):
        from services.canonical_publication_boundary import evaluate_publication
        v = evaluate_publication(self._base_pick(win_probability=float("nan")))
        assert "IMPOSSIBLE_WIN_PROBABILITY" in v.reasons

    def test_impossible_implied_probability_rejected(self):
        from services.canonical_publication_boundary import evaluate_publication
        v = evaluate_publication(self._base_pick(implied_probability=float("nan")))
        assert "IMPOSSIBLE_IMPLIED_PROBABILITY" in v.reasons

    def test_implied_disagreement_with_odds_rejected(self):
        # Odds +3500 → implied ≈ 2.78%; storing 95% is unit-mixing.
        from services.canonical_publication_boundary import evaluate_publication
        v = evaluate_publication(self._base_pick(implied_probability=95.0))
        assert "IMPOSSIBLE_IMPLIED_PROBABILITY" in v.reasons


# ─────────────────────────────────────────────────────────────────────
# §62 — Newcastle @ Coventry physical Soccer fixture
# ─────────────────────────────────────────────────────────────────────

class TestPhysicalSoccerRegression:
    def test_newcastle_coventry_over_7_5_impossible(self):
        """The exact physical card:
            Soccer · Newcastle @ Coventry · Total Goals Over 7.5
            +3500 · Lock 85.4 · Edge -1348% · undefined% · PLAYABLE

        must be canonically REJECTED and must never render impossible
        display values.
        """
        from services.canonical_publication_boundary import evaluate_publication
        from services.probability_units import (
            implied_probability_from_odds, edge_percentage_points,
        )

        # Truthful implied — never undefined.
        implied = implied_probability_from_odds(3500)
        assert implied is not None and abs(implied - 0.02778) < 0.001

        # Truthful edge — must be ≈ -1.35 pp, NOT -1348.
        edge, err = edge_percentage_points(
            0.0143, 3500, impl_is_odds=True, assume_model="fraction",
        )
        assert err is None
        assert abs(edge - (-1.35)) < 0.05

        # The impossible legacy card (edge_percent=-1348) MUST be
        # rejected at publication.
        pick = {
            "id": "newcastle-coventry-over-7-5",
            "prediction_id": "newcastle-coventry-over-7-5",
            "book_odds": 3500,
            "odds_source": "the_odds_api",
            "identity_class": "AUTHORITATIVE",
            "sport": "Soccer",
            "market": "Total Goals",
            "selection": "Over 7.5",
            "model_probability": 0.0143,
            "win_probability": 1.43,          # percent scale
            "implied_probability": None,      # frontend would render "undefined%"
            "edge_percent": -1348.0,          # unit-corrupt authority
        }
        v = evaluate_publication(pick)
        # At minimum, the impossible edge magnitude must reject.
        assert "IMPOSSIBLE_EDGE_MAGNITUDE" in v.reasons

    def test_board_dto_derives_implied_from_odds(self):
        """When the DTO receives a pick without implied_probability but
        with valid book_odds, it must derive the correct percentage."""
        from services.board_pick_dto import project_board_dto
        pick = {
            "id": "test-derive",
            "book_odds": 3500,
            # implied_probability intentionally missing
        }
        out = project_board_dto(pick)
        assert out.get("implied_probability") is not None
        assert abs(out["implied_probability"] - 2.78) < 0.05
