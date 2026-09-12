"""
UNIVERSAL LOCK-SCORE v4 CONFIDENCE-FIRST — CLASS-LEVEL ROOT CLOSURE
====================================================================
(2026-06-14)

Locks in the shared compute_lock_score change that:
  • Promotes calibrated win probability to a first-class component
    (``confidence``, weight 0.25 full / 0.30 pregame).
  • Reduces Edge to secondary value authority (0.35 → 0.15 full /
    0.18 pregame).
  • Removes the SECOND edge channel from ``_compute_data_quality_score``
    (``market_edge = 60 + edge × 3`` deleted → no double-count).
  • Uses EXPLICIT-PREGAME weights (not blind renormalization) when
    ROI/CLV are unavailable so Edge cannot regain dominance.
  • Stamps ``lock_score_version = "v4.confidence_first.2026-06-14"``
    and ``calibrated_win_probability`` on every pick for future
    calibration reconstruction.
  • Preserves 85 board floor, Apex gates, and existing sport-specific
    authority (NFL player-prop authority etc.).

Run:
    cd /app/backend && python -m pytest tests/test_lock_score_v4_confidence_first.py -v
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/app/backend")

from sports_engine import compute_lock_score


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def _strong_factors() -> dict:
    """5 well-clustered normalized factors + provenance dq string."""
    return {
        "a1 (norm)": 0.75, "a2 (norm)": 0.72, "a3 (norm)": 0.70,
        "a4 (norm)": 0.73, "a5 (norm)": 0.68,
        "__data_quality": "full",
    }


def _thin_factors() -> dict:
    return {
        "x1 (norm)": 0.55, "x2 (norm)": 0.53, "x3 (norm)": 0.52,
        "__data_quality": "sp_plus",
    }


def _score(factors, *, wp, edge, book=-186, prov="CAUSAL_INDEPENDENT"):
    pick = {
        "book_odds": book, "edge_percent": edge, "win_probability": wp,
        "sport": "NFL", "market": "Moneyline",
        "probability_provenance": prov,
    }
    ls, _ = compute_lock_score(factors, win_prob=wp, pick=pick,
                                edge_percent=edge)
    return ls, pick


# ═════════════════════════════════════════════════════════════
# 1. CONFIDENCE IS FIRST-CLASS + MONOTONIC IN win_prob
# ═════════════════════════════════════════════════════════════
class TestConfidenceMonotonic:
    def test_lock_score_monotonic_in_wp_at_zero_edge(self):
        """Holding edge and evidence constant, LS must rise as wp rises."""
        scores = []
        for wp in (50, 60, 70, 80, 90, 95, 99):
            ls, _ = _score(_strong_factors(), wp=wp, edge=0)
            scores.append(ls)
        for prev, curr in zip(scores, scores[1:]):
            assert curr >= prev, (
                f"Not monotonic in wp at edge=0: {scores}"
            )
        # Non-trivial spread: LS should meaningfully move across wp.
        assert scores[-1] - scores[0] >= 5.0, (
            f"wp has negligible authority: 50→99% only {scores[-1]-scores[0]:.1f} LS"
        )

    def test_confidence_component_present_and_reasonable(self):
        ls, pick = _score(_strong_factors(), wp=90, edge=5)
        lc = pick.get("lock_components", {})
        assert "confidence" in lc, "confidence component missing from telemetry"
        assert 95 <= lc["confidence"] <= 99, (
            f"90% wp should yield conf ~97; got {lc['confidence']}"
        )


# ═════════════════════════════════════════════════════════════
# 2. EDGE NO LONGER DOMINATES
# ═════════════════════════════════════════════════════════════
class TestEdgeNotDominant:
    def test_high_confidence_low_edge_reaches_board(self):
        """95% wp + 0% edge + strong evidence must reach the 85 floor."""
        ls, _ = _score(_strong_factors(), wp=95, edge=0)
        assert ls >= 85.0, f"HighConf/0Edge crushed: LS={ls}"

    def test_low_confidence_high_edge_not_elite(self):
        """55% wp + 15% edge + thin evidence must NOT reach 95+ Elite."""
        ls, _ = _score(_thin_factors(), wp=55, edge=15,
                       prov="MODEL_CONDITIONED")
        assert ls < 95.0, (
            f"LowConf/HighEdge reached Elite band: LS={ls}"
        )

    def test_effective_edge_weight_capped_pregame(self):
        """When ROI/CLV are unavailable (typical pregame), the
        effective edge weight must NOT exceed 25%.  Under v3
        renormalization it swelled to ~47%."""
        _, pick = _score(_strong_factors(), wp=78, edge=8)
        ew = (pick.get("lock_components") or {}).get("effective_weights")
        assert ew is not None, "effective_weights missing"
        assert ew.get("edge", 1.0) <= 0.25, (
            f"Edge effective weight too high pregame: {ew.get('edge')}"
        )
        assert ew.get("confidence", 0.0) >= 0.25, (
            f"Confidence effective weight too low pregame: {ew.get('confidence')}"
        )


# ═════════════════════════════════════════════════════════════
# 3. NO DOUBLE-COUNT — Edge stripped from data_quality
# ═════════════════════════════════════════════════════════════
class TestNoEdgeDoubleCount:
    def test_dq_independent_of_edge(self):
        """DQ must be identical across a range of edge values when
        every other input is held constant.  Under v3, DQ contained
        ``market_edge = 60 + edge × 3`` which drifted with edge."""
        dqs = []
        for e in (-10, -5, 0, 5, 10, 15, 20):
            _, pick = _score(_strong_factors(), wp=78, edge=e)
            dqs.append((pick.get("lock_components") or {}).get("data_quality"))
        assert len(set(dqs)) == 1, (
            f"DQ still drifts with edge (double-count regression): {dqs}"
        )


# ═════════════════════════════════════════════════════════════
# 4. FAVORITE / UNDERDOG PARITY
# ═════════════════════════════════════════════════════════════
class TestFavDogParity:
    def test_same_evidence_same_score_across_price(self):
        """Same wp/edge/evidence — LS must be identical for a fav
        priced -186 and a dog priced +170.  Chalk bias must not
        creep in through a hidden book-implied route."""
        ls_fav, _ = _score(_strong_factors(), wp=65, edge=6, book=-186)
        ls_dog, _ = _score(_strong_factors(), wp=65, edge=6, book=170)
        assert abs(ls_fav - ls_dog) < 0.5, (
            f"Fav/Dog parity broken: fav={ls_fav}, dog={ls_dog}"
        )


# ═════════════════════════════════════════════════════════════
# 5. VERSION STAMP + CALIBRATION FREEZE
# ═════════════════════════════════════════════════════════════
class TestVersionAndFreeze:
    def test_version_stamp_present(self):
        _, pick = _score(_strong_factors(), wp=78, edge=6)
        assert pick.get("lock_score_version") == \
            "v4.confidence_first.2026-06-14"

    def test_calibrated_wp_frozen(self):
        _, pick = _score(_strong_factors(), wp=87.5, edge=3)
        cwp = pick.get("calibrated_win_probability")
        assert cwp is not None
        # Accept fraction (0.875) or percent (87.5) — the helper
        # normalises to whichever form the pick already used.
        assert cwp in (round(87.5, 4), round(0.875, 4))

    def test_effective_weights_frozen(self):
        _, pick = _score(_strong_factors(), wp=78, edge=6)
        ew = (pick.get("lock_components") or {}).get("effective_weights")
        assert isinstance(ew, dict), "effective_weights not stamped"
        # Every stamped weight sums to 1.0 (per candidate).
        total = sum(ew.values())
        assert abs(total - 1.0) < 1e-3, (
            f"effective_weights don't sum to 1: {total}"
        )


# ═════════════════════════════════════════════════════════════
# 6. APEX / HIGH-TIER SAFETY
# ═════════════════════════════════════════════════════════════
class TestApexAndTierSafety:
    def test_high_edge_alone_never_apex(self):
        """Even at edge=25% with modest wp, LS must not reach 100."""
        ls, _ = _score(_thin_factors(), wp=55, edge=25,
                       prov="MODEL_CONDITIONED")
        assert ls < 99.0, f"High-edge alone reached ~Apex: LS={ls}"

    def test_high_confidence_alone_reachable_but_not_apex(self):
        """Peak confidence (96% wp) + 2% edge + strong evidence
        should reach at least Strong-Lock but not manufacture Apex
        purely from confidence.  Apex requires convergence gates
        outside compute_lock_score."""
        ls, _ = _score(_strong_factors(), wp=96, edge=2)
        assert ls >= 85.0, f"Peak-conf pick under-scored: LS={ls}"
        assert ls < 99.0, f"Peak-conf alone hit 99: LS={ls}"

    def test_convergence_reaches_elite_band(self):
        """Strong convergence (wp=92%, edge=12%, full evidence)
        should reach the Premium/Elite bands naturally."""
        ls, _ = _score(_strong_factors(), wp=92, edge=12)
        assert ls >= 90.0, f"Strong convergence didn't reach Elite: LS={ls}"


# ═════════════════════════════════════════════════════════════
# 7. FAIL-CLOSED / ROBUSTNESS
# ═════════════════════════════════════════════════════════════
class TestFailClosedRobustness:
    def test_empty_factors_still_bounded(self):
        for wp in (55.0, 65.0, 75.0, 85.0, 95.0):
            pick = {
                "book_odds": -110, "edge_percent": 5.0,
                "win_probability": wp, "sport": "MLB",
                "market": "Moneyline",
                "probability_provenance": "MODEL_CONDITIONED",
            }
            ls, _ = compute_lock_score({}, win_prob=wp, pick=pick,
                                        edge_percent=5.0)
            assert 55.0 <= ls <= 99.0, f"OOB score at wp={wp}: {ls}"

    def test_zero_wp_zero_edge_stays_low(self):
        ls, _ = _score(_thin_factors(), wp=0, edge=0,
                       prov="PRIOR_ONLY")
        assert ls < 80.0, f"wp=0/edge=0 reached Standard: LS={ls}"


if __name__ == "__main__":
    import subprocess as _sp
    _r = _sp.run(
        [sys.executable, "-m", "pytest", __file__, "-v", "--tb=short"],
        cwd="/app/backend",
    )
    sys.exit(_r.returncode)
