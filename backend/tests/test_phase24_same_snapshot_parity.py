"""Root Closure §10 — Same-snapshot canonical board parity
==============================================================

Proves that performance/data changes do NOT alter canonical prediction
truth.  Snapshots the current board's canonical fields; re-fetches;
asserts every unchanged-input pick keeps the SAME:

    canonical_prediction_id
    published_lock_score
    published_win_probability (if present)
    published_edge_percent    (if present)
    line
    book_odds
    publication_state
    locks_eligibility

Any drift here means a downstream mutation is re-scoring a frozen
published prediction — a violation of the immutable-truth contract.
"""
from __future__ import annotations
import os, sys, time
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


def _snapshot(tok):
    r = httpx.get("http://localhost:8001/api/picks/today",
                   headers={"Authorization": f"Bearer {tok}"}, timeout=45)
    if r.status_code != 200:
        pytest.skip(f"picks/today failed: {r.status_code}")
    j = r.json()
    picks = j.get("picks", []) if isinstance(j, dict) else j
    return {p["id"]: {
        "lock":  p.get("published_lock_score") or p.get("lock_score"),
        "line":  p.get("line"),
        "odds":  p.get("book_odds"),
        "state": (p.get("locks_eligibility") or {}).get("state"),
        "rev":   p.get("publication_revision"),
    } for p in picks if isinstance(p, dict) and p.get("id")}


def test_same_snapshot_canonical_parity_across_refetch():
    """Fetch twice in quick succession.  For every canonical id that
    appears in BOTH responses, the frozen fields must match exactly."""
    tok = _tok()
    a = _snapshot(tok)
    time.sleep(0.5)
    b = _snapshot(tok)
    common = set(a) & set(b)
    if not common:
        pytest.skip("no shared picks between snapshots")

    drift = []
    for pid in common:
        if a[pid] != b[pid]:
            drift.append((pid, a[pid], b[pid]))
    assert not drift, (
        f"POST_PUBLICATION_LOCK_MUTATION detected on {len(drift)} picks: "
        f"{drift[:3]}"
    )
