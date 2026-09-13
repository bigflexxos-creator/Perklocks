"""Compression-rollback regression guards (2026-09-13).

These tests lock in the surgical rollback of the over-aggressive BQ
authority compression that was crushing the entire Locks board.
Every test proves either:
  (a) the compression is gone (mid-composite MLB/CFB picks with
      strong multi-signal evidence can legitimately reach the
      90-99 band), OR
  (b) the working infrastructure fixes did NOT regress (NFL alt
      preservation, MLB weak-evidence filtering, WP-only picks
      still can't inflate).
"""
from __future__ import annotations
import sys, pathlib
_BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from sports_engine import compute_lock_score


# ─────────────────────────────────────────────────────────────
# CFB — Alabama-State-type strong ML must NOT collapse to LS≈68
# ─────────────────────────────────────────────────────────────
def test_cfb_strong_ml_uncompressed():
    factors = {
        "Projected Margin (norm)":   0.9097,
        "Expected Total (norm)":     0.7729,
        "Model Fair Prob (norm)":    0.91,
        "Sportsbook Implied (norm)": 0.0909,   # market anchor, excluded
        "SP+ Rating Δ (norm)":       0.9097,
    }
    pick = {"book_odds": -300, "edge_percent": 33.5,
             "win_probability": 91.0,
             "sport": "CFB", "market": "Alabama State ML",
             "data_quality": "sp_plus",
             "probability_provenance": "MODEL_CONDITIONED"}
    ls, _ = compute_lock_score(factors, win_prob=91.0, pick=pick,
                                 edge_percent=33.5)
    assert ls >= 88.0, f"strong CFB ML must not collapse; got {ls}"


def test_market_anchor_excluded_from_scoring():
    """Sportsbook Implied (norm) must not participate in convergence /
    market-align computation — it's a book reference, not evidence."""
    # Case A: presence of the market anchor
    with_anchor = {
        "Projected Margin (norm)": 0.90,
        "SP+ Rating Δ (norm)":     0.90,
        "Model Fair Prob (norm)":  0.90,
        "Sportsbook Implied (norm)": 0.10,   # opposite side
    }
    pick_a = {"sport": "CFB", "market": "X ML",
               "win_probability": 90.0,
               "probability_provenance": "MODEL_CONDITIONED"}
    ls_a, _ = compute_lock_score(with_anchor, win_prob=90.0, pick=pick_a)

    # Case B: same evidence without the market anchor
    without_anchor = {
        "Projected Margin (norm)": 0.90,
        "SP+ Rating Δ (norm)":     0.90,
        "Model Fair Prob (norm)":  0.90,
    }
    pick_b = dict(pick_a)
    pick_b.pop("bet_quality_authority", None)
    ls_b, _ = compute_lock_score(without_anchor, win_prob=90.0, pick=pick_b)
    # The two must produce the SAME lock score — market anchor was
    # correctly excluded from evidence-based scoring.
    assert abs(ls_a - ls_b) < 0.5, (
        f"market anchor must not change LS; with={ls_a} without={ls_b}"
    )


# ─────────────────────────────────────────────────────────────
# BQ LIFT — mid-composite with strong evidence must LIFT toward BQ
# ─────────────────────────────────────────────────────────────
def test_bq_lift_when_evidence_exceeds_composite():
    """When multi-signal evidence produces a BQ ceiling of ≥85 that
    exceeds the composite, LS must lift toward BQ.  Compression
    rollback contract."""
    factors = {
        "Expected BA (Statcast)":       0.90,
        "Barrel% (Quality of Contact)": 0.92,
        "Hard-Hit % (Statcast)":        0.90,
        "Matchup Advantage":            0.94,
        "DFS Projection vs Line":       0.92,
    }
    pick = {"sport": "MLB", "market": "Judge Over 0.5 Hits",
             "book_odds": -300, "win_probability": 82.0,
             "data_quality": "full",
             "probability_provenance": "PROP_MODEL_PRIMARY",
             "exact_threshold_hit_rate": 0.85, "sim_stability": 0.92}
    ls, _ = compute_lock_score(factors, win_prob=82.0, pick=pick,
                                edge_percent=10.0)
    bq = pick.get("bet_quality_authority") or {}
    bq_ceil = bq.get("ceiling") or 0
    # LS must be at least min(BQ, board floor).
    assert ls >= 88.0, f"strong evidence must lift LS ≥ 88; got {ls}"
    # BQ must never LOWER a composite — lift-only contract.
    # (We can't easily observe the composite pre-BQ, but if BQ >
    # 85 and LS < BQ, that would violate the lift contract.)
    if bq_ceil >= 85.0:
        assert ls >= bq_ceil - 0.15, (
            f"BQ={bq_ceil} was not lifted into LS={ls}"
        )


# ─────────────────────────────────────────────────────────────
# NFL prop preservation — LS≥96 must survive the rollback
# ─────────────────────────────────────────────────────────────
def test_nfl_prop_96_preserved():
    factors = {"L5 Avg vs Line": 0.92, "Home/Away Split": 0.9,
                "Career vs Opponent Hit%": 0.85,
                "L5 Threshold Support": 0.88}
    pick = {"sport": "NFL", "market": "player_pass_yds",
             "model_win_prob": 94.6, "book_odds": -400,
             "is_alt_line": False}
    ls, _ = compute_lock_score(factors, win_prob=94.6, pick=pick)
    assert ls >= 96.0, f"NFL prop 96+ tier must be preserved; got {ls}"


# ─────────────────────────────────────────────────────────────
# Guardrails — inflation still blocked
# ─────────────────────────────────────────────────────────────
def test_no_wp_only_inflation():
    """A pick with high WP but ZERO evidence factors must not reach
    the 98/99 tier through WP alone."""
    ls, _ = compute_lock_score({}, win_prob=95.0,
                                 pick={"sport": "MLB", "market": "X ML"})
    assert ls < 98.0, f"WP-only pick must not reach 98; got {ls}"


def test_weak_evidence_below_floor():
    factors = {"Team Offense": 0.55, "Bullpen ERA": 0.50}
    pick = {"sport": "MLB", "market": "Yankees ML",
             "win_probability": 54.0}
    ls, _ = compute_lock_score(factors, win_prob=54.0, pick=pick,
                                edge_percent=2.0)
    assert ls < 85.0, f"weak MLB evidence must remain below 85; got {ls}"
