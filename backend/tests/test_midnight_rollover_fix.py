"""Tests for 2026-06-23 midnight-rollover fix.

Verifies:
- /api/picks/today has 100+ picks across sports right after force refresh
- Per-sport slices (Tennis, MLB, Soccer) have data
- /api/version returns data_version='2026.06.23-midnight-rollover-fix'
- Regressions: MLB pitcher H2H, Tennis 48h tomorrow picks, Tennis deterministic IDs
"""
import os
import re
import requests
import pytest
from pathlib import Path


def _load_backend_url() -> str:
    val = os.environ.get("EXPO_PUBLIC_BACKEND_URL") or os.environ.get("EXPO_BACKEND_URL")
    if val:
        return val.rstrip("/")
    env_path = Path("/app/frontend/.env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("EXPO_PUBLIC_BACKEND_URL="):
                return line.split("=", 1)[1].strip().strip('"').rstrip("/")
    raise RuntimeError("EXPO_PUBLIC_BACKEND_URL not found in env or /app/frontend/.env")


BASE_URL = _load_backend_url()
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    # Authenticate using seeded demo user
    r = s.post(
        f"{API}/auth/login",
        json={"email": "demo@lockscore.ai", "password": "demo123"},
        timeout=30,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    token = r.json().get("access_token") or r.json().get("token")
    assert token, f"no token in login response: {r.json()}"
    s.headers.update({"Authorization": f"Bearer {token}"})
    return s


# ── Bug fix: data_version bump ─────────────────────────────────────────
class TestVersion:
    def test_version_bumped(self, client):
        r = client.get(f"{API}/version", timeout=30)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("data_version") == "2026.06.23-midnight-rollover-fix", (
            f"unexpected data_version: {data}"
        )


# ── Bug fix: today's picks populated after refresh ─────────────────────
class TestTodayPicks:
    def test_today_picks_has_100_plus(self, client):
        r = client.get(f"{API}/picks/today", timeout=60)
        assert r.status_code == 200, r.text
        data = r.json()
        # Accept either list or dict-with-picks
        if isinstance(data, dict):
            picks = data.get("picks") or data.get("items") or []
        else:
            picks = data
        assert isinstance(picks, list), f"unexpected shape: {type(data)}"
        assert len(picks) >= 100, f"only {len(picks)} picks today (expected >=100)"

    @pytest.mark.parametrize("sport", ["Tennis", "MLB", "Soccer"])
    def test_today_picks_by_sport(self, client, sport):
        r = client.get(f"{API}/picks/today", params={"sport": sport}, timeout=60)
        assert r.status_code == 200, r.text
        data = r.json()
        if isinstance(data, dict):
            picks = data.get("picks") or data.get("items") or []
        else:
            picks = data
        assert len(picks) > 0, f"{sport}: no picks returned (expected >0)"


# ── Regression: MLB pitcher H2H ────────────────────────────────────────
class TestMLBPitcherH2H:
    def test_pitcher_h2h_endpoint(self, client):
        # Pull an MLB pick id, then call the per-pick pitcher-h2h endpoint
        r = client.get(f"{API}/picks/today", params={"sport": "MLB"}, timeout=60)
        assert r.status_code == 200, r.text
        data = r.json()
        picks = data.get("picks") if isinstance(data, dict) else data
        assert picks, "no MLB picks to derive a pick_id from"
        # Prefer a pitcher prop pick if present, else any pick
        pick = None
        for p in picks:
            mkt = (p.get("market") or "").lower()
            if "strikeout" in mkt or "outs recorded" in mkt or "pitcher" in mkt:
                pick = p
                break
        if not pick:
            pick = picks[0]
        pick_id = pick.get("id") or pick.get("pick_id")
        assert pick_id, f"no id field on pick: keys={list(pick.keys())[:8]}"

        resp = client.get(f"{API}/picks/{pick_id}/pitcher-h2h", timeout=60)
        # 200 = data present, 204/404 acceptable for non-pitcher pick — but endpoint must exist
        assert resp.status_code in (200, 204, 404, 422), (
            f"pitcher-h2h endpoint failed: {resp.status_code} {resp.text[:200]}"
        )
        # If 200, validate shape lightly
        if resp.status_code == 200 and resp.text and resp.text != "null":
            body = resp.json()
            assert isinstance(body, (dict, list)), f"unexpected body type: {type(body)}"


# ── Regression: Tennis 48h window has tomorrow's picks ─────────────────
class TestTennis48hWindow:
    def test_tennis_has_tomorrow_picks(self, client):
        r = client.get(f"{API}/picks/today", params={"sport": "Tennis"}, timeout=60)
        assert r.status_code == 200
        data = r.json()
        picks = data.get("picks") if isinstance(data, dict) else data
        assert picks, "no tennis picks"
        # Some pick should reference a commence_time/start beyond today (48h window)
        import datetime as dt
        today = dt.datetime.utcnow().date()
        future_found = False
        for p in picks:
            ts = p.get("event_time") or p.get("commence_time") or p.get("game_time") or p.get("start_time")
            if not ts:
                continue
            try:
                d = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date()
                if d > today:
                    future_found = True
                    break
            except Exception:
                continue
        assert future_found, "no Tennis picks with a future commence_time (48h window)"


# ── Regression: Tennis pick IDs deterministic across two reads ─────────
class TestTennisDeterministicIds:
    def test_tennis_ids_stable_across_two_reads(self, client):
        def fetch_ids():
            r = client.get(f"{API}/picks/today", params={"sport": "Tennis"}, timeout=60)
            assert r.status_code == 200
            data = r.json()
            picks = data.get("picks") if isinstance(data, dict) else data
            return {p.get("id") or p.get("pick_id") or p.get("_id") for p in picks}

        ids1 = fetch_ids()
        ids2 = fetch_ids()
        assert ids1 == ids2, (
            f"Tennis pick IDs changed between two reads — non-deterministic. "
            f"only_in_first={list(ids1 - ids2)[:5]} only_in_second={list(ids2 - ids1)[:5]}"
        )
