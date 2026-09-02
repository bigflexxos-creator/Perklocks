"""PERKLOCKS Permanent Universal 85+ Reachability Contract (Root Closure 2026-06).

Enforces §1-§20 of the Locks-eligibility contract:
    * ONE canonical eligibility authority (`services.locks_eligibility`).
    * ELIGIBLE_BUT_MISSING == 0 on `/api/picks/today` for every sport
      and every supported market family.
    * Every eligible pick carries a `locks_eligibility` object with an
      explicit canonical state and reason code (no "UNKNOWN" / "TOP_N" /
      "FILTERED" allowed).
    * ALL == UNION(sport_i) at the serving layer.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
load_dotenv(os.path.join(_BACKEND, ".env"))


# ── Canonical eligibility contract ────────────────────────────────
def test_compute_locks_eligibility_pure_math():
    """Deterministic on the pick dict alone — no DB, no globals."""
    from services.locks_eligibility import (
        compute_locks_eligibility,
        CURRENT_PREGAME_LOCK, BLOCKED, EXPIRED,
        REASON_BELOW_85, REASON_OFF_BOARD, REASON_NO_BET,
        REASON_HIDDEN_MAIN_BOARD, REASON_NOT_PUBLISHED,
        REASON_EVENT_STARTED, REASON_EVENT_FINAL, REASON_SUPERSEDED,
    )
    from datetime import datetime, timezone, timedelta

    now = datetime(2026, 9, 2, 21, 0, tzinfo=timezone.utc)
    future = (now + timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    past   = (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    long_past = (now - timedelta(hours=12)).isoformat().replace("+00:00", "Z")

    base = {
        "id": "x", "publication_state": "PUBLISHED",
        "published_lock_score": 90.0, "off_board": False,
        "no_bet": False, "event_time": future,
    }
    e = compute_locks_eligibility(base, now=now)
    assert e["eligible"] and e["state"] == CURRENT_PREGAME_LOCK

    e = compute_locks_eligibility({**base, "off_board": True}, now=now)
    assert not e["eligible"] and e["reason_code"] == REASON_OFF_BOARD
    e = compute_locks_eligibility({**base, "no_bet": True}, now=now)
    assert not e["eligible"] and e["reason_code"] == REASON_NO_BET
    e = compute_locks_eligibility({**base, "hide_from_main_board": True}, now=now)
    assert not e["eligible"] and e["reason_code"] == REASON_HIDDEN_MAIN_BOARD
    e = compute_locks_eligibility({**base, "published_lock_score": 80.0}, now=now)
    assert not e["eligible"] and e["reason_code"] == REASON_BELOW_85
    e = compute_locks_eligibility({**base, "publication_state": "CANDIDATE"}, now=now)
    assert not e["eligible"] and e["reason_code"] == REASON_NOT_PUBLISHED
    e = compute_locks_eligibility({**base, "revision_state": "SUPERSEDED_IN_RUN"}, now=now)
    assert not e["eligible"] and e["reason_code"] == REASON_SUPERSEDED
    e = compute_locks_eligibility({**base, "event_time": past}, now=now)
    assert not e["eligible"] and e["state"] == EXPIRED
    assert e["reason_code"] == REASON_EVENT_STARTED
    e = compute_locks_eligibility({**base, "event_time": long_past}, now=now)
    assert not e["eligible"] and e["reason_code"] == REASON_EVENT_FINAL


# ── LIVE ELIGIBLE_BUT_MISSING == 0 invariant ──────────────────────
def test_live_zero_ebm_across_all_sports():
    """The core invariant: every canonically eligible pick MUST appear
    on `/api/picks/today`.  Executed against the running server."""
    import httpx, os
    base = "http://localhost:8001"
    r = httpx.post(f"{base}/api/auth/login",
                    json={"email":"demo@lockscore.ai","password":"demo123"},
                    timeout=10)
    if r.status_code != 200:
        pytest.skip(f"backend login failed: {r.status_code}")
    tok = r.json().get("access_token")
    h = {"Authorization": f"Bearer {tok}"}

    async def go():
        c = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = c[os.environ["DB_NAME"]]
        from services.locks_eligibility import compute_locks_eligibility
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat().replace("+00:00", "Z")

        # Canonical eligible universe today (from DB truth).
        elig_q = {
            "publication_state": "PUBLISHED",
            "published_lock_score": {"$gte": 85},
            "off_board":            {"$ne": True},
            "no_bet":               {"$ne": True},
            "hide_from_main_board": {"$ne": True},
            "status":               {"$in": ["pending", "open", None]},
            "event_time":           {"$gt": now_iso},
        }
        eligible_ids = set()
        async for p in db.picks.find(elig_q, {"id": 1, "sport": 1, "market": 1,
                                                "published_lock_score": 1,
                                                "revision_state": 1,
                                                "publication_state": 1,
                                                "off_board": 1, "no_bet": 1,
                                                "hide_from_main_board": 1,
                                                "event_time": 1}):
            if compute_locks_eligibility(p)["eligible"]:
                pid = p.get("id")
                if pid:
                    eligible_ids.add(pid)
        return eligible_ids

    eligible_ids = asyncio.get_event_loop().run_until_complete(go())
    if not eligible_ids:
        pytest.skip("no eligible picks live — cannot enforce invariant")

    # Now fetch /api/picks/today (all sports) and confirm every eligible
    # id is present.
    r = httpx.get(f"{base}/api/picks/today", headers=h, timeout=30).json()
    served = r.get("picks", []) if isinstance(r, dict) else r
    served_ids = {p.get("id") for p in served if isinstance(p, dict) and p.get("id")}
    ebm = eligible_ids - served_ids
    assert not ebm, (
        f"ELIGIBLE_BUT_MISSING = {len(ebm)} on /api/picks/today — Root Closure "
        f"invariant violated. sample={list(ebm)[:8]}"
    )


# ── ALL == UNION(sport_i) served-layer invariant ──────────────────
def test_all_equals_union_of_sports_live():
    import httpx
    r = httpx.post("http://localhost:8001/api/auth/login",
                    json={"email":"demo@lockscore.ai","password":"demo123"}, timeout=10)
    if r.status_code != 200:
        pytest.skip(f"backend login failed: {r.status_code}")
    tok = r.json().get("access_token")
    h = {"Authorization": f"Bearer {tok}"}

    def _ids(sp):
        j = httpx.get("http://localhost:8001/api/picks/today",
                       params={"sport": sp}, headers=h, timeout=30).json()
        picks = j.get("picks", j) if isinstance(j, dict) else j
        return {p.get("id") for p in picks if isinstance(p, dict) and p.get("id")}

    all_ids = _ids("all")
    union = set()
    for sp in ("MLB","NFL","CFB","NBA","NHL","Soccer","Tennis","UFC"):
        union |= _ids(sp)

    if not all_ids and not union:
        pytest.skip("no picks live — cannot enforce invariant")

    missing = union - all_ids
    assert not missing, (
        f"{len(missing)} picks in per-sport tabs missing from ALL: {list(missing)[:5]}"
    )


# ── Every rescued pick carries an explicit canonical state ────────
def test_every_served_pick_carries_locks_eligibility_object():
    """No pick may reach a consumer without the canonical eligibility
    object — that's the ONE authoritative field consumers must read."""
    import httpx
    r = httpx.post("http://localhost:8001/api/auth/login",
                    json={"email":"demo@lockscore.ai","password":"demo123"}, timeout=10)
    if r.status_code != 200:
        pytest.skip(f"backend login failed: {r.status_code}")
    tok = r.json().get("access_token")
    h = {"Authorization": f"Bearer {tok}"}

    resp = httpx.get("http://localhost:8001/api/picks/today", headers=h, timeout=30).json()
    picks = resp.get("picks", []) if isinstance(resp, dict) else resp
    if not picks:
        pytest.skip("no picks live")
    from services.locks_eligibility import _CANONICAL_REASONS
    for p in picks:
        assert isinstance(p, dict), f"non-dict pick {p!r}"
        elig = p.get("locks_eligibility")
        assert isinstance(elig, dict), f"pick missing locks_eligibility: {p.get('id')}"
        assert "eligible" in elig and "state" in elig
        rc = elig.get("reason_code")
        assert rc is None or rc in _CANONICAL_REASONS, (
            f"non-canonical reason_code={rc} on pick {p.get('id')}"
        )
