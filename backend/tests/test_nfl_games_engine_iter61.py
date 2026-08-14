"""Iter 61 — Backend smoke tests for the new NFL Game-Bets engine routes.

Covers:
  • GET /api/nfl/games/predict (ml | spread | total)
  • GET /api/nfl/games/safe-alts
  • GET /api/nfl/games/teams
  • GET /api/nfl/games/safe-bets
  • Pre-existing smoke: /api/nfl/safe-bets, /api/nfl/atd/leaderboard
"""
import math
import os
import pytest
import requests

BASE = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "https://canonical-parity.preview.emergentagent.com").rstrip("/")
HOME = "Seattle Seahawks"
AWAY = "Houston Texans"


@pytest.fixture(scope="module")
def auth_headers():
    """Login as demo user and return Bearer headers."""
    r = requests.post(
        f"{BASE}/api/auth/login",
        json={"email": "demo@lockscore.ai", "password": "demo123"},
        timeout=20,
    )
    assert r.status_code == 200, f"login failed {r.status_code}: {r.text[:300]}"
    tok = r.json().get("access_token")
    assert tok, "no access_token returned"
    return {"Authorization": f"Bearer {tok}"}


def _finite(*vals):
    for v in vals:
        assert isinstance(v, (int, float)), f"non-numeric: {v!r}"
        assert math.isfinite(float(v)), f"not finite: {v!r}"


# ─────────────────────────── /games/predict ───────────────────────────

class TestGamePredict:
    def test_moneyline(self, auth_headers):
        r = requests.get(
            f"{BASE}/api/nfl/games/predict",
            params={"home": HOME, "away": AWAY, "market": "ml"},
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 200, r.text[:300]
        j = r.json()
        for k in ("matchup", "market", "expected_margin", "p_home", "p_away",
                  "recommended_side", "true_probability", "home_rating", "away_rating"):
            assert k in j, f"missing key: {k} — got {list(j.keys())}"
        assert j["market"] == "moneyline"
        _finite(j["expected_margin"], j["p_home"], j["p_away"], j["true_probability"])
        # probabilities should sum ~1.0
        assert abs(j["p_home"] + j["p_away"] - 1.0) < 1e-3
        # 0 <= p <= 1
        assert 0.0 <= j["p_home"] <= 1.0
        assert 0.0 <= j["p_away"] <= 1.0
        # recommended_side ∈ {home, away}
        assert j["recommended_side"] in ("home", "away")
        # ratings shape
        for r_obj in (j["home_rating"], j["away_rating"]):
            for fk in ("team", "rating", "ppg", "opp_ppg", "win_rate", "n_games"):
                assert fk in r_obj

    def test_spread(self, auth_headers):
        r = requests.get(
            f"{BASE}/api/nfl/games/predict",
            params={"home": HOME, "away": AWAY, "market": "spread", "spread": -3.5},
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 200, r.text[:300]
        j = r.json()
        assert j["market"] == "spread"
        assert "p_home_covers" in j and "p_away_covers" in j
        _finite(j["p_home_covers"], j["p_away_covers"], j["true_probability"])
        assert abs(j["p_home_covers"] + j["p_away_covers"] - 1.0) < 1e-3

    def test_total(self, auth_headers):
        r = requests.get(
            f"{BASE}/api/nfl/games/predict",
            params={"home": HOME, "away": AWAY, "market": "total", "total": 44.5},
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 200, r.text[:300]
        j = r.json()
        assert j["market"] == "total"
        assert "p_over" in j and "p_under" in j
        _finite(j["p_over"], j["p_under"], j["true_probability"])
        assert abs(j["p_over"] + j["p_under"] - 1.0) < 1e-3


# ─────────────────────────── /games/safe-alts ───────────────────────────

class TestSafeAlts:
    def test_safe_alts(self, auth_headers):
        r = requests.get(
            f"{BASE}/api/nfl/games/safe-alts",
            params={"home": HOME, "away": AWAY, "min_probability": 0.78},
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 200, r.text[:300]
        j = r.json()
        for k in ("matchup", "favored", "expected_margin", "expected_total",
                  "ml_pick", "spread_pick", "total_pick", "home_rating", "away_rating"):
            assert k in j, f"missing key: {k}"
        # At least the slot keys exist (values may be None during off-season).
        # Ratings must be present and numeric.
        for r_obj in (j["home_rating"], j["away_rating"]):
            assert r_obj and "rating" in r_obj


# ─────────────────────────── /games/teams ───────────────────────────

class TestTeamsLeaderboard:
    def test_teams_limit(self, auth_headers):
        r = requests.get(
            f"{BASE}/api/nfl/games/teams",
            params={"limit": 10},
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 200, r.text[:300]
        j = r.json()
        assert "teams" in j and isinstance(j["teams"], list)
        assert len(j["teams"]) <= 10
        for team in j["teams"]:
            for fk in ("team", "rating", "ppg", "opp_ppg", "win_rate", "n_games"):
                assert fk in team, f"missing field {fk} in {team}"
            _finite(team["rating"], team["ppg"], team["opp_ppg"], team["win_rate"])
            assert isinstance(team["n_games"], int)


# ─────────────────────────── /games/safe-bets ───────────────────────────

class TestGameSafeBets:
    def test_safe_bets(self, auth_headers):
        r = requests.get(
            f"{BASE}/api/nfl/games/safe-bets",
            params={"limit": 10, "min_probability": 0.78},
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 200, r.text[:300]
        j = r.json()
        for k in ("count", "matchups_evaluated", "bets"):
            assert k in j, f"missing {k}"
        assert isinstance(j["bets"], list)
        # bets may be empty during off-season — acceptable per spec.


# ─────────────────────────── Smoke: pre-existing ───────────────────────────

class TestPreExistingSmoke:
    def test_safe_bets_player_props(self, auth_headers):
        r = requests.get(
            f"{BASE}/api/nfl/safe-bets",
            params={"limit": 5},
            headers=auth_headers, timeout=30,
        )
        assert r.status_code == 200, r.text[:300]
        j = r.json()
        # The legacy endpoint returns picks list — verify a list exists.
        assert "picks" in j and isinstance(j["picks"], list)

    def test_atd_leaderboard(self, auth_headers):
        r = requests.get(
            f"{BASE}/api/nfl/atd/leaderboard",
            params={"limit": 5},
            headers=auth_headers, timeout=30,
        )
        assert r.status_code == 200, r.text[:300]
        j = r.json()
        assert "picks" in j and isinstance(j["picks"], list)
