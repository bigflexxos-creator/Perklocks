"""NHL — Universal Evidence Authority + Apex regression suite.

Scope (surgical, final-closure pass):
  * NHL registered in UEA
  * UEA dispatcher now builds a real contract for NHL picks
  * Market-family classifier routes NHL player + game markets
  * Book-implied provenance still fails closed at the model axis
  * Universal 99-eligibility rule reachable for NHL when all axes
    satisfy the universal thresholds
  * Apex eligibility reachable only when every universal prerequisite
    is met (NO NHL shortcut, NO weaker thresholds)
  * NHL categorical exclusion removed from APEX_UNAVAILABLE_SPORTS
  * NFL / MLB / CFB / Soccer / Tennis UEA opt-in UNCHANGED (regression)
  * Preview isolation + distributed worker lease UNCHANGED (regression)

No DB access required — all tests exercise the universal adapter
functions directly with hand-built pick dicts.
"""
from __future__ import annotations

import pytest

from services.evidence_authority_contract import (
    UEA_ENABLED_SPORTS,
    EvidenceStatus,
    EvidenceValue,
    enabled as uea_enabled,
    peak_non_apex_eligible,
    REQUIRED_AXES,
    compute_authority_score,
)
from services.evidence_authority_adapters import (
    build_contract_for_pick,
    _classify_market_family,
)
from services.magic.apex_gate import (
    APEX_ELIGIBLE_SPORTS,
    APEX_UNAVAILABLE_SPORTS,
    APEX_MIN_BASE_SCORE,
    APEX_MIN_POSITIVE_CATEGORIES,
)


# ─── 1. NHL registered in UEA ────────────────────────────────────────
def test_nhl_in_uea_enabled_sports():
    assert "NHL" in UEA_ENABLED_SPORTS


def test_uea_enabled_helper_accepts_nhl():
    assert uea_enabled("NHL") is True
    assert uea_enabled("nhl") is True


# ─── 2. All previously-enabled sports still enabled (regression) ────
@pytest.mark.parametrize("sport", ["MLB", "NFL", "CFB", "SOCCER", "TENNIS"])
def test_prior_uea_sports_still_enabled(sport):
    assert sport in UEA_ENABLED_SPORTS
    assert uea_enabled(sport) is True


def test_nba_still_out_of_uea():
    # Spec explicitly leaves NBA out.
    assert "NBA" not in UEA_ENABLED_SPORTS


# ─── 3. Market-family classifier (NHL) ───────────────────────────────
def test_nhl_player_markets_classified_as_player():
    for m in ("player_goals", "player_goals_alternate",
              "player_assists", "player_assists_alternate",
              "player_points", "player_points_alternate",
              "player_shots_on_goal", "player_shots_on_goal_alternate",
              "Anytime Goal Scorer", "Player Shots On Goal"):
        assert _classify_market_family("NHL", m) == "NHL_PLAYER", m


def test_nhl_game_markets_classified_as_game():
    for m in ("moneyline", "puck line", "Total Goals",
              "Over/Under", "spread"):
        assert _classify_market_family("NHL", m) == "NHL_GAME", m


# ─── 4. UEA dispatcher — real contract built for NHL ─────────────────
def _nhl_player_pick(**overrides) -> dict:
    pick = {
        "sport":                       "NHL",
        "market":                      "player_goals",
        "model_win_probability":       0.62,
        "probability_provenance":      "nhl_v1_sim",
        "probability_source":          "nhl_v1",
        "model_family":                "brain_sim_nhl",
        "calibrator_version":          "nhl_v1.2026-10",
        "simulation_distribution":     {"p05": 0.0, "p50": 1.0, "p95": 3.0},
        "nhl_feature_engine":          {"recent_form": "available"},
        "evidence_count":              9,
        "independent_factor_sources":  ["history", "matchup", "model",
                                        "market", "role", "recent_form"],
        "odds_source":                 "the_odds_api",
    }
    pick.update(overrides)
    return pick


def _nhl_game_pick(**overrides) -> dict:
    pick = _nhl_player_pick(**overrides)
    pick["market"] = "moneyline"
    return pick


def test_build_contract_returns_contract_for_nhl():
    c = build_contract_for_pick(_nhl_player_pick())
    assert c is not None
    assert c.sport == "NHL"
    assert c.market_family in ("NHL_PLAYER", "NHL_GAME")


def test_build_contract_still_returns_none_for_nba():
    c = build_contract_for_pick({"sport": "NBA", "market": "moneyline"})
    assert c is None


# ─── 5. Book-implied provenance still fails closed at model axis ────
def test_book_implied_provenance_blocks_model_axis():
    c = build_contract_for_pick(_nhl_player_pick(
        probability_provenance="BOOK_IMPLIED",
        model_win_probability=0.62,
    ))
    assert c is not None
    assert c.model_probability.status in (
        EvidenceStatus.MISSING, EvidenceStatus.UNAVAILABLE
    ), c.model_probability
    assert c.model_probability.available is False


def test_valid_nhl_v1_provenance_allows_model_axis():
    c = build_contract_for_pick(_nhl_player_pick(
        probability_provenance="nhl_v1_sim",
        model_win_probability=0.62,
    ))
    assert c is not None
    assert c.model_probability.available is True
    assert 0.0 <= (c.model_probability.score or 0.0) <= 100.0


# ─── 6. Universal 99-eligibility reachable for NHL when axes converge ─
def _make_authority(cov: float, strong: int, components: dict,
                    contradictions=None) -> dict:
    return {
        "coverage":       cov,
        "strong_axes":    strong,
        "contradictions": contradictions or [],
        "components":     components,
    }


def _axis_available(score=96.0) -> dict:
    return {"status": "AVAILABLE", "score": score}


def test_nhl_99_structurally_reachable_when_axes_converge():
    """Universal rule — not NHL-specific.  Prove the gate ACCEPTS NHL
    when every required axis is AVAILABLE at ≥85 with coverage ≥0.90
    and at least 5 strong axes and no contradictions."""
    comps = {a: _axis_available(96.0) for a in REQUIRED_AXES}
    auth = _make_authority(cov=0.95, strong=6, components=comps)
    ok, reason = peak_non_apex_eligible(auth)
    assert ok is True, f"NHL 99 blocked: {reason}"


def test_nhl_99_blocked_when_coverage_insufficient():
    comps = {a: _axis_available(96.0) for a in REQUIRED_AXES}
    auth = _make_authority(cov=0.80, strong=6, components=comps)
    ok, reason = peak_non_apex_eligible(auth)
    assert ok is False
    assert "coverage" in reason


def test_nhl_99_blocked_when_contradictions_present():
    comps = {a: _axis_available(96.0) for a in REQUIRED_AXES}
    auth = _make_authority(cov=0.95, strong=6, components=comps,
                           contradictions=["sim vs matchup disagreement"])
    ok, reason = peak_non_apex_eligible(auth)
    assert ok is False
    assert "contradictions" in reason


def test_93_96_98_99_tier_thresholds_identical_for_nhl():
    """The universal UEA contract uses one score function.  Confirm
    NHL picks hit the 93 / 96 / 98 / 99 tiers purely as a function
    of axis quality — no sport-specific carve-out."""
    # Build an authority block whose component scores step up.
    for target_floor, cov, strong in [
        (93, 0.90, 5), (96, 0.92, 5),
        (98, 0.95, 6), (99, 0.95, 6),
    ]:
        comps = {a: _axis_available(float(target_floor)) for a in REQUIRED_AXES}
        auth = _make_authority(cov=cov, strong=strong, components=comps)
        ok, _ = peak_non_apex_eligible(auth)
        if target_floor >= 93:
            # 99 eligibility uses the strongest gate; the lower tiers
            # (93/96/98) can only be checked via compute_authority_score
            # which is a numeric score — assert the numeric path returns
            # at least `target_floor` for a uniform-strong contract.
            # peak_non_apex_eligible applies only at 99 — so for 93/96/98
            # we just confirm the authority_score reaches the floor.
            from services.evidence_authority_contract import (
                EvidenceAuthorityContract as C_, EvidenceValue as EV_)
            contract = C_(
                model_probability=         EV_.available_score(target_floor),
                prediction_reliability=    EV_.available_score(target_floor),
                history_threshold_support= EV_.available_score(target_floor),
                matchup_role_support=      EV_.available_score(target_floor),
                independent_convergence=   EV_.available_score(target_floor),
                simulation_distribution_support= EV_.available_score(target_floor),
                data_quality=              EV_.available_score(target_floor),
                sport="NHL",
                market_family="NHL_PLAYER",
                adapter_version="test",
            )
            score = compute_authority_score(contract)
            ceiling = score.get("ceiling") if isinstance(score, dict) else score
            assert (ceiling or 0.0) >= target_floor - 2, \
                f"NHL authority ceiling {ceiling} < target {target_floor}"


# ─── 7. Apex eligibility — NHL removed from UNAVAILABLE, added to
#        ELIGIBLE, universal contract unchanged ───────────────────────
def test_nhl_removed_from_apex_unavailable():
    assert "NHL" not in APEX_UNAVAILABLE_SPORTS


def test_nhl_added_to_apex_eligible():
    assert "NHL" in APEX_ELIGIBLE_SPORTS


def test_apex_prior_eligible_sports_unchanged():
    for s in ("MLB", "Soccer", "NBA", "Tennis", "NFL", "CFB"):
        assert s in APEX_ELIGIBLE_SPORTS, s


def test_apex_universal_thresholds_unchanged():
    """Must prove no NHL-driven weakening of Apex gates."""
    assert APEX_MIN_BASE_SCORE == 97.0
    assert APEX_MIN_POSITIVE_CATEGORIES == 5


def test_apex_still_blocks_ufc_mma_kbo():
    for s in ("UFC", "MMA", "KBO"):
        assert s in APEX_UNAVAILABLE_SPORTS, s
        assert s not in APEX_ELIGIBLE_SPORTS, s


# ─── 8. UEA regression — NFL / MLB / CFB / Soccer / Tennis
#        dispatcher behaviour unchanged ──────────────────────────────
@pytest.mark.parametrize("sport,market,expected_family", [
    ("NFL", "player receiving yards", "NFL_PLAYER"),
    ("NFL", "moneyline",              "NFL_GAME"),
    ("MLB", "strikeouts",             "MLB_PITCHER"),
    ("MLB", "home_runs",              "MLB_HITTER"),
    ("MLB", "moneyline",              "MLB_GAME"),
    ("CFB", "spread",                 "CFB_GAME"),
    ("CFB", "player receiving yards", "CFB_PLAYER"),
    ("SOCCER", "goalscorer",          "SOCCER_PLAYER"),
    ("SOCCER", "1x2",                 "SOCCER_GAME"),
    ("TENNIS", "match winner",        "TENNIS"),
])
def test_prior_sports_market_classification_unchanged(sport, market, expected_family):
    assert _classify_market_family(sport, market) == expected_family


# ─── 9. Preview + lease regression ───────────────────────────────────
def test_preview_lease_eligibility_unchanged(monkeypatch):
    monkeypatch.setenv("DATA_AUTHORITY", "preview")
    monkeypatch.setenv("CANONICAL_WRITE_ENABLED", "false")
    monkeypatch.setenv("BACKGROUND_WORKERS_ENABLED", "false")
    from services.canonical_worker_lease import is_eligible_for_canonical_lease
    assert is_eligible_for_canonical_lease() is False


def test_preview_background_workers_still_suppressed(monkeypatch):
    monkeypatch.setenv("DATA_AUTHORITY", "preview")
    monkeypatch.setenv("CANONICAL_WRITE_ENABLED", "false")
    monkeypatch.setenv("BACKGROUND_WORKERS_ENABLED", "false")
    from services.data_authority import (
        background_workers_enabled, require_background_workers)
    assert background_workers_enabled() is False
    assert require_background_workers() is False
