"""Phase-3 refactor validation — /api/picks/today, /parlay, /bet-killer, /refresh
all moved from server.py → routes/picks_routes.py. Verifies:
  - static-route precedence (no shadowing by /{pick_id})
  - payload shapes intact
  - Phase 1+2 routes still 200 (regression)
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "https://bet-edge-ai-1.preview.emergentagent.com").rstrip("/")
INTERNAL_URL = "http://localhost:8001"  # fallback for slow endpoints (settle)
EMAIL = "demo@lockscore.ai"
PASSWORD = "demo123"


@pytest.fixture(scope="session")
def token():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": EMAIL, "password": PASSWORD},
        timeout=20,
    )
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


@pytest.fixture(scope="session")
def headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.fixture(scope="session")
def sample_pick(headers):
    """Get one real pick ID from /picks/today for parameterized route tests."""
    r = requests.get(f"{BASE_URL}/api/picks/today", headers=headers, timeout=60)
    assert r.status_code == 200
    picks = r.json().get("picks", [])
    assert picks, "No picks in /picks/today — cannot run parameterized tests"
    return picks[0]


# ──────────────────────────── Phase 3: /today ────────────────────────────
class TestPicksToday:
    def test_today_basic_shape(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/today", headers=headers, timeout=60)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "picks" in body and isinstance(body["picks"], list)
        # Should be a populated slate (~100+ picks)
        assert len(body["picks"]) >= 1, f"Empty slate: {len(body['picks'])}"
        # Spot-check required decorated fields
        p = body["picks"][0]
        for k in ("lock_score", "probability", "event"):
            assert k in p, f"Missing {k} in pick payload: {list(p.keys())[:20]}"

    def test_today_NOT_shadowed_by_pick_id_route(self, headers):
        """Critical: /today must not be captured as pick_id='today'."""
        r = requests.get(f"{BASE_URL}/api/picks/today", headers=headers, timeout=60)
        assert r.status_code == 200
        # If shadowed → would return {"detail":"Pick not found"} with 404
        text = r.text.lower()
        assert "pick not found" not in text

    @pytest.mark.parametrize("params", [
        {"sports": "MLB,Soccer"},
        {"leagues": "EPL,La Liga"},
        {"markets": "Hits,Total Bases"},
        {"search": "jefferson"},
        {"min_lock": 95},
        {"sort": "lock", "direction": "desc"},
        {"sort": "time"},
        {"sort": "edge"},
        {"sort": "win"},
        {"sort": "implied"},
        {"line_type": "main"},
        {"line_type": "alt"},
        {"line_type": "both"},
        {"lite": "true"},
    ])
    def test_today_filters(self, headers, params):
        r = requests.get(f"{BASE_URL}/api/picks/today", headers=headers,
                         params=params, timeout=60)
        assert r.status_code == 200, f"params={params} → {r.status_code} {r.text[:200]}"
        body = r.json()
        assert "picks" in body and isinstance(body["picks"], list)

    def test_today_events_filter(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/today",
                         headers=headers,
                         params={"events": "Yankees @ Red Sox|Dodgers @ Mets"},
                         timeout=60)
        assert r.status_code == 200
        assert "picks" in r.json()

    def test_today_game_ids_filter(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/today",
                         headers=headers,
                         params={"game_ids": "evt_abc,evt_def"},
                         timeout=60)
        assert r.status_code == 200
        body = r.json()
        # Filter on fake IDs → should return empty list (graceful)
        assert isinstance(body.get("picks"), list)


# ──────────────────────────── Phase 3: /parlay ────────────────────────────
class TestPicksParlay:
    def _validate_envelope(self, body):
        for k in ("parlay", "parlays", "window_hours", "rank", "sport_mode"):
            assert k in body, f"Missing key {k}; got {list(body.keys())}"
        assert isinstance(body["parlays"], list)

    def test_parlay_not_shadowed(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/parlay",
                         headers=headers,
                         params={"legs": 3, "mode": "standard"},
                         timeout=90)
        assert r.status_code == 200, r.text
        assert "pick not found" not in r.text.lower()

    def test_parlay_standard_3legs(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/parlay",
                         headers=headers,
                         params={"legs": 3, "mode": "standard"},
                         timeout=90)
        assert r.status_code == 200, r.text
        body = r.json()
        self._validate_envelope(body)
        # populated slate → should have at least 1 parlay
        if body["parlays"]:
            card = body["parlays"][0]
            assert "alternates" in card, f"missing alternates: {list(card.keys())}"
            assert "alternates_count" in card
            assert isinstance(card["alternates"], list)
            assert len(card["alternates"]) <= 5

    def test_parlay_high_risk_10legs(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/parlay",
                         headers=headers,
                         params={"legs": 10, "mode": "high_risk"},
                         timeout=120)
        assert r.status_code == 200, r.text
        self._validate_envelope(r.json())

    def test_parlay_advanced_safer(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/parlay",
                         headers=headers,
                         params={"mode": "advanced", "advanced_sub": "safer", "legs": 4},
                         timeout=90)
        assert r.status_code == 200, r.text
        self._validate_envelope(r.json())

    def test_parlay_advanced_ev(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/parlay",
                         headers=headers,
                         params={"mode": "advanced", "advanced_sub": "ev", "legs": 6},
                         timeout=90)
        assert r.status_code == 200, r.text
        self._validate_envelope(r.json())

    def test_parlay_single_sport_nfl(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/parlay",
                         headers=headers,
                         params={"sport": "NFL", "sport_mode": "single"},
                         timeout=90)
        assert r.status_code == 200, r.text
        body = r.json()
        self._validate_envelope(body)
        assert body["sport_mode"] == "single"

    def test_parlay_custom_multi_sport(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/parlay",
                         headers=headers,
                         params={"include_sports": "MLB,Soccer", "sport_mode": "custom"},
                         timeout=90)
        assert r.status_code == 200, r.text
        self._validate_envelope(r.json())

    def test_parlay_short_window(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/parlay",
                         headers=headers,
                         params={"window_hours": 4},
                         timeout=90)
        assert r.status_code == 200, r.text
        body = r.json()
        self._validate_envelope(body)
        # window_hours echoed back (auto-ladder may expand it)
        assert isinstance(body["window_hours"], (int, float))

    def test_parlay_locked_ids(self, headers, sample_pick):
        pick_id = sample_pick["id"] if isinstance(sample_pick, dict) and "id" in sample_pick else sample_pick.get("pick_id")
        if not pick_id:
            pytest.skip("No pick id field discoverable on sample pick")
        r = requests.get(f"{BASE_URL}/api/picks/parlay",
                         headers=headers,
                         params={"locked_ids": pick_id, "legs": 3},
                         timeout=90)
        assert r.status_code == 200, r.text
        self._validate_envelope(r.json())

    def test_parlay_rank_cycle(self, headers):
        r2 = requests.get(f"{BASE_URL}/api/picks/parlay",
                          headers=headers, params={"rank": 2}, timeout=90)
        r3 = requests.get(f"{BASE_URL}/api/picks/parlay",
                          headers=headers, params={"rank": 3}, timeout=90)
        assert r2.status_code == 200, r2.text
        assert r3.status_code == 200, r3.text
        b2, b3 = r2.json(), r3.json()
        self._validate_envelope(b2)
        self._validate_envelope(b3)
        assert b2["rank"] in (2, 1)  # may clamp if not enough parlays
        assert b3["rank"] in (3, 2, 1)


# ─────────────────────────── Phase 3: /bet-killer ────────────────────────
class TestPicksBetKiller:
    def test_bet_killer_returns_empty(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/bet-killer", headers=headers, timeout=20)
        assert r.status_code == 200, r.text
        assert "pick not found" not in r.text.lower()
        body = r.json()
        assert body == {"picks": []}


# ──────────────────────────── Phase 3: /refresh ───────────────────────────
class TestPicksRefresh:
    def test_refresh_soft_rate_limit_flow(self, headers):
        r1 = requests.post(f"{BASE_URL}/api/picks/refresh", headers=headers, timeout=30)
        assert r1.status_code == 200, r1.text
        b1 = r1.json()
        # Either fresh refresh OR already-cooled-down from a prior test run.
        if b1.get("refreshed"):
            for k in ("refreshed", "queued", "count", "date",
                      "cooldown_seconds", "next_refresh_at", "last_refresh_at"):
                assert k in b1, f"Missing {k}; got {list(b1.keys())}"
            # Second call within cooldown → soft rate limit (still 200)
            time.sleep(1)
            r2 = requests.post(f"{BASE_URL}/api/picks/refresh", headers=headers, timeout=30)
            assert r2.status_code == 200, r2.text
            b2 = r2.json()
            assert b2.get("rate_limited") is True
            assert "retry_after_minutes" in b2
            assert b2["refreshed"] is False
        else:
            # Already rate-limited from a prior recent run — still must be 200
            assert b1.get("rate_limited") is True, f"Unexpected payload: {b1}"
            assert "retry_after_minutes" in b1


# ──────────────────── Phase 1+2 regression ────────────────────
class TestPhase12Regression:
    @pytest.mark.parametrize("path", [
        "/api/picks/all",
        "/api/picks/nrfi-yrfi",
        "/api/picks/markets/NFL",
        "/api/picks/refresh-status",
        "/api/picks/under-of-the-day",
        "/api/picks/rollover",
        "/api/picks/history?days=7",
    ])
    def test_get_200(self, headers, path):
        r = requests.get(f"{BASE_URL}{path}", headers=headers, timeout=60)
        assert r.status_code == 200, f"{path} → {r.status_code} {r.text[:200]}"
        assert "pick not found" not in r.text.lower()

    def test_post_settle(self, headers):
        # 60s public ingress timeout; settle can take ~90s. Try public, fall back to internal.
        try:
            r = requests.post(f"{BASE_URL}/api/picks/settle", headers=headers, timeout=120)
            if r.status_code in (502, 504):
                raise requests.RequestException("ingress timeout")
            assert r.status_code == 200, r.text
        except requests.RequestException:
            r = requests.post(f"{INTERNAL_URL}/api/picks/settle", headers=headers, timeout=180)
            assert r.status_code == 200, r.text


# ─────────────── Phase 1+2 parameterized routes (/{pick_id}) ─────────────
class TestPickIdRoutes:
    def test_pick_detail(self, headers, sample_pick):
        pid = sample_pick.get("id") or sample_pick.get("pick_id")
        assert pid
        r = requests.get(f"{BASE_URL}/api/picks/{pid}", headers=headers, timeout=30)
        assert r.status_code == 200, r.text

    def test_pick_probability(self, headers, sample_pick):
        pid = sample_pick.get("id") or sample_pick.get("pick_id")
        r = requests.get(f"{BASE_URL}/api/picks/{pid}/probability",
                         headers=headers, timeout=30)
        assert r.status_code == 200, r.text

    def test_pick_simulation(self, headers, sample_pick):
        pid = sample_pick.get("id") or sample_pick.get("pick_id")
        r = requests.get(f"{BASE_URL}/api/picks/{pid}/simulation",
                         headers=headers, timeout=30)
        # 200 for supported sports, 404 for unsupported — both acceptable
        assert r.status_code in (200, 404), r.text

    def test_pick_ai_explain(self, headers, sample_pick):
        pid = sample_pick.get("id") or sample_pick.get("pick_id")
        r = requests.post(f"{BASE_URL}/api/picks/{pid}/ai-explain",
                          headers=headers, json={}, timeout=60)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("source") in ("cached", "live", "fallback")

    def test_pick_loss_analysis(self, headers, sample_pick):
        pid = sample_pick.get("id") or sample_pick.get("pick_id")
        r = requests.post(f"{BASE_URL}/api/picks/{pid}/loss-analysis",
                          headers=headers, json={}, timeout=60)
        assert r.status_code == 200, r.text
