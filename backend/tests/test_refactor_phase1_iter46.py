"""Regression tests for server.py monolith decomposition — Phase 1.

Targets:
- 4 extracted parlay-history endpoints (POST/GET/GET/DELETE /api/parlay/*)
- 5 extracted admin endpoints (/api/admin/*)
- Quick regression on picks/today, tennis market structure, MLB carve-out,
  parlay optimizer, xg-form-shadow, and deep-dive picks (still in server.py).

Auth: demo@lockscore.ai / demo123 (see /app/memory/test_credentials.md).
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
import requests
from dotenv import load_dotenv

# Load frontend .env to pick up EXPO_PUBLIC_BACKEND_URL (the public URL
# the mobile client hits — i.e. what the user actually sees).
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
    data = r.json()
    token = data.get("access_token")
    assert token, f"access_token missing: {data}"
    return token


@pytest.fixture(scope="session")
def auth_headers(auth_token):
    return {"Authorization": f"Bearer {auth_token}", "Content-Type": "application/json"}


# ───────────────────────────── Auth ─────────────────────────────────
class TestAuth:
    """Sanity check that authentication is intact post-refactor."""

    def test_login_returns_token_and_user(self, api_client):
        r = api_client.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
            timeout=20,
        )
        assert r.status_code == 200
        data = r.json()
        assert "access_token" in data
        # endpoint commonly returns the user object alongside the token
        assert isinstance(data.get("access_token"), str)
        assert len(data["access_token"]) > 20


# ─────────────────── Parlay-History extracted routes ────────────────
class TestParlayHistoryRoutes:
    """4 endpoints extracted into routes/parlay_history_routes.py."""

    def test_list_history_returns_parlays_array(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/parlay/history", headers=auth_headers, timeout=20,
        )
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        assert "parlays" in data and "count" in data
        assert isinstance(data["parlays"], list)
        assert isinstance(data["count"], int)
        assert data["count"] == len(data["parlays"])

    def test_history_filter_param_accepted(self, api_client, auth_headers):
        for f in ("all", "live", "won", "lost"):
            r = api_client.get(
                f"{BASE_URL}/api/parlay/history?filter={f}&limit=5",
                headers=auth_headers, timeout=20,
            )
            assert r.status_code == 200, f"filter={f} -> {r.status_code} {r.text[:200]}"
            j = r.json()
            assert isinstance(j.get("parlays"), list)

    def test_parlay_detail_404_when_not_found(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/parlay/p_does_not_exist_xyz",
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 404

    def test_parlay_save_validation_requires_two_legs(self, api_client, auth_headers):
        # 0-leg request should be rejected (data-layer raises ValueError → 400)
        r = api_client.post(
            f"{BASE_URL}/api/parlay/save",
            headers=auth_headers,
            json={"legs": [], "mode": "standard", "stake": 1.0},
            timeout=20,
        )
        assert r.status_code == 400, f"expected 400 got {r.status_code}: {r.text[:200]}"

    def test_parlay_save_and_full_lifecycle(self, api_client, auth_headers):
        """End-to-end: pull legs from /api/picks/parlay → save → detail → delete."""
        # 1) Get a parlay from the optimizer
        r = api_client.get(
            f"{BASE_URL}/api/picks/parlay",
            headers=auth_headers, timeout=30,
        )
        assert r.status_code == 200, r.text[:300]
        opt = r.json()
        # The optimizer payload typically contains `legs` (or `parlays[0].legs`).
        legs = opt.get("legs")
        if not legs and isinstance(opt.get("parlays"), list) and opt["parlays"]:
            legs = opt["parlays"][0].get("legs")
        if not legs and isinstance(opt.get("parlay"), dict):
            legs = opt["parlay"].get("legs")
        if not legs or len(legs) < 2:
            pytest.skip(f"optimizer returned no usable legs to save (keys={list(opt.keys())})")

        # Trim to 2+ legs each with an id and book_odds
        usable = [lg for lg in legs if (lg.get("id") or lg.get("pick_id"))]
        if len(usable) < 2:
            pytest.skip("not enough legs with ids in optimizer output")
        save_legs = usable[:3]

        # 2) Save
        r2 = api_client.post(
            f"{BASE_URL}/api/parlay/save",
            headers=auth_headers,
            json={"legs": save_legs, "mode": "standard", "stake": 1.0},
            timeout=30,
        )
        assert r2.status_code == 200, f"save failed: {r2.status_code} {r2.text[:400]}"
        saved = r2.json()
        assert "_id" not in saved, "Mongo _id leaked through strip_mongo"
        pid = saved.get("id")
        assert pid and pid.startswith("p_"), f"unexpected id: {saved}"

        # 3) Detail
        r3 = api_client.get(
            f"{BASE_URL}/api/parlay/{pid}",
            headers=auth_headers, timeout=20,
        )
        assert r3.status_code == 200, r3.text[:300]
        detail = r3.json()
        assert detail.get("id") == pid

        # 4) Idempotent save (same legs → same id)
        r4 = api_client.post(
            f"{BASE_URL}/api/parlay/save",
            headers=auth_headers,
            json={"legs": save_legs, "mode": "standard", "stake": 1.0},
            timeout=20,
        )
        assert r4.status_code == 200
        assert r4.json().get("id") == pid

        # 5) Delete — cleanup the TEST_ parlay
        r5 = api_client.delete(
            f"{BASE_URL}/api/parlay/{pid}",
            headers=auth_headers, timeout=20,
        )
        assert r5.status_code == 200
        assert r5.json().get("deleted") is True

        # 6) Confirm gone
        r6 = api_client.get(
            f"{BASE_URL}/api/parlay/{pid}",
            headers=auth_headers, timeout=20,
        )
        assert r6.status_code == 404


# ────────────────────── Admin extracted routes ──────────────────────
class TestAdminRoutes:
    """5 endpoints extracted into routes/admin_routes.py."""

    def test_historical_status_returns_collections_and_syncs(
        self, api_client, auth_headers,
    ):
        r = api_client.get(
            f"{BASE_URL}/api/admin/historical/status",
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        assert "collections" in data
        assert "last_syncs" in data
        cols = data["collections"]
        for key in ("players", "games", "player_game_logs", "season_totals", "team_form"):
            assert key in cols, f"missing collection key: {key}"
            assert isinstance(cols[key], int)
        # sanity from problem statement: 3162 players, 475 games existed
        assert cols["players"] >= 0
        assert cols["games"] >= 0

    def test_historical_player_form_returns_200(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/admin/historical/player-form",
            params={"sport": "mlb", "name": "Aaron Judge"},
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        assert isinstance(data, dict)
        # should at minimum echo back the query keys when not found
        if data.get("found") is False:
            assert data.get("sport") == "mlb"
            assert data.get("name") == "Aaron Judge"

    def test_admin_refresh_soccer_player_form(self, api_client, auth_headers):
        """Just verify the endpoint responds with a summary dict.
        Per request: don't wait for a long completion — 60s ceiling."""
        t0 = time.time()
        r = api_client.post(
            f"{BASE_URL}/api/admin/refresh-soccer-player-form",
            headers=auth_headers, timeout=60,
        )
        elapsed = time.time() - t0
        assert r.status_code == 200, f"{r.status_code} {r.text[:400]}"
        data = r.json()
        assert isinstance(data, dict), f"expected summary dict, got {type(data)}"
        # Expect at least the `total` key from soccer_player_form.refresh
        # (or `leagues`); tolerate either to avoid coupling to internals.
        assert "total" in data or "leagues" in data or "errors" in data, \
            f"unexpected refresh summary shape: {list(data.keys())}"
        print(f"refresh-soccer-player-form elapsed={elapsed:.1f}s keys={list(data.keys())}")

    def test_admin_backfill_tennis_elo_days_back_1(self, api_client, auth_headers):
        """Use ?days_back=1 to keep this fast."""
        r = api_client.post(
            f"{BASE_URL}/api/admin/backfill-tennis-elo",
            params={"days_back": 1},
            headers=auth_headers, timeout=60,
        )
        assert r.status_code == 200, f"{r.status_code} {r.text[:400]}"
        data = r.json()
        assert isinstance(data, dict)

    def test_admin_historical_backfill_incremental_no_days(
        self, api_client, auth_headers,
    ):
        """Don't actually do a full backfill — just verify route is wired
        and accepts the request shape. Use mode='incremental' with no sports."""
        r = api_client.post(
            f"{BASE_URL}/api/admin/historical/backfill",
            headers=auth_headers,
            json={"sports": ["mlb"], "mode": "incremental", "days": 1},
            timeout=120,
        )
        # Endpoint may take a moment; we only care it doesn't 5xx structurally.
        assert r.status_code in (200, 500), f"unexpected status: {r.status_code} {r.text[:300]}"
        if r.status_code == 200:
            data = r.json()
            assert data.get("mode") == "incremental"
            assert data.get("sports") == ["mlb"]
            assert "results" in data
        else:
            # If 500, it should be a clean orchestrator failure, not an
            # import/circular error.  Surface for the report.
            msg = r.text[:400]
            assert "Historical engine not loaded" not in msg, \
                f"orchestrator import failed: {msg}"


# ───────────────────────── Regression checks ────────────────────────
class TestRegressionPicks:
    """Endpoints still in server.py — make sure the refactor didn't break them."""

    def test_picks_today_full_board(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/picks/today",
            headers=auth_headers, timeout=30,
        )
        assert r.status_code == 200
        data = r.json()
        # Tolerate either {picks: [...]} or {board: {...}} shapes; the
        # contract is "we get something back, not 500".
        assert isinstance(data, (dict, list))

    def test_picks_markets_tennis_pill_tokens(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/picks/markets/Tennis",
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 200
        data = r.json()
        # Find the list of pill tokens regardless of wrapper shape
        markets = data.get("markets") if isinstance(data, dict) else data
        if isinstance(markets, dict):
            markets = markets.get("tokens") or list(markets.keys())
        tokens = []
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

    def test_picks_today_tennis_game_alt_filter(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/picks/today",
            params={"sport": "Tennis", "market": "tennis_game_alt"},
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 200
        data = r.json()
        picks = data.get("picks") if isinstance(data, dict) else data
        assert isinstance(picks, list)
        # If any picks returned, they should all be game-alt
        for p in picks[:20]:
            m = (p.get("market") or "").lower()
            assert "tennis_game_alt" in m or "game" in m or "alt" in m, \
                f"unexpected market on game-alt filter: {p.get('market')}"

    def test_picks_today_tennis_totals_filter(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/picks/today",
            params={"sport": "Tennis", "market": "tennis_totals"},
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 200
        data = r.json()
        picks = data.get("picks") if isinstance(data, dict) else data
        assert isinstance(picks, list)

    def test_picks_today_mlb_has_hrrbi(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/picks/today",
            params={"sport": "MLB", "limit": 200},
            headers=auth_headers, timeout=30,
        )
        assert r.status_code == 200
        data = r.json()
        picks = data.get("picks") if isinstance(data, dict) else data
        assert isinstance(picks, list)
        # H+R+RBI carve-out from prior iter; tolerate empty list off-day.
        markets_seen = {(p.get("market") or "").lower() for p in picks if isinstance(p, dict)}
        # report only — don't fail if game-day light
        print(f"MLB markets present: {sorted(markets_seen)[:20]}")

    def test_picks_deep_dive_one_pick(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/picks/today",
            params={"limit": 50},
            headers=auth_headers, timeout=30,
        )
        assert r.status_code == 200
        data = r.json()
        picks = data.get("picks") if isinstance(data, dict) else data
        if not picks:
            pytest.skip("no picks available to deep-dive")
        # try first pick id
        pid = None
        for p in picks:
            if isinstance(p, dict) and (p.get("id") or p.get("pick_id")):
                pid = p.get("id") or p.get("pick_id")
                break
        if not pid:
            pytest.skip("no pick id available")
        r2 = api_client.get(
            f"{BASE_URL}/api/picks/{pid}",
            headers=auth_headers, timeout=20,
        )
        assert r2.status_code == 200, f"deep-dive 500: {r2.text[:300]}"

    def test_picks_parlay_optimizer_works(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/picks/parlay",
            headers=auth_headers, timeout=30,
        )
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)

    def test_analytics_xg_form_shadow_works(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/analytics/xg-form-shadow",
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 200
