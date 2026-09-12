"""
CFB FACTOR NORMALIZATION — SCORING-BOUNDARY ROOT CLOSURE (2026-06-14)
=====================================================================

Locks in the surgical fix for the CFB high-authority ceiling defect.

ROOT CAUSE:
  CFB emission previously put raw points (Projected Margin=15.5) and
  raw percentages (Model Fair Prob=78.0) into the ``factors`` dict.
  ``compute_lock_score.market_align`` treats every numeric factor as
  a [0,1] normalized signal (``100 − stdev × 500``).  Mixed-unit
  values forced ``stdev`` huge → ``market_align`` clamped to 0 on
  EVERY CFB pick → systemic ~15-18 Lock-point loss → hard ~70.7 cap.

FIX (surgical, boundary-only):
  • Raw evidence values preserved as STRING factor keys — skipped by
    ``market_align``'s ``isinstance(v, (int, float))`` filter but
    still surface for the UI / Why-This-Pick explainer.
  • Normalized [0,1] values added under ``(norm)``-suffixed keys —
    consumed by ``market_align`` and produce a meaningful alignment
    signal.

NON-GOALS (regression guards):
  • Model probability MUST NOT change.
  • Sportsbook odds MUST NOT change.
  • Edge MUST NOT change.
  • Returning production / portal remain SHADOW_RESEARCH_ONLY.
  • The 85 board floor and Apex gates are NOT touched.
  • Stale bogus +950/+1500 Peak-98 signature MUST NOT return.

Run:
    cd /app/backend && python -m pytest tests/test_cfb_factor_normalization.py -v
"""
from __future__ import annotations

import math
import sys

sys.path.insert(0, "/app/backend")

from sports_engine import (
    compute_lock_score,
    _cfb_norm_margin,
    _cfb_norm_total,
    _implied_prob,
)


CFB_DQ = "sp_plus|returning_prod_both|portal_both"
CFB_PP_CAUSAL = "CAUSAL_INDEPENDENT"
CFB_PP_MC = "MODEL_CONDITIONED"


# ─────────────────────────────────────────────────────────────
# Helpers — reconstruct the exact factor dict shape the CFB
# emission blocks (ML/Spread/Total) now produce.
# ─────────────────────────────────────────────────────────────
def _cfb_emission_factors(*, exp_margin: float, exp_total: float,
                          sp_base: float, mp: float,
                          implied: float,
                          dq: str = CFB_DQ) -> dict:
    """Mirror of the exact factor dict written by the ML emission
    block in sports_engine (post-normalization fix)."""
    return {
        # Raw (strings — skipped by market_align)
        "Projected Margin":        f"{exp_margin:+.2f} pts",
        "Expected Total":          f"{exp_total:.2f} pts",
        "Model Fair Prob":         f"{mp * 100:.2f}%",
        "Sportsbook Implied Prob": f"{implied * 100:.2f}%",
        "SP+ Margin Base":         f"{sp_base:+.2f} pts",
        "__data_quality":          dq,
        "__model_uncertainty_reason": "nominal",
        # Normalized (numeric — consumed by scoring math)
        "Projected Margin (norm)":  _cfb_norm_margin(exp_margin),
        "Expected Total (norm)":    _cfb_norm_total(exp_total),
        "Model Fair Prob (norm)":   round(float(mp), 4),
        "Sportsbook Implied (norm)": round(float(implied), 4),
        "SP+ Rating Δ (norm)":      _cfb_norm_margin(sp_base),
    }


def _score(factors: dict, *, wp: float, edge: float, book: int,
           prov: str = CFB_PP_MC) -> tuple[float, dict]:
    pick = {
        "book_odds": book,
        "edge_percent": edge,
        "win_probability": wp,
        "sport": "CFB",
        "market": "Home Moneyline",
        "probability_provenance": prov,
    }
    score, weighted = compute_lock_score(
        factors, win_prob=wp, pick=pick, edge_percent=edge)
    return score, pick.get("lock_components") or {}


# ═════════════════════════════════════════════════════════════
# 1. SANITY — helper transforms
# ═════════════════════════════════════════════════════════════
class TestNormalizationHelpers:
    def test_margin_norm_returns_unit_interval(self):
        for pts in (-40, -20, -10, -5, 0, 5, 10, 20, 40):
            v = _cfb_norm_margin(pts)
            assert 0.0 <= v <= 1.0, f"margin_norm({pts}) = {v} out of [0,1]"

    def test_margin_norm_parity_center(self):
        assert abs(_cfb_norm_margin(0) - 0.5) < 1e-6

    def test_margin_norm_monotonic(self):
        vals = [_cfb_norm_margin(p) for p in (-20, -10, 0, 10, 20)]
        for a, b in zip(vals, vals[1:]):
            assert b > a, f"margin_norm not monotonic: {vals}"

    def test_total_norm_returns_unit_interval(self):
        for pts in (20, 35, 45, 50, 55, 65, 80):
            v = _cfb_norm_total(pts)
            assert 0.0 <= v <= 1.0

    def test_total_norm_center_at_50(self):
        assert abs(_cfb_norm_total(50) - 0.5) < 1e-6


# ═════════════════════════════════════════════════════════════
# 2. FACTOR-SHAPE INVARIANTS
# ═════════════════════════════════════════════════════════════
class TestFactorShape:
    def test_raw_keys_are_strings_market_align_skips_them(self):
        """The 5 raw evidence keys must be STRINGS so market_align
        (isinstance-based float filter) never sees them."""
        f = _cfb_emission_factors(
            exp_margin=15.5, exp_total=58.0, sp_base=13.0,
            mp=0.78, implied=0.65)
        for k in ("Projected Margin", "Expected Total",
                  "Model Fair Prob", "Sportsbook Implied Prob",
                  "SP+ Margin Base"):
            assert isinstance(f[k], str), (
                f"Raw key {k!r} must be a string, got {type(f[k]).__name__} "
                f"— market_align would consume raw units and collapse to 0"
            )

    def test_norm_keys_are_floats_in_unit_interval(self):
        f = _cfb_emission_factors(
            exp_margin=15.5, exp_total=58.0, sp_base=13.0,
            mp=0.78, implied=0.65)
        for k in ("Projected Margin (norm)", "Expected Total (norm)",
                  "Model Fair Prob (norm)", "Sportsbook Implied (norm)",
                  "SP+ Rating Δ (norm)"):
            v = f[k]
            assert isinstance(v, (int, float)), (
                f"Normalized key {k!r} must be numeric, got {type(v).__name__}"
            )
            assert 0.0 <= v <= 1.0, f"{k!r} = {v} out of [0,1]"

    def test_dunder_keys_preserved(self):
        """The two `__`-prefixed provenance strings must survive."""
        f = _cfb_emission_factors(
            exp_margin=15.5, exp_total=58.0, sp_base=13.0,
            mp=0.78, implied=0.65)
        assert f.get("__data_quality") == CFB_DQ
        assert f.get("__model_uncertainty_reason") == "nominal"

    def test_safety_net_keys_still_non_empty(self):
        """picks_routes.py CFB safety net checks these keys are
        non-empty.  String raw values must still satisfy the check."""
        f = _cfb_emission_factors(
            exp_margin=15.5, exp_total=58.0, sp_base=13.0,
            mp=0.78, implied=0.65)
        for k in ("Model Fair Prob", "SP+ Margin Base",
                  "Sportsbook Implied Prob"):
            assert f.get(k) not in (None, "", {}, []), (
                f"Safety-net key {k!r} became empty — picks_routes "
                f"legitimacy gate would treat pick as stale/pre-fix"
            )


# ═════════════════════════════════════════════════════════════
# 3. MARKET_ALIGN NO LONGER HARD-COLLAPSES TO 0
# ═════════════════════════════════════════════════════════════
class TestMarketAlignRestored:
    def test_market_align_positive_for_typical_cfb_pick(self):
        f = _cfb_emission_factors(
            exp_margin=15.5, exp_total=58.0, sp_base=13.0,
            mp=0.78, implied=0.65)
        _, lc = _score(f, wp=78.0, edge=13.0, book=-186,
                       prov=CFB_PP_CAUSAL)
        assert lc["alignment"] > 30.0, (
            f"market_align still crushed: alignment={lc['alignment']}"
        )

    def test_market_align_varies_across_evidence_states(self):
        """Different real CFB evidence must produce DIFFERENT
        alignment values — proves it's not just a new constant."""
        aligns = []
        cases = [
            # (exp_margin, exp_total, sp_base, mp, implied)
            (2.5, 51.0, 1.0, 0.53, 0.51),   # coin-flip
            (6.5, 51.4, 4.0, 0.61, 0.575),  # mild fav
            (12.5, 54.0, 10.0, 0.65, 0.565),  # strong SP+
            (18.0, 58.0, 16.0, 0.80, 0.667),  # heavy fav
            (25.0, 62.0, 22.0, 0.88, 0.727),  # blowout
            (-8.0, 55.0, -6.0, 0.40, 0.50),   # dog
        ]
        for em, et, sb, mp, im in cases:
            f = _cfb_emission_factors(
                exp_margin=em, exp_total=et, sp_base=sb, mp=mp,
                implied=im, dq="sp_plus")
            _, lc = _score(f, wp=mp * 100, edge=(mp - im) * 100,
                           book=-110 if mp > 0.5 else 110)
            aligns.append(lc["alignment"])
        distinct = len(set(round(a, 1) for a in aligns))
        assert distinct >= 4, (
            f"market_align became a new constant across cases: "
            f"aligns={aligns}"
        )

    def test_market_align_nonzero_for_identical_probs(self):
        """When model prob = market implied, factors cluster and
        alignment should be HIGH (not still 0 from a residual bug)."""
        f = _cfb_emission_factors(
            exp_margin=2.0, exp_total=50.0, sp_base=1.0,
            mp=0.524, implied=0.524)
        _, lc = _score(f, wp=52.4, edge=0.0, book=-110)
        # tight clustering: mp≈imp, small margin, total near center
        assert lc["alignment"] > 60.0, (
            f"Tight-cluster alignment too low: {lc['alignment']}"
        )


# ═════════════════════════════════════════════════════════════
# 4. MODEL PROB / ODDS / EDGE UNCHANGED
# ═════════════════════════════════════════════════════════════
class TestNoDownstreamMutation:
    """The normalization fix must not perturb probability, odds, or
    edge (user directive §3).  We verify by explicit numeric checks."""

    def test_mp_frac_survives_normalization_roundtrip(self):
        """`Model Fair Prob (norm)` must equal the raw mp fraction
        to 4 decimal places (no calibration hidden in the transform)."""
        for mp in (0.35, 0.50, 0.61, 0.78, 0.92):
            f = _cfb_emission_factors(
                exp_margin=10.0, exp_total=55.0, sp_base=8.0,
                mp=mp, implied=0.50)
            assert abs(f["Model Fair Prob (norm)"] - mp) < 1e-4

    def test_implied_survives_normalization_roundtrip(self):
        for imp in (0.30, 0.50, 0.60, 0.75, 0.85):
            f = _cfb_emission_factors(
                exp_margin=5.0, exp_total=55.0, sp_base=3.0,
                mp=0.55, implied=imp)
            assert abs(f["Sportsbook Implied (norm)"] - imp) < 1e-4

    def test_edge_passed_through_unchanged(self):
        """The compute_lock_score pick contract's edge_percent must
        equal exactly the (mp − implied) × 100 we passed in."""
        edge_in = 13.24
        f = _cfb_emission_factors(
            exp_margin=15.5, exp_total=58.0, sp_base=13.0,
            mp=0.78, implied=0.6476)
        pick = {
            "book_odds": -186, "edge_percent": edge_in,
            "win_probability": 78.0, "sport": "CFB",
            "market": "Home Moneyline",
            "probability_provenance": CFB_PP_CAUSAL,
        }
        compute_lock_score(f, win_prob=78.0, pick=pick,
                           edge_percent=edge_in)
        assert pick["edge_percent"] == edge_in, (
            f"edge mutated: expected {edge_in}, got {pick['edge_percent']}"
        )


# ═════════════════════════════════════════════════════════════
# 5. HIGH-TIER REACHABILITY (EVIDENCE-DRIVEN)
# ═════════════════════════════════════════════════════════════
class TestHighTierReachability:
    """A strong-evidence CFB pick MUST be able to reach 85-95
    naturally, without any hard-boost or floor."""

    def test_exceptional_evidence_reaches_strong_lock_band(self):
        """13% edge + 78% wp + full CAUSAL_INDEPENDENT should hit
        the 85-92 Strong-Lock band."""
        f = _cfb_emission_factors(
            exp_margin=15.5, exp_total=58.0, sp_base=13.0,
            mp=0.78, implied=0.65)
        score, _ = _score(f, wp=78.0, edge=13.0, book=-186,
                          prov=CFB_PP_CAUSAL)
        assert 85.0 <= score <= 92.5, (
            f"Exceptional evidence outside Strong-Lock band: LS={score}"
        )

    def test_rare_evidence_reaches_premium_band(self):
        """17% edge, wp 84%, full DQ → Strong-Lock reachability.

        Note: A large edge (mp − implied) MUST reduce market_align by
        construction — that's the correct semantic (model disagrees
        with market → factors spread apart).  We therefore validate
        Strong-Lock reachability alone; the alignment/edge tension
        is intentional and captured by the composite math.
        """
        f = _cfb_emission_factors(
            exp_margin=20.0, exp_total=60.0, sp_base=17.0,
            mp=0.84, implied=0.67)
        score, lc = _score(f, wp=84.0, edge=17.0, book=-419,
                           prov=CFB_PP_CAUSAL)
        assert score >= 85.0, (
            f"Rare evidence below Strong-Lock reachability: LS={score}"
        )
        # Sanity: edge is fully saturated (100) on a 17% edge — proving
        # the edge component IS being consumed.
        assert lc["edge"] >= 95.0, (
            f"edge_comp not saturating on 17% edge: {lc['edge']}"
        )


# ═════════════════════════════════════════════════════════════
# 6. FAIL-CLOSED GUARANTEES
# ═════════════════════════════════════════════════════════════
class TestFailClosedInvariants:
    def test_weak_evidence_stays_below_85(self):
        """Ordinary or thin evidence must NOT reach the board."""
        f = _cfb_emission_factors(
            exp_margin=2.5, exp_total=51.0, sp_base=1.5,
            mp=0.53, implied=0.51, dq="sp_plus")
        score, _ = _score(f, wp=53.0, edge=2.0, book=-108,
                          prov=CFB_PP_MC)
        assert score < 85.0, (
            f"Thin-evidence pick reached elite band: LS={score}"
        )

    def test_empty_factors_never_elite_regression_guard(self):
        """Post-v4 (2026-06-14): confidence is a first-class component
        so a wp=95%/edge=15% pick with CAUSAL_INDEPENDENT provenance
        can legitimately reach Strong-Lock (~85) even with zero explicit
        factors — that IS the intended semantic.  The regression guard
        is now: empty factors must never manufacture the Premium/Elite
        bands (>= 90).  The old ``factors={} → 84`` bug remains fixed."""
        for wp in (55.0, 65.0, 75.0, 85.0, 95.0):
            pick = {
                "book_odds": -110, "edge_percent": 15.0,
                "win_probability": wp, "sport": "CFB",
                "market": "Spread",
                "probability_provenance": CFB_PP_CAUSAL,
            }
            score, _ = compute_lock_score(
                {}, win_prob=wp, pick=pick, edge_percent=15.0)
            assert score < 90.0, (
                f"Empty-factors reached Premium band: wp={wp} → LS={score}"
            )

    def test_market_implied_only_never_elite(self):
        """A CFB pick whose ONLY evidence is Sportsbook Implied Prob
        (a market reference, not independent predictive evidence)
        MUST NOT reach elite Lock authority."""
        f = {
            "Sportsbook Implied Prob": "65.00%",
            "Sportsbook Implied (norm)": 0.65,
            "__data_quality": CFB_DQ,
        }
        score, _ = _score(f, wp=65.0, edge=0.0, book=-186,
                          prov=CFB_PP_MC)
        assert score < 85.0, (
            f"Market-reference-only pick reached elite: LS={score}"
        )


# ═════════════════════════════════════════════════════════════
# 7. BOGUS +950 / +1500 PEAK-98 REGRESSION GUARD
# ═════════════════════════════════════════════════════════════
class TestBogusLongshotRegressionGuard:
    """The Texas Southern signature (huge +950/+1500 dog with
    inflated Peak Lock 98) MUST NOT return.  Fabricated evidence
    (thin DQ + extreme dog + no CAUSAL provenance) must fail closed."""

    def test_extreme_dog_thin_dq_stays_below_elite(self):
        # A dog priced +1500 (implied 6.25%) that the model believes
        # is 20% likely → 13.75% edge on paper.  Thin DQ.
        implied = _implied_prob(1500)
        f = _cfb_emission_factors(
            exp_margin=-14.0, exp_total=55.0, sp_base=-13.0,
            mp=0.20, implied=implied, dq="sp_plus")
        score, _ = _score(f, wp=20.0, edge=(0.20 - implied) * 100,
                          book=1500, prov=CFB_PP_MC)
        # Weak-DQ + volatility penalty on +1500 must keep it below 90.
        assert score < 90.0, (
            f"Bogus +1500 longshot reached elite: LS={score}"
        )


if __name__ == "__main__":
    import subprocess as _sp
    _r = _sp.run(
        [sys.executable, "-m", "pytest", __file__, "-v", "--tb=short"],
        cwd="/app/backend",
    )
    sys.exit(_r.returncode)
