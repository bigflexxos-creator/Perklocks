"""P0D — Expo Go History freshness / settlement authority
============================================================

Root cause of "Expo Go History is wrong/stale/incompletely graded":
The `/api/picks/history` handler already fires a fire-and-forget
settlement pass and projects results through `HistoryProjectionService`
— but the response did NOT tell the CLIENT that a settlement task was
still in flight. Result: pull-to-refresh #1 showed stale data,
pull-to-refresh #2 showed different data, no explanation.

Fix (P0D):

  1. Backend attaches `settlement_freshness` to every /history response:
        settlement_in_flight       : bool
        settlement_cooldown_until  : float (monotonic seconds)
        recommended_repoll_seconds : int | None
        unresolved_with_past_event : int (diagnostic)
  2. Frontend `app/history.tsx` schedules an automatic re-poll when
     the backend reports `settlement_in_flight=true`, using
     `recommended_repoll_seconds` bounded to [3, 15] s.
  3. Timer is cleared on unmount / filter change.

Live proof (2026-09-02):
    GET /api/picks/history?days=30 →
        picks_count: 1543
        settlement_freshness.settlement_in_flight        : true
        settlement_freshness.recommended_repoll_seconds  : 4
        settlement_freshness.unresolved_with_past_event  : 558
"""
from __future__ import annotations
import os, sys, re
import pytest, httpx

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_BASE = "http://localhost:8001"


def _tok():
    r = httpx.post(f"{_BASE}/api/auth/login",
                    json={"email": "demo@lockscore.ai", "password": "demo123"},
                    timeout=10)
    if r.status_code != 200:
        pytest.skip(f"login failed {r.status_code}")
    return r.json()["access_token"]


def test_p0d_history_ships_settlement_freshness():
    tok = _tok()
    r = httpx.get(f"{_BASE}/api/picks/history?days=30",
                    headers={"Authorization": f"Bearer {tok}"}, timeout=120)
    assert r.status_code == 200, r.text[:200]
    j = r.json()
    assert "settlement_freshness" in j, (
        "P0D: /api/picks/history must attach `settlement_freshness` "
        "so clients can auto-refresh after a background settlement "
        "pass completes."
    )
    sf = j["settlement_freshness"]
    for field in ("settlement_in_flight",
                    "settlement_cooldown_until",
                    "recommended_repoll_seconds",
                    "unresolved_with_past_event"):
        assert field in sf, f"P0D: missing `{field}` in settlement_freshness"
    assert isinstance(sf["settlement_in_flight"], bool), "settlement_in_flight must be bool"
    assert isinstance(sf["unresolved_with_past_event"], int), \
        "unresolved_with_past_event must be int"


def test_p0d_frontend_wires_auto_repoll():
    """Static contract: the History screen honours the freshness hint."""
    p = "/app/frontend/app/history.tsx"
    with open(p, "r") as f:
        src = f.read()
    assert "settlement_freshness" in src, (
        "P0D: history screen must read `settlement_freshness` from "
        "the API response."
    )
    assert "recommended_repoll_seconds" in src, (
        "P0D: history screen must consume `recommended_repoll_seconds`."
    )
    # setTimeout to auto re-poll
    assert re.search(r"setTimeout\s*\(\s*\(\)\s*=>\s*\{[^}]*load\s*\(\s*\)",
                       src), (
        "P0D: history screen must schedule a setTimeout(load, …) when "
        "the backend reports a settlement pass is in flight."
    )
    # cleanup on unmount
    assert re.search(r"clearTimeout\s*\(\s*repollRef\.current\s*\)", src), (
        "P0D: history screen must clearTimeout on unmount to avoid "
        "leaking timers across screen mounts."
    )
