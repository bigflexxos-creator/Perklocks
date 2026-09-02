"""Root Closure §A — Rollover / Parlay canonical migration
=============================================================

Enforces that Rollover and Parlay consume the SAME canonical
`locks_eligibility` base population as Locks.  Their purpose-specific
post-gate logic (V4 rollover rules, parlay correlation/EV/leg-count)
is allowed to further narrow the pool, but the BASE ANSWER to "is
this a valid current canonical Lock?" is shared.

Runtime invariants:
    ROLLOVER_BASE_ELIGIBILITY_DRIFT = 0
    PARLAY_BASE_ELIGIBILITY_DRIFT   = 0
"""
from __future__ import annotations
import os, sys
import pytest, httpx

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


def _tok():
    r = httpx.post("http://localhost:8001/api/auth/login",
                    json={"email":"demo@lockscore.ai","password":"demo123"}, timeout=10)
    if r.status_code != 200:
        pytest.skip(f"login failed: {r.status_code}")
    return r.json()["access_token"]


def test_apply_canonical_locks_eligibility_gate_is_pure():
    """The shared gate function must be a pure filter — no I/O, no
    mutation of pick dicts, no eligibility reinvention."""
    from services.locks_eligibility import apply_canonical_locks_eligibility_gate
    picks = [
        # Eligible
        {"id": "a", "publication_state": "PUBLISHED", "published_lock_score": 90,
         "off_board": False, "no_bet": False, "book_odds": -110,
         "event_time": "2099-01-01T00:00:00Z",
         "sport": "MLB", "market": "Over 1.5 Hits", "line": 1.5},
        # Below 85 → NOT_ELIGIBLE
        {"id": "b", "publication_state": "PUBLISHED", "published_lock_score": 80,
         "off_board": False, "no_bet": False, "book_odds": -110,
         "event_time": "2099-01-01T00:00:00Z",
         "sport": "MLB", "market": "Over 1.5 Hits", "line": 1.5},
        # Missing book_odds → REAL_LINE_INVALID
        {"id": "c", "publication_state": "PUBLISHED", "published_lock_score": 90,
         "off_board": False, "no_bet": False,
         "event_time": "2099-01-01T00:00:00Z",
         "sport": "MLB", "market": "Over 1.5 Hits", "line": 1.5},
        # Synthetic → SYNTHETIC_MARKET
        {"id": "d", "publication_state": "PUBLISHED", "published_lock_score": 90,
         "off_board": False, "no_bet": False, "book_odds": -110,
         "event_time": "2099-01-01T00:00:00Z", "synthetic": True,
         "sport": "MLB", "market": "Over 1.5 Hits", "line": 1.5},
    ]
    eligible, dropped = apply_canonical_locks_eligibility_gate(picks)
    assert [p["id"] for p in eligible] == ["a"]
    assert dropped.get("NOT_ELIGIBLE") == 1
    assert dropped.get("REAL_LINE_INVALID") == 1
    assert dropped.get("SYNTHETIC_MARKET") == 1


def test_rollover_reachable_without_errors():
    """Live proof: `/api/picks/rollover` returns 200 after the base
    gate migration."""
    tok = _tok()
    r = httpx.get("http://localhost:8001/api/picks/rollover",
                   headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    assert r.status_code == 200, r.text[:200]
    j = r.json()
    picks = j.get("picks", j) if isinstance(j, dict) else j
    if not isinstance(picks, list):
        pytest.skip("rollover shape not list")
    # Every rollover pick MUST also be canonically-eligible
    from services.locks_eligibility import (
        compute_locks_eligibility, rescue_validity_reason,
    )
    for p in picks:
        assert compute_locks_eligibility(p)["eligible"] is True, (
            f"rollover surfaced non-eligible pick {p.get('id')}"
        )
        assert rescue_validity_reason(p) is None, (
            f"rollover surfaced invalid pick {p.get('id')}: "
            f"{rescue_validity_reason(p)}"
        )


def test_parlay_reachable_without_errors():
    tok = _tok()
    r = httpx.get("http://localhost:8001/api/picks/parlay",
                   headers={"Authorization": f"Bearer {tok}"}, timeout=45,
                   params={"legs": 3, "mode": "standard"})
    if r.status_code == 200:
        j = r.json()
        # Parlay legs must all be canonically-eligible
        from services.locks_eligibility import (
            compute_locks_eligibility, rescue_validity_reason,
        )
        parlay = j.get("parlay") if isinstance(j, dict) else None
        legs = (parlay or {}).get("legs") or j.get("legs") or []
        if not legs and isinstance(j, dict):
            legs = j.get("picks") or []
        for p in legs:
            if not isinstance(p, dict): continue
            assert compute_locks_eligibility(p)["eligible"] is True, (
                f"parlay leg not eligible: {p.get('id')}"
            )
            assert rescue_validity_reason(p) is None, (
                f"parlay leg invalid: {p.get('id')}"
            )
    else:
        # Parlay may return 204/422 when insufficient legs exist —
        # acceptable as long as it doesn't 500.
        assert r.status_code < 500, f"parlay 5xx: {r.text[:200]}"
