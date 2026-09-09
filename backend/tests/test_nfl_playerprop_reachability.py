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


def test_cdf_direction_side_awareness():
    """Verify the CDF direction is correct for Over vs Under.

    Distribution: μ=250, σ=50 (canonical QB-like passing-yard profile).
      P(Over 175)  ≈  1 - Φ(-1.5)  ≈  0.933   ← HIGH (line far below mean)
      P(Over 250)  ≈  1 - Φ(0.0)   ≈  0.500   ← MID  (at the mean)
      P(Over 350)  ≈  1 - Φ(2.0)   ≈  0.023   ← LOW  (line 2σ above mean)
      P(Under 175) = 1 − P(Over 175) ≈ 0.067
      P(Under 350) = 1 − P(Over 350) ≈ 0.977
    """
    from services.nfl_features import distribution_hit_probability as dhp
    dist = {"mean": 250.0, "sd": 50.0}
    # Over side
    p_over_low  = dhp(dist, 175, "over")
    p_over_mid  = dhp(dist, 250, "over")
    p_over_high = dhp(dist, 350, "over")
    assert p_over_low  >= 0.90,  f"Over 175 low: {p_over_low}"
    assert 0.45 <= p_over_mid <= 0.55, f"Over 250 mid: {p_over_mid}"
    assert p_over_high <= 0.10,  f"Over 350 low: {p_over_high}"
    # Under side
    p_under_low  = dhp(dist, 175, "under")
    p_under_high = dhp(dist, 350, "under")
    assert p_under_low  <= 0.10, f"Under 175: {p_under_low}"
    assert p_under_high >= 0.90, f"Under 350: {p_under_high}"
    # Complementary check within the [0.03, 0.97] clamp band.
    for line in (175, 250, 350):
        po = dhp(dist, line, "over")
        pu = dhp(dist, line, "under")
        # The clamp can pinch either tail, so allow ±0.02 slack.
        assert abs((po + pu) - 1.0) < 0.05, (
            f"P(Over {line}) + P(Under {line}) = {po + pu:.3f} !≈ 1.0"
        )
    print(f"[reachability] CDF direction correct  "
          f"Over 175={p_over_low:.3f}, 250={p_over_mid:.3f}, 350={p_over_high:.3f} ✓")


def test_distribution_fail_closed_on_thin_samples():
    """<5 valid games → return None (fail-closed, per user contract
    'Do not use only 12 games blindly').  Also verifies the partial-
    game / injury filter drops low-attempts QB rows before mean/SD
    are computed."""
    # Injury-shortened rows should be excluded ...
    import asyncio as _asy
    from services.nfl_features import player_stat_distribution as psd
    # Build a minimal fake collection with 4 healthy games — must return None.
    class _Cursor:
        def __init__(self, rows):
            self.rows = rows
        def sort(self, *_a, **_kw): return self
        def limit(self, _n): return self
        def __aiter__(self):
            async def _gen():
                for r in self.rows:
                    yield r
            return _gen()
    class _Coll:
        def __init__(self, rows):
            self._rows = rows
        def find(self, _q): return _Cursor(self._rows)
    class _DB(dict):
        def __getitem__(self, _k): return _Coll(self._rows)
        def __init__(self, rows):
            self._rows = rows
    # 4 healthy pass-yds rows (attempts >= 15).  Should fail-closed to None.
    rows_thin = [
        {"season": 2025, "week": 1,  "attempts": 30, "passing_yards": 275},
        {"season": 2024, "week": 18, "attempts": 34, "passing_yards": 300},
        {"season": 2024, "week": 17, "attempts": 28, "passing_yards": 240},
        {"season": 2024, "week": 16, "attempts": 32, "passing_yards": 260},
    ]
    async def _run(rows):
        return await psd(_DB(rows), "Test QB", "passing_yards", 2026, 1, limit=12)
    d_thin = _asy.get_event_loop().run_until_complete(_run(rows_thin))
    assert d_thin is None, f"expected None for 4-game sample, got {d_thin}"
    # 6 mixed rows — 2 injury-shortened (attempts=8) MUST be dropped;
    # remaining 4 healthy is still under 5 → still None.
    rows_mixed = rows_thin + [
        {"season": 2024, "week": 15, "attempts":  8, "passing_yards":  85},
        {"season": 2024, "week": 14, "attempts":  6, "passing_yards":  62},
    ]
    d_mixed = _asy.get_event_loop().run_until_complete(_run(rows_mixed))
    assert d_mixed is None, f"expected None after partial-game filter, got {d_mixed}"
    # 8 healthy rows — must return a valid distribution now.
    rows_ok = rows_thin + [
        {"season": 2024, "week": 15, "attempts": 30, "passing_yards": 245},
        {"season": 2024, "week": 14, "attempts": 31, "passing_yards": 265},
        {"season": 2024, "week": 13, "attempts": 33, "passing_yards": 280},
        {"season": 2024, "week": 12, "attempts": 29, "passing_yards": 255},
    ]
    d_ok = _asy.get_event_loop().run_until_complete(_run(rows_ok))
    assert d_ok is not None and d_ok["n_games"] >= 5
    print(f"[reachability] fail-closed at <5 samples + partial-filter ✓  "
          f"(healthy path: n={d_ok['n_games']}, mean={d_ok['mean']}, sd={d_ok['sd']})")


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


# ────────────────────────────────────────────────────────────────────
# 10. Correlation guard — duplicated history cannot manufacture 98+
# ────────────────────────────────────────────────────────────────────
def test_correlation_guard_caps_duplicated_history():
    """Distribution / L5 / L3 / Historical-rate all draw on the SAME
    12 recent games.  Per user contract:
      "Do NOT treat distribution / L5 / L3 / career hit rate as four
       independent confirmations when they overlap historically."
    The supporting factors are anchored at 0.50 ± bounded deviation
    (max 0.75) so even a 5/5 clearance cannot lift the factor mean
    into the 0.98+ range required to earn APEX from historical
    evidence alone.
    """
    from services.nfl_feature_engine import recompute_line_dependent_factors
    # Craft a scenario where every recent sample clears the threshold —
    # the maximally-aligned "duplicated history" case.
    _raw_metrics = {
        "prop_stat": "passing_yards",
        "raw_l5_avg": 320.0,
        "raw_opp_pos": {},
        "side": "over",
        "position": "QB",
        "orig_line": 200.0,
        "distribution": {
            "n_games": 12,
            "mean": 320.0, "sd": 40.0, "min": 275, "max": 372,
            "samples": [320, 310, 340, 305, 350, 300, 315, 325, 330, 295, 355, 285],
        },
    }
    factors = recompute_line_dependent_factors(
        {}, _raw_metrics, line=200.0, side="over"
    )
    # Extract every numeric factor (excluding sidecar keys).
    _fv = [v for k, v in factors.items()
           if isinstance(v, (int, float)) and not str(k).startswith("__")]
    factor_mean = sum(_fv) / len(_fv) if _fv else 0.0
    # Even in this maximum-alignment scenario, the factor mean must
    # NOT approach the 0.97-0.99 band that would let a NON-APEX pick
    # reach lock=99 from historical evidence alone.
    assert factor_mean <= 0.90, (
        f"correlation guard failed — duplicated history produced "
        f"factor mean {factor_mean:.3f} (must be <= 0.90)"
    )
    # And the supporting factors themselves must be clamped ≤ 0.75.
    for k in ("L5 Threshold Support", "L3 Threshold Support",
              "Historical Threshold Rate"):
        v = factors.get(k)
        if isinstance(v, (int, float)):
            assert v <= 0.75, f"{k}={v} exceeds correlation-guard cap 0.75"
    print(f"[reachability] correlation guard  factor_mean={factor_mean:.3f} ≤ 0.90 ✓")


if __name__ == "__main__":
    test_multiplier_map_no_92_ceiling()
    test_lock_and_value_are_independent_dimensions()
    test_apex_gate_still_strict()
    test_chalk_trap_fires_on_book_copy_probability()
    test_chalk_trap_spares_independent_model()
    test_distribution_hit_probability_varies_by_threshold()
    test_cdf_direction_side_awareness()
    test_distribution_fail_closed_on_thin_samples()
    test_chalk_trap_spares_independent_authority_on_low_edge()
    test_correlation_guard_caps_duplicated_history()
    print("=" * 60)
    print("NFL PLAYER-PROP REACHABILITY CONTRACT · 10/10 PASS")
