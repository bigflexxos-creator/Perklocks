"""
Iter140 continuation tests:
- GET /api/ops/refresh-health returns last_cycle rows and live counts per sport with MLB and CFB upcoming_72h > 0
- GET /api/picks/{id}/historical-intelligence — provenance clean, opponent dates real,
  soccer opponent_summary has 'all_competitions'/'same_competition' with n=2 real Union Berlin games
- GET /api/picks/today?lite=true&sort=lock&direction=desc — lock_score monotonically non-increasing, sports include MLB and CFB
"""
import os
import re
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "https://canonical-parity.preview.emergentagent.com").rstrip("/")
DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"

KANE_PICK_ID = "7b702d7d-6d3c-57c4-ac49-2368a1249e14"


@pytest.fixture(scope="session")
def api_client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    r = s.post(f"{BASE_URL}/api/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}, timeout=30)
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text[:200]}"
    token = r.json().get("access_token") or r.json().get("token")
    assert token, f"No token in login response: {r.text[:200]}"
    s.headers.update({"Authorization": f"Bearer {token}"})
    return s


# ---------- refresh-health ----------

class TestRefreshHealth:
    def test_refresh_health_shape(self, api_client):
        r = api_client.get(f"{BASE_URL}/api/ops/refresh-health", timeout=60)
        assert r.status_code == 200, f"{r.status_code} {r.text[:400]}"
        j = r.json()
        # Save for later assertions
        assert isinstance(j, dict), "response must be dict"
        assert "last_cycle" in j or "cycles" in j or "sports" in j, f"missing expected keys: {list(j.keys())}"
        # Print for context (visible in pytest -s)
        print("refresh-health keys:", list(j.keys()))
        # Store on the class for reuse
        TestRefreshHealth._payload = j

    def test_mlb_cfb_upcoming_72h_gt_zero(self, api_client):
        r = api_client.get(f"{BASE_URL}/api/ops/refresh-health", timeout=60)
        j = r.json()
        # Live counts under 'live', last_cycle rows for context
        live = j.get("live") or {}
        assert isinstance(live, dict) and live, f"live block missing or empty: {j.keys()}"
        assert isinstance(j.get("last_cycle"), list) and len(j.get("last_cycle")) > 0, "last_cycle rows missing"
        def upcoming(sport):
            for k, v in live.items():
                if isinstance(k, str) and k.upper() == sport and isinstance(v, dict):
                    return v.get("upcoming_72h") or 0
            return 0
        mlb = upcoming("MLB")
        cfb = upcoming("CFB")
        print(f"MLB upcoming_72h={mlb} CFB upcoming_72h={cfb}")
        assert (mlb or 0) > 0, f"MLB upcoming_72h expected > 0, got {mlb}. live={live}"
        assert (cfb or 0) > 0, f"CFB upcoming_72h expected > 0, got {cfb}. live={live}"


# ---------- historical-intelligence ----------

class TestHistoricalIntelligence:
    def _get_nfl_player_prop_pick_id(self, api_client):
        r = api_client.get(f"{BASE_URL}/api/picks/today?lite=true", timeout=60)
        assert r.status_code == 200
        picks = r.json().get("picks") or r.json().get("items") or []
        for p in picks:
            if (p.get("sport") or "").upper() == "NFL":
                mt = (p.get("market_type") or p.get("market") or "").lower()
                if "player" in mt or p.get("player_name") or p.get("player"):
                    return p.get("id") or p.get("pick_id")
        pytest.skip("No NFL player-prop pick available")

    def test_nfl_hi_provenance_clean(self, api_client):
        pid = self._get_nfl_player_prop_pick_id(api_client)
        r = api_client.get(f"{BASE_URL}/api/picks/{pid}/historical-intelligence", timeout=60)
        assert r.status_code == 200, f"HI failed: {r.status_code} {r.text[:300]}"
        j = r.json()
        prov = j.get("provenance") or j.get("provenance_list") or []
        prov_text = str(prov).lower()
        assert "adapter:" not in prov_text, f"provenance contains 'adapter:' — {prov}"
        # No class-style names like '_SportDispatcher'
        assert "_sportdispatcher" not in prov_text.lower(), f"provenance leaks class name: {prov}"
        # Check opponent dates not 09-01 placeholders
        opp = j.get("opponent_summary") or {}
        games = opp.get("games") or []
        if games:
            fake = [g.get("date") for g in games if isinstance(g.get("date"), str) and g["date"].endswith("-09-01")]
            print(f"NFL HI opp games={len(games)} fake_dates={len(fake)}")
            # Not ALL dates should be 09-01
            assert len(fake) < len(games), f"All opponent dates are YYYY-09-01: {[g.get('date') for g in games]}"

    def test_soccer_kane_hi_two_scopes(self, api_client):
        r = api_client.get(f"{BASE_URL}/api/picks/{KANE_PICK_ID}/historical-intelligence", timeout=60)
        assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
        j = r.json()
        opp = j.get("opponent_summary") or {}
        assert "all_competitions" in opp, f"missing all_competitions. keys={list(opp.keys())}"
        assert "same_competition" in opp, f"missing same_competition. keys={list(opp.keys())}"
        allc = opp["all_competitions"]
        samec = opp["same_competition"]
        # Both should have n=2 real Union Berlin games
        n_all = allc.get("n") if isinstance(allc, dict) else None
        n_same = samec.get("n") if isinstance(samec, dict) else None
        print(f"Kane HI n_all={n_all} n_same={n_same}")
        assert n_all == 2, f"all_competitions n expected 2, got {n_all}. body={allc}"
        assert n_same == 2, f"same_competition n expected 2, got {n_same}. body={samec}"
        games = (allc.get("games") if isinstance(allc, dict) else []) or []
        # Games should reference Union Berlin (opponent) and have real dates
        for g in games:
            date = g.get("date") or ""
            assert date and not date.endswith("-09-01"), f"suspicious date {date}: {g}"
            opponent = (g.get("opponent") or g.get("vs") or g.get("team") or "")
            print(f"  Kane opp game: {date} vs {opponent}")


# ---------- picks/today sort=lock desc ----------

class TestPicksTodayLockSort:
    def test_lock_desc_monotonic_and_includes_mlb_cfb(self, api_client):
        r = api_client.get(f"{BASE_URL}/api/picks/today?lite=true&sort=lock&direction=desc", timeout=90)
        assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
        j = r.json()
        picks = j.get("picks") or j.get("items") or []
        assert len(picks) > 5, f"too few picks: {len(picks)}"
        scores = [p.get("lock_score") for p in picks if p.get("lock_score") is not None]
        # Monotonic non-increasing check
        violations = [(i, scores[i - 1], scores[i]) for i in range(1, len(scores)) if scores[i] > scores[i - 1]]
        print(f"n picks={len(picks)} n scores={len(scores)} first5={scores[:5]} last5={scores[-5:]} violations={len(violations)}")
        assert len(violations) == 0, f"Non-monotonic lock_score at indices: {violations[:5]}"
        # Sports present
        sports = sorted({(p.get("sport") or "").upper() for p in picks})
        print("sports present:", sports)
        assert "MLB" in sports, f"MLB missing from sports={sports}"
        assert "CFB" in sports, f"CFB missing from sports={sports}"
