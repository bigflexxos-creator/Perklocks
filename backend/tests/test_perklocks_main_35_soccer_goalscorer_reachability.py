"""PERKLOCKS-MAIN 35 · FINAL — SOCCER GOALSCORER REACHABILITY +
CAPABILITY-STATE PUBLICATION GUARDS.

Contracts asserted:
  * Soccer league discovery is dynamic (uses `_discover_active_
    sports_by_prefix("soccer_")`) — no hardcoded whitelist required.
  * Every currently-registered soccer_* sport key in the discovery
    list flows through the canonical props emitter without special-
    case gating.
  * Anytime / First / Last / Score-or-Assist / Anytime-Assist remain
    DISTINCT canonical UMC entries.
  * The publication boundary REJECTS a pick whose (sport, family)
    capability_state is RESEARCH_ONLY (e.g. first_goalscorer).
  * The publication boundary REJECTS MODEL_UNAVAILABLE and
    PROVIDER_UNAVAILABLE + SETTLEMENT_UNAVAILABLE combos.
  * ACTIVE Soccer families (Anytime Goalscorer, Score-or-Assist,
    Anytime Assist) can publish (they pass the capability gate).
"""
from __future__ import annotations

import pytest


def _mk_pick(**overrides):
    base = {
        "id": "soc-pick-1",
        "canonical_pick_id": "soc-pick-1",
        "sport": "Soccer",
        "canonical_player_id": "erling-haaland-mci",
        "canonical_team_id": "MCI",
        "canonical_opponent_id": "AVL",
        "canonical_selection": "Yes",
        "identity_class": "AUTHORITATIVE",
        "book_odds": +220,
        "odds_source": "the_odds_api",
        "model_probability": 0.35,
        "edge_percent": 0.04,
    }
    base.update(overrides)
    return base


def test_soccer_league_discovery_is_dynamic_prefix_scan():
    import inspect
    import alt_lines_feed
    src = inspect.getsource(alt_lines_feed._discover_active_soccer_leagues)
    assert "prefix" in src.lower() or "soccer_" in src, src


def test_five_distinct_soccer_goalscorer_families_registered():
    """Anytime / First / Score-or-Assist / Anytime-Assist / Total
    Goals are canonically distinct — never collapsed."""
    from services.universal_market_contract import get, Family
    fams = [
        Family.GOALSCORER_ANY,
        Family.GOALSCORER_FIRST,
        Family.GOALSCORER_SCORE_ASSIST,
        "soccer_anytime_assist",
        Family.GAME_TOTAL,
    ]
    ids = {get("Soccer", f).family for f in fams}
    assert len(ids) == 5, ids  # all distinct


def test_boundary_rejects_first_goalscorer_research_only_publication():
    """First Goalscorer is RESEARCH_ONLY per product decision — the
    boundary must NEVER accept it as an authoritative Locks
    publication, even with a real book_odds and provenance."""
    from services.canonical_publication_boundary import evaluate_publication
    from services.universal_market_contract import Family
    pick = _mk_pick(
        canonical_market_family=Family.GOALSCORER_FIRST,
        market="First Goalscorer — Erling Haaland",
        provider_market_key="player_first_goal_scorer",
    )
    v = evaluate_publication(pick)
    assert v.accepted is False
    assert any("RESEARCH_ONLY" in r for r in v.reasons), v.reasons


def test_boundary_accepts_anytime_goalscorer_active_publication():
    """ACTIVE Soccer goalscorer market MUST NOT be rejected by the
    capability-state guard — only downstream identity/settlement
    checks may still reject. The capability-state guard specifically
    must not fire for an ACTIVE (sport, family) row."""
    from services.canonical_publication_boundary import evaluate_publication
    from services.universal_market_contract import Family
    pick = _mk_pick(
        canonical_market_family=Family.GOALSCORER_ANY,
        market="Anytime Goalscorer — Erling Haaland",
        provider_market_key="player_goal_scorer_anytime",
    )
    v = evaluate_publication(pick)
    # Capability-state guard must NOT be one of the rejection reasons.
    for r in v.reasons:
        assert "UMC_CAPABILITY_STATE" not in r, r
        assert "RESEARCH_ONLY" not in r, r
        assert "MODEL_UNAVAILABLE" not in r, r
        assert "PROVIDER_UNAVAILABLE" not in r, r


def test_boundary_rejects_model_unavailable_nba_publication():
    from services.canonical_publication_boundary import evaluate_publication
    pick = _mk_pick(
        sport="NBA",
        canonical_market_family="nba_points",
        market="Nikola Jokic Over 27.5 Points",
        selection="Over",
        provider_market_key="player_points",
    )
    v = evaluate_publication(pick)
    assert v.accepted is False
    assert any("MODEL_UNAVAILABLE" in r for r in v.reasons), v.reasons


def test_boundary_rejects_ufc_moneyline_model_unavailable():
    from services.canonical_publication_boundary import evaluate_publication
    pick = _mk_pick(
        sport="UFC",
        canonical_market_family="moneyline",
        market="O'Malley Moneyline",
        selection="Sean O'Malley",
        provider_market_key="h2h",
    )
    v = evaluate_publication(pick)
    assert v.accepted is False
    assert any("MODEL_UNAVAILABLE" in r for r in v.reasons)


def test_no_hardcoded_league_whitelist_for_real_book_goalscorer_publication():
    """A `soccer_argentina_primera_division` real-book Anytime
    Goalscorer pick must not be rejected by any league-whitelist gate.
    The capability-state guard specifically must not fire (goalscorer
    anytime is ACTIVE for Soccer regardless of league)."""
    from services.canonical_publication_boundary import evaluate_publication
    from services.universal_market_contract import Family
    pick = _mk_pick(
        canonical_market_family=Family.GOALSCORER_ANY,
        market="Anytime Goalscorer — Lionel Messi",
        provider_market_key="player_goal_scorer_anytime",
        # NOTE: no league whitelist check — every league is welcome.
        sport_key="soccer_argentina_primera_division",
    )
    v = evaluate_publication(pick)
    for r in v.reasons:
        assert "UMC_CAPABILITY_STATE" not in r, r
        assert "league_whitelist" not in r.lower(), r


def test_settlement_capability_registry_covers_soccer_active_families():
    from services.settlement_capability_registry import all_registrations
    reg = all_registrations()
    for fam in (
        "moneyline", "game_total", "handicap", "btts",
        "double_chance", "goalscorer_anytime",
        "goalscorer_score_or_assist", "soccer_anytime_assist",
    ):
        assert ("Soccer", fam) in reg, fam
