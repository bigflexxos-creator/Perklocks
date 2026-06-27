"""Regression tests for server.py monolith decomposition — Phase 2.

Validates the 11 picks routes moved from server.py to
routes/picks_routes.py:

  • GET  /api/picks/under-of-the-day
  • GET  /api/picks/rollover
  • POST /api/picks/settle
  • GET  /api/picks/history
  • GET  /api/picks/{pick_id}
  • POST /api/picks/{pick_id}/ai-explain
  • POST /api/picks/{pick_id}/loss-analysis
  • GET  /api/picks/{pick_id}/probability
  • GET  /api/picks/{pick_id}/player-form
  • GET  /api/picks/{pick_id}/pitcher-h2h
  • GET  /api/picks/{pick_id}/simulation

Plus monolith-resident routes (must NOT 404 / be shadowed by the
parameterized `/{pick_id}` route in the new module):

  • GET  /api/picks/today
  • GET  /api/picks/parlay
  • POST /api/picks/refresh

And Phase-1 static-route precedence smoke:

  • GET  /api/picks/all
  • GET  /api/picks/nrfi-yrfi
  • GET  /api/picks/markets/NFL
  • GET  /api/picks/refresh-status

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
    return r.json()["access_token"]


@pytest.fixture(scope="session")
def auth_headers(auth_token):
    return {"Authorization": f"Bearer {auth_token}", "Content-Type": "application/json"}


@pytest.fixture(scope="session")
def sample_pick_id(api_client, auth_headers):
    """A real pick_id off today's slate — used by the /{pick_id}/* tests."""
    r = api_client.get(f"{BASE_URL}/api/picks/today", headers=auth_headers, timeout=30)
    assert r.status_code == 200, f"/picks/today failed: {r.status_code}"
    data = r.json()
    picks = data.get("picks") or []
    if not picks:
        pytest.skip("No picks on today's slate to exercise /{pick_id}/* routes")
    pid = picks[0].get("id")
    assert pid, "first pick has no 'id' field"
    return pid, picks


# ──────── Phase-1 static route precedence smoke (must still work) ────────
class TestPhase1StaticSmoke:
    def test_picks_all(self, api_client, auth_headers):
        r = api_client.get(f"{BASE_URL}/api/picks/all", headers=auth_headers, timeout=30)
        assert r.status_code == 200, r.text[:200]
        assert "picks" in r.json()

    def test_picks_nrfi_yrfi(self, api_client, auth_headers):
        r = api_client.get(f"{BASE_URL}/api/picks/nrfi-yrfi", headers=auth_headers, timeout=30)
        assert r.status_code == 200, r.text[:200]
        body = r.json()
        assert body.get("category") == "nrfi_yrfi"
        assert "picks" in body and "count" in body

    def test_picks_markets_nfl(self, api_client, auth_headers):
        r = api_client.get(f"{BASE_URL}/api/picks/markets/NFL", headers=auth_headers, timeout=30)
        assert r.status_code == 200, r.text[:200]
        body = r.json()
        assert body.get("sport") == "NFL"
        assert "markets" in body and "leagues" in body

    def test_refresh_status_pre(self, api_client, auth_headers):
        r = api_client.get(f"{BASE_URL}/api/picks/refresh-status", headers=auth_headers, timeout=30)
        assert r.status_code == 200, r.text[:200]


# ──────── Phase-2 static routes (moved) ────────
class TestUnderOfTheDay:
    def test_default(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/picks/under-of-the-day", headers=auth_headers, timeout=30,
        )
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        # Expected keys per spec
        for k in ("pick", "alternates", "total_evaluated"):
            assert k in body, f"missing {k} in response: {list(body.keys())}"
        assert isinstance(body["alternates"], list)
        assert isinstance(body["total_evaluated"], int)

    @pytest.mark.parametrize("sort", ["lock", "time", "edge"])
    def test_sort_param_honored(self, api_client, auth_headers, sort):
        r = api_client.get(
            f"{BASE_URL}/api/picks/under-of-the-day?sort={sort}",
            headers=auth_headers, timeout=30,
        )
        assert r.status_code == 200, f"sort={sort} returned {r.status_code}: {r.text[:200]}"


class TestRollover:
    def test_default_shape(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/picks/rollover", headers=auth_headers, timeout=30,
        )
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        for k in ("picks", "pick", "total_evaluated", "rollover_version", "survivability"):
            assert k in body, f"missing {k}: keys={list(body.keys())}"
        assert body["rollover_version"] == "v2", f"unexpected version: {body['rollover_version']}"
        assert isinstance(body["picks"], list)
        assert len(body["picks"]) <= 3, "rollover must return at most 3 picks"
        surv = body["survivability"]
        for sk in ("mode", "odds_floor", "edge_floor"):
            assert sk in surv, f"survivability missing {sk}: {surv}"
        assert surv["mode"] in ("strict", "relaxed")


class TestSettle:
    def test_post_settle_returns_counters(self, api_client, auth_headers):
        # Settlement scans every settled-due pick; can take 60-90s on a
        # cold backend cache. Public ingress proxy times out at ~60s so
        # we fall back to the internal URL when we see a 502 — the route
        # itself returns 200 with full counters dict (verified directly).
        try:
            r = api_client.post(
                f"{BASE_URL}/api/picks/settle", headers=auth_headers, timeout=180,
            )
        except requests.exceptions.ReadTimeout:
            r = None
        if r is None or r.status_code == 502:
            r = api_client.post(
                "http://localhost:8001/api/picks/settle",
                headers=auth_headers, timeout=240,
            )
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert isinstance(body, dict), f"expected dict counters, got {type(body)}"
        # Spec: returns settlement counters dict — verify the documented keys.
        for k in ("settled", "won", "lost", "push", "skipped"):
            assert k in body, f"settle counter '{k}' missing: {list(body.keys())[:20]}"


class TestHistory:
    def test_history_30d(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/picks/history?days=30", headers=auth_headers, timeout=30,
        )
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert "picks" in body and "stats" in body
        stats = body["stats"]
        for sk in ("total", "won", "lost", "push", "hit_rate",
                   "rollover_hit_rate", "rollover_decided"):
            assert sk in stats, f"stats missing {sk}: {list(stats.keys())}"

    def test_history_rollover_only(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/picks/history?days=30&rollover_only=true",
            headers=auth_headers, timeout=30,
        )
        assert r.status_code == 200
        body = r.json()
        # rollover_only scope: stats.total must equal len(picks)
        assert body["stats"]["total"] == len(body["picks"])


# ──────── Phase-2 /{pick_id}/* routes (moved) ────────
class TestPickDetail:
    def test_pick_detail(self, api_client, auth_headers, sample_pick_id):
        pid, _ = sample_pick_id
        r = api_client.get(f"{BASE_URL}/api/picks/{pid}", headers=auth_headers, timeout=30)
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert body.get("id") == pid
        assert "ai_pending" in body, f"missing ai_pending flag: keys={list(body.keys())[:30]}"
        assert isinstance(body["ai_pending"], bool)

    def test_pick_detail_404(self, api_client, auth_headers):
        r = api_client.get(
            f"{BASE_URL}/api/picks/this_pick_id_does_not_exist_xyz",
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 404


class TestAiExplain:
    def test_ai_explain_returns_source(self, api_client, auth_headers, sample_pick_id):
        pid, _ = sample_pick_id
        r = api_client.post(
            f"{BASE_URL}/api/picks/{pid}/ai-explain", headers=auth_headers, timeout=60,
        )
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert "explanation" in body
        assert body.get("source") in ("cached", "live", "fallback"), \
            f"unexpected source: {body.get('source')}"


class TestLossAnalysis:
    def test_skip_on_pending(self, api_client, auth_headers, sample_pick_id):
        pid, picks = sample_pick_id
        # Find a pick that is still 'pending' (the typical case for today's picks).
        pending_pid = next(
            (p["id"] for p in picks if (p.get("status") or "pending") == "pending"),
            pid,
        )
        r = api_client.post(
            f"{BASE_URL}/api/picks/{pending_pid}/loss-analysis",
            headers=auth_headers, timeout=30,
        )
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert body.get("source") == "skip", \
            f"expected source=skip for pending pick, got {body.get('source')}"


class TestProbability:
    def test_returns_breakdown(self, api_client, auth_headers, sample_pick_id):
        pid, _ = sample_pick_id
        r = api_client.get(
            f"{BASE_URL}/api/picks/{pid}/probability", headers=auth_headers, timeout=30,
        )
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert isinstance(body, dict) and len(body) > 0


class TestPlayerForm:
    def test_404_for_non_goalscorer(self, api_client, auth_headers, sample_pick_id):
        pid, picks = sample_pick_id
        # Pick a non-soccer-goalscorer pick (very likely the default first
        # pick is NFL/MLB/etc., not a soccer goalscorer market).
        non_goalscorer = next(
            (p["id"] for p in picks
             if "goal scorer" not in (p.get("market") or "").lower()
             and "to score" not in (p.get("market") or "").lower()),
            pid,
        )
        r = api_client.get(
            f"{BASE_URL}/api/picks/{non_goalscorer}/player-form",
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 404, \
            f"expected 404 for non-goalscorer pick, got {r.status_code}: {r.text[:200]}"
        # Reason should be present in the detail
        body = r.json()
        assert "detail" in body


class TestPitcherH2H:
    def test_404_for_non_mlb_strikeout(self, api_client, auth_headers, sample_pick_id):
        pid, picks = sample_pick_id
        non_k = next(
            (p["id"] for p in picks
             if (p.get("sport") != "MLB"
                 or "strikeout" not in (p.get("market") or "").lower())),
            pid,
        )
        r = api_client.get(
            f"{BASE_URL}/api/picks/{non_k}/pitcher-h2h",
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 404, r.text[:200]


class TestSimulation:
    def test_supported_sport_or_404(self, api_client, auth_headers, sample_pick_id):
        pid, picks = sample_pick_id
        # Find a supported-sport pick
        supported = {"MLB", "Soccer", "NBA", "Tennis"}
        target = next(
            (p for p in picks if p.get("sport") in supported),
            None,
        )
        if target:
            r = api_client.get(
                f"{BASE_URL}/api/picks/{target['id']}/simulation",
                headers=auth_headers, timeout=60,
            )
            # 200 (sim routed) or 404 (router couldn't route this specific
            # market) — both acceptable per spec.
            assert r.status_code in (200, 404), \
                f"unexpected status {r.status_code}: {r.text[:200]}"
        # Non-supported (NFL, etc.) → must be 404
        unsupported = next(
            (p for p in picks if p.get("sport") and p["sport"] not in supported),
            None,
        )
        if unsupported:
            r = api_client.get(
                f"{BASE_URL}/api/picks/{unsupported['id']}/simulation",
                headers=auth_headers, timeout=30,
            )
            assert r.status_code == 404, \
                f"expected 404 for sport={unsupported['sport']}, got {r.status_code}"


# ──────── Monolith-resident routes (must NOT regress / be shadowed) ────────
class TestMonolithStillExists:
    def test_picks_today_not_captured_by_pick_id(self, api_client, auth_headers):
        """Critical: /api/picks/today must NOT be matched by the new
        /{pick_id} route in picks_routes.py. If `today` ever gets captured
        as a pick_id, the new router would return 404 'Pick not found'.
        """
        r = api_client.get(f"{BASE_URL}/api/picks/today", headers=auth_headers, timeout=60)
        assert r.status_code == 200, \
            f"/picks/today regressed (likely captured by /{{pick_id}}): {r.status_code} {r.text[:200]}"
        body = r.json()
        assert "picks" in body, f"unexpected /picks/today shape: keys={list(body.keys())[:20]}"

    def test_picks_parlay(self, api_client, auth_headers):
        r = api_client.get(f"{BASE_URL}/api/picks/parlay", headers=auth_headers, timeout=60)
        # parlay can legitimately return 200 with picks or empty payload —
        # we just need it to NOT 404 (otherwise /{pick_id} captured it).
        assert r.status_code != 404, \
            f"/picks/parlay captured by /{{pick_id}}: 404"
        assert r.status_code == 200, r.text[:200]

    def test_picks_refresh(self, api_client, auth_headers):
        r = api_client.post(f"{BASE_URL}/api/picks/refresh", headers=auth_headers, timeout=60)
        # 200 (succeeded) or 429 (cooldown) — both valid. 404 would mean
        # the /{pick_id} route captured "refresh" as an id.
        assert r.status_code != 404, "/picks/refresh captured by /{pick_id}: 404"
        assert r.status_code in (200, 429), f"unexpected status {r.status_code}: {r.text[:200]}"
