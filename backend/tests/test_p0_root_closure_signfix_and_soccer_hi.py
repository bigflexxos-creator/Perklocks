"""P0 Root Closure — CFB sign-fix + Soccer HI identity resolution
regression suite (2026-06).

Locks in:
    1. CFB SP+ sign-fix maths remain correct (defense contribution
       uses `(a_def - 25.0)` not `(25.0 - a_def)`).
    2. Northwestern @ Indiana Total O47.5 produces WP ≈ 42-47%
       (never the pre-fix 96.69%).
    3. Soccer team identity aliases catch "Nott'm Forest",
       "Ipswich" (short forms) and canonicalise to EPL names.
    4. Soccer BTTS / total / handicap / double_chance entity
       resolution NEVER lets selection strings ("Yes"/"No") leak
       through — always falls back to home_team from event string.
"""
from __future__ import annotations
import pytest

from services.cfb_game_model import (
    estimate_cfb_game,
    cfb_over_probability,
)
from services.soccer_team_identity import canonical_team_key, teams_equal


INDIANA = {
    "rating": 32.4, "offense_rating": 40.8,
    "defense_rating": 9.9, "year": 2025,
}
NORTHWESTERN = {
    "rating": 6.0, "offense_rating": 24.4,
    "defense_rating": 20.0, "year": 2025,
}


def test_cfb_signfix_indiana_over_47p5_is_not_extreme():
    ratings = {"indiana": INDIANA, "northwestern": NORTHWESTERN}
    r = estimate_cfb_game(
        ctx={"cfb_sp_ratings_by_team": ratings,
             "cfb_returning_prod_by_team": {},
             "cfb_portal_net_by_team": {}},
        home_team="Indiana", away_team="Northwestern",
    )
    assert r.available is True
    # Corrected formula:
    #   h_pts = 40.8 + (20 - 25)   = 35.8
    #   a_pts = 24.4 + (9.9 - 25)  = 9.3
    #   total = 45.1
    # Bug produced total ~85.3 → P(Over 47.5) ~99.9%.
    # Fix produces total ~45 → P(Over 47.5) < 55%.
    assert 40.0 <= r.expected_total <= 50.0, (
        f"Expected total ~45 with signfix, got {r.expected_total}")
    p_over = cfb_over_probability(
        r.expected_total, 47.5, True, r.total_sigma)
    assert p_over < 0.55, (
        f"P(Over 47.5) must be sub-55% with signfix, got {p_over:.4f}")
    assert p_over > 0.30, (
        f"P(Over 47.5) sanity — got {p_over:.4f}")


def test_soccer_team_identity_aliases_nottm_forest():
    assert canonical_team_key("Nottingham Forest") == canonical_team_key("Nott'm Forest")
    assert canonical_team_key("Nott'm Forest") == "nottingham forest"
    assert teams_equal("Nott'm Forest", "Nottingham Forest") is True


def test_soccer_team_identity_covers_epl_short_forms():
    assert canonical_team_key("Ipswich Town") == canonical_team_key("Ipswich")
    assert canonical_team_key("Newcastle United") == canonical_team_key("Newcastle")
    assert canonical_team_key("Brighton and Hove Albion") == canonical_team_key("Brighton")


def test_soccer_hi_entity_resolver_never_lets_selection_leak():
    """BTTS 'Yes' / 'No' picks with team=Yes noise from publishers
    must fall through to home_team.  This exercises the fix in
    routes/historical_intelligence_routes._resolve_entity."""
    from routes.historical_intelligence_routes import _resolve_entity
    for family in ("btts", "total", "double_chance", "handicap"):
        pick = {
            "sport": "Soccer",
            "market": "BTTS Yes" if family == "btts" else "Total Goals Over 2.5",
            "team": "Yes",  # publisher noise
            "canonical_team_id": "fallback:x",
            "home_team": None,
            "away_team": None,
            "event": "Crystal Palace @ Brighton and Hove Albion",
        }
        et, eid, ename = _resolve_entity(pick, "Soccer", family)
        assert et == "team"
        assert ename == "Brighton and Hove Albion", (
            f"family={family}: expected home_team fallback "
            f"'Brighton and Hove Albion', got {ename!r}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
