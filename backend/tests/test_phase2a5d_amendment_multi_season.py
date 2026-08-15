"""Phase 2A.5D AMENDMENT — Multi-Season Scorer Strength.

Tests cases A–H from the directive.
"""
from __future__ import annotations

import os, sys
BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


# ═════════════════════════════════════════════════════════════════════
# CASE A — Elite prior + strong current start
# ═════════════════════════════════════════════════════════════════════
def test_case_a_elite_prior_plus_strong_current():
    from services.soccer_scorer_multi_season import (
        SeasonSample, compute_multi_season_posterior,
    )
    cur = SeasonSample(minutes=800, games=10, starts=10,
                        goals=8, xg=7.5, xa=2.0, shots=30, sot=15)
    pri = SeasonSample(minutes=3000, games=34, starts=32,
                        goals=30, xg=28.0, xa=5.0, shots=140, sot=60)
    p = compute_multi_season_posterior(current=cur, prior=pri)
    assert p.quality_profile in ("ELITE_ATTACKING_PROFILE",
                                  "STRONG_ATTACKING_PROFILE")
    assert p.xg_per_90 >= 0.60
    assert p.prior_weight > 0


# ═════════════════════════════════════════════════════════════════════
# CASE B — Elite prior + early-season sample (small)
# ═════════════════════════════════════════════════════════════════════
def test_case_b_elite_prior_early_season():
    from services.soccer_scorer_multi_season import (
        SeasonSample, compute_multi_season_posterior,
    )
    cur = SeasonSample(minutes=200, games=3, starts=3,
                        goals=1, xg=1.5, xa=0.5, shots=8, sot=3)
    pri = SeasonSample(minutes=3000, games=34, starts=32,
                        goals=28, xg=27.0, xa=6.0, shots=140, sot=55)
    p = compute_multi_season_posterior(current=cur, prior=pri)
    # Prior should DOMINATE in early season.
    assert p.prior_weight > p.current_weight * 5
    assert p.quality_profile in ("ELITE_ATTACKING_PROFILE",
                                  "STRONG_ATTACKING_PROFILE")


# ═════════════════════════════════════════════════════════════════════
# CASE C — Prior weight decays as current-season minutes grow
# ═════════════════════════════════════════════════════════════════════
def test_case_c_prior_weight_decays():
    from services.soccer_scorer_multi_season import (
        SeasonSample, compute_multi_season_posterior,
    )
    pri = SeasonSample(minutes=3000, games=34, starts=32,
                        goals=28, xg=27, xa=6, shots=140, sot=55)
    ratios = []
    for cur_min in (300, 1500, 3000, 5000):
        cur = SeasonSample(minutes=cur_min, games=cur_min // 90,
                            starts=cur_min // 100, goals=cur_min * 0.008,
                            xg=cur_min * 0.008, xa=cur_min * 0.002,
                            shots=cur_min * 0.04, sot=cur_min * 0.015)
        p = compute_multi_season_posterior(current=cur, prior=pri)
        # ratio of current weight to prior weight
        r = p.current_weight / max(1.0, p.prior_weight)
        ratios.append(r)
    # Strictly monotonic increase — current dominance grows with minutes.
    assert ratios[0] < ratios[1] < ratios[2] < ratios[3]
    # At 5000 current minutes, current dominates prior.
    assert ratios[-1] > 1.0


# ═════════════════════════════════════════════════════════════════════
# CASE D — Elite prior but confirmed OUT
# ═════════════════════════════════════════════════════════════════════
def test_case_d_elite_prior_but_out():
    from services.soccer_scorer_multi_season import (
        SeasonSample, compute_multi_season_posterior,
    )
    cur = SeasonSample(minutes=0, games=0, starts=0)
    pri = SeasonSample(minutes=3000, goals=30, xg=28, xa=6)
    p = compute_multi_season_posterior(current=cur, prior=pri,
                                        availability="out")
    assert p.reason_if_unavailable is not None
    assert p.reason_if_unavailable.startswith("PLAYER_UNAVAILABLE")
    assert p.xg_per_90 == 0.0
    assert p.minutes_total == 0.0


# ═════════════════════════════════════════════════════════════════════
# CASE F — Breakout player (data-derived, no whitelist)
# ═════════════════════════════════════════════════════════════════════
def test_case_f_breakout_player_from_evidence():
    from services.soccer_scorer_multi_season import (
        SeasonSample, compute_multi_season_posterior,
    )
    # Prior: modest.
    pri = SeasonSample(minutes=2000, games=25, starts=15,
                        goals=4, xg=4.0, xa=2, shots=35, sot=14)
    # Current: sustained elite sample.
    cur = SeasonSample(minutes=2400, games=27, starts=27,
                        goals=25, xg=24, xa=8, shots=130, sot=55)
    p = compute_multi_season_posterior(current=cur, prior=pri)
    # Posterior should have risen substantially over prior alone.
    assert p.quality_profile in ("STRONG_ATTACKING_PROFILE",
                                  "ELITE_ATTACKING_PROFILE",
                                  "ABOVE_AVERAGE"), (
        f"breakout must improve profile, got {p.quality_profile}"
    )
    # xG/90 posterior must exceed the prior alone (prior xg/90 ≈ 0.18).
    assert p.xg_per_90 > 0.45


# ═════════════════════════════════════════════════════════════════════
# CASE G — Short hot streak does NOT create fake elite
# ═════════════════════════════════════════════════════════════════════
def test_case_g_short_hot_streak_shrunk():
    from services.soccer_scorer_multi_season import (
        SeasonSample, compute_multi_season_posterior,
    )
    # Prior: average.
    pri = SeasonSample(minutes=2500, games=28, starts=15,
                        goals=5, xg=5.5, xa=2, shots=40, sot=15)
    # Current: 4 goals in 4 games off low xG (~ 1.2 xG).
    cur = SeasonSample(minutes=360, games=4, starts=4,
                        goals=4, xg=1.2, xa=0.4, shots=12, sot=6)
    p = compute_multi_season_posterior(current=cur, prior=pri)
    # Blended xG/90 should NOT be elite (prior dominates in small sample).
    assert p.xg_per_90 < 0.55, (
        f"short hot streak must not promote to elite, got {p.xg_per_90}"
    )


# ═════════════════════════════════════════════════════════════════════
# CASE H — Club transfer applies uncertainty
# ═════════════════════════════════════════════════════════════════════
def test_case_h_club_transfer_env_shift():
    from services.soccer_scorer_multi_season import (
        SeasonSample, compute_multi_season_posterior,
    )
    pri = SeasonSample(minutes=3000, goals=25, xg=24, xa=7,
                        shots=130, sot=55,
                        team="Team Old", league="Serie A")
    cur = SeasonSample(minutes=500, games=6, starts=6, goals=3,
                        xg=3.5, xa=1.0, shots=20, sot=8,
                        team="Team New", league="Serie A")
    p_no_transfer = compute_multi_season_posterior(
        current=cur, prior=pri, current_team="Team Old",
        current_league="Serie A")
    p_transfer = compute_multi_season_posterior(
        current=cur, prior=pri, current_team="Team New",
        current_league="Serie A")
    # env_shift active on transfer.
    assert p_transfer.env_shift < 1.0
    # Historical ability retained but attenuated.
    assert p_transfer.xg_per_90 < p_no_transfer.xg_per_90


# ═════════════════════════════════════════════════════════════════════
# No hardcoded star names — same evidence yields same profile
# ═════════════════════════════════════════════════════════════════════
def test_no_name_bias_two_players_same_evidence_same_profile():
    from services.soccer_scorer_multi_season import (
        SeasonSample, compute_multi_season_posterior,
    )
    stats = dict(minutes=3000, games=34, starts=32,
                  goals=28, xg=27, xa=6, shots=140, sot=55)
    prior_a = SeasonSample(**stats, team="Team A", league="EPL")
    prior_b = SeasonSample(**stats, team="Team B", league="EPL")
    cur = SeasonSample(minutes=500, xg=4, goals=4)
    pa = compute_multi_season_posterior(current=cur, prior=prior_a,
                                          current_team="Team A",
                                          current_league="EPL")
    pb = compute_multi_season_posterior(current=cur, prior=prior_b,
                                          current_team="Team B",
                                          current_league="EPL")
    assert pa.quality_profile == pb.quality_profile
    assert abs(pa.xg_per_90 - pb.xg_per_90) < 1e-6


# ═════════════════════════════════════════════════════════════════════
# Bridge integration — prior_form_row wires through
# ═════════════════════════════════════════════════════════════════════
def test_bridge_accepts_prior_form_row_and_updates_profile():
    from services.soccer_scorer_bridge import compute_soccer_scorer_factors_sync
    r_no_prior = compute_soccer_scorer_factors_sync(
        player="Test Player",
        market_key="player_goal_scorer_anytime",
        book_implied=0.30,
        form_row={"xg": 1.5, "goals": 1, "minutes": 180, "games": 3,
                  "starts": 3, "position": "FW", "form_score": 60,
                  "shots_per_90": 2.5, "sot_per_90": 1.0},
        league="EPL",
    )
    r_with_prior = compute_soccer_scorer_factors_sync(
        player="Test Player",
        market_key="player_goal_scorer_anytime",
        book_implied=0.30,
        form_row={"xg": 1.5, "goals": 1, "minutes": 180, "games": 3,
                  "starts": 3, "position": "FW", "form_score": 60,
                  "shots_per_90": 2.5, "sot_per_90": 1.0},
        prior_form_row={"xg": 25, "goals": 24, "minutes": 2900,
                        "games": 33, "starts": 32,
                        "shots": 135, "sot": 55, "xa": 6},
        league="EPL",
    )
    assert r_no_prior is not None and r_with_prior is not None
    assert r_with_prior.get("multi_season_profile") in (
        "ELITE_ATTACKING_PROFILE", "STRONG_ATTACKING_PROFILE"
    )
    # engine_version tag reflects the amendment.
    assert "multi_season" in (r_with_prior.get("engine_version") or "")


def test_bridge_no_prior_row_backwards_compatible():
    from services.soccer_scorer_bridge import compute_soccer_scorer_factors_sync
    r = compute_soccer_scorer_factors_sync(
        player="X", market_key="player_goal_scorer_anytime",
        book_implied=0.30,
        form_row={"xg": 8, "goals": 9, "minutes": 2000, "games": 25,
                  "starts": 22, "position": "FW", "form_score": 68},
        league="MLS",
    )
    assert r is not None
    # Backwards compat — no multi_season_profile when no prior data.
    assert r.get("multi_season_profile") is None


# ═════════════════════════════════════════════════════════════════════
# No Lock Score manipulation
# ═════════════════════════════════════════════════════════════════════
def test_multi_season_does_not_boost_lock_score_directly():
    """The amendment must NOT introduce any Lock Score anchor or floor.
    Multi-season improves MODEL probability + evidence quality, not the
    composite."""
    src = open(os.path.join(BACKEND, "services",
                             "soccer_scorer_multi_season.py"), "r").read()
    # Guard against forbidden CODE patterns (docstring mentions are fine).
    for forbidden in ("min(88.", "min(90.",
                       "lock_score =", "elite_boost",
                       "lock += ", "+ 10"):
        assert forbidden not in src, (
            f"multi-season module must not manipulate Lock Score: {forbidden}"
        )
    # Also assert the module never imports compute_lock_score.
    assert "compute_lock_score" not in src


# ═════════════════════════════════════════════════════════════════════
# Phase 2A.5 / 2A.5B / 2A.5C preservation
# ═════════════════════════════════════════════════════════════════════
def test_scorer_bridge_and_game_model_still_intact():
    from services.soccer_scorer_bridge import compute_soccer_scorer_factors_sync
    from services.soccer_game_model import estimate_soccer_game_probabilities
    from services.board_visibility import _canonical_grade
    assert compute_soccer_scorer_factors_sync is not None
    assert estimate_soccer_game_probabilities is not None
    assert _canonical_grade(87.0) == "Playable"
