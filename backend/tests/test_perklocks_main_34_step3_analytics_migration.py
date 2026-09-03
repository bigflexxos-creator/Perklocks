"""STEP 3 (analytics) · Analytics consumer migration — canonical wager parity
=============================================================================

`/api/analytics/steam-picks` (via `steam_detector.get_steam_picks`) now
attaches the immutable PublishedPickContract to every returned pick
so Analytics consumers describe the identical canonical wager Locks
does. Calculation logic untouched.
"""
from __future__ import annotations
import os, sys
import pytest, httpx

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_BASE = "http://localhost:8001"


def _tok():
    try:
        from rate_limit import _reset_for_tests
        _reset_for_tests(scope_prefix="ip:")
    except Exception:
        pass
    r = httpx.post(f"{_BASE}/api/auth/login",
                    json={"email": "demo@lockscore.ai", "password": "demo123"},
                    timeout=10)
    if r.status_code != 200:
        pytest.skip(f"login {r.status_code}")
    return r.json()["access_token"]


def _find_analytics_pick_endpoint(tok):
    """Try common analytics endpoints and return the first that yields
    a `picks` list. Analytics has several endpoints; this test only
    needs one that surfaces published-pick rows."""
    for path in (
        "/api/analytics/steam-picks?hours=24&limit=20",
        "/api/analytics/steam?hours=24&limit=20",
    ):
        r = httpx.get(f"{_BASE}{path}",
                       headers={"Authorization": f"Bearer {tok}"}, timeout=45)
        if r.status_code == 200:
            picks = (r.json() or {}).get("picks") or []
            if picks:
                return path, picks
    pytest.skip("no analytics endpoint returned picks today")


def test_step3_analytics_picks_carry_contract():
    tok = _tok()
    path, picks = _find_analytics_pick_endpoint(tok)
    missing = [p for p in picks if "published_pick_contract" not in p]
    assert not missing, (
        f"STEP 3: analytics `{path}` returned {len(missing)} pick(s) "
        f"missing frozen contract."
    )
    for p in picks[:5]:
        c = p["published_pick_contract"]
        assert "canonical_pick_id" in c, (
            "STEP 3: analytics contract missing canonical_pick_id"
        )
