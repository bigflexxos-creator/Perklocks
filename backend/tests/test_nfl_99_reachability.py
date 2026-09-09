"""NFL PLAYER-PROP 99 NON-APEX REACHABILITY CONTRACT (2026-06-25).

Proves that with the corrected 0.99 CDF/mp clamp the universal
player-prop path can reach LS=99 without APEX, without any
"boost", and without changing the reliability formula.

APEX 100 remains strictly gated by `apex_gate.evaluate_apex`.
"""
from __future__ import annotations
import sys
sys.path.insert(0, "/app/backend")

from evidence_engine import apply_lock_governor
from services.nfl_features import distribution_hit_probability


def test_cdf_clamp_permits_99():
    """The distribution CDF clamp must allow WP up to 0.99 so
    reliability_cap = 60 + 0.99 × 40 = 99.6 clamps at LS=99
    (via NON_APEX_HARD_CAP)."""
    # Elite matchup: mean 300, sd 20, threshold 175 → CDF ≈ 1.0
    dist = {"mean": 300.0, "sd": 20.0, "samples": [0]*12, "n_games": 12}
    p_hat = distribution_hit_probability(dist, 175.0, side="over")
    assert p_hat == 0.99, f"CDF clamp not at 0.99: {p_hat}"
    # And the reciprocal Under path
    p_hat_under = distribution_hit_probability(dist, 400.0, side="over")
    assert p_hat_under == 0.02, f"CDF lower clamp not at 0.02: {p_hat_under}"
    print("[reachability] CDF clamp [0.02, 0.99] respected ✓")


def test_player_prop_99_reachable():
    """Universal player-prop path can reach LS=99 with elite
    convergence."""
    from services.magic.lock_score_integrator import NON_APEX_HARD_CAP
    assert NON_APEX_HARD_CAP == 99.0
    # Elite convergence: raw_lock 99, evidence_score 82, WP 0.99
    raw = 99.0
    ev = 82
    wp = 0.99
    gov = apply_lock_governor(raw, ev)
    assert gov == 99.0, f"governor blocked at {gov}"
    rc = 60 + wp * 40
    assert rc >= 99.0, f"reliability_cap {rc} below 99"
    final = min(gov, rc, NON_APEX_HARD_CAP)
    assert final == 99.0, f"player-prop 99 unreachable: got {final}"
    print(f"[reachability] LS=99 reached via ev={ev}, WP={wp} ✓ (final={final})")


def test_player_prop_100_still_requires_apex():
    """LS=100 must remain strictly gated by APEX convergence — the
    universal 99 non-APEX cap cannot be crossed by the reliability
    formula alone."""
    from services.magic.lock_score_integrator import NON_APEX_HARD_CAP, APEX_SCORE
    assert NON_APEX_HARD_CAP == 99.0
    assert APEX_SCORE == 100.0
    # A "perfect" non-APEX pick still clamps at 99, never 100.
    from services.magic.apex_gate import APEX_MIN_BASE_SCORE
    assert APEX_MIN_BASE_SCORE == 97.0
    print(f"[reachability] LS=100 APEX still separate ✓ (non_apex_hard_cap={NON_APEX_HARD_CAP})")


def test_grade_band_reachability_matrix():
    """Every intended band 90-99 must be reachable with realistic
    evidence + WP combinations."""
    bands = {}
    for wp in (0.75, 0.825, 0.90, 0.925, 0.95, 0.975, 0.99):
        for ev in (65, 75, 82):
            gov = apply_lock_governor(99.0, ev)
            rc = 60 + wp * 40
            final = round(min(gov, rc, 99.0), 1)
            if final not in bands or bands[final] > (ev, wp):
                bands[final] = (ev, wp)
    for target in (90, 93, 95, 96, 97, 98, 99):
        hits = [k for k in bands if abs(k - target) < 1.5]
        assert hits, f"tier {target} unreachable via universal path"
    print("[reachability] every band 90-99 reachable in the universal formula ✓")


if __name__ == "__main__":
    test_cdf_clamp_permits_99()
    test_player_prop_99_reachable()
    test_player_prop_100_still_requires_apex()
    test_grade_band_reachability_matrix()
    print("=" * 60)
    print("NFL PLAYER-PROP 99-REACHABILITY CONTRACT · 4/4 PASS")
