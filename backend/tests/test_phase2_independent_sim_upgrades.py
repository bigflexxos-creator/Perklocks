"""PHASE 2 CONTINUATION — Independent-simulator upgrade regressions.

Proves NBA / Soccer game / Tennis simulators produce
EMPIRICAL_INDEPENDENT provenance when real matchup evidence is
threaded in (recent gamelog rows / team-form ctx / surface Elo ctx)
while the legacy MODEL_CONDITIONED fallback is preserved for callers
that don't supply the ctx.

No provider calls, no DB writes — all in-process fixtures.
"""
from __future__ import annotations


# ─────────────────────────────────────────────────────────────────────
# §1 — NBA upgrade: real L10 rows → EMPIRICAL_INDEPENDENT.
# ─────────────────────────────────────────────────────────────────────
def test_nba_with_real_gamelogs_is_empirical_independent():
    from brain.sim_nba import simulate_nba_pick
    recent = [
        {"points": 27, "rebounds": 8, "assists": 6, "minutes": 35,
         "usage": 28, "pace": 100, "rest_days": 1},
        {"points": 31, "rebounds": 7, "assists": 8, "minutes": 36,
         "usage": 30, "pace": 102, "rest_days": 2},
        {"points": 24, "rebounds": 9, "assists": 5, "minutes": 34,
         "usage": 27, "pace": 98,  "rest_days": 1},
        {"points": 29, "rebounds": 6, "assists": 7, "minutes": 35,
         "usage": 29, "pace": 101, "rest_days": 2},
    ]
    out = simulate_nba_pick(
        {"sport": "NBA", "market": "LeBron Over 25.5 Points",
         "win_probability": 60.0}, recent_rows=recent,
    )
    assert out is not None
    assert out["provenance"] == "EMPIRICAL_INDEPENDENT"
    # λ should reflect real recent mean (~27.75) — NOT back-solve to
    # match model_wp.  If the sim was still MODEL_CONDITIONED it would
    # have calibrated to model_wp=60% at line 25.5 which requires a
    # very different λ.
    assert 25.0 <= out["sim_lambda"] <= 32.0


def test_nba_without_gamelogs_stays_model_conditioned():
    from brain.sim_nba import simulate_nba_pick
    out = simulate_nba_pick(
        {"sport": "NBA", "market": "LeBron Over 25.5 Points",
         "win_probability": 60.0},
    )
    assert out is not None
    assert out["provenance"] == "MODEL_CONDITIONED"


# ─────────────────────────────────────────────────────────────────────
# §2 — Soccer game upgrade: real team-form ctx → EMPIRICAL_INDEPENDENT
# via the authoritative services.soccer_game_model.
# ─────────────────────────────────────────────────────────────────────
def test_soccer_game_with_real_form_ctx_is_empirical_independent():
    from brain.sim_soccer import simulate_soccer_pick
    soccer_ctx = {
        "home_form": {"gf_avg": 2.1, "ga_avg": 0.9, "n_matches": 12},
        "away_form": {"gf_avg": 1.4, "ga_avg": 1.3, "n_matches": 12},
    }
    out = simulate_soccer_pick(
        {"sport": "Soccer",
         "market": "Man City Moneyline",
         "win_probability": 55.0,
         "event": "Man City vs Arsenal",
         "selection": "Man City",
         "factors": {}},
        soccer_ctx=soccer_ctx,
    )
    assert out is not None
    assert out["provenance"] == "EMPIRICAL_INDEPENDENT"
    assert out["sim_lambda_derivation"].startswith("authoritative_tier_")


def test_soccer_game_without_ctx_stays_model_conditioned():
    from brain.sim_soccer import simulate_soccer_pick
    out = simulate_soccer_pick(
        {"sport": "Soccer", "market": "Man City Moneyline",
         "win_probability": 55.0,
         "event": "Man City vs Arsenal",
         "factors": {"xG Combined": 60.0, "xG Difference": 55.0,
                     "Defensive Form": 60.0, "Home Advantage": 60.0}},
    )
    assert out is not None
    assert out["provenance"] == "MODEL_CONDITIONED"
    assert out["sim_lambda_derivation"] == "factors"


# ─────────────────────────────────────────────────────────────────────
# §3 — Tennis upgrade: surface Elo + hold/break ctx → EMPIRICAL.
# ─────────────────────────────────────────────────────────────────────
def test_tennis_with_elo_ctx_is_empirical_independent():
    from brain.sim_tennis import simulate_tennis_pick
    tennis_ctx = {
        "home": "Carlos Alcaraz", "away": "Novak Djokovic",
        "surface": "clay",
        "surface_elo_a": 2200, "surface_elo_b": 2100,
        "sackmann_a": {"win_pct": 82, "first_serve_won_pct": 78,
                        "hold_pct": 86, "break_saved_pct": 70},
        "sackmann_b": {"win_pct": 75, "first_serve_won_pct": 74,
                        "hold_pct": 82, "break_saved_pct": 66},
    }
    out = simulate_tennis_pick(
        {"sport": "Tennis", "market": "Alcaraz Moneyline",
         "selection": "Carlos Alcaraz",
         "home_team": "Carlos Alcaraz",
         "away_team": "Novak Djokovic",
         "win_probability": 50.0},
        tennis_ctx=tennis_ctx,
    )
    assert out is not None
    assert out["provenance"] == "EMPIRICAL_INDEPENDENT"
    assert out["sim_serve_derivation"] == "elo_hold_break"
    assert out["input_quality"] in ("FULL", "STRONG", "PARTIAL")


def test_tennis_without_ctx_stays_model_conditioned():
    from brain.sim_tennis import simulate_tennis_pick
    out = simulate_tennis_pick(
        {"sport": "Tennis", "market": "Alcaraz Moneyline",
         "selection": "Carlos Alcaraz", "win_probability": 72.0},
    )
    assert out is not None
    assert out["provenance"] == "MODEL_CONDITIONED"
    assert out["sim_serve_derivation"] == "model_calibrated"
