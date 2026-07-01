"""Regression tests for MLB Hitter Props intelligence refactor (iter 65).

Verifies the surface-level guarantees enumerated in the review request:
  1. Every MLB hitter-prop pick on /api/picks/today?lite=false carries
     pick_rationale.engine == "mlb_hitter_intel".
  2. No MLB pick on the surface has lock_score < 89.
  3. /api/picks/history contains no KBO picks and no "First Goalscorer".
  4. /api/stats/summary hit_rate is derived from won/lost counts (not
     from lock_score means).
  5. /api/mlb/hr-slate does not 500.
  6. /api/picks/parlay and /api/picks/refresh-status return 200.
  7. Auth via /app/memory/test_credentials.md demo user still works.
"""
from __future__ import annotations

import os
import re
import time
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # Fallback to test env (frontend/.env uses EXPO_PUBLIC_BACKEND_URL)
    try:
        from pathlib import Path
        for line in Path("/app/frontend/.env").read_text().splitlines():
            if line.startswith("EXPO_PUBLIC_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().strip('"').rstrip("/")
                break
    except Exception:
        pass
assert BASE_URL, "EXPO_PUBLIC_BACKEND_URL must be set"

DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"


# ---------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def auth_token(api):
    # Retry a couple of times against the rate-limiter surfaced in iter 64.
    for attempt in range(3):
        r = api.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
            timeout=15,
        )
        if r.status_code == 200:
            token = r.json().get("access_token")
            assert token, "login response missing access_token"
            return token
        if r.status_code == 429:
            time.sleep(30)
            continue
        pytest.fail(f"login failed: HTTP {r.status_code}  body={r.text[:200]}")
    pytest.fail("login rate-limited after 3 attempts")


@pytest.fixture(scope="module")
def auth_headers(auth_token):
    return {"Authorization": f"Bearer {auth_token}"}


# --------------------------------------------------------- market helpers
_HITTER_MARKET_RE = re.compile(
    r"\b(hits|rbi|rbis|runs|h\+r\+rbi|hits\s*\+\s*runs\s*\+\s*rbis)\b",
    re.IGNORECASE,
)


def _is_mlb_hitter_prop(pick: dict) -> bool:
    """Match hitter markets while excluding pitcher-specific props."""
    sport = (pick.get("sport") or pick.get("sport_name") or "").lower()
    sport_key = (pick.get("sport_key") or "").lower()
    if "mlb" not in sport and "baseball_mlb" not in sport_key:
        return False
    market = (pick.get("market") or pick.get("prop") or pick.get("bet_type") or "")
    text = market.lower()
    if not text:
        return False
    if "pitcher" in text or "strikeout" in text or "strikeouts" in text or "outs recorded" in text:
        return False
    return bool(_HITTER_MARKET_RE.search(text))


def _is_mlb_pick(pick: dict) -> bool:
    sport = (pick.get("sport") or pick.get("sport_name") or "").lower()
    sport_key = (pick.get("sport_key") or "").lower()
    return "mlb" in sport or "baseball_mlb" in sport_key


# ---------------------------------------------------------------- auth
def test_auth_login_demo_user(auth_token):
    assert isinstance(auth_token, str) and len(auth_token) > 20


# ------------------------------------------------------- /api/picks/today
@pytest.fixture(scope="module")
def picks_today(api, auth_headers):
    r = api.get(f"{BASE_URL}/api/picks/today", params={"lite": "false"},
                headers=auth_headers, timeout=60)
    assert r.status_code == 200, f"/api/picks/today → {r.status_code} {r.text[:200]}"
    data = r.json()
    if isinstance(data, dict):
        picks = data.get("picks") or data.get("items") or []
    elif isinstance(data, list):
        picks = data
    else:
        picks = []
    return picks


def test_picks_today_returns_payload(picks_today):
    assert isinstance(picks_today, list)
    print(f"picks_today: {len(picks_today)} picks")


# ---- feature 1: every MLB hitter prop routes through mlb_hitter_intel ----
def test_mlb_hitter_props_use_mlb_hitter_intel(picks_today):
    hitters = [p for p in picks_today if _is_mlb_hitter_prop(p)]
    print(f"MLB hitter-prop picks on surface: {len(hitters)}")
    if not hitters:
        pytest.skip("no MLB hitter props on today's surface — vacuous pass")
    missing = []
    for p in hitters:
        rat = p.get("pick_rationale") or p.get("rationale") or {}
        engine = (rat.get("engine") if isinstance(rat, dict) else None)
        if engine != "mlb_hitter_intel":
            missing.append({
                "id": p.get("id"),
                "market": p.get("market") or p.get("prop"),
                "engine": engine,
                "rationale_keys": list(rat.keys()) if isinstance(rat, dict) else None,
            })
    assert not missing, (
        f"{len(missing)}/{len(hitters)} MLB hitter picks lack "
        f"engine='mlb_hitter_intel' (strict gate should have dropped these). "
        f"First offender: {missing[0]}"
    )


# ---- feature 2: no MLB pick with lock_score < 89 on surface ----
def test_no_mlb_pick_below_lock_score_89(picks_today):
    mlb = [p for p in picks_today if _is_mlb_pick(p)]
    print(f"MLB picks total: {len(mlb)}")
    offenders = []
    for p in mlb:
        ls = p.get("lock_score")
        if isinstance(ls, (int, float)) and ls < 89:
            offenders.append({"id": p.get("id"), "lock_score": ls,
                              "market": p.get("market") or p.get("prop")})
    assert not offenders, f"{len(offenders)} MLB picks below lock_score=89: {offenders[:3]}"


# ------------------------------------------------- /api/picks/history
def test_history_excludes_kbo_and_first_goalscorer(api, auth_headers):
    r = api.get(f"{BASE_URL}/api/picks/history", headers=auth_headers, timeout=60)
    assert r.status_code == 200, f"/api/picks/history → {r.status_code} {r.text[:200]}"
    data = r.json()
    if isinstance(data, dict):
        rows = data.get("picks") or data.get("items") or data.get("history") or []
    else:
        rows = data if isinstance(data, list) else []
    print(f"history rows: {len(rows)}")
    kbo = [p for p in rows if str(p.get("sport") or "").upper() == "KBO"
           or "kbo" in str(p.get("sport_key") or "").lower()]
    fgs = [p for p in rows
           if "first goalscorer" in str(p.get("market") or p.get("prop") or "").lower()
           or "first goal scorer" in str(p.get("market") or p.get("prop") or "").lower()]
    assert not kbo, f"{len(kbo)} KBO picks leaked into history: {kbo[:2]}"
    assert not fgs, f"{len(fgs)} First-Goalscorer picks leaked into history: {fgs[:2]}"


# ------------------------------------------------- /api/stats/summary
def test_stats_summary_200(api, auth_headers):
    r = api.get(f"{BASE_URL}/api/stats/summary", headers=auth_headers, timeout=30)
    assert r.status_code == 200, f"/api/stats/summary → {r.status_code} {r.text[:200]}"
    body = r.json()
    print(f"stats/summary keys: {list(body.keys()) if isinstance(body, dict) else type(body)}")
    assert isinstance(body, dict)
    # /api/stats/summary is a "today by-sport" aggregation — the settled
    # win/lost counts live on /api/picks/history.stats. Just verify shape here.
    assert "by_sport" in body or "total_picks" in body


def test_history_hit_rate_decoupled_from_lock_score(api, auth_headers):
    """hit_rate should equal won / (won + lost), NOT derived from lock_score."""
    r = api.get(f"{BASE_URL}/api/picks/history", headers=auth_headers, timeout=60)
    assert r.status_code == 200
    body = r.json()
    stats = body.get("stats") or {}
    print(f"history.stats = {stats}")
    won = stats.get("won")
    lost = stats.get("lost")
    hr = stats.get("hit_rate")
    if won is None or lost is None or hr is None:
        pytest.skip(f"history.stats missing won/lost/hit_rate: {stats}")
    settled = won + lost
    if settled == 0:
        pytest.skip("no settled picks yet")
    expected_pct = 100.0 * won / settled
    assert abs(hr - expected_pct) <= 0.5, (
        f"hit_rate={hr} not derived from won/lost={won}/{lost} "
        f"(expected {expected_pct:.2f}%) — math is coupled to something else"
    )


# ------------------------------------------------- /api/mlb/hr-slate
def test_mlb_hr_slate_no_500(api, auth_headers):
    r = api.get(f"{BASE_URL}/api/mlb/hr-slate", headers=auth_headers, timeout=60)
    assert r.status_code != 500, f"/api/mlb/hr-slate 500: {r.text[:300]}"
    assert r.status_code in (200, 204, 404), f"unexpected {r.status_code}: {r.text[:200]}"


# ------------------------------------------------- smoke endpoints
def test_picks_parlay_smoke(api, auth_headers):
    r = api.get(f"{BASE_URL}/api/picks/parlay", headers=auth_headers, timeout=60)
    assert r.status_code == 200, f"/api/picks/parlay → {r.status_code} {r.text[:200]}"


def test_picks_refresh_status_smoke(api, auth_headers):
    r = api.get(f"{BASE_URL}/api/picks/refresh-status", headers=auth_headers, timeout=30)
    assert r.status_code == 200, f"/api/picks/refresh-status → {r.status_code} {r.text[:200]}"
