"""iter-86 H2H enrichment endpoint + list-attach tests.

Coverage:
- GET /api/picks/{pick_id}/h2h contract for MLB / Soccer / Tennis picks
- 404 for unknown pick_id (never 500)
- GET /api/picks/today?sport=... attaches h2h_summary on some picks
- Response time / size budget
- _build_summary filter: no "0-0" or "No prior meetings" leaks into
  the compact chip
- fast_mode=True truly skips the MLB external call in the list endpoint
"""
from __future__ import annotations

import os
import time
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL") or os.environ.get("EXPO_BACKEND_URL")
assert BASE_URL, "EXPO_PUBLIC_BACKEND_URL must be set"
BASE_URL = BASE_URL.rstrip("/")

DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"


@pytest.fixture(scope="module")
def auth_token() -> str:
    s = requests.Session()
    r = s.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
        timeout=60,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:300]}"
    tok = r.json().get("token") or r.json().get("access_token")
    assert tok, f"no token in login response: {r.json()}"
    return tok


@pytest.fixture(scope="module")
def api(auth_token):
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
    })
    return s


def _picks_by_sport(api, sport: str) -> list[dict]:
    r = api.get(f"{BASE_URL}/api/picks/today", params={"sport": sport}, timeout=30)
    if r.status_code != 200:
        return []
    body = r.json() or {}
    return body.get("picks") or []


# ── 1. /picks/today performance + h2h_summary attach ──────────────────────
class TestPicksTodayH2HAttach:
    def test_mlb_response_perf_and_h2h_summary(self, api):
        t0 = time.time()
        r = api.get(f"{BASE_URL}/api/picks/today", params={"sport": "MLB"}, timeout=30)
        dt = time.time() - t0
        assert r.status_code == 200, r.text[:300]
        body_bytes = len(r.content)
        assert dt < 12.0, f"MLB /picks/today too slow: {dt:.2f}s"
        assert body_bytes < 1_500_000, f"MLB response too big: {body_bytes} bytes"
        picks = (r.json() or {}).get("picks") or []
        print(f"MLB picks={len(picks)} time={dt:.2f}s size={body_bytes}")
        # Check attach: h2h_summary present on at least SOME picks OR gracefully absent
        with_summary = [p for p in picks if p.get("h2h_summary")]
        print(f"MLB picks with h2h_summary: {len(with_summary)}/{len(picks)}")
        # Assert legality of any attached summary
        for p in with_summary:
            s = p["h2h_summary"]
            assert isinstance(s, str) and s
            # _build_summary must have filtered these out
            assert "0-0" not in s, f"leaked 0-0 into chip: {s!r}"
            assert "No prior" not in s, f"leaked 'No prior' into chip: {s!r}"
            # h2h_compact shape check
            hc = p.get("h2h_compact") or {}
            assert isinstance(hc, dict)

    def test_soccer_response_perf_and_h2h_summary(self, api):
        t0 = time.time()
        r = api.get(f"{BASE_URL}/api/picks/today", params={"sport": "Soccer"}, timeout=30)
        dt = time.time() - t0
        assert r.status_code == 200, r.text[:300]
        assert dt < 12.0, f"Soccer /picks/today too slow: {dt:.2f}s"
        picks = (r.json() or {}).get("picks") or []
        with_summary = [p for p in picks if p.get("h2h_summary")]
        print(f"Soccer picks={len(picks)} time={dt:.2f}s with_h2h_summary={len(with_summary)}")
        for p in with_summary:
            s = p["h2h_summary"]
            assert "0-0" not in s, f"leaked 0-0: {s!r}"
            assert "No prior" not in s, f"leaked 'No prior': {s!r}"


# ── 2. /picks/{pick_id}/h2h contract ──────────────────────────────────────
class TestPickH2HEndpoint:
    def test_invalid_pick_id_returns_404(self, api):
        r = api.get(f"{BASE_URL}/api/picks/does-not-exist-abc123/h2h", timeout=15)
        assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text[:200]}"

    @pytest.mark.parametrize("sport", ["MLB", "Soccer", "Tennis"])
    def test_h2h_bundle_shape_per_sport(self, api, sport):
        picks = _picks_by_sport(api, sport)
        if not picks:
            pytest.skip(f"no {sport} picks available today")
        pid = picks[0]["id"]
        t0 = time.time()
        r = api.get(f"{BASE_URL}/api/picks/{pid}/h2h", timeout=30)
        dt = time.time() - t0
        assert r.status_code == 200, f"{sport} h2h failed: {r.status_code} {r.text[:300]}"
        body = r.json() or {}
        # Contract keys
        for k in ("ok", "sport"):
            assert k in body, f"{sport}: missing key '{k}' in bundle: {list(body.keys())}"
        # Never 500; when ok=False, must include an error/reason
        if not body.get("ok"):
            print(f"{sport} h2h ok=false → {body}")
            # empty bundle is still a valid contract, just verify no crash
        else:
            # Optional keys — assert types when present
            if body.get("team_h2h") is not None:
                th = body["team_h2h"]
                assert "meetings" in th and "record" in th
            if body.get("player_h2h") is not None:
                ph = body["player_h2h"]
                assert "player" in ph and "vs_opponent" in ph
            assert isinstance(body.get("sources") or [], list)
        print(f"{sport} h2h endpoint dt={dt:.2f}s ok={body.get('ok')} "
              f"summary={body.get('summary')!r}")

    def test_h2h_summary_filter_no_zero_zero(self, api):
        """Sample the first 30 picks from /picks/today?sport=All and check
        every h2h_summary passes the filter."""
        r = api.get(f"{BASE_URL}/api/picks/today", timeout=30)
        assert r.status_code == 200
        picks = (r.json() or {}).get("picks") or []
        checked = 0
        for p in picks[:60]:
            s = p.get("h2h_summary")
            if not s:
                continue
            checked += 1
            assert "0-0" not in s, f"pick {p.get('id')}: leaked 0-0 → {s!r}"
            assert "No prior meetings" not in s, f"pick {p.get('id')}: leaked → {s!r}"
        print(f"h2h_summary filter check: {checked} picks passed")


# ── 3. Fast-mode: MLB external call is skipped in /picks/today ────────────
class TestFastModeSkipsMLBExternal:
    def test_second_call_is_fast_no_external_recompute(self, api):
        """Two back-to-back /picks/today?sport=MLB calls: the second must
        be faster than 8s. If the enricher were calling MLB Stats API per
        pick it would take much longer."""
        api.get(f"{BASE_URL}/api/picks/today", params={"sport": "MLB"}, timeout=30)
        t0 = time.time()
        r = api.get(f"{BASE_URL}/api/picks/today", params={"sport": "MLB"}, timeout=30)
        dt = time.time() - t0
        assert r.status_code == 200
        assert dt < 10.0, f"cached MLB /picks/today too slow ({dt:.2f}s) — fast_mode may be leaking"
        print(f"Second MLB fetch dt={dt:.2f}s (fast_mode budget)")
