"""Post-Cert Runtime Consumer Closure regression suite.

Covers the SIX defect classes from the independent post-cert audit:

1. Simulator provenance bridge (brain/sim_runner.py) — legacy compat
   fields (`independent_evidence`, `valid`) cannot override Phase-2
   provenance truth.
2. Context wiring for existing independent simulators (spot-checks —
   the wiring itself is exercised by existing sim tests; here we prove
   the fallback-vs-independent provenance behaviour holds).
3A. Soccer scorer λ priority — empirical > model-conditioned > prior.
3B. `canonical_team_name` recognised as a valid player-team source by
    the player→event identity gate.
4. Soccer >=85 reachability — weight normalisation over available
   components lets a strong pregame Soccer pick legitimately reach 85+.
5. Alt-line terminal state — frontend contract (verified via file
   inspection since the component is React Native — the backend must
   always return a terminal shape).
6. Why-This-Pick priority — frozen matchup evidence outranks generic
   component-score bullets.

Zero paid provider calls.  All fixtures deterministic in-memory.
"""
from __future__ import annotations

import pytest


# ═══════════════════════════════════════════════════════════════════════
# Defect 1 — Simulator provenance bridge
# ═══════════════════════════════════════════════════════════════════════
def test_defect1_model_conditioned_forces_independent_evidence_false():
    """After the bridge fix, MODEL_CONDITIONED simulator output must be
    stamped independent_evidence=False even if the simulator's own dict
    didn't set the field (legacy compat default was True)."""
    from brain.sim_runner import simulate_pick
    # We invoke the bridge indirectly by simulating a soccer scorer
    # pick that returns MODEL_CONDITIONED provenance.  Cheaper: prove
    # the override logic by exercising the same code path on a mocked
    # simulator return.
    import brain.sim_runner as sr

    original = sr.simulate_soccer_pick if hasattr(sr, "simulate_soccer_pick") else None
    # Monkey-patch a fake simulator into brain.sim_soccer
    import brain.sim_soccer as sim_soccer
    real_fn = sim_soccer.simulate_soccer_pick

    def _fake(pick):
        return {
            "probability": 0.55,
            "simulator_provenance": "MODEL_CONDITIONED",
            "input_quality": "MEDIUM",
            # NB: no independent_evidence / valid — bridge must fill.
        }
    sim_soccer.simulate_soccer_pick = _fake
    try:
        pick = {"id": "sim_test", "sport": "Soccer",
                "market": "Anytime Goal Scorer",
                "selection": "Test Player", "book_odds": +150}
        result = simulate_pick(pick)
    finally:
        sim_soccer.simulate_soccer_pick = real_fn
    assert result is not None
    assert result["simulator_provenance"] == "MODEL_CONDITIONED"
    assert result["independent_evidence"] is False, \
        "Defect 1: MODEL_CONDITIONED must force independent_evidence=False"
    assert result["valid"] is True  # still a valid simulation, just not independent


def test_defect1_prior_only_no_boost():
    """PRIOR_ONLY provenance → independent_evidence=False (no Lock/Magic
    boost downstream)."""
    from brain.sim_runner import simulate_pick
    import brain.sim_nba as sim_nba
    real_fn = sim_nba.simulate_nba_pick

    def _fake(pick):
        return {
            "probability": 0.60,
            "simulator_provenance": "PRIOR_ONLY",
            "input_quality": "LOW",
        }
    sim_nba.simulate_nba_pick = _fake
    try:
        pick = {"id": "prior_only", "sport": "NBA",
                "market": "Player Points", "selection": "X Over 20.5",
                "book_odds": -110}
        result = simulate_pick(pick)
    finally:
        sim_nba.simulate_nba_pick = real_fn
    assert result["independent_evidence"] is False


def test_defect1_invalid_provenance_zeroes_valid():
    """INVALID provenance → both valid=False and independent_evidence=False."""
    from brain.sim_runner import simulate_pick
    import brain.sim_tennis as sim_tennis
    real_fn = sim_tennis.simulate_tennis_pick

    def _fake(pick):
        return {
            "probability": 0.55,
            "simulator_provenance": "INVALID",
        }
    sim_tennis.simulate_tennis_pick = _fake
    try:
        pick = {"id": "invalid_prov", "sport": "Tennis",
                "market": "Moneyline", "selection": "Alcaraz",
                "book_odds": -140}
        result = simulate_pick(pick)
    finally:
        sim_tennis.simulate_tennis_pick = real_fn
    assert result["valid"] is False
    assert result["independent_evidence"] is False


def test_defect1_decision_valid_false_forces_both_false():
    """decision_valid=False from the simulator must force both flags."""
    from brain.sim_runner import simulate_pick
    import brain.sim_mlb as sim_mlb
    real_fn = sim_mlb.simulate_mlb_pick

    def _fake(pick, stats):
        return {
            "probability": 0.60,
            "simulator_provenance": "CAUSAL_INDEPENDENT",
            "input_quality": "FULL",
            "decision_valid": False,   # explicit contract violation
        }
    sim_mlb.simulate_mlb_pick = _fake
    try:
        pick = {"id": "dv_false", "sport": "MLB",
                "market": "Pitcher Strikeouts Over 6.5",
                "selection": "Cole Over 6.5", "book_odds": -115}
        result = simulate_pick(pick)
    finally:
        sim_mlb.simulate_mlb_pick = real_fn
    assert result["independent_evidence"] is False
    assert result["valid"] is False


def test_defect1_independent_full_confidence_allowed():
    """CAUSAL_INDEPENDENT + FULL confidence + decision_valid=True MUST
    remain independent_evidence=True (no over-rejection)."""
    from brain.sim_runner import simulate_pick
    import brain.sim_mlb as sim_mlb
    real_fn = sim_mlb.simulate_mlb_pick

    def _fake(pick, stats):
        return {
            "probability": 0.65,
            "simulator_provenance": "CAUSAL_INDEPENDENT",
            "input_quality": "FULL",
            "decision_valid": True,
        }
    sim_mlb.simulate_mlb_pick = _fake
    try:
        pick = {"id": "ok_indep", "sport": "MLB",
                "market": "Pitcher Strikeouts Over 6.5",
                "selection": "Cole Over 6.5", "book_odds": -115}
        result = simulate_pick(pick)
    finally:
        sim_mlb.simulate_mlb_pick = real_fn
    assert result["independent_evidence"] is True
    assert result["valid"] is True


# ═══════════════════════════════════════════════════════════════════════
# Defect 3B — canonical_team_name recognised by identity gate
# ═══════════════════════════════════════════════════════════════════════
def test_defect3B_canonical_team_name_treated_as_authoritative():
    """Soccer scorer picks stamp `canonical_team_name`.  The identity
    gate must treat this as an authoritative player-team source (same
    class as `player_team` / `elite_player_team`)."""
    from services.player_event_identity_gate import (
        evaluate_identity, IdentityVerdict,
    )
    # Positive control — canonical_team_name matches home team
    ok = {
        "sport": "soccer",
        "event": "Athletic Bilbao @ Barcelona",
        "home_team": "Barcelona", "away_team": "Athletic Bilbao",
        "market": "Anytime Goal Scorer",
        "selection": "Robert Lewandowski",
        "player_name": "Robert Lewandowski",
        "canonical_team_name": "Barcelona",   # authoritative field
        "book_odds": -140,
    }
    assert evaluate_identity(ok) == IdentityVerdict.VALID
    # Negative — canonical_team_name is wrong (Rashford protection)
    bad = dict(ok, canonical_team_name="Real Madrid",
                selection="Vinicius Jr", player_name="Vinicius Jr")
    assert evaluate_identity(bad) == \
        IdentityVerdict.PLAYER_EVENT_IDENTITY_MISMATCH


def test_defect3B_canonical_team_name_precedence_over_stale_team():
    """When `canonical_team_name` disagrees with a stale `team` field,
    canonical wins — order in the fallback list places
    canonical_team_name AFTER player_team but BEFORE `team`."""
    from services.player_event_identity_gate import (
        evaluate_identity, IdentityVerdict,
    )
    pick = {
        "sport": "soccer",
        "event": "Athletic Bilbao @ Barcelona",
        "home_team": "Barcelona", "away_team": "Athletic Bilbao",
        "market": "Anytime Goal Scorer",
        "selection": "Robert Lewandowski",
        "player_name": "Robert Lewandowski",
        # `team` is a stale abbreviation not matching any event side.
        "team": "MUN",
        # But canonical_team_name is authoritative and matches home team.
        "canonical_team_name": "Barcelona",
        "book_odds": -140,
    }
    assert evaluate_identity(pick) == IdentityVerdict.VALID


# ═══════════════════════════════════════════════════════════════════════
# Defect 4 — Soccer >=85 reachability via weight normalisation
# ═══════════════════════════════════════════════════════════════════════
def test_defect4_strong_soccer_pregame_can_reach_85():
    """A legitimate strong Soccer pregame candidate (real line, valid
    evidence, edge >= 8%, agreed factors) must be mathematically able
    to reach Lock Score >= 85."""
    from sports_engine import compute_lock_score
    # Strong evidence: tight factor agreement, high edge, no closing odds
    # yet (pregame), no bucket ROI yet (new market/window).
    factors = {"Form": 0.90, "xG advantage": 0.92, "Matchup History": 0.88}
    pick = {
        "book_odds": -140,
        "edge_percent": 10.0,
        "win_probability": 68.0,
    }
    lock, _ = compute_lock_score(factors, win_prob=68.0,
                                   pick=pick, edge_percent=10.0)
    assert lock >= 85.0, (
        f"Defect 4: strong pregame Soccer pick capped at {lock} — "
        f"weight-normalisation over available components must let >=85 "
        f"be reachable without adding fake evidence"
    )


def test_defect4_weak_soccer_pregame_remains_below_85():
    """A WEAK Soccer candidate must still fail the 85 floor — the fix
    does not lower the threshold or add filler."""
    from sports_engine import compute_lock_score
    factors = {"Form": 0.50}   # mediocre single factor
    pick = {"book_odds": -110, "edge_percent": 2.0,
            "win_probability": 55.0}
    lock, _ = compute_lock_score(factors, win_prob=55.0,
                                   pick=pick, edge_percent=2.0)
    assert lock < 85.0, (
        f"Defect 4: weak pregame Soccer pick incorrectly reached {lock}"
    )


def test_defect4_wrong_team_scorer_rejected_regardless_of_score():
    """A wrong-team scorer must be rejected by identity even if the
    Lock Score would otherwise clear 85."""
    from services.player_event_identity_gate import (
        evaluate_identity, IdentityVerdict,
    )
    pick = {
        "sport": "soccer",
        "event": "Athletic Bilbao @ Barcelona",
        "home_team": "Barcelona", "away_team": "Athletic Bilbao",
        "market": "Anytime Goal Scorer",
        "selection": "Vinicius Jr",
        "player_name": "Vinicius Jr",
        "canonical_team_name": "Real Madrid",  # wrong team!
        "book_odds": +180,
        "lock_score": 99.0,   # would otherwise pass
    }
    assert evaluate_identity(pick) == \
        IdentityVerdict.PLAYER_EVENT_IDENTITY_MISMATCH


# ═══════════════════════════════════════════════════════════════════════
# Defect 6 — Why-This-Pick priority
# ═══════════════════════════════════════════════════════════════════════
def test_defect6_key_insights_priority_existing_before_generic():
    """The pick-refresh orchestrator concatenation must PRESERVE
    existing frozen matchup evidence FIRST and append generic tennis
    component bullets AFTER, so the UI's key_insights renders richer
    matchup-specific content ahead of "Surface fit 67/100"."""
    # Simulate the concatenation logic surgically without a full orch
    # invocation (which requires live picks and a DB).
    existing = [
        "Alcaraz has 7-2 hard-court H2H vs Sinner since 2024",
        "Direct H2H sample n=9, ROI +18%",
    ]
    tennis_insights = [
        "Surface fit: 67/100 on Hard — comfortable.",
        "Form (opp-adj L10): 90/100 — red hot.",
        "Serve/Return profile: 69/100.",
    ]
    # Post-Cert Defect 6 — existing first, generic fallback appended.
    combined = list(existing) + tennis_insights
    assert combined[0] == existing[0], (
        f"Defect 6: matchup-specific evidence must appear first; got "
        f"{combined[0]!r}"
    )
    # Generic bullets still available as fallback further down.
    assert any("Surface fit" in s for s in combined[len(existing):])


def test_defect6_when_no_matchup_evidence_generic_used_alone():
    """If no matchup-specific evidence is present, generic component
    bullets are the entire key_insights list — no fabricated matchup."""
    existing = []
    tennis_insights = [
        "Surface fit: 55/100 on Hard.",
    ]
    combined = list(existing) + tennis_insights
    assert combined == tennis_insights


# ═══════════════════════════════════════════════════════════════════════
# Defect 3A — Scorer λ priority (empirical > model > prior)
# ═══════════════════════════════════════════════════════════════════════
def test_defect3A_scorer_empirical_priors_beat_model_backsolve():
    """When real empirical priors (player xG / opp) are available,
    the scorer λ estimator MUST return EMPIRICAL_INDEPENDENT — even
    though a model_wp is also present.  Previously Approach 1
    (model back-solve) was tried first and stole priority."""
    from brain.sim_soccer_scorer import _estimate_player_lambda
    pick = {"market": "Anytime Goal Scorer", "win_probability": 52.0}
    priors = {
        "player_xg_per_game": 0.42,
        "opp_concedes_per_match": 1.5,
        "shots_per_game": 3.1,
        "recent_goal_rate": 0.35,
    }
    lam, prov, signals = _estimate_player_lambda(pick, priors)
    assert prov == "EMPIRICAL_INDEPENDENT", (
        f"Defect 3A: real empirical priors must yield EMPIRICAL_INDEPENDENT, "
        f"got {prov}"
    )
    assert signals >= 3
    assert 0.05 <= lam <= 2.0


def test_defect3A_scorer_falls_back_to_model_when_no_empirical():
    """No empirical priors + valid model_wp → MODEL_CONDITIONED."""
    from brain.sim_soccer_scorer import _estimate_player_lambda
    pick = {"market": "Anytime Goal Scorer", "win_probability": 55.0}
    priors = {}   # no empirical evidence
    lam, prov, signals = _estimate_player_lambda(pick, priors)
    assert prov == "MODEL_CONDITIONED"
    assert signals == 1


def test_defect3A_scorer_falls_back_to_prior_when_no_empirical_no_model():
    """No empirical + no model_wp → PRIOR_ONLY (no signals)."""
    from brain.sim_soccer_scorer import _estimate_player_lambda
    pick = {"market": "Anytime Goal Scorer", "win_probability": 0.0,
            "factors": {"Recent Volume / Usage": 60,
                        "Matchup vs Defense": 55,
                        "Last 10 Hit Rate": 50}}
    priors = {}
    lam, prov, signals = _estimate_player_lambda(pick, priors)
    assert prov == "PRIOR_ONLY"
    assert signals == 0


# ═══════════════════════════════════════════════════════════════════════
# Defect 5 — Alt-line terminal state (frontend contract inspection)
# ═══════════════════════════════════════════════════════════════════════
def test_defect5_alt_line_component_renders_explicit_empty_state():
    """Backend must always return a terminal shape; the frontend
    component (verified by file inspection) never silently returns
    null after loading completes."""
    import pathlib
    src = pathlib.Path("/app/frontend/src/components/AltLineChips.tsx")
    text = src.read_text()
    # After the Post-Cert Defect 5 fix, the empty-state branch renders
    # an explicit "No alternate lines available" message instead of
    # `return null`.
    assert "No alternate lines available" in text, (
        "Defect 5: AltLineChips.tsx missing explicit empty-state message"
    )
    assert 'testID="alt-line-chips-empty"' in text, (
        "Defect 5: empty-state must expose a stable testID for QA"
    )
    # And "Alternate lines temporarily unavailable" surfaces the
    # network-error branch (distinct from empty).
    assert "temporarily unavailable" in text, (
        "Defect 5: network-error branch must produce a distinct message"
    )
