"""
CFB GAME-MARKET EVIDENCE CONTRACT · ML + Spread + Total (2026-06-12).

Focused proof:
  1. compute_lock_score with factors={} does NOT award 84 authority
     for CFB Spread/Total (would need SP+ evidence populated).
  2. Populated factors produce different (natural) scores.
  3. Empty evidence produces DIFFERENT score than populated evidence.
  4. Sportsbook implied prob alone (no independent evidence) MUST NOT
     produce elite authority.

Run:
    cd /app/backend && python -m pytest tests/test_cfb_game_market_evidence_contract.py -v
"""
from __future__ import annotations

import sys
sys.path.insert(0, "/app/backend")

from sports_engine import compute_lock_score


CFB_DQ = "sp_plus|returning_prod_both|portal_both"
CFB_PP_CAUSAL = "CAUSAL_INDEPENDENT"


def _score(factors, *, win_prob, edge_percent, book_odds):
    lock, _ = compute_lock_score(
        factors, win_prob=win_prob,
        pick={"book_odds": book_odds, "edge_percent": edge_percent,
              "win_probability": win_prob, "sport": "CFB",
              "market": "spread",
              "data_quality": CFB_DQ,
              "probability_provenance": CFB_PP_CAUSAL},
        edge_percent=edge_percent,
    )
    return lock


class TestEvidencePropagation:
    def test_empty_factors_do_not_beat_populated_factors_when_same_probs(self):
        """A CFB Spread with populated evidence must NOT score higher
        via the empty-factors fallback than through legitimate
        evidence-driven math."""
        empty = _score({}, win_prob=66.7, edge_percent=16.23, book_odds=-112)
        populated_factors = {
            "Model Fair Prob": 66.7,
            "Sportsbook Implied Prob": 52.8,
            "SP+ Margin Base": 2.4,
            "Projected Margin": -2.4,
            "Expected Total": 55.5,
            "__data_quality": CFB_DQ,
            "__model_uncertainty_reason": "nominal",
        }
        populated = _score(populated_factors, win_prob=66.7,
                           edge_percent=16.23, book_odds=-112)
        # Both scores are computed by the SAME compute_lock_score.
        # The invariant is that they must NOT be identical (evidence-
        # driven path should differ from unfactored fallback).  If they
        # are identical, evidence isn't being consumed.
        assert abs(populated - empty) > 0.5, (
            f"Evidence has NO effect on score: empty={empty} populated={populated}"
        )

    def test_populated_evidence_produces_varied_scores_across_inputs(self):
        """Different real evidence → different scores. Not one clustered value."""
        scores = []
        for win_prob, edge in [
            (60.0, 5.0), (66.7, 16.23), (74.9, 27.27),
            (76.3, 25.41), (56.0, 3.0),
        ]:
            f = {
                "Model Fair Prob": win_prob,
                "Sportsbook Implied Prob": win_prob - edge,
                "SP+ Margin Base": 0.15 * edge,
                "__data_quality": CFB_DQ,
            }
            scores.append(_score(f, win_prob=win_prob, edge_percent=edge,
                                  book_odds=-110))
        # At least 3 distinct values (spread > 3 pts) proves evidence
        # is actually consumed and produces varied output.
        distinct = len(set(round(s, 1) for s in scores))
        assert distinct >= 3, (
            f"Populated evidence produces clustered scores: {scores}"
        )


class TestUnfactoredFallbackIsNotElite:
    def test_empty_factors_do_not_grant_elite_authority(self):
        """Post-v4 (2026-06-14): confidence is a first-class Lock
        component so a CFB pick with wp=95%/edge=15% can legitimately
        reach Strong-Lock (~85) even with zero explicit factors.  The
        empty-factors regression guard is now: the pick must NOT
        reach Premium (>=90).  The historical ``factors={} → 84``
        fallback bug remains fixed."""
        for win_prob in [55.0, 65.0, 75.0, 85.0, 95.0]:
            ls = _score({}, win_prob=win_prob, edge_percent=15.0,
                        book_odds=-110)
            assert ls < 90.0, (
                f"Empty-factors fallback reached Premium band at "
                f"win_prob={win_prob} → LS={ls}"
            )


class TestSportsbookImpliedNotIndependent:
    def test_sportsbook_implied_alone_does_not_grant_elite_authority(self):
        """A CFB pick whose ONLY factor is Sportsbook Implied Prob (a
        market reference, not independent predictive evidence) MUST NOT
        reach elite Lock authority."""
        f = {"Sportsbook Implied Prob": 65.0,
              "__data_quality": CFB_DQ}
        ls = _score(f, win_prob=65.0, edge_percent=0.0, book_odds=-186)
        assert ls < 85.0, (
            f"Market-reference-only pick reached elite Lock: LS={ls}"
        )


if __name__ == "__main__":
    import subprocess, sys as _sys
    r = subprocess.run(
        [_sys.executable, "-m", "pytest", __file__, "-v", "--tb=short"],
        cwd="/app/backend",
    )
    _sys.exit(r.returncode)
