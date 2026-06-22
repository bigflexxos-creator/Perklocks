"""Phase B — Soccer / NBA / Tennis Monte Carlo Simulator tests.

Coverage:
  • GET /api/picks/{id}/simulation for Soccer pick → returns sim_lambda_pick,
    sim_lambda_opp, sim_market_category (moneyline/totals/btts/atgs/draw), sim_runs=10000
  • GET /api/picks/{id}/simulation for NBA pick → returns sim_market_category,
    sim_lambda OR sim_expected_margin (depends on market)
  • GET /api/picks/{id}/simulation for Tennis pick → returns sim_pick_serve_pct,
    sim_opp_serve_pct, sim_avg_total_games, sim_pick_match_win_pct
  • GET /api/picks/{id}/simulation returns 404 for UFC (unsupported sport)
  • GET /api/analytics/sim-backtest?days=30 → aggregate + by_sport (no sport filter)
  • GET /api/analytics/sim-backtest?days=30&sport=MLB → no by_sport (filter applied)
  • Math sanity for NBA/Tennis: sim_wp within ±10pp of model_wp (calibrated)
  • Soccer/NBA/Tennis simulate_*_pick() unit tests
"""
import os
import pytest
import requests
from dotenv import load_dotenv

load_dotenv("/app/frontend/.env")

BASE_URL = (os.environ.get("EXPO_PUBLIC_BACKEND_URL")
            or os.environ.get("EXPO_BACKEND_URL")
            or "").rstrip("/")
assert BASE_URL, "EXPO_PUBLIC_BACKEND_URL not set"

DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"

# Verified sample pick ids (from review request)
SOCCER_PICK_ID = "fc87a363-f776-5a6e-b107-bd8c8d099693"
TENNIS_PICK_ID = "6fcdec67-aa21-4553-a802-3e26943d2839"
MLB_PICK_ID = "57f9f5a1-8d4b-530a-99f5-3ba5c186b718"


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def auth_token():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}, timeout=20)
    if r.status_code != 200:
        s.post(f"{BASE_URL}/api/auth/register",
               json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD, "name": "Demo"}, timeout=20)
        r = s.post(f"{BASE_URL}/api/auth/login",
                   json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}, timeout=20)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def headers(auth_token):
    return {"Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def nba_pick_id(headers):
    """Find any NBA pick (offseason in Jun-2026 → /picks/today may be empty)."""
    import asyncio
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")
    from motor.motor_asyncio import AsyncIOMotorClient
    async def grab():
        c = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = c[os.environ["DB_NAME"]]
        p = await db.picks.find_one({"sport": "NBA"}, {"_id": 0, "id": 1})
        return p["id"] if p else None
    return asyncio.get_event_loop().run_until_complete(grab())


@pytest.fixture(scope="module")
def ufc_pick_id():
    import asyncio
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")
    from motor.motor_asyncio import AsyncIOMotorClient
    async def grab():
        c = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = c[os.environ["DB_NAME"]]
        p = await db.picks.find_one({"sport": "UFC"}, {"_id": 0, "id": 1})
        return p["id"] if p else None
    return asyncio.get_event_loop().run_until_complete(grab())


# ── Soccer sim endpoint ────────────────────────────────────────────────


class TestSoccerSimulation:
    def test_soccer_pick_sim_schema(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/{SOCCER_PICK_ID}/simulation",
                         headers=headers, timeout=30)
        assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
        body = r.json()
        for key in ["sim_win_probability", "sim_ci_lower", "sim_ci_upper",
                    "sim_runs", "sim_signal", "sim_disagreement_with_model",
                    "sim_lambda_pick", "sim_lambda_opp", "sim_market_category"]:
            assert key in body, f"missing key: {key} in {body}"
        assert body["sim_runs"] == 10000
        assert body["sim_market_category"] in ("moneyline", "totals", "btts", "atgs", "draw")
        assert body["sim_lambda_pick"] > 0
        assert body["sim_lambda_opp"] > 0
        assert 0 <= body["sim_win_probability"] <= 100


# ── NBA sim endpoint ───────────────────────────────────────────────────


class TestNBASimulation:
    def test_nba_pick_sim_schema(self, headers, nba_pick_id):
        if not nba_pick_id:
            pytest.skip("No NBA pick available in DB")
        r = requests.get(f"{BASE_URL}/api/picks/{nba_pick_id}/simulation",
                         headers=headers, timeout=30)
        assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
        body = r.json()
        for key in ["sim_win_probability", "sim_ci_lower", "sim_ci_upper",
                    "sim_runs", "sim_signal", "sim_market_category"]:
            assert key in body, f"missing key: {key} in {body}"
        assert body["sim_runs"] == 10000
        assert body["sim_market_category"] in (
            "moneyline", "points", "rebounds", "assists",
            "threes", "pra", "team_total"
        )
        # Either sim_lambda (counting prop) or sim_expected_margin (moneyline)
        # or sim_expected_total (team_total)
        has_param = ("sim_lambda" in body or
                     "sim_expected_margin" in body or
                     "sim_expected_total" in body)
        assert has_param, f"NBA sim missing lambda/margin/total: {body}"

    def test_nba_within_10pp_of_model(self, headers, nba_pick_id):
        """Calibrated sim should land within ±10pp of model_wp."""
        if not nba_pick_id:
            pytest.skip("No NBA pick available")
        # Fetch model wp
        r1 = requests.get(f"{BASE_URL}/api/picks/{nba_pick_id}",
                          headers=headers, timeout=30)
        if r1.status_code != 200:
            pytest.skip("Cannot fetch NBA pick detail")
        model_wp = r1.json().get("win_probability") or 0
        r2 = requests.get(f"{BASE_URL}/api/picks/{nba_pick_id}/simulation",
                          headers=headers, timeout=30)
        assert r2.status_code == 200
        sim_wp = r2.json()["sim_win_probability"]
        delta = abs(sim_wp - model_wp)
        assert delta <= 10.0, f"NBA sim should be calibrated within ±10pp; got |{sim_wp} - {model_wp}| = {delta}"


# ── Tennis sim endpoint ────────────────────────────────────────────────


class TestTennisSimulation:
    def test_tennis_pick_sim_schema(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/{TENNIS_PICK_ID}/simulation",
                         headers=headers, timeout=30)
        assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
        body = r.json()
        for key in ["sim_win_probability", "sim_pick_serve_pct", "sim_opp_serve_pct",
                    "sim_avg_total_games", "sim_pick_match_win_pct",
                    "sim_market_category", "sim_signal"]:
            assert key in body, f"missing key: {key} in {body}"
        assert body["sim_market_category"] in ("moneyline", "totals")
        assert 0 < body["sim_pick_serve_pct"] < 100
        assert 0 < body["sim_opp_serve_pct"] < 100
        assert body["sim_avg_total_games"] > 0

    def test_tennis_within_10pp_of_model(self, headers):
        """Calibrated tennis sim should land within ±10pp of model_wp."""
        r1 = requests.get(f"{BASE_URL}/api/picks/{TENNIS_PICK_ID}",
                          headers=headers, timeout=30)
        if r1.status_code != 200:
            pytest.skip("Cannot fetch Tennis pick detail")
        model_wp = r1.json().get("win_probability") or 0
        r2 = requests.get(f"{BASE_URL}/api/picks/{TENNIS_PICK_ID}/simulation",
                          headers=headers, timeout=30)
        sim_wp = r2.json()["sim_win_probability"]
        delta = abs(sim_wp - model_wp)
        assert delta <= 10.0, f"Tennis sim should be calibrated; got |{sim_wp} - {model_wp}| = {delta}"


# ── Unsupported sport returns 404 ──────────────────────────────────────


class TestUnsupportedSport:
    def test_ufc_returns_404(self, headers, ufc_pick_id):
        if not ufc_pick_id:
            pytest.skip("No UFC pick in DB")
        r = requests.get(f"{BASE_URL}/api/picks/{ufc_pick_id}/simulation",
                         headers=headers, timeout=30)
        assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text}"
        detail = r.json().get("detail", "").lower()
        assert "ufc" in detail or "not" in detail or "unavailable" in detail


# ── Backtest endpoint per-sport ────────────────────────────────────────


class TestSimBacktestBySport:
    def test_backtest_no_sport_returns_by_sport(self, headers):
        r = requests.get(f"{BASE_URL}/api/analytics/sim-backtest?days=30",
                         headers=headers, timeout=30)
        assert r.status_code == 200
        body = r.json()
        assert "n" in body
        assert "days" in body and body["days"] == 30
        assert "by_sport" in body, "Aggregate response must include by_sport key"
        # by_sport may be empty dict if no sim picks settled — that's still
        # valid (key present). When sim picks exist, must include all 4.
        assert isinstance(body["by_sport"], dict)

    def test_backtest_with_sport_no_by_sport(self, headers):
        r = requests.get(f"{BASE_URL}/api/analytics/sim-backtest?days=30&sport=MLB",
                         headers=headers, timeout=30)
        assert r.status_code == 200
        body = r.json()
        assert body.get("sport") == "MLB"
        # by_sport should NOT be active (either absent or null)
        bs = body.get("by_sport")
        assert bs is None or bs == {}, f"sport filter should suppress by_sport, got {bs}"


# ── Unit tests: simulate_*_pick functions ──────────────────────────────


class TestSimulatorUnitTests:
    def test_simulate_soccer_pick_moneyline(self):
        from brain.sim_soccer import simulate_soccer_pick
        pick = {
            "sport": "Soccer",
            "market": "Manchester City Moneyline",
            "event": "Manchester City vs Liverpool",
            "win_probability": 55.0,
            "factors": {"xG Combined": 65, "xG Difference": 60},
        }
        out = simulate_soccer_pick(pick)
        assert out is not None
        assert out["sim_runs"] == 10000
        assert out["sim_market_category"] == "moneyline"
        assert out["sim_lambda_pick"] > 0 and out["sim_lambda_opp"] > 0

    def test_simulate_soccer_totals(self):
        from brain.sim_soccer import simulate_soccer_pick
        pick = {
            "sport": "Soccer",
            "market": "Over 2.5 Total Goals",
            "win_probability": 55.0,
            "factors": {"xG Combined": 60, "xG Difference": 50},
        }
        out = simulate_soccer_pick(pick)
        assert out is not None
        assert out["sim_market_category"] == "totals"

    def test_simulate_soccer_unsupported_market(self):
        from brain.sim_soccer import simulate_soccer_pick
        out = simulate_soccer_pick({"sport": "Soccer", "market": "Half Time Score 1-0"})
        assert out is None

    def test_simulate_soccer_wrong_sport(self):
        from brain.sim_soccer import simulate_soccer_pick
        out = simulate_soccer_pick({"sport": "MLB", "market": "Manchester City Moneyline"})
        assert out is None

    def test_simulate_nba_calibrated_to_model_wp(self):
        from brain.sim_nba import simulate_nba_pick
        pick = {
            "sport": "NBA",
            "market": "Lakers Moneyline",
            "win_probability": 65.0,
            "factors": {"Recent Form (L10)": 55},
        }
        out = simulate_nba_pick(pick)
        assert out is not None
        assert out["sim_runs"] == 10000
        assert out["sim_market_category"] == "moneyline"
        # Calibrated → within 10pp of 65
        assert abs(out["sim_win_probability"] - 65.0) <= 10.0

    def test_simulate_nba_points_prop(self):
        from brain.sim_nba import simulate_nba_pick
        pick = {
            "sport": "NBA",
            "market": "Player Over 25.5 Points",
            "win_probability": 55.0,
            "factors": {"Last 10 Hit Rate": 65, "Matchup vs Defense": 55, "Recent Volume / Usage": 60},
        }
        out = simulate_nba_pick(pick)
        assert out is not None
        assert out["sim_market_category"] == "points"
        assert "sim_lambda" in out
        assert "sim_expected_stat" in out
        # Within 10pp
        assert abs(out["sim_win_probability"] - 55.0) <= 10.0

    def test_simulate_nba_wrong_sport(self):
        from brain.sim_nba import simulate_nba_pick
        out = simulate_nba_pick({"sport": "Tennis", "market": "Lakers Moneyline"})
        assert out is None

    def test_simulate_tennis_moneyline_calibrated(self):
        from brain.sim_tennis import simulate_tennis_pick
        pick = {
            "sport": "Tennis",
            "market": "Djokovic Moneyline",
            "win_probability": 70.0,
            "factors": {"Hold %": 80, "Break %": 25},
        }
        out = simulate_tennis_pick(pick)
        assert out is not None
        assert out["sim_market_category"] == "moneyline"
        assert 0 < out["sim_pick_serve_pct"] < 100
        assert abs(out["sim_win_probability"] - 70.0) <= 10.0, \
               f"Tennis sim should calibrate to model_wp: got {out['sim_win_probability']} vs 70"

    def test_simulate_tennis_totals(self):
        from brain.sim_tennis import simulate_tennis_pick
        pick = {
            "sport": "Tennis",
            "market": "Over 22.5 Total Games",
            "win_probability": 55.0,
            "factors": {},
        }
        out = simulate_tennis_pick(pick)
        assert out is not None
        assert out["sim_market_category"] == "totals"
        assert out["sim_avg_total_games"] > 0


# ── sim_runner routes all 4 sports ─────────────────────────────────────


class TestSimRunnerRouting:
    def test_runner_routes_soccer(self):
        from brain.sim_runner import simulate_pick
        out = simulate_pick({
            "sport": "Soccer",
            "market": "Manchester City Moneyline",
            "win_probability": 55.0,
            "factors": {"xG Combined": 60, "xG Difference": 55},
        })
        assert out is not None and out["sim_runs"] == 10000

    def test_runner_routes_nba(self):
        from brain.sim_runner import simulate_pick
        out = simulate_pick({
            "sport": "NBA",
            "market": "Lakers Moneyline",
            "win_probability": 60.0,
            "factors": {},
        })
        assert out is not None

    def test_runner_routes_tennis(self):
        from brain.sim_runner import simulate_pick
        out = simulate_pick({
            "sport": "Tennis",
            "market": "Djokovic Moneyline",
            "win_probability": 65.0,
            "factors": {},
        })
        assert out is not None

    def test_runner_skips_unsupported(self):
        from brain.sim_runner import simulate_pick
        out = simulate_pick({
            "sport": "UFC",
            "market": "Diego Lopes Moneyline",
            "win_probability": 60.0,
        })
        assert out is None

    def test_apply_simulations_mutates(self):
        from brain.sim_runner import apply_simulations
        picks = [
            {"id": "p1", "sport": "Soccer", "market": "Team A Moneyline",
             "win_probability": 55.0, "factors": {"xG Combined": 50, "xG Difference": 55},
             "lock_score": 80.0},
            {"id": "p2", "sport": "UFC", "market": "X Moneyline", "win_probability": 55.0},
        ]
        counts = apply_simulations(picks)
        assert counts["applied"] >= 1
        # First pick should now have sim_* fields
        assert "sim_win_probability" in picks[0]
        # UFC pick not touched
        assert "sim_win_probability" not in picks[1]
