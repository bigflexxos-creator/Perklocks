"""Soccer Goal Scorer Simulator — Phase B.1 tests.

Validates:
- ATGS picks route to sim_soccer_scorer and emit sim_market_category='scorer_atgs'
- λ is calibrated to model_wp (within ±3pp on healthy WPs)
- key_insights parsers populate sim_shots_per_game, sim_player_xg_per_game, sim_recent_goal_rate
- Anomalous low-WP picks (~1%) produce strong positive disagreement / 'stronger' signal
- Non-scorer Soccer markets still use team Poisson (sim_market_category NOT scorer_*)
- Regression: MLB / NBA / Tennis sims still work
"""
import os
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "https://canonical-parity.preview.emergentagent.com").rstrip("/")

OYARZABAL_PICK_ID = "fc87a363-f776-5a6e-b107-bd8c8d099693"   # Mikel Oyarzabal ATGS
MESSI_PICK_ID = "2fada0b5-cea7-5634-93a7-da8b9a7df446"        # Lionel Messi ATGS

ANOMALOUS_PICKS = [
    "5f040638-935c-41e0-b6e3-91d78ce049ad",  # Can Yilmaz Uzun
    "30dd5324-4456-4c27-bb02-2923a014f82f",  # Kenan Yildiz
    "dbdf61da-c257-4cb8-9ce6-cec7e6041e83",  # Benjamin Nygren
]

# Non-scorer soccer
TOTAL_GOALS_PICK = "768b2830-63b9-46e7-aeea-828eca63f1cf"

# Regression IDs (from iteration_24)
MLB_PICK = "57f9f5a1-8d4b-530a-99f5-3ba5c186b718"
NBA_PICK = "7a0eee4d-cb32-4bea-9517-dcca27bf0c4a"
TENNIS_PICK = "6fcdec67-aa21-4553-a802-3e26943d2839"


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"email": "demo@lockscore.ai", "password": "demo123"},
                      timeout=20)
    assert r.status_code == 200, f"Login failed: {r.text}"
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def headers(token):
    return {"Authorization": f"Bearer {token}"}


def _get_pick(pick_id, headers):
    r = requests.get(f"{BASE_URL}/api/picks/{pick_id}", headers=headers, timeout=20)
    assert r.status_code == 200, f"Pick fetch failed: {r.status_code} {r.text[:200]}"
    return r.json()


def _get_sim(pick_id, headers):
    r = requests.get(f"{BASE_URL}/api/picks/{pick_id}/simulation", headers=headers, timeout=30)
    assert r.status_code == 200, f"Sim fetch failed: {r.status_code} {r.text[:200]}"
    return r.json()


class TestAtgsSchema:
    """ATGS sim returns correct schema, market category, runs."""

    def test_oyarzabal_schema(self, headers):
        sim = _get_sim(OYARZABAL_PICK_ID, headers)
        assert sim["sim_market_category"] == "scorer_atgs"
        assert sim["sim_runs"] == 10000
        assert isinstance(sim["sim_player_xg"], (int, float))
        assert isinstance(sim["sim_expected_goals"], (int, float))
        assert isinstance(sim["sim_p_score_2plus"], (int, float))
        assert isinstance(sim["sim_p_hattrick"], (int, float))
        # P(2+) should be less than P(score >=1)
        assert sim["sim_p_score_2plus"] < sim["sim_win_probability"]
        assert sim["sim_p_hattrick"] < sim["sim_p_score_2plus"]
        # CI is well-formed
        assert sim["sim_ci_lower"] <= sim["sim_win_probability"] <= sim["sim_ci_upper"]

    def test_messi_schema(self, headers):
        sim = _get_sim(MESSI_PICK_ID, headers)
        assert sim["sim_market_category"] == "scorer_atgs"
        assert sim["sim_runs"] == 10000


class TestCalibration:
    """λ is calibrated to model_wp so sim P(score≥1) lands within ±3pp."""

    def test_oyarzabal_calibrated_to_model_wp(self, headers):
        pick = _get_pick(OYARZABAL_PICK_ID, headers)
        sim = _get_sim(OYARZABAL_PICK_ID, headers)
        model_wp = float(pick["win_probability"])
        delta = abs(sim["sim_win_probability"] - model_wp)
        assert delta <= 3.0, f"Sim {sim['sim_win_probability']} vs model {model_wp} delta {delta}pp > 3pp"

    def test_messi_calibrated_to_model_wp(self, headers):
        pick = _get_pick(MESSI_PICK_ID, headers)
        sim = _get_sim(MESSI_PICK_ID, headers)
        model_wp = float(pick["win_probability"])
        delta = abs(sim["sim_win_probability"] - model_wp)
        assert delta <= 3.0, f"Sim {sim['sim_win_probability']} vs model {model_wp} delta {delta}pp > 3pp"


class TestKeyInsightsParsing:
    """Verify regex parsers populate sim_shots_per_game, sim_player_xg_per_game, sim_recent_goal_rate."""

    def test_kenan_yildiz_parses_xg_and_shots(self, headers):
        """Pick insights include 'Expected goals (xG) average: 0.73 per game' and '4 shots on target per match'."""
        sim = _get_sim("30dd5324-4456-4c27-bb02-2923a014f82f", headers)
        # xG/match parser should fire
        assert sim.get("sim_player_xg_per_game") is not None, f"Expected sim_player_xg_per_game in {sim}"
        assert abs(sim["sim_player_xg_per_game"] - 0.73) < 0.05
        # shots parser should fire
        assert sim.get("sim_shots_per_game") is not None, f"Expected sim_shots_per_game in {sim}"
        assert abs(sim["sim_shots_per_game"] - 4.0) < 0.01

    def test_nygren_parses_recent_goal_rate(self, headers):
        """Pick insights include 'scored in 5 of last 10 club matches'."""
        sim = _get_sim("dbdf61da-c257-4cb8-9ce6-cec7e6041e83", headers)
        assert sim.get("sim_recent_goal_rate") is not None, f"Expected sim_recent_goal_rate in {sim}"
        # 5/10 = 50%
        assert abs(sim["sim_recent_goal_rate"] - 50.0) < 1.0

    def test_uzun_parses_shots_and_recent(self, headers):
        sim = _get_sim("5f040638-935c-41e0-b6e3-91d78ce049ad", headers)
        assert sim.get("sim_shots_per_game") is not None
        assert sim.get("sim_recent_goal_rate") is not None


class TestAnomalousLowWp:
    """Picks with model_wp ~1% should produce stronger signal because parsed xG suggests much higher P."""

    @pytest.mark.parametrize("pid", ANOMALOUS_PICKS)
    def test_low_wp_picks_signal_stronger(self, pid, headers):
        pick = _get_pick(pid, headers)
        sim = _get_sim(pid, headers)
        model_wp = float(pick["win_probability"])
        assert model_wp <= 2.0, f"Pick {pid} model_wp {model_wp}% not low; review pick fixtures"
        # Sim should produce a higher probability because key_insights parsed xG > 0
        assert sim["sim_win_probability"] > model_wp, \
            f"Sim WP {sim['sim_win_probability']} not greater than model {model_wp} for {pid}"
        # Should be flagged stronger
        assert sim["sim_disagreement_with_model"] > 5.0, \
            f"Expected positive disagreement >5 for {pid}; got {sim['sim_disagreement_with_model']}"
        assert sim["sim_signal"] == "stronger"
        # And scorer category
        assert sim["sim_market_category"].startswith("scorer_")


class TestNonScorerSoccerRouting:
    """Non-scorer Soccer markets (Total Goals, BTTS, ML) must NOT go to the scorer sim."""

    def test_total_goals_uses_team_poisson(self, headers):
        sim = _get_sim(TOTAL_GOALS_PICK, headers)
        cat = sim.get("sim_market_category", "")
        assert not cat.startswith("scorer_"), f"Expected non-scorer category, got {cat}"
        # Should expose lambda_pick / lambda_opp from team-level Poisson model
        assert "sim_lambda_pick" in sim or "sim_lambda_opp" in sim or cat in {"totals", "btts", "moneyline", "draw"}


class TestRegressionOtherSports:
    """Regression: MLB / NBA / Tennis simulators still functional."""

    def test_mlb_sim_works(self, headers):
        sim = _get_sim(MLB_PICK, headers)
        assert sim["sim_runs"] >= 1000
        assert "sim_win_probability" in sim
        # MLB sim should NOT carry scorer fields
        assert not str(sim.get("sim_market_category", "")).startswith("scorer_")

    def test_nba_sim_works(self, headers):
        sim = _get_sim(NBA_PICK, headers)
        assert "sim_win_probability" in sim
        assert sim["sim_runs"] >= 1000

    def test_tennis_sim_works(self, headers):
        sim = _get_sim(TENNIS_PICK, headers)
        assert "sim_win_probability" in sim
        # Tennis uses 3000 runs per iteration_24 notes
        assert sim["sim_runs"] >= 1000
