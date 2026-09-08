"""
NFL PLAYER-PROP REACHABILITY CONTRACT (2026-06-09 · Stage 2 P0-C).

Non-production, test-only fixtures that prove the NFL player-prop scoring
path is free of every artificial ceiling from 92 → 100 APEX.  These are
mathematical scoring-path proofs; they DO NOT touch the sportsbook feed
and DO NOT create synthetic production lines.

Run:
    python -m tests.test_nfl_playerprop_reachability
    # OR
    pytest tests/test_nfl_playerprop_reachability.py -v
"""
from __future__ import annotations

import sys
sys.path.insert(0, "/app/backend")

from evidence_engine import (
    evidence_multiplier, apply_lock_governor,
)


# ────────────────────────────────────────────────────────────────────
# 1. Evidence multiplier map — 93/94/95/96/97/98/99 all reachable
# ────────────────────────────────────────────────────────────────────
def test_multiplier_map_no_92_ceiling():
    """Score 60-79 → 0.93 previously clamped base 99 to 92.1.
    The new granular map (0.93 / 0.95 / 0.97 / 1.00) unlocks the whole
    93-99 corridor from evidence scores 65+.
    """
    # Old regression: score 65 must NOT still land at 0.93.
    assert evidence_multiplier(65) > 0.93, "score 65 still at 0.93 cap"
    # Every 93-99 lock must be reachable with SOME evidence_score.
    reachable: dict[float, bool] = {v: False for v in (93, 94, 95, 96, 97, 98, 99)}
    for score in range(0, 101):
        for base in (95.0, 96.0, 97.0, 98.0, 99.0, 100.0):
            v = apply_lock_governor(base, score)
            for target in list(reachable):
                if abs(v - target) < 0.5:
                    reachable[target] = True
    missing = [k for k, ok in reachable.items() if not ok]
    assert not missing, f"unreachable lock tiers: {missing}"
    print("[reachability] multiplier map covers every 93-99 tier ✓")


# ────────────────────────────────────────────────────────────────────
# 2. Lock ≠ Value — high hit-rate + neutral edge earns elite Lock
# ────────────────────────────────────────────────────────────────────
def test_lock_and_value_are_independent_dimensions():
    """A canonically-modelled QB Over 199.5 Passing Yards priced at -650
    (imp 0.867) with an INDEPENDENT model P̂ = 0.895 (5+pp lift over book)
    and 3 confirming signals must earn ELITE Lock even though the edge
    (~+3%) reads as Neutral Value.  Value/Sim-Edge lanes surface the
    high-edge longshots separately.
    """
    # Simulate the numeric contract the runtime enforces on such a pick:
    # base is the Magic block-8 refined score; evidence_score reflects the
    # convergence of 3 confirming signals + independent probability.
    base = 99.0
    evidence_score = 82        # 3 aligned signals + independent P̂
    lock = apply_lock_governor(base, evidence_score)
    assert lock >= 98.0, f"lock={lock}  strong-evidence path did not reach 98+"
    print(f"[reachability] strong-evidence path lock={lock:.1f} ✓ (Elite)")


# ────────────────────────────────────────────────────────────────────
# 3. Contract: 100 APEX still requires exceptional convergence
# ────────────────────────────────────────────────────────────────────
def test_apex_gate_still_strict():
    """APEX must remain rare — the 97+ base + ALIGNED_STRONG magic tier
    + zero contradictions + ≥5/6 category votes + role/context + market
    intel + eligible sport/market gate must ALL clear.
    """
    from services.magic.apex_gate import (
        APEX_MIN_BASE_SCORE, APEX_ELIGIBLE_SPORTS,
    )
    assert APEX_MIN_BASE_SCORE == 97.0
    assert "NFL" in APEX_ELIGIBLE_SPORTS
    from services.magic.lock_score_integrator import (
        NON_APEX_HARD_CAP, APEX_SCORE,
    )
    assert NON_APEX_HARD_CAP == 99.0
    assert APEX_SCORE == 100.0
    print("[reachability] APEX gate contract preserved ✓ "
          f"(min_base={APEX_MIN_BASE_SCORE}, cap_non_apex={NON_APEX_HARD_CAP})")


# ────────────────────────────────────────────────────────────────────
# 4. Chalk-trap fail-closed on book-copy probability
# ────────────────────────────────────────────────────────────────────
def test_chalk_trap_fires_on_book_copy_probability():
    """A pick whose model probability is a mirror of book_implied AND
    lacks 3 corroborating signals + 8pp edge MUST be trap-demoted at
    the read layer.  This defends against a -1400 chalk lock that
    would otherwise sneak Elite tier purely because the book set the
    line so heavy.
    """
    from services.chalk_trap import apply_chalk_kill_switch
    pick = {
        "book_odds": -1400,
        "edge_percent": 0.0,           # book copy
        "win_probability": 93.3,
        "implied_probability": 93.3,
        "is_alt": True,
        "lock_score": 84.1,
        "lock_score_v2": 84.1,
        "market": "Test QB Over 174.5 Player Pass Yds  · ALT LOCK",
        "sport": "NFL",
        "data_driven_contribs": {},    # zero DD signals
        "mp_from_book_seed": True,
    }
    stats = apply_chalk_kill_switch([pick])
    assert stats["trapped"] >= 1, f"chalk_trap did not fire: {stats}"
    assert pick["chalk_trap"] is True
    assert pick["lock_score"] <= 72.0
    print("[reachability] chalk-trap fail-closed on book-copy probability ✓")


# ────────────────────────────────────────────────────────────────────
# 5. Chalk-trap SPARES independently-modelled high-hit alt
# ────────────────────────────────────────────────────────────────────
def test_chalk_trap_spares_independent_model():
    """An alt with book_odds ≤ -250 but ≥ 2pp genuine positive edge
    (independent model beats book) must PASS the trap (see
    chalk_trap.py L129 spare_alt clause).  This proves the trap only
    hits book-copies, not independently-modelled favorites."""
    from services.chalk_trap import apply_chalk_kill_switch
    pick = {
        "book_odds": -320,
        "edge_percent": 4.5,           # independent model +4.5pp
        "win_probability": 82.0,
        "implied_probability": 76.2,
        "is_alt": True,
        "lock_score": 95.0,
        "market": "Test WR Over 4.5 Player Receptions  · ALT LOCK",
        "sport": "NFL",
        "mp_from_book_seed": False,
        "data_driven_contribs": {"form": 0.015, "matchup": 0.02, "usage": 0.010},
    }
    stats = apply_chalk_kill_switch([pick])
    assert not pick.get("chalk_trap"), "trap fired on independent-edge alt"
    assert stats["spared_alt"] >= 1, f"spare_alt path did not fire: {stats}"
    assert pick["lock_score"] == 95.0, "lock demoted on a spared alt"
    print("[reachability] chalk-trap spared independent-model alt ✓")


# ────────────────────────────────────────────────────────────────────
# 6. Per-rung independent distribution (Stage-2 P0)
# ────────────────────────────────────────────────────────────────────
def test_distribution_hit_probability_varies_by_threshold():
    """Same coherent distribution → strictly monotone P̂ as the
    threshold slides across the ladder.  Proves the per-rung
    authority mandate — different rungs must NOT share one shared
    hit probability."""
    from services.nfl_features import distribution_hit_probability as dhp
    dist = {"n_games": 12, "mean": 262.4, "sd": 48.7,
            "min": 164, "max": 371, "samples": []}
    rungs = [174.5, 199.5, 224.5, 249.5, 265.5, 285.5, 305.5, 325.5, 350.5, 375.5]
    probs = [dhp(dist, r, "over") for r in rungs]
    print("[reachability] per-rung P̂ over Burrow-like distribution:")
    for r, p in zip(rungs, probs):
        print(f"  P̂(>= {r:>6}) = {p:.3f}")
    # Every subsequent rung MUST be strictly ≤ the prior (survival
    # function is monotone non-increasing).
    for a, b in zip(probs, probs[1:]):
        assert a >= b, f"non-monotone survival: {a} !>= {b}"
    # And the ladder must exhibit meaningful spread — a very high P̂
    # near the mean drops to a very low P̂ four sigma above.
    assert probs[0] >= 0.90, f"chalk rung P̂ too low: {probs[0]}"
    assert probs[-1] <= 0.15, f"longshot rung P̂ too high: {probs[-1]}"
    print("[reachability] per-rung distribution monotone + spread ✓")


def test_chalk_trap_spares_independent_authority_on_low_edge():
    """A -1400 chalk alt whose model probability came from the
    coherent player distribution (``mp_from_book_seed=False``) must
    NOT be trap-demoted merely because edge≈0.  This is the direct
    Burrow-174.5 regression: the model agrees with the book because
    the SAME distribution that grades every rung honestly puts the
    QB well above 174 yards — that agreement is EVIDENCE, not a
    book-copy defect."""
    from services.chalk_trap import apply_chalk_kill_switch
    pick = {
        "book_odds": -1400,
        "edge_percent": 0.4,
        "win_probability": 93.7,
        "implied_probability": 93.3,
        "is_alt": True,
        "lock_score": 96.0,
        "market": "Independent QB Over 174.5 Player Pass Yds · ALT LOCK",
        "sport": "NFL",
        "mp_from_book_seed": False,      # << key differentiator
        "data_driven_contribs": {"distribution": 0.005},
    }
    stats = apply_chalk_kill_switch([pick])
    assert not pick.get("chalk_trap"), (
        "independent-authority chalk was trap-demoted despite low edge"
    )
    assert stats.get("spared_independent", 0) >= 1, (
        f"independent-authority spare clause did not fire: {stats}"
    )
    print("[reachability] independent-authority chalk stays Elite ✓")


if __name__ == "__main__":
    test_multiplier_map_no_92_ceiling()
    test_lock_and_value_are_independent_dimensions()
    test_apex_gate_still_strict()
    test_chalk_trap_fires_on_book_copy_probability()
    test_chalk_trap_spares_independent_model()
    test_distribution_hit_probability_varies_by_threshold()
    test_chalk_trap_spares_independent_authority_on_low_edge()
    print("=" * 60)
    print("NFL PLAYER-PROP REACHABILITY CONTRACT · 7/7 PASS")
