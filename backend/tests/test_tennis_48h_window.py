"""Tests for the tennis 48-hour lookahead window feature.

Validates:
  - GET /api/picks/today?sport=Tennis returns picks for tomorrow (2026-06-23).
  - GET /api/picks/today (no filter) shows Tennis picks dated tomorrow.
  - fetch_extra_tennis_picks(days_ahead=1) yields events for today AND tomorrow.
  - GET /api/version returns data_version='2026.06.22-tennis-48h-window'.
  - No regression on MLB pitcher H2H endpoint.
"""

import os
import sys
import asyncio
from datetime import datetime, timezone, timedelta

import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL") or os.environ.get("EXPO_BACKEND_URL")
assert BASE_URL, "EXPO_PUBLIC_BACKEND_URL must be set in env"
BASE_URL = BASE_URL.rstrip("/")

# Allow `import tennis_extra` from inside this test file
sys.path.insert(0, "/app/backend")

EXPECTED_DATA_VERSION = "2026.06.22-tennis-48h-window"
TODAY = "2026-06-22"
TOMORROW = "2026-06-23"


@pytest.fixture(scope="module")
def auth_headers():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": "demo@lockscore.ai", "password": "demo123"},
        timeout=20,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    token = r.json().get("access_token")
    assert token, f"no access_token in login response: {r.json()}"
    return {"Authorization": f"Bearer {token}"}


# ── Version endpoint ─────────────────────────────────────────────
def test_api_version_returns_expected_data_version():
    r = requests.get(f"{BASE_URL}/api/version", timeout=20)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("data_version") == EXPECTED_DATA_VERSION, (
        f"expected {EXPECTED_DATA_VERSION}, got {data.get('data_version')}"
    )


# ── /picks/today?sport=Tennis returns tomorrow's matches ─────────
def test_picks_today_tennis_returns_tomorrow_picks(auth_headers):
    r = requests.get(
        f"{BASE_URL}/api/picks/today",
        params={"sport": "Tennis"},
        headers=auth_headers,
        timeout=30,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    picks = body if isinstance(body, list) else body.get("picks", [])
    assert isinstance(picks, list), f"picks should be a list, got {type(picks)}"
    assert len(picks) > 0, "Expected tennis picks but got none"

    # All returned picks should be Tennis sport
    sports = {p.get("sport") for p in picks}
    assert sports == {"Tennis"}, f"Non-tennis sports leaked through: {sports}"

    tomorrow_picks = [
        p for p in picks
        if (p.get("event_time") or "").startswith(TOMORROW)
    ]
    assert len(tomorrow_picks) >= 5, (
        f"Expected ≥5 tennis picks dated {TOMORROW}, got {len(tomorrow_picks)}. "
        f"Sample event_times: {[p.get('event_time') for p in picks[:10]]}"
    )


# ── /picks/today (all sports) — verify Tennis tomorrow picks present
def test_picks_today_all_sports_includes_tomorrow_tennis(auth_headers):
    r = requests.get(f"{BASE_URL}/api/picks/today", headers=auth_headers, timeout=30)
    assert r.status_code == 200, r.text
    body = r.json()
    picks = body if isinstance(body, list) else body.get("picks", [])
    assert isinstance(picks, list)

    tennis_tomorrow = [
        p for p in picks
        if p.get("sport") == "Tennis"
        and (p.get("event_time") or "").startswith(TOMORROW)
    ]
    assert len(tennis_tomorrow) >= 1, (
        f"Expected at least one Tennis pick dated {TOMORROW} in all-sports "
        f"feed. Total picks={len(picks)}"
    )

    # And at least one non-tennis sport should be present (no regression)
    non_tennis_sports = {p.get("sport") for p in picks if p.get("sport") != "Tennis"}
    assert len(non_tennis_sports) > 0, (
        f"Expected other sports in all-sports feed alongside Tennis, "
        f"but only got Tennis. Picks count={len(picks)}"
    )


# ── tennis_extra/picks.py — direct scraper call (today + tomorrow) ──
def test_fetch_extra_tennis_picks_returns_today_and_tomorrow():
    from tennis_extra.picks import fetch_extra_tennis_picks

    picks = asyncio.run(fetch_extra_tennis_picks(days_ahead=1))
    assert isinstance(picks, list)
    assert len(picks) > 0, "fetch_extra_tennis_picks returned 0 picks"

    today_picks = [p for p in picks if (p.get("event_time") or "").startswith(TODAY)]
    tomorrow_picks = [p for p in picks if (p.get("event_time") or "").startswith(TOMORROW)]

    assert len(today_picks) >= 1, (
        f"Expected today's ({TODAY}) tennis picks. Got dates: "
        f"{sorted({(p.get('event_time') or '')[:10] for p in picks})}"
    )
    assert len(tomorrow_picks) >= 1, (
        f"Expected tomorrow's ({TOMORROW}) tennis picks. Got dates: "
        f"{sorted({(p.get('event_time') or '')[:10] for p in picks})}"
    )


# ── No regression: MLB pitcher H2H endpoint ─────────────────────
def test_mlb_pitcher_h2h_no_regression(auth_headers):
    pick_id = "48600ae0-0268-5e82-99f2-8aab24d563cd"
    r = requests.get(
        f"{BASE_URL}/api/picks/{pick_id}/pitcher-h2h",
        headers=auth_headers,
        timeout=20,
    )
    # 200 → working, 404 → pick rotated out (acceptable but flag).
    assert r.status_code in (200, 404), f"unexpected status {r.status_code}: {r.text}"
    if r.status_code == 404:
        pytest.skip("Reference pick rotated out of DB — endpoint reachable.")
    data = r.json()
    # Shouldn't be an error payload
    assert "error" not in data or not data.get("error"), data
