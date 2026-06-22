"""Phase A — MLB Monte Carlo Simulator tests.

Coverage:
  • GET /api/picks/{id}/simulation for MLB picks → returns sim_* schema
  • GET /api/picks/{id}/simulation for non-MLB picks → 404
  • GET /api/analytics/sim-backtest?days=30 → proper structure (n may be 0)
  • Math sanity: Over 0.5 Hits for elite hitter (BA .310) → sim_wp 75–95%
"""
import os
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "").rstrip("/") or \
           os.environ.get("EXPO_BACKEND_URL", "").rstrip("/")

DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"


@pytest.fixture(scope="module")
def auth_token():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}, timeout=20)
    if r.status_code != 200:
        # try register
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
def mlb_pick(headers):
    """Fetch a MLB pick from today's slate."""
    r = requests.get(f"{BASE_URL}/api/picks/today?sport=MLB", headers=headers, timeout=30)
    assert r.status_code == 200, f"picks/today MLB failed: {r.status_code}"
    picks = r.json().get("picks", [])
    # Prefer a hitter-prop pick that the simulator can route
    routable = [p for p in picks if any(k in (p.get("market") or "").lower()
                for k in ["hits", "home run", "strikeout", "out", "rbi"])]
    if routable:
        return routable[0]
    return picks[0] if picks else None


@pytest.fixture(scope="module")
def non_mlb_pick(headers):
    """Find a Tennis/NBA/Soccer pick to ensure 404."""
    for sport in ["Tennis", "NBA", "Soccer", "UFC"]:
        r = requests.get(f"{BASE_URL}/api/picks/today?sport={sport}",
                         headers=headers, timeout=30)
        if r.status_code == 200:
            picks = r.json().get("picks", [])
            if picks:
                return picks[0]
    return None


# ── Sim endpoint — MLB ─────────────────────────────────────────────────


class TestPickSimulationMLB:
    def test_mlb_pick_returns_sim_schema(self, headers, mlb_pick):
        if not mlb_pick:
            pytest.skip("No MLB picks available today")
        pid = mlb_pick["id"]
        r = requests.get(f"{BASE_URL}/api/picks/{pid}/simulation",
                         headers=headers, timeout=30)
        # Some markets are non-routable → 404 is acceptable; otherwise must be 200
        if r.status_code == 404:
            pytest.skip(f"Pick market not routable by simulator: {mlb_pick.get('market')}")
        assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
        body = r.json()
        # Schema assertions
        for key in ["sim_win_probability", "sim_ci_lower", "sim_ci_upper",
                    "sim_runs", "sim_signal", "sim_disagreement_with_model"]:
            assert key in body, f"missing key: {key} in {body}"
        assert body["sim_runs"] == 10000, f"expected 10000 runs, got {body['sim_runs']}"
        assert 0.0 <= body["sim_win_probability"] <= 100.0
        assert body["sim_ci_lower"] <= body["sim_win_probability"] <= body["sim_ci_upper"]
        assert body["sim_signal"] in ("stronger", "weaker", "neutral")


# ── Sim endpoint — non-MLB → 404 ──────────────────────────────────────


class TestPickSimulationNonMLB:
    def test_non_mlb_returns_404(self, headers, non_mlb_pick):
        if not non_mlb_pick:
            pytest.skip("No non-MLB picks available")
        pid = non_mlb_pick["id"]
        r = requests.get(f"{BASE_URL}/api/picks/{pid}/simulation",
                         headers=headers, timeout=30)
        assert r.status_code == 404, f"expected 404 for {non_mlb_pick.get('sport')}, got {r.status_code}"
        # Detail message should mention MLB only / unavailable
        detail = r.json().get("detail", "").lower()
        assert "mlb" in detail or "not" in detail or "unavailable" in detail


# ── Backtest endpoint ─────────────────────────────────────────────────


class TestSimBacktest:
    def test_backtest_schema(self, headers):
        r = requests.get(f"{BASE_URL}/api/analytics/sim-backtest?days=30",
                         headers=headers, timeout=30)
        assert r.status_code == 200, f"backtest failed: {r.status_code} {r.text}"
        body = r.json()
        assert "n" in body
        assert "days" in body
        assert body["days"] == 30
        if body["n"] == 0:
            assert "message" in body, "n=0 path must include explanation message"
        else:
            for key in ["calibration", "strategies", "brier", "log_loss", "brier_skill_score"]:
                assert key in body, f"missing key when n>0: {key}"


# ── Math sanity ───────────────────────────────────────────────────────


class TestSimMath:
    def test_elite_hitter_over_05_hits(self):
        """Elite hitter BA .310 with 4 ABs → P(>=1 hit) ≈ 1 - (1-.310)^4 ≈ 77%."""
        from brain.sim_mlb import simulate_mlb_pick
        pick = {
            "sport": "MLB",
            "market": "Over 0.5 Hits",
            "win_probability": 70.0,
        }
        out = simulate_mlb_pick(pick, {"ba": 0.310})
        assert out is not None
        wp = out["sim_win_probability"]
        assert 70.0 <= wp <= 90.0, f"expected 70-90% sim_wp for elite hitter, got {wp}%"
        assert out["sim_runs"] == 10000

    def test_non_mlb_returns_none(self):
        from brain.sim_mlb import simulate_mlb_pick
        out = simulate_mlb_pick({"sport": "Tennis", "market": "Set Winner"}, None)
        assert out is None

    def test_unroutable_market_returns_none(self):
        from brain.sim_mlb import simulate_mlb_pick
        out = simulate_mlb_pick({"sport": "MLB", "market": "Money Line"}, None)
        assert out is None
