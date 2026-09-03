"""STEP 3 · My Bets consumer migration — Locks == My Bets canonical wager parity
=================================================================================

`/api/user/bets` and `/api/user/analytics/history` now attach the
immutable `published_pick_contract` block to every returned row so
My Bets, Locks, Pick Breakdown, Parlay, and History all describe the
identical canonical wager. No ranking/selection logic touched.
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


def test_step3_user_bets_ships_contract_on_every_row():
    tok = _tok()
    r = httpx.get(f"{_BASE}/api/user/bets?limit=25",
                    headers={"Authorization": f"Bearer {tok}"}, timeout=60)
    if r.status_code != 200:
        pytest.skip(f"user/bets {r.status_code}")
    bets = r.json().get("bets", [])
    if not bets:
        pytest.skip("no tracked bets today")
    missing = [b for b in bets if "published_pick_contract" not in b]
    assert not missing, (
        f"STEP 3: {len(missing)} /user/bets row(s) missing frozen "
        f"contract."
    )
    # Every attached contract must expose the identity spine.
    for b in bets[:10]:
        c = b["published_pick_contract"]
        for k in ("sport", "selection", "publication_state",
                    "canonical_pick_id"):
            assert k in c, f"STEP 3: /user/bets contract missing `{k}`"


def test_step3_user_analytics_history_ships_contract():
    tok = _tok()
    r = httpx.get(f"{_BASE}/api/user/analytics/history?limit=25",
                    headers={"Authorization": f"Bearer {tok}"}, timeout=60)
    if r.status_code != 200:
        pytest.skip(f"analytics/history {r.status_code}")
    bets = r.json().get("history", [])
    if not bets:
        pytest.skip("no tracked bets history")
    missing = [b for b in bets if "published_pick_contract" not in b]
    assert not missing, (
        f"STEP 3: {len(missing)} /user/analytics/history row(s) "
        f"missing frozen contract."
    )
