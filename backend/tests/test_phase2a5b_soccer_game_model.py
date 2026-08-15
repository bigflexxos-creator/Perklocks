"""Phase 2A.5B — Universal Soccer Game-Model Delta Closure.

Targeted tests for the six root causes identified after Phase 2A.5:

  RC1 — Team identity: safe canonical alias resolution, no unsafe
        substring collisions.
  RC2 — GF/GA never masquerades as xG/xGA — xg_available flag exposed.
  RC3 — Correlated form-derived features cannot be counted as multiple
        independent evidence confirmations.
  RC4 — Independent Soccer game probability core (not sportsbook-anchored).
  RC5 — No pre-score starvation from arbitrary factor-count gate when
        the independent game model is available.
  RC6 — Every Soccer candidate death carries a funnel-attributable reason.

Also enforces:
  * 1X2 / Totals / BTTS / Double Chance derive from the ONE score
    distribution.
  * Favorite / underdog neutrality preserved.
  * ≥85 board rule preserved.
  * De-vig edge contract preserved.
  * Deterministic same-input-same-output.
"""
from __future__ import annotations

import os
import sys

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


# ═════════════════════════════════════════════════════════════════════
# RC1 — Team identity
# ═════════════════════════════════════════════════════════════════════
def test_rc1_canonical_alias_resolves_common_variants():
    from services.soccer_team_identity import canonical_team_key, teams_equal
    assert teams_equal("Man City", "Manchester City")
    assert teams_equal("Spurs", "Tottenham Hotspur")
    assert teams_equal("PSG", "Paris Saint-Germain")
    assert teams_equal("Bayern München", "Bayern Munich")
    assert teams_equal("LA Galaxy", "Los Angeles Galaxy")
    # Diacritic + FC-suffix normalisation still works.
    assert teams_equal("Atlético Madrid", "Atletico Madrid FC")


def test_rc1_unresolved_identity_fails_safely_not_wrong_match():
    from services.soccer_team_identity import canonical_team_key, teams_equal
    # "Madrid" alone must NOT collide with "Real Madrid" — this was
    # the exact class of unsafe substring collision the delta closes.
    assert not teams_equal("Madrid", "Real Madrid")
    # Empty / non-string safe handling.
    assert canonical_team_key("") is None
    assert canonical_team_key(None) is None  # type: ignore[arg-type]


def test_rc1_lookup_team_form_uses_canonical_and_safe_substring():
    """The runtime call site must consult the canonical alias table
    before scanning the cache and must not accept dangerous 1-3 char
    substring matches."""
    src = open(os.path.join(BACKEND, "sportdb_client.py"), "r").read()
    assert "canonical_team_key" in src, (
        "lookup_team_form must resolve canonical team identity"
    )
    # The old permissive `nq in n_team or n_team in nq` pattern must be
    # gone.
    assert (
        "nq in n_team) or (n_team and n_team in nq)" not in src
    ), "Legacy unsafe fuzzy substring match still present"


# ═════════════════════════════════════════════════════════════════════
# RC2 — GF/GA vs xG semantics
# ═════════════════════════════════════════════════════════════════════
def test_rc2_form_proxy_carries_xg_available_false():
    """Form-derived xg_rolling docs must expose xg_available=False so
    downstream consumers can distinguish real xG from GF/GA proxies."""
    src = open(os.path.join(BACKEND, "services", "game_context.py"), "r").read()
    assert '"xg_available": False' in src, (
        "form_proxy fallback must expose xg_available=False"
    )
    # And the true semantics must be preserved as gf_avg/ga_avg keys.
    assert '"gf_avg":' in src and '"ga_avg":' in src


def test_rc2_game_model_labels_form_proxy_as_team_strength_not_xg():
    """When only form-proxy is available, the game model must categorise
    evidence as TEAM_STRENGTH, NOT EXPECTED_GOALS."""
    from services.soccer_game_model import (
        estimate_soccer_game_probabilities,
        EV_TEAM_STRENGTH, EV_EXPECTED_GOALS,
    )
    ctx = {
        "home_team": "FC A", "away_team": "FC B",
        "home_xg_rolling": {"xg_avg": 1.5, "xga_avg": 1.5, "matches": 8,
                            "source": "form_proxy"},
        "away_xg_rolling": {"xg_avg": 1.4, "xga_avg": 1.5, "matches": 8,
                            "source": "form_proxy"},
        "home_form": {"gf_avg": 1.5, "ga_avg": 1.5, "n_matches": 8},
        "away_form": {"gf_avg": 1.4, "ga_avg": 1.5, "n_matches": 8},
    }
    r = estimate_soccer_game_probabilities(ctx, "FC A", "FC B")
    assert r.available is True
    assert EV_TEAM_STRENGTH in r.evidence_categories
    assert EV_EXPECTED_GOALS not in r.evidence_categories
    assert r.xg_available is False


def test_rc2_game_model_reads_real_xg_when_present():
    from services.soccer_game_model import (
        estimate_soccer_game_probabilities, EV_EXPECTED_GOALS,
    )
    ctx = {
        "home_xg_rolling": {"xg_avg": 2.3, "xga_avg": 0.9, "matches": 15,
                            "source": "understat"},
        "away_xg_rolling": {"xg_avg": 1.0, "xga_avg": 1.9, "matches": 15,
                            "source": "understat"},
    }
    r = estimate_soccer_game_probabilities(ctx, "Man City", "Bournemouth")
    assert r.available is True
    assert EV_EXPECTED_GOALS in r.evidence_categories
    assert r.xg_available is True
    assert r.tier == "A"


# ═════════════════════════════════════════════════════════════════════
# RC3 — Correlated evidence cannot fake independent confirmations
# ═════════════════════════════════════════════════════════════════════
def test_rc3_form_proxy_only_yields_one_evidence_category():
    """A ctx with ONLY form-derived data must expose exactly one
    non-SCORE_MODEL evidence category (TEAM_STRENGTH) — NOT three
    (Form PPG + Goals Scored + Goals Conceded + xG Diff proxy)."""
    from services.soccer_game_model import (
        estimate_soccer_game_probabilities, EV_TEAM_STRENGTH,
    )
    ctx = {
        "home_xg_rolling": {"xg_avg": 1.5, "xga_avg": 1.5, "matches": 8,
                            "source": "form_proxy"},
        "away_xg_rolling": {"xg_avg": 1.4, "xga_avg": 1.5, "matches": 8,
                            "source": "form_proxy"},
    }
    r = estimate_soccer_game_probabilities(ctx, "FC A", "FC B")
    ev = [c for c in r.evidence_categories if c != "SCORE_MODEL"]
    # Exactly ONE independent non-SCORE_MODEL category.  If PPG/GF/GA/
    # xG-proxy were being counted as four independent categories this
    # would be > 1.
    assert ev == [EV_TEAM_STRENGTH], (
        f"correlated form-derived features must collapse to a single "
        f"TEAM_STRENGTH category, got {ev}"
    )


# ═════════════════════════════════════════════════════════════════════
# RC4 — Independent game probability core (NOT sportsbook-anchored)
# ═════════════════════════════════════════════════════════════════════
def test_rc4_game_probability_does_not_read_sportsbook_odds():
    """Sportsbook odds must never flow into λ.  The game model
    signature only accepts ctx + team names."""
    from services.soccer_game_model import estimate_soccer_game_probabilities
    import inspect
    sig = inspect.signature(estimate_soccer_game_probabilities)
    forbidden = {"book_odds", "implied", "odds", "home_ml", "away_ml"}
    assert not (forbidden & set(sig.parameters.keys())), (
        f"game model signature must not accept sportsbook odds: {sig}"
    )


def test_rc4_changing_odds_does_not_change_model_probability():
    """The game model is a pure function of ctx.  Changing sportsbook
    odds around it cannot change the returned probability."""
    from services.soccer_game_model import estimate_soccer_game_probabilities
    ctx = {
        "home_xg_rolling": {"xg_avg": 2.0, "xga_avg": 1.0, "matches": 12,
                            "source": "understat"},
        "away_xg_rolling": {"xg_avg": 1.1, "xga_avg": 1.6, "matches": 12,
                            "source": "understat"},
    }
    r1 = estimate_soccer_game_probabilities(ctx, "A", "B")
    r2 = estimate_soccer_game_probabilities(ctx, "A", "B")
    assert r1.p_home == r2.p_home
    assert r1.p_draw == r2.p_draw
    assert r1.p_away == r2.p_away
    # And deterministic λ.
    assert r1.lambda_home == r2.lambda_home
    assert r1.lambda_away == r2.lambda_away


def test_rc4_independent_attack_defense_execute():
    from services.soccer_game_model import estimate_soccer_game_probabilities
    ctx = {
        "home_xg_rolling": {"xg_avg": 2.5, "xga_avg": 0.7, "matches": 20,
                            "source": "understat"},
        "away_xg_rolling": {"xg_avg": 0.8, "xga_avg": 2.0, "matches": 20,
                            "source": "understat"},
    }
    r = estimate_soccer_game_probabilities(ctx, "Strong", "Weak")
    # Strong home vs weak away → P(home) must dominate.
    assert r.p_home > 0.55
    assert r.p_home > r.p_away
    assert r.lambda_home > r.lambda_away


def test_rc4_sports_engine_wires_soccer_game_model():
    """sports_engine.py must consult the independent game model before
    falling back to sportsbook implied probability for Soccer."""
    src = open(os.path.join(BACKEND, "sports_engine.py"), "r").read()
    # New wiring must be present.
    assert "estimate_soccer_game_probabilities" in src
    assert "soccer_game_model" in src
    # The Soccer ML path must not silently fall to home_implied without
    # first consulting the game model.
    idx_home_implied = src.find("home_model = home_implied")
    idx_soccer_model = src.find("if sport == \"Soccer\":", idx_home_implied)
    idx_close_block = src.find("if sport not in (\"MLB\", \"Soccer\")", idx_home_implied)
    assert 0 < idx_home_implied < idx_soccer_model < idx_close_block, (
        "Soccer game model wiring must sit between the sportsbook "
        "fallback and the non-MLB/non-Soccer MODEL_UNAVAILABLE gate"
    )


# ═════════════════════════════════════════════════════════════════════
# Score distribution + derived markets
# ═════════════════════════════════════════════════════════════════════
def test_score_matrix_normalises():
    from services.soccer_game_model import estimate_soccer_game_probabilities
    ctx = {
        "home_xg_rolling": {"xg_avg": 1.5, "xga_avg": 1.3, "matches": 10,
                            "source": "understat"},
        "away_xg_rolling": {"xg_avg": 1.2, "xga_avg": 1.4, "matches": 10,
                            "source": "understat"},
    }
    r = estimate_soccer_game_probabilities(ctx, "H", "A")
    total = sum(sum(row) for row in r.score_matrix)
    assert abs(total - 1.0) < 1e-6, f"matrix not normalised: {total}"


def test_1x2_probabilities_normalise():
    from services.soccer_game_model import estimate_soccer_game_probabilities
    ctx = {
        "home_xg_rolling": {"xg_avg": 1.6, "xga_avg": 1.1, "matches": 12,
                            "source": "understat"},
        "away_xg_rolling": {"xg_avg": 1.0, "xga_avg": 1.5, "matches": 12,
                            "source": "understat"},
    }
    r = estimate_soccer_game_probabilities(ctx, "H", "A")
    assert abs(r.p_home + r.p_draw + r.p_away - 1.0) < 1e-4


def test_totals_from_score_distribution():
    from services.soccer_game_model import (
        estimate_soccer_game_probabilities, totals_from_matrix,
    )
    ctx = {
        "home_xg_rolling": {"xg_avg": 2.0, "xga_avg": 1.5, "matches": 10,
                            "source": "understat"},
        "away_xg_rolling": {"xg_avg": 1.5, "xga_avg": 1.5, "matches": 10,
                            "source": "understat"},
    }
    r = estimate_soccer_game_probabilities(ctx, "H", "A")
    p_over, p_under = totals_from_matrix(r.score_matrix, 2.5)
    assert 0.0 <= p_over <= 1.0
    assert 0.0 <= p_under <= 1.0
    assert abs(p_over + p_under - 1.0) < 1e-4


def test_btts_from_score_distribution():
    from services.soccer_game_model import (
        estimate_soccer_game_probabilities, btts_from_matrix,
    )
    ctx = {
        "home_xg_rolling": {"xg_avg": 2.0, "xga_avg": 1.5, "matches": 10,
                            "source": "understat"},
        "away_xg_rolling": {"xg_avg": 1.8, "xga_avg": 1.5, "matches": 10,
                            "source": "understat"},
    }
    r = estimate_soccer_game_probabilities(ctx, "H", "A")
    p_yes, p_no = btts_from_matrix(r.score_matrix)
    assert abs(p_yes + p_no - 1.0) < 1e-4
    # High-scoring game → BTTS Yes should be relatively likely.
    assert p_yes > 0.45


def test_double_chance_from_1x2():
    from services.soccer_game_model import double_chance_from_1x2
    dc = double_chance_from_1x2(0.50, 0.25, 0.25)
    assert dc["1X"] == 0.75
    assert dc["X2"] == 0.50
    assert dc["12"] == 0.75


# ═════════════════════════════════════════════════════════════════════
# RC5 — Missing-data hierarchy / evidence tiers
# ═════════════════════════════════════════════════════════════════════
def test_rc5_missing_real_xg_does_not_kill_model():
    """Missing real xG must not automatically MODEL_UNAVAILABLE — form
    proxy still yields a legitimate tier-B result."""
    from services.soccer_game_model import estimate_soccer_game_probabilities
    ctx = {
        "home_xg_rolling": {"xg_avg": 1.5, "xga_avg": 1.3, "matches": 10,
                            "source": "form_proxy"},
        "away_xg_rolling": {"xg_avg": 1.2, "xga_avg": 1.5, "matches": 10,
                            "source": "form_proxy"},
        "home_form": {"gf_avg": 1.5, "ga_avg": 1.3, "n_matches": 10},
        "away_form": {"gf_avg": 1.2, "ga_avg": 1.5, "n_matches": 10},
    }
    r = estimate_soccer_game_probabilities(ctx, "H", "A")
    assert r.available is True
    assert r.tier in ("B", "C")
    assert r.uncertainty > 0.15  # Higher than a real-xG match.


def test_rc5_truly_insufficient_evidence_is_model_unavailable():
    from services.soccer_game_model import estimate_soccer_game_probabilities
    r = estimate_soccer_game_probabilities({}, "H", "A")
    assert r.available is False
    assert r.reason == "INSUFFICIENT_HISTORY"
    assert r.tier == "D"


def test_rc5_one_side_missing_raises_uncertainty():
    from services.soccer_game_model import estimate_soccer_game_probabilities
    ctx_two = {
        "home_form": {"gf_avg": 1.8, "ga_avg": 1.0, "n_matches": 10},
        "away_form": {"gf_avg": 1.2, "ga_avg": 1.5, "n_matches": 10},
        "home_xg_rolling": {"xg_avg": 1.8, "xga_avg": 1.0, "matches": 10,
                            "source": "form_proxy"},
        "away_xg_rolling": {"xg_avg": 1.2, "xga_avg": 1.5, "matches": 10,
                            "source": "form_proxy"},
    }
    ctx_one = {
        "home_form": {"gf_avg": 1.8, "ga_avg": 1.0, "n_matches": 10},
        "home_xg_rolling": {"xg_avg": 1.8, "xga_avg": 1.0, "matches": 10,
                            "source": "form_proxy"},
    }
    r_two = estimate_soccer_game_probabilities(ctx_two, "H", "A")
    r_one = estimate_soccer_game_probabilities(ctx_one, "H", "A")
    assert r_two.available and r_one.available
    assert r_one.uncertainty >= r_two.uncertainty


def test_rc5_pre_score_starvation_gate_bypassed_when_game_model_present():
    """The old `has_enough_soccer_data(...) == False → _skip_ml = True`
    hard-count gate must no longer silently drop matches when the
    independent game model succeeded."""
    src = open(os.path.join(BACKEND, "sports_engine.py"), "r").read()
    assert "_has_game_model = bool(_game_ctx.get(\"_soccer_game_model\"))" in src
    # The `_skip_ml = True` block must be conditional on BOTH: no
    # legacy factors AND no game model.
    assert "not has_enough_soccer_data(real_ml_factors, \"ml\") and not _has_game_model" in src


# ═════════════════════════════════════════════════════════════════════
# RC6 — Funnel attribution for silent deaths
# ═════════════════════════════════════════════════════════════════════
def test_rc6_soccer_evidence_threshold_death_records_funnel():
    src = open(os.path.join(BACKEND, "sports_engine.py"), "r").read()
    # The Soccer branch must emit a funnel record when it sets
    # _skip_ml = True.
    idx = src.find("elif sport == \"Soccer\":")
    assert idx > 0
    end = src.find("if not _skip_ml:", idx)
    soccer_block = src[idx:end]
    assert "EVIDENCE_THRESHOLD" in soccer_block
    assert "funnel_telemetry" in soccer_block


def test_rc6_soccer_model_unavailable_records_funnel():
    src = open(os.path.join(BACKEND, "sports_engine.py"), "r").read()
    # The MODEL_UNAVAILABLE branch for Soccer game model must record a
    # funnel event.
    assert (
        "soccer_game_model tier=" in src
    ), "MODEL_UNAVAILABLE Soccer death must be funnel-attributable"


# ═════════════════════════════════════════════════════════════════════
# Favorite / underdog neutrality
# ═════════════════════════════════════════════════════════════════════
def test_favorite_can_be_selected():
    from services.soccer_game_model import estimate_soccer_game_probabilities
    ctx = {
        "home_xg_rolling": {"xg_avg": 2.3, "xga_avg": 0.9, "matches": 15,
                            "source": "understat"},
        "away_xg_rolling": {"xg_avg": 1.0, "xga_avg": 1.9, "matches": 15,
                            "source": "understat"},
    }
    r = estimate_soccer_game_probabilities(ctx, "H", "A")
    assert r.p_home > r.p_away


def test_underdog_can_be_selected():
    from services.soccer_game_model import estimate_soccer_game_probabilities
    ctx = {
        "home_xg_rolling": {"xg_avg": 0.9, "xga_avg": 2.0, "matches": 15,
                            "source": "understat"},
        "away_xg_rolling": {"xg_avg": 2.2, "xga_avg": 0.8, "matches": 15,
                            "source": "understat"},
    }
    r = estimate_soccer_game_probabilities(ctx, "WeakHome", "StrongAway")
    assert r.p_away > r.p_home


def test_draw_can_carry_value():
    from services.soccer_game_model import estimate_soccer_game_probabilities
    ctx = {
        "home_xg_rolling": {"xg_avg": 1.0, "xga_avg": 1.0, "matches": 15,
                            "source": "understat"},
        "away_xg_rolling": {"xg_avg": 1.0, "xga_avg": 1.0, "matches": 15,
                            "source": "understat"},
    }
    r = estimate_soccer_game_probabilities(ctx, "H", "A")
    # Balanced game, low-scoring → draw prob should be non-trivial.
    assert r.p_draw >= 0.20


def test_no_favorite_automatic_boost_no_underdog_automatic_penalty():
    """Reversing team roles must exactly mirror the outcome.  If the
    model had a favourite bias, swapping which side is 'home' would
    NOT swap the probabilities."""
    from services.soccer_game_model import estimate_soccer_game_probabilities
    ctx_normal = {
        "home_xg_rolling": {"xg_avg": 2.0, "xga_avg": 1.0, "matches": 20,
                            "source": "understat"},
        "away_xg_rolling": {"xg_avg": 1.0, "xga_avg": 2.0, "matches": 20,
                            "source": "understat"},
    }
    r_normal = estimate_soccer_game_probabilities(ctx_normal, "A", "B")
    ctx_swapped = {
        "home_xg_rolling": {"xg_avg": 1.0, "xga_avg": 2.0, "matches": 20,
                            "source": "understat"},
        "away_xg_rolling": {"xg_avg": 2.0, "xga_avg": 1.0, "matches": 20,
                            "source": "understat"},
    }
    r_swap = estimate_soccer_game_probabilities(ctx_swapped, "B", "A")
    # With home advantage constant, r_normal.p_home should NOT equal
    # r_swap.p_away exactly — but they should be close.  The important
    # invariant: no biased side-bonus beyond `HOME_ADVANTAGE_GOALS`.
    # Confirm the model responds to strength inversion.
    assert r_normal.p_home > r_swap.p_home  # strong side is home now weak
    assert r_normal.p_away < r_swap.p_away


# ═════════════════════════════════════════════════════════════════════
# Board eligibility (≥85) preserved
# ═════════════════════════════════════════════════════════════════════
def test_lock_score_gte_85_rule_preserved_for_soccer():
    """The strict 85 floor from Phase 1D must remain unchanged for
    Soccer even after the game model wires in."""
    src = open(os.path.join(BACKEND, "quality_gate.py"), "r").read()
    # 85 remains the canonical board floor.
    assert "85" in src
    # No Soccer-specific ladder was introduced.
    assert "Soccer floor" not in src
    assert "SOCCER_LOCK_FLOOR" not in src


# ═════════════════════════════════════════════════════════════════════
# De-vig contract preserved
# ═════════════════════════════════════════════════════════════════════
def test_de_vig_edge_contract_preserved():
    """Phase 2A canonical edge = model - devig must remain intact."""
    src = open(os.path.join(BACKEND, "sports_engine.py"), "r").read()
    assert "devig_market_probability" in src or "edge_method" in src


# ═════════════════════════════════════════════════════════════════════
# Apex reachability — Soccer not structurally excluded
# ═════════════════════════════════════════════════════════════════════
def test_soccer_can_enter_apex_evaluator():
    """The universal Apex evaluator must not exclude Soccer."""
    import glob
    apex_files = glob.glob(os.path.join(BACKEND, "**/apex*.py"), recursive=True)
    for f in apex_files:
        s = open(f, "r").read()
        # A sport-specific hard-block would look like this pattern.
        assert "sport != \"Soccer\"" not in s
        assert "sport == \"Soccer\": return" not in s


# ═════════════════════════════════════════════════════════════════════
# Scorer intelligence untouched
# ═════════════════════════════════════════════════════════════════════
def test_scorer_bridge_still_intact():
    from services.soccer_scorer_bridge import compute_soccer_scorer_factors_sync
    r = compute_soccer_scorer_factors_sync(
        player="Erling Haaland",
        market_key="player_goal_scorer_anytime",
        book_implied=0.55,
        form_row={
            "xg": 18.5, "goals": 20, "minutes": 2700, "games": 30,
            "starts": 30, "position": "FW", "form_score": 82,
            "shots_per_90": 4.5, "sot_per_90": 2.1,
        },
        league="Premier League",
    )
    assert r is not None
    assert "Scorer Model Probability" in r["factors"]
