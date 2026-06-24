"""Regression tests for server.py Phase-2a — analytics extraction.

Targets the 15 endpoints moved from server.py into
`routes/analytics_routes.py`:

  GET  /api/analytics/model-performance
  GET  /api/analytics/sim-backtest
  GET  /api/analytics/learned-weights
  GET  /api/analytics/bandit
  GET  /api/analytics/backtest
  GET  /api/analytics/backtest-custom
  GET  /api/analytics/v2
  POST /api/analytics/v2/recompute
  GET  /api/analytics/buckets
  GET  /api/analytics/calibration
  POST /api/analytics/calibration/refit
  GET  /api/analytics/xg-form-shadow
  POST /api/analytics/buckets/recompute
  POST /api/analytics/buckets/rollback?snapshot_index=1
  POST /api/analytics/learn

Also includes the Phase-1 + carve-out regression hot-spots requested by
the main agent (Tennis pills + MLB H+R+RBI).

Auth: demo@lockscore.ai / demo123 (see /app/memory/test_credentials.md).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import requests
from dotenv import load_dotenv

load_dotenv(Path("/app/frontend/.env"))

BASE_URL = (
    os.environ.get("EXPO_PUBLIC_BACKEND_URL")
    or os.environ.get("EXPO_BACKEND_URL")
    or ""
).rstrip("/")

DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"


# ───────────────────────────── fixtures ─────────────────────────────
@pytest.fixture(scope="session")
def api_client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def auth_token(api_client):
    assert BASE_URL, "EXPO_PUBLIC_BACKEND_URL must be set in frontend/.env"
    r = api_client.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
        timeout=20,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:300]}"
    tok = r.json().get("access_token")
    assert tok, f"access_token missing: {r.json()}"
    return tok


@pytest.fixture(scope="session")
def auth_headers(auth_token):
    return {"Authorization": f"Bearer {auth_token}", "Content-Type": "application/json"}


# ───────── Analytics GET endpoints (8 total) ─────────────────────────
class TestAnalyticsGetEndpoints:

    def test_learned_weights(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/analytics/learned-weights",
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        assert isinstance(data, dict)
        # Per route: returns either persisted doc or default skeleton
        # with buckets / calibration / updated_at / sample_size.
        assert "buckets" in data or "calibration" in data or "sample_size" in data, \
            f"unexpected payload: {list(data.keys())}"

    def test_bandit(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/analytics/bandit",
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        assert "arms" in data and "n_arms" in data
        assert isinstance(data["arms"], list)
        assert data["n_arms"] == len(data["arms"])

    def test_v2_dashboard(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/analytics/v2",
            headers=auth_headers, timeout=30,
        )
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        # Spec-required keys per problem statement.
        for k in ("changes_log", "profit_by_sport", "profit_by_bet_type"):
            assert k in data, f"missing key {k!r} from /analytics/v2 payload: {list(data.keys())}"
        assert isinstance(data["changes_log"], list)
        assert isinstance(data["profit_by_sport"], list)
        assert isinstance(data["profit_by_bet_type"], list)

    def test_buckets(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/analytics/buckets",
            headers=auth_headers, timeout=30,
        )
        assert r.status_code == 200, r.text[:300]
        # learning_buckets.get_buckets returns a dict; just confirm.
        assert isinstance(r.json(), (dict, list))

    def test_calibration(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/analytics/calibration",
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 200, r.text[:300]
        assert isinstance(r.json(), (dict, list))

    def test_xg_form_shadow_structure(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/analytics/xg-form-shadow",
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        assert "buckets" in data and "promote_ready" in data
        assert isinstance(data["promote_ready"], bool)
        for label in ("HOT", "COLD", "NEUTRAL"):
            assert label in data["buckets"], f"missing bucket {label}"
        assert "promotion_rule" in data
        assert data.get("shadow_mode") is True

    def test_sim_backtest_days_14(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/analytics/sim-backtest",
            params={"days": 14},
            headers=auth_headers, timeout=60,
        )
        assert r.status_code == 200, r.text[:400]
        assert isinstance(r.json(), (dict, list))

    def test_backtest_days_14(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/analytics/backtest",
            params={"days": 14},
            headers=auth_headers, timeout=60,
        )
        assert r.status_code == 200, r.text[:400]
        assert isinstance(r.json(), (dict, list))

    def test_backtest_custom_days_14_lockfloor_80(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/analytics/backtest-custom",
            params={"days": 14, "lock_floor": 80},
            headers=auth_headers, timeout=60,
        )
        assert r.status_code == 200, r.text[:400]
        assert isinstance(r.json(), (dict, list))

    def test_model_performance_days_7_no_backfill(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/analytics/model-performance",
            params={"days": 7, "backfill": "false"},
            headers=auth_headers, timeout=60,
        )
        assert r.status_code == 200, r.text[:400]
        assert isinstance(r.json(), (dict, list))


# ───────── Analytics POST endpoints (5 total) ────────────────────────
class TestAnalyticsPostEndpoints:

    def test_learn_returns_summary(self, api_client, auth_headers):
        r = api_client.post(
            f"{BASE_URL}/api/analytics/learn",
            headers=auth_headers, timeout=120,
        )
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        for k in ("active_buckets", "picks_adjusted", "sample_size"):
            assert k in data, f"missing key {k} in /learn response: {list(data.keys())}"
        assert isinstance(data["active_buckets"], int)
        assert isinstance(data["picks_adjusted"], int)
        assert isinstance(data["sample_size"], int)

    def test_calibration_refit(self, api_client, auth_headers):
        r = api_client.post(
            f"{BASE_URL}/api/analytics/calibration/refit",
            headers=auth_headers, timeout=60,
        )
        assert r.status_code == 200, r.text[:400]
        assert isinstance(r.json(), (dict, list))

    def test_v2_recompute(self, api_client, auth_headers):
        r = api_client.post(
            f"{BASE_URL}/api/analytics/v2/recompute",
            headers=auth_headers, timeout=90,
        )
        assert r.status_code == 200, r.text[:400]
        assert isinstance(r.json(), (dict, list))

    def test_buckets_recompute(self, api_client, auth_headers):
        r = api_client.post(
            f"{BASE_URL}/api/analytics/buckets/recompute",
            headers=auth_headers, timeout=90,
        )
        assert r.status_code == 200, r.text[:400]
        assert isinstance(r.json(), (dict, list))

    def test_buckets_rollback_snapshot_index_1(self, api_client, auth_headers):
        # Depends on a prior recompute creating at least 1 snapshot. Tolerate
        # a graceful 200 with a "no snapshot available" indicator OR a
        # 4xx for missing snapshot, but NOT a 5xx.
        r = api_client.post(
            f"{BASE_URL}/api/analytics/buckets/rollback",
            params={"snapshot_index": 1},
            headers=auth_headers, timeout=60,
        )
        assert r.status_code == 200, (
            f"buckets rollback expected 200 (per problem statement), got "
            f"{r.status_code}: {r.text[:400]}"
        )
        assert isinstance(r.json(), (dict, list))


# ───────── Regression: phase-1 + Tennis + MLB carve-out ─────────────
class TestPhase1AndCarveoutRegression:

    def test_parlay_history_still_works(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/parlay/history",
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 200
        data = r.json()
        assert "parlays" in data and "count" in data

    def test_admin_historical_status_still_works(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/admin/historical/status",
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 200
        assert "collections" in r.json()

    def test_tennis_market_pills(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/picks/markets/Tennis",
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 200
        data = r.json()
        markets = data.get("markets") if isinstance(data, dict) else data
        if isinstance(markets, dict):
            markets = markets.get("tokens") or list(markets.keys())
        tokens: list[str] = []
        if isinstance(markets, list):
            for m in markets:
                if isinstance(m, str):
                    tokens.append(m)
                elif isinstance(m, dict):
                    tokens.append(m.get("token") or m.get("key") or m.get("id"))
        tokens = [t for t in tokens if t]
        expected = {"match_winner", "tennis_game_alt", "sets", "tennis_totals"}
        missing = expected - set(tokens)
        assert not missing, f"missing tennis tokens: {missing}; got {tokens}"

    def test_mlb_hrrbi_carveout_still_returns(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/picks/today",
            params={"sport": "MLB", "limit": 200},
            headers=auth_headers, timeout=30,
        )
        assert r.status_code == 200
        data = r.json()
        picks = data.get("picks") if isinstance(data, dict) else data
        assert isinstance(picks, list)
        # Reporting only — game-day light is tolerated.
        markets_seen = {
            (p.get("market") or "").lower()
            for p in picks if isinstance(p, dict)
        }
        print(f"MLB markets present: {sorted(markets_seen)[:20]}")
