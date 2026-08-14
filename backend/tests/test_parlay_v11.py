"""Parlay Module V1.1 — time window, sport_mode, leg-count-aware diversification."""
import os
import pytest
import requests
from collections import Counter
from datetime import datetime, timezone, timedelta

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    BASE_URL = "https://canonical-parity.preview.emergentagent.com"

EMAIL = "demo@lockscore.ai"
PASSWORD = "demo123"


@pytest.fixture(scope="module")
def auth_token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"email": EMAIL, "password": PASSWORD},
                      timeout=30)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    return r.json()["access_token"]


@pytest.fixture
def headers(auth_token):
    return {"Authorization": f"Bearer {auth_token}"}


def _parlay(headers, **params):
    r = requests.get(f"{BASE_URL}/api/picks/parlay", params=params, headers=headers, timeout=60)
    assert r.status_code == 200, f"parlay {params}: {r.status_code} {r.text[:300]}"
    return r.json()


def _max_same_sport(parlay):
    sports = [L.get("sport") for L in parlay["legs"]]
    return max(Counter(sports).values()) if sports else 0


# ────── B1 AUTO mode + 24h window ──────
def test_b1_auto_default_24h(headers):
    data = _parlay(headers, legs=3, mode="standard", sport_mode="auto", window_hours=24)
    assert data.get("sport_mode") == "auto"
    assert data.get("window_hours") == 24
    if not data.get("parlays"):
        pytest.skip(f"no parlays returned: {data.get('reason')}")
    assert len(data["parlays"]) >= 1
    # event_time within window (allow +30min floor as backend does)
    cap = datetime.now(timezone.utc) + timedelta(hours=24, minutes=1)
    floor = datetime.now(timezone.utc) - timedelta(hours=1)
    for p in data["parlays"]:
        for leg in p["legs"]:
            et = leg.get("event_time")
            if not et:
                continue
            t = datetime.fromisoformat(et.replace("Z", "+00:00"))
            assert floor <= t <= cap, f"event_time {et} outside 24h window"


# ────── B2 CUSTOM mode ──────
def test_b2_custom_include_sports(headers):
    data = _parlay(headers, legs=4, mode="standard", sport_mode="custom",
                   include_sports="Soccer,MLB", window_hours=48)
    assert data.get("sport_mode") == "custom"
    assert data.get("window_hours") == 48
    if not data.get("parlays"):
        pytest.skip(f"no parlays: {data.get('reason')}")
    for p in data["parlays"]:
        for leg in p["legs"]:
            assert leg.get("sport") in {"Soccer", "MLB"}, \
                f"leg sport {leg.get('sport')} not in include list"


# ────── B3 SINGLE mode ──────
def test_b3_single_mode_soccer(headers):
    data = _parlay(headers, legs=4, mode="standard", sport_mode="single",
                   sport="Soccer", window_hours=168)
    assert data.get("sport_mode") == "single"
    if not data.get("parlays"):
        pytest.skip(f"no soccer parlays: {data.get('reason')}")
    for p in data["parlays"]:
        for leg in p["legs"]:
            assert leg.get("sport") == "Soccer", f"non-soccer leg in single mode: {leg.get('sport')}"


# ────── B4 Diversification caps (AUTO) ──────
def test_b4a_diversification_3leg(headers):
    data = _parlay(headers, legs=3, mode="standard", sport_mode="auto", window_hours=168)
    if not data.get("parlays"):
        pytest.skip(f"no parlays: {data.get('reason')}")
    for p in data["parlays"]:
        assert _max_same_sport(p) <= 2, f"3-leg AUTO: {_max_same_sport(p)} same-sport legs (>2)"


def test_b4b_diversification_8leg(headers):
    data = _parlay(headers, legs=8, mode="high_risk", sport_mode="auto", window_hours=168)
    if not data.get("parlays"):
        pytest.skip(f"no 8-leg parlays: {data.get('reason')}")
    for p in data["parlays"]:
        # 8 legs → max 50% same sport = 4
        legcount = len(p["legs"])
        # Actual cap is half of target_legs which is 8 (high_risk clamps to 10-20),
        # but legs requested = 8 → high_risk min is 10 actually. Check leg count.
        if legcount >= 6:
            cap = max(2, legcount // 2)
            assert _max_same_sport(p) <= cap, f"{legcount}-leg: {_max_same_sport(p)} > {cap}"


def test_b4c_diversification_15leg(headers):
    data = _parlay(headers, legs=15, mode="high_risk", sport_mode="auto", window_hours=168)
    if not data.get("parlays"):
        pytest.skip(f"no 15-leg parlays: {data.get('reason')}")
    for p in data["parlays"]:
        legcount = len(p["legs"])
        if legcount >= 11:
            cap = max(2, (legcount * 4) // 10)
            assert _max_same_sport(p) <= cap, f"{legcount}-leg: {_max_same_sport(p)} > 40% cap {cap}"


# ────── B5 Time window enforcement ──────
def test_b5a_tiny_1h_window(headers):
    data = _parlay(headers, legs=3, mode="standard", sport_mode="auto", window_hours=1)
    # Either returns parlays in 1h OR returns reason about not enough picks.
    # NOTE: When backend returns empty result via the `reason` branch it
    # currently does NOT echo back `window_hours`/`sport_mode` — see action items.
    if not data.get("parlays"):
        assert data.get("reason"), "empty result but no reason given"
        assert "1h" in data["reason"] or "within" in data["reason"], \
            f"reason should mention window: {data['reason']}"
    else:
        cap = datetime.now(timezone.utc) + timedelta(hours=1, minutes=1)
        for p in data["parlays"]:
            for leg in p["legs"]:
                et = leg.get("event_time")
                if et:
                    t = datetime.fromisoformat(et.replace("Z", "+00:00"))
                    assert t <= cap, f"leg {et} outside 1h window"


def test_b5b_week_window_at_least_24h(headers):
    d24 = _parlay(headers, legs=3, mode="standard", sport_mode="auto", window_hours=24)
    d168 = _parlay(headers, legs=3, mode="standard", sport_mode="auto", window_hours=168)
    assert d168.get("window_hours") == 168
    # Sanity: 168h should not error out
    # No strict assertion on pool size (depends on data)


# ────── B6 Backward compat ──────
def test_b6a_no_new_params(headers):
    data = _parlay(headers, legs=3, mode="standard")
    assert data.get("sport_mode") == "auto"
    assert data.get("window_hours") == 24


def test_b6b_legacy_exclude_sports(headers):
    data = _parlay(headers, legs=3, mode="standard", exclude_sports="Soccer", window_hours=168)
    if not data.get("parlays"):
        pytest.skip(f"no parlays: {data.get('reason')}")
    for p in data["parlays"]:
        for leg in p["legs"]:
            assert leg.get("sport") != "Soccer", "Soccer leg present but exclude_sports=Soccer"
