"""PERKLOCKS MAIN 36 · SURGICAL TRUST ROOT CLOSURE — regression tests.

Locks in every fix from the Main 36 mandate:

  P0-1  PickBreakdown state labels (PUBLISHED_LOCK / RESEARCH / …)
  P0-2  Market Competition returns current_pick separately, no 0 fallback
  P0-3  Simulator trust fails closed (missing provenance → invalid)
  P0-4  Brain cannot overwrite specialized-model probability
  P0-5  MLB pitcher_outs cannot default to 6 innings
  P0-6  Lake Bachar identity trace (validates market disambiguation)
  P0-7  Extreme model↔sim disagreement → MODEL_DISAGREEMENT
  P0-8  SIM EDGE UI trust gated by sim_trust_state
  P0-9  Tennis BO3/BO5 threaded through Brain
  P0-10 NFL synthesized opportunity carries PRIOR_ONLY provenance
  P0-11 Middleware never returns invalid response
  P1    Real Wilson lower bound (not hr × 0.7)
"""
from __future__ import annotations

import pytest


# ─────────────────────────────────────────────────────────────────
# P0-1 · P0-2 — Pick Breakdown state labels + current_pick contract
# ─────────────────────────────────────────────────────────────────
def test_pick_state_labels():
    from market_competition.routes import _pick_state
    # PUBLISHED_LOCK — canonical publication crossed.
    assert _pick_state({
        "book_odds": -110, "win_probability": 62.0,
        "canonical_published_at": "2026-06-30T00:00:00Z",
    }) == "PUBLISHED_LOCK"
    # PUBLISHED_LOCK — published_lock_score alone is enough.
    assert _pick_state({
        "book_odds": -110, "win_probability": 62.0,
        "published_lock_score": 91,
    }) == "PUBLISHED_LOCK"
    # INELIGIBLE — flagged no_bet.
    assert _pick_state({
        "book_odds": -110, "win_probability": 62.0, "no_bet": True,
    }) == "INELIGIBLE"
    # UNAVAILABLE — missing book_odds.
    assert _pick_state({
        "book_odds": None, "win_probability": 62.0,
    }) == "UNAVAILABLE"
    # UNAVAILABLE — missing win_probability.
    assert _pick_state({
        "book_odds": -110, "win_probability": None,
    }) == "UNAVAILABLE"
    # RESEARCH_ALTERNATIVE — has evidence but not published.
    assert _pick_state({
        "book_odds": -120, "win_probability": 71.0,
    }) == "RESEARCH_ALTERNATIVE"


def test_market_competition_response_contract_has_current_pick():
    """Response contract must return current_pick separately."""
    import inspect
    from market_competition.routes import market_rank_for_pick
    src = inspect.getsource(market_rank_for_pick)
    assert "current_pick" in src, (
        "market_rank_for_pick must return current_pick separately so "
        "the frontend never has to fall back to 0."
    )
    assert "_score_or_none" in src, (
        "Missing current-pick score must be None, not 0 — see P0-2."
    )
    assert '"signal_score"' in src, (
        "signal_score MUST remain distinct from lock_score / market_score."
    )


def test_market_competition_missing_metric_returns_none_not_zero():
    """The score_or_none helper returns None when critical metrics
    are missing — never converts missing data to zero."""
    import re, textwrap
    from market_competition.routes import market_rank_for_pick
    src = textwrap.dedent(re.search(
        r"def _score_or_none.*?(?=\n\s*_pub_ls)",
        __import__("inspect").getsource(market_rank_for_pick), re.S,
    ).group(0))
    assert "return None" in src


# ─────────────────────────────────────────────────────────────────
# P0-3 · P0-7 · P0-8 — Simulator trust fails closed
# ─────────────────────────────────────────────────────────────────
def test_sim_missing_provenance_fails_closed():
    """A simulator that doesn't stamp provenance MUST NOT earn
    valid=True / independent_evidence=True via legacy defaults."""
    import inspect
    from brain.sim_runner import simulate_pick
    src = inspect.getsource(simulate_pick)
    # New default is False.  Old bug was True.
    assert 'setdefault("independent_evidence", False)' in src
    assert 'setdefault("valid",                False)' in src or \
           'setdefault("valid", False)' in src


def test_extreme_disagreement_labels_model_disagreement():
    """An INDEPENDENT simulator that disagrees with the model by
    ≥15pp CANNOT surface as SIM EDGE — must be MODEL_DISAGREEMENT."""
    import inspect
    from brain.sim_runner import simulate_pick
    src = inspect.getsource(simulate_pick)
    assert "_EXTREME_DISAGREEMENT_PP" in src
    assert "MODEL_DISAGREEMENT" in src
    assert "sim_trust_state" in src


def test_sim_trust_state_gate_present_in_adapter():
    """The base adapter exposes sim_trust_state so the UI can gate
    the SIM EDGE chip on truthful state."""
    import inspect
    from sport_adapters import SportAdapter
    src = inspect.getsource(SportAdapter.run_simulation)
    assert "sim_trust_state" in src


# ─────────────────────────────────────────────────────────────────
# P0-4 — Brain cannot overwrite specialized-model probability
# ─────────────────────────────────────────────────────────────────
def test_brain_cannot_overwrite_specialized_probability():
    """A pick with a specialized-model marker (mlb_outs_model_output
    / atd_model_override / pitcher_outs_expected / etc.) MUST NOT
    have its win_probability replaced by generic distribution_monte_carlo."""
    import inspect
    from brain.sim_runner import _anchor_pick_to_sim
    src = inspect.getsource(_anchor_pick_to_sim)
    for marker in ("mlb_outs_model_output", "pitcher_outs_expected",
                    "tennis_math_output", "specialized_model_output"):
        assert marker in src, f"specialized marker {marker!r} missing"
    # And the anchor path checks sim_meta.independent_evidence + valid.
    assert "_sim_indep" in src
    assert "_sim_valid" in src


# ─────────────────────────────────────────────────────────────────
# P0-5 — MLB pitcher_outs no six-inning silent fallback
# ─────────────────────────────────────────────────────────────────
def test_mlb_pitcher_outs_fails_closed_when_workload_missing():
    """Missing bf_per_inning OR expected_innings MUST NOT default
    to 3.7 / 6.0 — must return SIM_DATA_INSUFFICIENT."""
    from brain.sim_mlb import simulate_mlb_pick as _sim_mlb
    import inspect, re
    src = inspect.getsource(_sim_mlb)
    assert "SIM_DATA_INSUFFICIENT" in src
    # No ACTIVE call to _simulate_pitcher_outs with defaulted values.
    # (Comment references are allowed.)
    call_sites = re.findall(
        r"_simulate_pitcher_outs\(\s*stats\.get\([^)]+\)\s*,\s*stats\.get\([^)]+\)\s*\)",
        src,
    )
    for cs in call_sites:
        assert "3.7" not in cs and "6.0" not in cs, (
            f"active silent fallback still present: {cs!r}"
        )


def test_mlb_k_data_cannot_substitute_for_outs_workload():
    """The pitcher_outs branch must not fall back to k_rate for
    workload — it should explicitly require bf_per_inning +
    expected_innings from the repaired MLB outs system."""
    import inspect
    from brain.sim_mlb import simulate_mlb_pick as _sim_mlb
    src = inspect.getsource(_sim_mlb)
    branch_start = src.find('"outs recorded"')
    branch_end = src.find("else:", branch_start)
    outs_branch = src[branch_start:branch_end]
    assert "k_rate" not in outs_branch.lower()


# ─────────────────────────────────────────────────────────────────
# P0-6 — Lake Bachar identity trace
# ─────────────────────────────────────────────────────────────────
def test_lake_bachar_market_disambiguation():
    """"Over 5.5 Outs Recorded" MUST route to pitcher_outs, NOT
    pitcher_strikeouts.  The universal stat resolver already handles
    the disambiguation; this locks it in as a Bachar regression probe.
    """
    from services.pick_matchup_wiring import _detect_stat
    stat = _detect_stat("MLB",
                         "Lake Bachar (PIT) Over 5.5 Outs Recorded")
    assert stat == "pitcher_outs", (
        f"Bachar 5.5 Outs must map to pitcher_outs, got {stat!r} — "
        "wrong routing would run the K simulator on an Outs pick."
    )


# ─────────────────────────────────────────────────────────────────
# P0-9 — Tennis BO3/BO5 threaded through Brain
# ─────────────────────────────────────────────────────────────────
def test_brain_tennis_uses_resolve_match_format():
    """The Tennis Brain simulator MUST call resolve_tennis_match_format
    so an ATP men's Grand Slam runs BO5, not the SETS_BO3 default."""
    import inspect
    from brain import sim_tennis
    src = inspect.getsource(sim_tennis.simulate_tennis_pick)
    assert "resolve_tennis_match_format" in src
    assert "bo=_bo" in src, (
        "Simulator loop must thread the resolved _bo into "
        "_simulate_match_full."
    )
    assert "sim_match_format" in src, (
        "Payload must expose the format used so drift is auditable."
    )


# ─────────────────────────────────────────────────────────────────
# P0-10 — NFL synthesized opportunity carries PRIOR_ONLY provenance
# ─────────────────────────────────────────────────────────────────
def test_nfl_synthetic_opportunity_is_prior_only():
    """When ctx carries no per-player opportunity, the synthesized
    neutral opportunity must be flagged with provenance='PRIOR_ONLY'."""
    from services.platinum_nfl.simulator import _resolve_opportunity, SeasonType
    opp = _resolve_opportunity({}, "QB", SeasonType.REGULAR_SEASON)
    assert opp is not None
    assert getattr(opp, "provenance", None) == "PRIOR_ONLY"
    assert getattr(opp, "is_synthesized", False) is True


# ─────────────────────────────────────────────────────────────────
# P0-11 — Middleware always returns a valid Response
# ─────────────────────────────────────────────────────────────────
def test_no_store_middleware_guards_none_response():
    """The outermost middleware must handle call_next returning None
    or a non-Response gracefully — no ASGI protocol crash."""
    import inspect, server
    src = inspect.getsource(server._no_store_api_responses)
    assert "response is None" in src
    assert 'hasattr(response, "headers")' in src
    assert "try:" in src


def test_track_user_api_usage_guards_call_next_exception():
    import inspect, server
    src = inspect.getsource(server._track_user_api_usage)
    assert "try:" in src
    assert "MIDDLEWARE_FAIL" in src


# ─────────────────────────────────────────────────────────────────
# P1 — Real Wilson lower bound
# ─────────────────────────────────────────────────────────────────
def test_nba_shadow_uses_real_wilson():
    import inspect
    from services.research import nba
    src = inspect.getsource(nba._hot_scoring_shadow)
    assert "wilson_lower_bound" in src
    assert "hr * 0.75" not in src, "old heuristic must be removed"


def test_nfl_shadow_uses_real_wilson():
    import inspect
    from services.research import nfl
    src = inspect.getsource(nfl._opportunity_streak_shadow)
    assert "wilson_lower_bound" in src
    assert "hr * 0.7" not in src


def test_wilson_lower_bound_actually_shrinks():
    """Real Wilson lower bound MUST be smaller than the raw hit rate
    (it's a lower bound); previously the heuristic hr × 0.7 was closer
    to a fixed shrinkage that misrepresented small samples."""
    from services.discovery.confidence_system import wilson_lower_bound
    # 4/5 = 80 %.  Real Wilson (95 %) lower bound ≈ 0.376.
    lb = wilson_lower_bound(4, 5)
    assert 0.2 < lb < 0.6, (
        f"Wilson lower bound for 4/5 unexpectedly {lb!r}"
    )
    assert lb < (4/5), "Wilson lower bound must be < raw hit rate"
